# SPDX-License-Identifier: Apache-2.0
"""Retain immutable image bytes; the managed artifact is canonical after import.

Registry tags are resolved once before copying by digest. OCI normalization may
change the image configuration: source equivalence is NOT a registration claim.
Source manifests are provenance; stored graph integrity is checked separately.
"""
import hashlib
import json
import os
import re
from pathlib import Path
import shutil
import stat
import subprocess
import tempfile
from urllib.parse import unquote, urlparse

from ..util import ValidationError, sha256, atomic_json
from ..image_store import (verify_layout, select_manifest, digest_value, regular_path,
                           unique_object, INDEX_TYPES, MANIFEST_TYPES, MAX_METADATA)
from .config import sync_dir, ensure_directories


class ArtifactStore:
    kind = ''
    def ingest(self, item, skopeo='skopeo', timeout=3600):
        raise NotImplementedError

    def runtime_image(self, artifact):
        raise NotImplementedError

    def verify(self, artifact):
        from ..image_store import verify_managed
        return verify_managed(self.runtime_image(artifact))


def _safe_tree(root):
    for p in Path(root).rglob('*'):
        if p.is_symlink() or not (p.is_dir() or stat.S_ISREG(p.stat().st_mode)):
            raise ValidationError('Store tree contains a symlink or special file: ' + str(p))


def _sync_tree(root):
    _safe_tree(root)
    for path in Path(root).rglob('*'):
        if path.is_file():
            with path.open('rb') as f:
                os.fsync(f.fileno())
    for path in sorted([p for p in Path(root).rglob('*') if p.is_dir()], reverse=True):
        sync_dir(path)
    sync_dir(root)


def _freeze_tree(root):
    for path in Path(root).rglob('*'):
        path.chmod(0o555 if path.is_dir() else 0o444)
    Path(root).chmod(0o555)


def remote_reference(value: str) -> str:
    """Validate a fully qualified registry import locator, not a runtime image.

    Only register accepts an explicit tag. Execution still requires a frozen
    managed locator or a digest-only direct image. Never infer ':latest'.
    """
    if not isinstance(value, str):
        raise ValidationError('Registry source must be a string')
    value = value.removeprefix('docker://')
    if '@' in value:
        if value.count('@') != 1:
            raise ValidationError('Invalid registry digest reference; credentials are not allowed')
        repo, digest = value.rsplit('@', 1)
        digest_value(digest)
    else:
        repo, sep, tag = value.rpartition(':')
        if not sep or '/' in tag or not re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.-]{0,127}', tag):
            raise ValidationError('Registry import requires an explicit :tag or @sha256:digest')
    registry, slash, path = repo.partition('/')
    host = r'(?:[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?|\[[0-9A-Fa-f:]+\])(?::[0-9]+)?'
    component = r'[a-z0-9]+(?:(?:[._]|__|-+)[a-z0-9]+)*'
    if not slash or not re.fullmatch(host, registry) or not re.fullmatch(component + r'(?:/' + component + r')*', path):
        raise ValidationError('Use registry/repository with an explicit tag or digest; no embedded credentials or URL parameters')
    return value


def registry_repository(reference: str) -> str:
    """Return the repository from a validated explicit tag or digest locator."""
    reference = remote_reference(reference)
    return reference.rsplit('@', 1)[0] if '@' in reference else reference.rsplit(':', 1)[0]


class Skopeo:
    def __init__(self, executable, timeout):
        self.executable = shutil.which(executable)
        if not self.executable:
            raise ValidationError('OCI import requires declared skopeo executable: ' + executable)
        self.timeout = timeout
        self.version = self.run(['--version']).decode('utf-8').strip()
        if 'skopeo' not in self.version.lower():
            raise ValidationError('Declared OCI copier is not skopeo')

    def run(self, arguments):
        # Registry credentials are consumed by skopeo's normal credential store.
        # Do not persist stdout/stderr/env: they may expose private registry data.
        result = subprocess.run([self.executable] + arguments, capture_output=True, timeout=self.timeout)
        if result.returncode:
            raise ValidationError('skopeo ' + arguments[0] + ' failed (exit %d); inspect registry access/trust locally' % result.returncode)
        return result.stdout

    def pin_reference(self, reference: str, expected_digest=None):
        """Resolve the raw top-level manifest once; return a digest-only locator.

        This deliberately does not fetch/compare source config JSON. Skopeo's
        conversion of a source to OCI is part of import, not an equivalence
        proof. The complete stored image graph is verified after the copy.
        """
        reference = remote_reference(reference)
        declared = reference.rsplit('@', 1)[1] if '@' in reference else None
        if expected_digest is not None:
            digest_value(expected_digest)
            if declared is not None and declared != expected_digest:
                raise ValidationError('Declared source digest differs from registry reference')
            declared = expected_digest
        raw = self.run(['inspect', '--raw', 'docker://' + reference])
        if len(raw) > MAX_METADATA:
            raise ValidationError('Registry manifest exceeds size limit')
        digest = 'sha256:' + hashlib.sha256(raw).hexdigest()
        if declared is not None and digest != declared:
            raise ValidationError('Registry manifest bytes do not match the requested digest')
        doc = json.loads(raw, object_pairs_hook=unique_object)
        if not isinstance(doc, dict) or doc.get('schemaVersion') != 2 or doc.get('subject') or doc.get('artifactType'):
            raise ValidationError('Only runnable OCI/Docker schema-2 sources are supported')
        if doc.get('mediaType') not in INDEX_TYPES | MANIFEST_TYPES:
            raise ValidationError('Unsupported source manifest media type')
        return registry_repository(reference) + '@' + digest, raw


class FilesystemStore(ArtifactStore):
    kind = 'filesystem'
    def __init__(self, root):
        self.root = Path(root).resolve(strict=True)

    def runtime_image(self, artifact):
        content = artifact['content']
        managed = {'store_kind': self.kind, 'root': str(self.root),
                   'relative_path': content['relative_path']}
        result = {'kind': artifact['kind'], 'platform': artifact['platform'], 'tools': artifact['tools'],
                  'managed': managed, 'origin': content['origin']}
        if artifact['kind'] == 'sif':
            p = self.root / content['relative_path']
            result.update(uri=p.as_uri(), source=str(p), sif_sha256=content['sif_sha256'])
        elif artifact['kind'] == 'oci':
            managed.update({key: content[key] for key in ('stored_digest', 'manifest_digest', 'config_digest')})
            result.update(uri='managed-oci:' + content['stored_digest'], digest=content['stored_digest'],
                          source_digest=content['source_digest'])
        else:
            raise ValidationError('Unsupported stored artifact kind')
        return result


    def _publish(self, temp, relative, verifier):
        final = self.root / relative
        ensure_directories(self.root, str(Path(relative).parent))
        if final.is_symlink() or any(p.is_symlink() for p in final.parents if p != self.root.parent):
            raise ValidationError('Managed object path may not be a symlink')
        if final.exists():
            verifier(final)
            return final
        _sync_tree(temp)
        os.rename(temp, final)  # Caller holds the catalog publication lock.
        _freeze_tree(final)
        sync_dir(final.parent)
        verifier(final)
        return final

    def ingest(self, item, skopeo='skopeo', timeout=3600):
        ensure_directories(self.root, 'store/tmp')
        ensure_directories(self.root, 'store/objects')
        temp = Path(tempfile.mkdtemp(prefix='import-', dir=self.root / 'store/tmp'))
        try:
            if item['kind'] == 'sif':
                uri = item['uri']
                parsed = urlparse(uri)
                if parsed.scheme != 'file' or parsed.netloc or parsed.query or parsed.fragment:
                    raise ValidationError('SIF registration requires file:///absolute.sif; fetch remote SIF explicitly first')
                source = Path(unquote(parsed.path))
                if not source.is_absolute() or source.is_symlink() or not stat.S_ISREG(source.stat().st_mode):
                    raise ValidationError('SIF source must be an absolute regular file')
                before = sha256(source)
                expected = item.get('sha256', before)
                digest_value('sha256:' + expected)
                if before != expected:
                    raise ValidationError('Source SIF SHA256 mismatch')
                shutil.copyfile(source, temp / 'image.sif')
                if sha256(temp / 'image.sif') != before or sha256(source) != before:
                    raise ValidationError('SIF changed while importing')
                relative = 'store/objects/sif/' + before
                def check(root):
                    if sha256(regular_path(root, 'image.sif')) != before:
                        raise ValidationError('Existing managed SIF is corrupt')
                self._publish(temp, relative, check)
                return {'kind': 'sif', 'sif_sha256': before, 'identity': 'sha256:' + before,
                        'relative_path': relative + '/image.sif', 'origin': uri,
                        'retention': 'managed-no-automatic-deletion',
                        'identity_basis': 'managed-artifact', 'import_policy': 'managed-canonical-v1'}
            if item['kind'] != 'oci':
                raise ValidationError('Unsupported filesystem image kind')
            layout = temp / 'layout'
            proof = {}
            transfer = None
            if 'oci_layout' in item:
                source = Path(item['oci_layout']).expanduser().resolve(strict=True)
                before = verify_layout(source, item.get('digest'))
                _safe_tree(source)
                shutil.copytree(source, layout, symlinks=True)
                verify_layout(layout, before['root_digest'])
                source_digest = before['root_digest']
                origin = str(source)
            else:
                reference = remote_reference(item['uri'])
                requested_digest = reference.rsplit('@', 1)[1] if '@' in reference else item.get('digest')
                if '@' in reference and 'digest' in item and digest_value(item['digest']) != requested_digest:
                    raise ValidationError('Declared source digest differs from registry reference')
                copier = Skopeo(skopeo, timeout)
                # Catalog.register resolves tags before idempotency checks. Use
                # that digest here so tag movement cannot retarget the copy.
                locator = registry_repository(reference) + '@' + requested_digest if requested_digest else reference
                pinned, source_raw = copier.pin_reference(locator, requested_digest)
                source_digest = pinned.rsplit('@', 1)[1]
                proof[source_digest] = source_raw
                copier.run(['copy', '--all', '--format', 'oci', 'docker://' + pinned, 'oci:' + str(layout) + ':managed'])
                origin = reference
                transfer = {'name': 'skopeo', 'version': copier.version,
                            'mode': 'all-platforms/OCI-normalization/managed-canonical',
                            'resolved_reference': pinned,
                            'source_reference_type': 'digest' if '@' in reference else 'tag',
                            'source_equivalence': 'NOT_ASSERTED'}
            data = verify_layout(layout)
            chosen = select_manifest(data, item['platform'])
            stored = data['root_digest']
            # The raw source root manifest is provenance, not a runtime dependency.
            proof_refs = []
            for digest, raw in sorted(proof.items()):
                p = self.root / ('store/source-proofs/' + digest[7:] + '.json')
                ensure_directories(self.root, 'store/source-proofs')
                if p.exists():
                    if p.is_symlink() or sha256(p) != digest[7:]:
                        raise ValidationError('Stored source proof blob is corrupt')
                else:
                    # Atomic non-overwriting publication.
                    fd, name = tempfile.mkstemp(dir=p.parent)
                    try:
                        with os.fdopen(fd, 'wb') as stream:
                            stream.write(raw); stream.flush(); os.fsync(stream.fileno())
                        os.link(name, p); p.chmod(0o444); sync_dir(p.parent)
                    finally:
                        os.unlink(name)
                proof_refs.append({'relative_path': str(p.relative_to(self.root)), 'digest': digest})
            relative = 'store/objects/oci/' + stored[7:]
            self._publish(temp, relative, lambda p: verify_layout(p / 'layout', stored))
            return {'kind': 'oci', 'identity': stored, 'stored_digest': stored,
                    'source_digest': source_digest, 'manifest_digest': chosen['manifest_digest'],
                    'config_digest': chosen['config_digest'], 'relative_path': relative + '/layout',
                    'origin': origin, 'source_proof_blobs': proof_refs, 'transfer': transfer,
                    'retention': 'managed-no-automatic-deletion',
                    'identity_basis': 'managed-artifact', 'import_policy': 'managed-canonical-v1',
                    'source_equivalence': 'NOT_ASSERTED' if transfer else 'LOCAL_COPY_VERIFIED'}
        finally:
            if temp.exists():
                for path in [temp] + [p for p in temp.rglob('*') if p.is_dir() and not p.is_symlink()]:
                    path.chmod(0o700)
                shutil.rmtree(temp)


STORES = {'filesystem': FilesystemStore}


def store_class(kind):
    cls = STORES.get(kind)
    if cls is None or not issubclass(cls, ArtifactStore):
        raise ValidationError('Unsupported ArtifactStore: ' + str(kind))
    return cls
