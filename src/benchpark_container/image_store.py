# SPDX-License-Identifier: Apache-2.0
"""Read-only integrity checks for managed container content (stdlib-only worker).

A digest proves bytes, not availability, licensing, signature trust or GPU support.
No catalog/config lookup or network operation is performed here.
"""
import hashlib
import json
from pathlib import Path
import re
import stat

from .util import ValidationError, sha256

DIGEST = re.compile(r'sha256:[0-9a-f]{64}\Z')
INDEX_TYPES = {'application/vnd.oci.image.index.v1+json',
               'application/vnd.docker.distribution.manifest.list.v2+json'}
MANIFEST_TYPES = {'application/vnd.oci.image.manifest.v1+json',
                  'application/vnd.docker.distribution.manifest.v2+json'}
MAX_METADATA = 16 * 1024 * 1024


def digest_value(value):
    if not isinstance(value, str) or not DIGEST.fullmatch(value):
        raise ValidationError('Expected a full lowercase sha256 digest')
    return value


def unique_object(pairs):
    out = {}
    for key, value in pairs:
        if key in out:
            raise ValidationError('Duplicate JSON key: ' + key)
        out[key] = value
    return out


def regular_path(root, relative):
    root = Path(root).resolve(strict=True)
    rel = Path(relative)
    if rel.is_absolute() or '..' in rel.parts or not rel.parts:
        raise ValidationError('Unsafe managed artifact path')
    p = root
    for part in rel.parts:
        p = p / part
        if p.is_symlink():
            raise ValidationError('Managed artifact may not contain a symlink: ' + str(p))
    if not stat.S_ISREG(p.stat().st_mode):
        raise ValidationError('Managed artifact is not a regular file: ' + str(p))
    return p


def read_json(path):
    path = Path(path)
    if path.stat().st_size > MAX_METADATA:
        raise ValidationError('Container metadata exceeds size limit')
    try:
        return json.loads(path.read_text('utf-8'), object_pairs_hook=unique_object)
    except (ValueError, UnicodeError) as error:
        raise ValidationError('Invalid container JSON: ' + str(path)) from error


def checked_blob(root, descriptor):
    if not isinstance(descriptor, dict):
        raise ValidationError('Invalid OCI descriptor')
    digest = digest_value(descriptor.get('digest'))
    size = descriptor.get('size')
    if not isinstance(size, int) or isinstance(size, bool) or size < 0:
        raise ValidationError('OCI descriptor requires a nonnegative byte size')
    p = regular_path(root, 'blobs/sha256/' + digest[7:])
    if p.stat().st_size != size or sha256(p) != digest[7:]:
        raise ValidationError('OCI blob digest/size mismatch: ' + digest)
    return p


def platform_name(config):
    os_name, arch = config.get('os'), config.get('architecture')
    if not isinstance(os_name, str) or not isinstance(arch, str):
        raise ValidationError('OCI config lacks OS/architecture')
    return os_name + '/' + arch + ('/' + config['variant'] if config.get('variant') else '')


def matching_platform(actual, declared):
    return actual == declared or (declared.count('/') == 1 and actual.startswith(declared + '/'))


def verify_layout(root, expected_digest=None):
    """Verify every reachable manifest/config/layer, including all index members.

    Missing foreign blobs are rejected even when descriptor.urls is present.
    Blobs are never unpacked. All layers must already be retained locally.
    """
    root = Path(root)
    layout = read_json(regular_path(root, 'oci-layout'))
    if layout != {'imageLayoutVersion': '1.0.0'}:
        raise ValidationError('Unsupported OCI layout version')
    index = read_json(regular_path(root, 'index.json'))
    if index.get('schemaVersion') != 2 or len(index.get('manifests', [])) != 1:
        raise ValidationError('Managed OCI layout requires exactly one root descriptor')
    top = index['manifests'][0]
    if expected_digest and top.get('digest') != digest_value(expected_digest):
        raise ValidationError('Managed OCI root digest changed')
    manifests, visited, active = [], set(), set()

    def walk(desc, depth=0):
        if depth > 32:
            raise ValidationError('OCI descriptor nesting exceeds limit')
        p = checked_blob(root, desc)
        digest = desc['digest']
        if digest in active:
            raise ValidationError('Cyclic OCI descriptor graph')
        if digest in visited:
            return
        active.add(digest)
        doc = read_json(p)
        if doc.get('schemaVersion') != 2 or doc.get('subject') or doc.get('artifactType'):
            raise ValidationError('Only runnable OCI/Docker schema-2 image graphs are supported')
        media = desc.get('mediaType', doc.get('mediaType'))
        if doc.get('mediaType', media) != media:
            raise ValidationError('OCI descriptor/media type mismatch')
        if media in INDEX_TYPES:
            if not isinstance(doc.get('manifests'), list) or not doc['manifests']:
                raise ValidationError('Empty OCI image index')
            for child in doc['manifests']:
                walk(child, depth + 1)
        elif media in MANIFEST_TYPES:
            config = read_json(checked_blob(root, doc.get('config')))
            layers = doc.get('layers')
            if not isinstance(layers, list):
                raise ValidationError('OCI manifest requires layers')
            for layer in layers:
                checked_blob(root, layer)
            diff_ids = config.get('rootfs', {}).get('diff_ids')
            if (config.get('rootfs', {}).get('type') != 'layers' or not isinstance(diff_ids, list)
                    or len(diff_ids) != len(layers)):
                raise ValidationError('OCI config rootfs does not match layer count')
            for value in diff_ids:
                digest_value(value)
            platform = platform_name(config)
            advertised = desc.get('platform')
            if advertised:
                if not matching_platform(platform, platform_name(advertised)):
                    raise ValidationError('OCI index platform differs from its config')
            manifests.append({'manifest_digest': digest, 'config_digest': doc['config']['digest'],
                              'platform': platform, 'config': config, 'layers': layers})
        else:
            raise ValidationError('Unsupported OCI manifest media type: ' + str(media))
        active.remove(digest)
        visited.add(digest)

    walk(top)
    if not manifests:
        raise ValidationError('OCI layout contains no runnable manifest')
    return {'root_digest': top['digest'], 'manifests': manifests}


def select_manifest(verified, platform):
    matches = [m for m in verified['manifests'] if matching_platform(m['platform'], platform)]
    if len(matches) != 1:
        raise ValidationError('OCI platform must select exactly one manifest: ' + platform)
    return matches[0]


def verify_managed(base):
    """Verify the frozen store locator; never consult a mutable catalog again."""
    managed = base.get('managed')
    if not isinstance(managed, dict) or managed.get('store_kind') != 'filesystem':
        raise ValidationError('Unsupported managed image store')
    root = Path(managed['root'])
    if base.get('kind') == 'sif':
        p = regular_path(root, managed['relative_path'])
        if sha256(p) != base['sif_sha256'] or str(p) != base['source']:
            raise ValidationError('Managed SIF content or path changed')
        return {'kind': 'sif', 'source': str(p)}
    if base.get('kind') == 'oci':
        rel = Path(managed['relative_path'])
        if rel.is_absolute() or '..' in rel.parts:
            raise ValidationError('Unsafe OCI store path')
        # Include parents in the no-symlink check.
        p = regular_path(root, str(rel / 'index.json')).parent
        data = verify_layout(p, managed['stored_digest'])
        chosen = select_manifest(data, base['platform'])
        if (chosen['config_digest'] != managed['config_digest']
                or chosen['manifest_digest'] != managed['manifest_digest']):
            raise ValidationError('Managed selected manifest/config identity changed')
        return dict(chosen, root_digest=data['root_digest'], layout=str(p))
    raise ValidationError('Unknown managed image kind')


def selected_layout(verified, destination):
    """Build a transient, single-manifest view of retained OCI blobs.

    External tools may choose the host platform by default; supplying exactly
    the frozen selected manifest prevents a different implicit image selection.
    No source-registry fetch and no changes to the saved store occur here.
    """
    import os
    import shutil
    from .util import atomic_json
    destination = Path(destination)
    (destination / 'blobs/sha256').mkdir(parents=True, exist_ok=False)
    root = Path(verified['layout'])
    mp = root / 'blobs/sha256' / verified['manifest_digest'][7:]
    manifest = read_json(mp)
    digests = {verified['manifest_digest'], verified['config_digest']}
    digests.update(x['digest'] for x in verified['layers'])
    for digest in digests:
        source = regular_path(root, 'blobs/sha256/' + digest[7:])
        target = destination / 'blobs/sha256' / digest[7:]
        try:
            os.link(source, target)
        except OSError:
            shutil.copyfile(source, target)
    atomic_json(destination / 'oci-layout', {'imageLayoutVersion': '1.0.0'})
    atomic_json(destination / 'index.json', {'schemaVersion': 2, 'manifests': [{
        'mediaType': manifest['mediaType'], 'digest': verified['manifest_digest'],
        'size': mp.stat().st_size}]})
    verify_layout(destination, verified['manifest_digest'])
    return destination
