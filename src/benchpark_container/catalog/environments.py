# SPDX-License-Identifier: Apache-2.0
"""Environment Release v2: alternatives contain a base AND an optional Overlay.

No build execution, runtime invocation or dependency solving occurs here.
"""
import copy
from datetime import datetime, timezone
from pathlib import Path
import os
import shutil
import tempfile
from urllib.parse import urlparse, unquote

from ..contracts import container_tools
from ..environment_snapshot import (hex_hash, check_squashfs, composition, make_snapshot,
                           validate_build_record, validate_validations,
                           validate_environment_binding, verify_environment_files)
from ..image_store import regular_path, read_json, verify_managed, digest_value, MAX_METADATA
from ..util import ValidationError, strict, safe_name, identity, sha256, atomic_json, check_no_secrets, json_bytes
from .config import locked, ensure_directories, sync_dir
from .store import store_class


def validate_declaration(data):
    from .manager import validate_manifest, fixed_release
    strict(data, ('schema_version', 'name', 'release', 'environments'), 'environment registration',
           ('schema_version', 'name', 'release', 'environments'))
    if type(data['schema_version']) is not int or data['schema_version'] != 2:
        raise ValidationError('Unsupported environment registration schema')
    safe_name(data['name']); fixed_release(data['release'])
    envs = data['environments']
    if not isinstance(envs, list) or not envs or len(envs) > 32:
        raise ValidationError('At least one bounded environment binding is required')
    ids, selections = set(), set()
    for item in envs:
        strict(item, ('id', 'platform', 'accelerator', 'runtimes', 'tools', 'base', 'overlay', 'build_record', 'validations'),
               'environment binding', ('id', 'platform', 'accelerator', 'runtimes', 'tools', 'base'))
        safe_name(item['id'], 'environment binding ID')
        if item['id'] in ids:
            raise ValidationError('Duplicate environment binding ID')
        ids.add(item['id'])
        runtimes = item['runtimes']
        if not isinstance(runtimes, list) or not runtimes:
            raise ValidationError('Environment binding requires explicit runtimes')
        # Reuse the v1 artifact validation without conflating alternatives with parts.
        base = item['base']
        strict(base, ('kind', 'uri', 'oci_layout', 'sha256', 'digest'), 'base source', ('kind',))
        artifact = dict(base, **{k: item[k] for k in ('platform', 'accelerator', 'runtimes', 'tools')})
        validate_manifest({'schema_version': 1, 'name': data['name'], 'release': data['release'], 'artifacts': [artifact]})
        if base['kind'] == 'sif':
            hex_hash(base.get('sha256'))
        else:
            uri = base.get('uri', '')
            if 'oci_layout' in base:
                digest_value(base.get('digest'))
            elif '@sha256:' not in uri:
                raise ValidationError('v2 OCI source must be digest-pinned')
        for runtime in runtimes:
            key = (item['platform'], item['accelerator'], runtime)
            if key in selections:
                raise ValidationError('Ambiguous environment alternatives; one binding per runtime/platform/accelerator')
            selections.add(key)
        overlay = item.get('overlay')
        if 'overlay' in item and overlay is None:
            raise ValidationError('Omit overlay for a standalone image')
        if overlay is not None:
            strict(overlay, ('format', 'uri', 'sha256', 'readonly'), 'overlay source',
                   ('format', 'uri', 'sha256', 'readonly'))
            if base['kind'] != 'sif' or overlay['format'] != 'squashfs' or overlay['readonly'] is not True:
                raise ValidationError('Only SIF plus one immutable read-only squashfs Overlay is supported')
            hex_hash(overlay['sha256'])
            _local_file(overlay['uri'])
            # A binding is executable only by an explicitly capable backend.
            from ..backends.registry import backend_class
            for name in runtimes:
                if 'squashfs' not in backend_class(name).capabilities.readonly_overlay_formats:
                    raise ValidationError('Runtime cannot execute a squashfs Overlay: ' + name)
            validate_build_record(item.get('build_record'), base['sha256'], overlay['sha256'])
            validate_validations(item.get('validations', []), base['sha256'], overlay['sha256'], runtimes)
        elif 'build_record' in item or item.get('validations'):
            raise ValidationError('Build/validation records in v2 currently belong to Overlay bindings only')
    if len(json_bytes(data)) > MAX_METADATA:
        raise ValidationError('Environment declaration exceeds metadata size limit')
    check_no_secrets(data)
    return data


def _local_file(uri):
    if not isinstance(uri, str):
        raise ValidationError('Overlay source requires a file URI')
    u = urlparse(uri)
    if u.scheme != 'file' or u.netloc or u.query or u.fragment:
        raise ValidationError('Overlay requires file:///absolute-path without host/query/fragment')
    p = Path(unquote(u.path))
    if not p.is_absolute() or '..' in p.parts:
        raise ValidationError('Unsafe Overlay source path')
    for part in (p, *p.parents):
        if part.is_symlink():
            raise ValidationError('Overlay source path may not traverse symlinks')
    if not p.is_file():
        raise ValidationError('Overlay source must be a regular file')
    return p


def _sparse_copy(source, target):
    # Preserve zero regions without platform-specific cp/sparse flags. Hashes
    # cover logical bytes including holes; this is not a reflink deduplicator.
    with Path(source).open('rb') as src, Path(target).open('xb') as dst:
        size = 0
        for chunk in iter(lambda: src.read(1024 * 1024), b''):
            size += len(chunk)
            if chunk == bytes(len(chunk)):
                dst.seek(len(chunk), 1)
            else:
                dst.write(chunk)
        dst.truncate(size)


def ingest_overlay(store, declaration):
    source = _local_file(declaration['uri'])
    check_squashfs(source)
    expected = declaration['sha256']
    if sha256(source) != expected:
        raise ValidationError('Source Overlay SHA256 mismatch')
    ensure_directories(store.root, 'store/tmp')
    temp = Path(tempfile.mkdtemp(prefix='overlay-', dir=store.root / 'store/tmp'))
    relative = 'store/objects/overlay-squashfs/' + expected
    try:
        _sparse_copy(source, temp / 'overlay.squashfs')
        if sha256(temp / 'overlay.squashfs') != expected or sha256(source) != expected:
            raise ValidationError('Overlay changed while importing')
        def verify(root):
            p = regular_path(root, 'overlay.squashfs')
            if sha256(p) != expected:
                raise ValidationError('Existing managed Overlay is corrupt')
            check_squashfs(p)
        store._publish(temp, relative, verify)
    finally:
        if temp.exists():
            shutil.rmtree(temp)
    return {'format': 'squashfs', 'sha256': expected, 'readonly': True,
            'content': {'relative_path': relative + '/overlay.squashfs', 'origin': declaration['uri'],
                        'retention': 'managed-no-automatic-deletion'}}


def ingest_record(store, record):
    digest = identity(record)
    ensure_directories(store.root, 'store/tmp')
    temp = Path(tempfile.mkdtemp(prefix='build-record-', dir=store.root / 'store/tmp'))
    relative = 'store/objects/build-record/' + digest
    try:
        atomic_json(temp / 'record.json', record)
        def verify(root):
            if read_json(regular_path(root, 'record.json')) != record:
                raise ValidationError('Existing Build Record is corrupt')
        store._publish(temp, relative, verify)
    finally:
        if temp.exists():
            shutil.rmtree(temp)
    return {'sha256': digest, 'relative_path': relative + '/record.json', 'record': record}


def environment_image(catalog, item):
    """Flatten only at the runtime boundary; keep full composition in snapshot."""
    keys = ('id', 'platform', 'accelerator', 'runtimes', 'tools', 'base', 'overlay', 'build_record', 'validations')
    strict(item, keys, 'stored environment', ('id', 'platform', 'accelerator', 'runtimes', 'tools', 'base'))
    artifact = item['base']
    if any(artifact[k] != item[k] for k in ('platform', 'accelerator', 'tools', 'runtimes')):
        raise ValidationError('Stored base metadata differs from environment binding')
    store = store_class(catalog.header['store']['kind'])(catalog.root)
    image = store.runtime_image(artifact)
    image['accelerator'] = item['accelerator']
    base_id = {'kind': image['kind'], 'digest': 'sha256:' + image['sif_sha256'] if image['kind'] == 'sif' else image['digest']}
    overlay = item.get('overlay')
    overlay_id, record = None, None
    if overlay is not None:
        strict(overlay, ('format', 'sha256', 'readonly', 'content'), 'stored overlay',
               ('format', 'sha256', 'readonly', 'content'))
        strict(overlay['content'], ('relative_path', 'origin', 'retention'), 'overlay content',
               ('relative_path', 'origin', 'retention'))
        overlay_id = {k: overlay[k] for k in ('format', 'sha256', 'readonly')}
        relative = overlay['content']['relative_path']
        if relative != 'store/objects/overlay-squashfs/' + overlay['sha256'] + '/overlay.squashfs':
            raise ValidationError('Non-canonical Overlay object locator')
        image['overlay'] = dict(overlay_id, source=str(catalog.root / relative), managed={
            'store_kind': 'filesystem', 'root': str(catalog.root), 'relative_path': relative})
        saved = item['build_record']
        strict(saved, ('sha256', 'relative_path', 'record'), 'stored build record',
               ('sha256', 'relative_path', 'record'))
        record = saved['record']
        if saved['sha256'] != identity(record) or saved['relative_path'] != 'store/objects/build-record/' + saved['sha256'] + '/record.json':
            raise ValidationError('Build Record identity/path mismatch')
        validate_validations(item.get('validations', []), base_id['digest'][7:], overlay['sha256'], item['runtimes'])
    elif item.get('build_record') is not None or item.get('validations'):
        raise ValidationError('Unexpected Build Record for standalone image')
    comp = composition(base_id, overlay_id, item['tools'], item['platform'], item['accelerator'])
    image['environment_snapshot'] = make_snapshot(comp, record, item.get('validations', []))
    validate_environment_binding(image)
    return image


def validate_entry(catalog, entry):
    envs = entry.get('environments')
    if not isinstance(envs, list) or not envs:
        raise ValidationError('Stored environment release is empty')
    ids, selections = set(), set()
    for item in envs:
        safe_name(item['id'])
        if item['id'] in ids or not isinstance(item['runtimes'], list) or not item['runtimes']:
            raise ValidationError('Invalid stored environment ID/runtimes')
        ids.add(item['id'])
        for rt in item['runtimes']:
            safe_name(rt)
            key = (item['platform'], item['accelerator'], rt)
            if key in selections:
                raise ValidationError('Ambiguous stored environment')
            selections.add(key)
        environment_image(catalog, item)  # Metadata integrity only, no runtime or Git.


def verify_entry(catalog, entry):
    for item in entry['environments']:
        verify_environment_files(environment_image(catalog, item))
        if 'build_record' in item:
            saved = item['build_record']
            if read_json(regular_path(catalog.root, saved['relative_path'])) != saved['record']:
                raise ValidationError('Retained Build Record has changed')
    return {'name': entry['name'], 'release': entry['release'], 'status': 'VERIFIED_STORED',
            'entry_sha256': entry['entry_sha256'], 'runtime_validation': 'NOT_PERFORMED_AT_REGISTRATION',
            'declared_validations': {e['id']: e.get('validations', []) for e in entry['environments']},
            'provenance_verification': 'declarative-integrity-only', 'source_accessed': False}


def register(catalog, declaration, skopeo, timeout):
    data = copy.deepcopy(validate_declaration(declaration))
    destination = catalog._entry_path(data['name'], data['release'])
    with locked(catalog.root / 'catalog.lock'):
        if destination.exists():
            old = catalog.read(data['name'], data['release'])
            if old['registration_sha256'] != identity(data):
                raise ValidationError('Immutable name/release already exists; choose a new release')
            catalog.validate(data['name'], data['release'])
            return {'status': 'ALREADY_REGISTERED', 'entry': old}
        store = store_class(catalog.header['store']['kind'])(catalog.root)
        envs = []
        for decl in data['environments']:
            item = {k: copy.deepcopy(decl[k]) for k in ('id', 'platform', 'accelerator', 'tools', 'runtimes')}
            base_input = dict(decl['base'], **{k: decl[k] for k in ('platform', 'accelerator', 'tools', 'runtimes')})
            content = store.ingest(base_input, skopeo=skopeo, timeout=timeout)
            item['base'] = {k: base_input[k] for k in ('kind', 'platform', 'accelerator', 'tools', 'runtimes')}
            item['base']['content'] = content
            if 'overlay' in decl:
                item['overlay'] = ingest_overlay(store, decl['overlay'])
                item['build_record'] = ingest_record(store, decl['build_record'])
                item['validations'] = copy.deepcopy(decl.get('validations', []))
            verify_environment_files(environment_image(catalog, item))
            envs.append(item)
        entry = {'schema_version': 2, 'name': data['name'], 'release': data['release'],
                 'catalog_id': catalog.header['catalog_id'], 'environments': envs,
                 'registration_sha256': identity(data), 'registered_at': datetime.now(timezone.utc).isoformat(),
                 'import_policy': 'managed-environment-v2'}
        if len(json_bytes(entry)) + 100 > MAX_METADATA:
            raise ValidationError('Environment release exceeds metadata size limit')
        entry['entry_sha256'] = identity(entry)
        validate_entry(catalog, entry)
        ensure_directories(catalog.root, str(destination.parent.relative_to(catalog.root)))
        atomic_json(destination, entry)
        sync_dir(destination.parent)
    return {'status': 'REGISTERED', 'entry': entry}


def select(catalog, entry, runtime, platform, cls):
    candidates = [e for e in entry['environments'] if e['platform'] == platform
                  and (runtime['gpu'] == 'none' or e['accelerator'] == runtime['gpu'])
                  and runtime['runtime'] in e['runtimes']]
    if len(candidates) != 1:
        raise ValidationError('Catalog must resolve exactly one environment binding')
    item = candidates[0]
    if item['base']['kind'] not in cls.capabilities.image_kinds:
        raise ValidationError('Runtime cannot execute this environment base')
    if 'overlay' in item and 'squashfs' not in cls.capabilities.readonly_overlay_formats:
        raise ValidationError('Runtime does not support read-only squashfs Overlay')
    image = environment_image(catalog, item)
    verify_environment_files(image)
    return image, item
