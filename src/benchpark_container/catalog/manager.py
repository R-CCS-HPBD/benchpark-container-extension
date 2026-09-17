# SPDX-License-Identifier: Apache-2.0
"""Immutable releases, atomic registration and explicit catalog name resolution."""
import json
from datetime import datetime, timezone
from pathlib import Path
import uuid

from ..contracts import base_tools
from ..util import ValidationError, atomic_json, safe_name, strict, sha256, identity, check_no_secrets
from ..image_store import verify_managed, regular_path, read_json, digest_value
from .config import load_yaml, read_config, locked, sync_dir, ensure_directories
from .store import store_class, remote_reference, Skopeo


def fixed_release(value):
    safe_name(value, 'release')
    if value.lower() in ('latest', 'default', 'none'):
        raise ValidationError('A fixed named release is required, not latest/default/none')
    return value


def validate_manifest(data):
    strict(data, ('schema_version', 'name', 'release', 'artifacts'), 'image registration',
           ('schema_version', 'name', 'release', 'artifacts'))
    if data['schema_version'] != 1:
        raise ValidationError('Unsupported image registration schema')
    safe_name(data['name']); fixed_release(data['release'])
    if not isinstance(data['artifacts'], list) or not data['artifacts']:
        raise ValidationError('At least one image artifact is required')
    seen = set()
    for item in data['artifacts']:
        strict(item, ('kind', 'uri', 'oci_layout', 'sha256', 'digest', 'platform', 'accelerator', 'runtimes', 'tools'),
               'image binding', ('kind', 'platform', 'tools', 'accelerator'))
        if item['kind'] not in ('sif', 'oci'):
            raise ValidationError('Unknown container artifact kind')
        if item['platform'] not in ('linux/amd64', 'linux/arm64'):
            raise ValidationError('Use a supported canonical platform: linux/amd64 or linux/arm64')
        if item['accelerator'] not in ('none', 'nvidia', 'amd'):
            raise ValidationError('Unknown image accelerator family')
        base_tools(item['tools'])
        if ('uri' in item) == ('oci_layout' in item):
            raise ValidationError('Declare exactly one uri or oci_layout source')
        if item['kind'] == 'sif' and ('oci_layout' in item or 'digest' in item):
            raise ValidationError('SIF requires a file URI and optional sha256')
        if item['kind'] == 'oci' and 'sha256' in item:
            raise ValidationError('OCI uses a manifest digest, not SIF sha256')
        for key in ('uri', 'oci_layout'):
            if key in item and (not isinstance(item[key], str) or not item[key]):
                raise ValidationError('Image source must be a nonempty string')
        if 'digest' in item: digest_value(item['digest'])
        if 'sha256' in item: digest_value('sha256:' + str(item['sha256']))
        runtimes = item.get('runtimes', [])
        if not isinstance(runtimes, list) or not all(isinstance(x, str) for x in runtimes) or len(set(runtimes)) != len(runtimes):
            raise ValidationError('Runtime binding must be a unique list')
        for runtime in runtimes:
            safe_name(runtime, 'runtime name')  # Not a hardcoded list; supports future backends.
        key = (item['kind'], item['platform'], item['accelerator'], tuple(sorted(runtimes)))
        if key in seen:
            raise ValidationError('Duplicate image binding')
        seen.add(key)
    check_no_secrets(data)
    return data


def init_catalog(path, name):
    safe_name(name, 'catalog name')
    root = Path(path).expanduser().absolute()
    root.mkdir(parents=True, exist_ok=False)
    atomic_json(root / 'catalog.yaml', {'schema_version': 1, 'name': name,
        'catalog_id': uuid.uuid4().hex, 'store': {'kind': 'filesystem'}})
    (root / 'entries').mkdir()
    (root / 'store').mkdir()
    (root / '.gitignore').write_text('store/\ncatalog.lock\n', encoding='utf-8')
    sync_dir(root)
    return {'root': str(root.resolve()), 'name': name}


class Catalog:
    def __init__(self, root):
        self.root = Path(root).expanduser().resolve(strict=True)
        self.header = load_yaml(self.root / 'catalog.yaml')
        strict(self.header, ('schema_version', 'name', 'catalog_id', 'store'), 'catalog',
               ('schema_version', 'name', 'catalog_id', 'store'))
        if self.header['schema_version'] != 1:
            raise ValidationError('Unsupported Catalog schema')
        safe_name(self.header['name']); safe_name(self.header['catalog_id'])
        strict(self.header['store'], ('kind',), 'ArtifactStore', ('kind',))
        store_class(self.header['store']['kind'])

    def _entry_path(self, name, release):
        safe_name(name); fixed_release(release)
        return self.root / 'entries' / name / (release + '.json')

    def read(self, name, release):
        p = regular_path(self.root, str(self._entry_path(name, release).relative_to(self.root)))
        entry = read_json(p)
        if (entry.get('schema_version') != 1 or entry.get('name') != name
                or entry.get('release') != release or entry.get('catalog_id') != self.header['catalog_id']
                or not isinstance(entry.get('artifacts'), list) or not entry['artifacts']):
            raise ValidationError('Catalog entry identity/schema mismatch')
        body = dict(entry); expected = body.pop('entry_sha256', None)
        if identity(body) != expected:
            raise ValidationError('Immutable catalog entry checksum mismatch')
        check_no_secrets(entry)
        return entry

    def list(self):
        rows = []
        for path in sorted((self.root / 'entries').glob('*/*.json')):
            data = self.read(path.parent.name, path.stem)
            rows.append({'name': data['name'], 'release': data['release'],
                         'integrity': 'NOT_CHECKED_USE_VALIDATE',
                         'storage_present': all((self.root/a['content']['relative_path']).exists() for a in data['artifacts']),
                         'artifacts': [{k: a[k] for k in ('kind', 'platform', 'accelerator')} for a in data['artifacts']]})
        return rows

    def image(self, artifact):
        return store_class(self.header['store']['kind'])(self.root).runtime_image(artifact)

    def validate(self, name, release):
        entry = self.read(name, release)
        for item in entry['artifacts']:
            verify_managed(self.image(item))
        return {'name': name, 'release': release, 'status': 'VERIFIED_STORED',
                'runtime_validation': 'NOT_PERFORMED', 'entry_sha256': entry['entry_sha256'],
                'identity_basis': 'managed-artifact', 'source_accessed': False,
                'source_equivalence': 'NOT_ASSERTED'}

    def register(self, declaration, skopeo='skopeo', timeout=3600):
        data = json.loads(json.dumps(validate_manifest(declaration)))
        # Resolve mutable local source paths to bytes BEFORE idempotency checks.
        # A rewritten foo.sif must not silently reuse its previous release.
        from urllib.parse import urlparse, unquote
        from ..image_store import verify_layout
        copier = None
        for item in data['artifacts']:
            if item['kind'] == 'oci' and 'uri' in item:
                reference = remote_reference(item['uri'])
                if '@' not in reference:
                    # Register is an explicit import operation: a changed tag
                    # must not silently alias an already registered release.
                    if copier is None:
                        copier = Skopeo(skopeo, timeout)
                    pinned, _ = copier.pin_reference(reference, item.get('digest'))
                    item['digest'] = pinned.rsplit('@', 1)[1]
                elif 'digest' in item and item['digest'] != reference.rsplit('@', 1)[1]:
                    raise ValidationError('Declared source digest differs from registry reference')
            if item['kind'] == 'sif' and 'sha256' not in item:
                parsed = urlparse(item.get('uri', ''))
                if parsed.scheme == 'file' and not parsed.netloc:
                    item['sha256'] = sha256(Path(unquote(parsed.path)))
            if 'oci_layout' in item and 'digest' not in item:
                item['digest'] = verify_layout(item['oci_layout'])['root_digest']
        destination = self._entry_path(data['name'], data['release'])
        with locked(self.root / 'catalog.lock'):
            if destination.exists():
                old = self.read(data['name'], data['release'])
                if old['registration_sha256'] != identity(data):
                    raise ValidationError('Immutable name/release already exists; choose a new release')
                self.validate(data['name'], data['release'])
                return {'status': 'ALREADY_REGISTERED', 'entry': old}
            store = store_class(self.header['store']['kind'])(self.root)
            artifacts = []
            for item in data['artifacts']:
                content = store.ingest(item, skopeo=skopeo, timeout=timeout)
                artifact = {key: item[key] for key in ('kind', 'platform', 'accelerator', 'tools')}
                artifact.update(runtimes=item.get('runtimes', []), content=content)
                verify_managed(self.image(artifact))
                artifacts.append(artifact)
            entry = {'schema_version': 1, 'name': data['name'], 'release': data['release'],
                     'catalog_id': self.header['catalog_id'], 'artifacts': artifacts,
                     'registration_sha256': identity(data),
                     'registered_at': datetime.now(timezone.utc).isoformat(),
                     'import_policy': 'managed-canonical-v1'}
            entry['entry_sha256'] = identity(entry)
            # Publication is last. Earlier verified CAS objects can be orphans,
            # but a failed import never publishes an incomplete valid release.
            ensure_directories(self.root, str(destination.parent.relative_to(self.root)))
            atomic_json(destination, entry)
            destination.chmod(0o444)
            sync_dir(destination.parent)
            return {'status': 'REGISTERED', 'entry': entry}


def lookup(name, release, config=None):
    fixed_release(release)
    locations = read_config(config)['catalogs']
    if ':' in name:
        alias, logical = name.split(':', 1)
        safe_name(alias); safe_name(logical)
        if alias not in locations:
            raise ValidationError('Unknown catalog alias: ' + alias)
        candidates = [(alias, Catalog(locations[alias]))]
    else:
        logical = safe_name(name)
        candidates = [(alias, Catalog(root)) for alias, root in sorted(locations.items())]
    found = [(alias, catalog) for alias, catalog in candidates if catalog._entry_path(logical, release).exists()]
    if not found:
        raise ValidationError('No registered image release: ' + name + '/' + release)
    if len(found) != 1:
        raise ValidationError('Ambiguous image; specify catalog:name: ' + ', '.join(alias + ':' + logical for alias, _ in found))
    alias, catalog = found[0]
    return alias, catalog, catalog.read(logical, release)
