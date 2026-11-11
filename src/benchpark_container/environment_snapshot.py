# SPDX-License-Identifier: Apache-2.0
"""Frozen composite-environment validation; stdlib only, no Catalog or network.

A content hash is an integrity check, not a signature or proof of compatibility.
Build declarations are carried into CER so interpretation never needs a Catalog.
"""
import hashlib
import json
import re
import stat
import struct
from pathlib import Path, PurePosixPath
from urllib.parse import urlparse

from .contracts import container_tools
from .image_store import digest_value, regular_path, MAX_METADATA
from .util import ValidationError, identity, strict, check_no_secrets, sha256, json_bytes


def hex_hash(value):
    if not isinstance(value, str):
        raise ValidationError('Expected a SHA256 string')
    digest_value('sha256:' + value)
    return value


def relative_file(value):
    if (not isinstance(value, str) or not value or value.startswith('-')
            or any(c in value for c in '\\\x00\n\r') or '//' in value
            or PurePosixPath(value).is_absolute() or '..' in value.split('/')
            or '.' in value.split('/') or not PurePosixPath(value).name):
        raise ValidationError('Expected a normalized recipe-relative file path')
    return value


def text_file(data):
    strict(data, ('path', 'sha256', 'content', 'executable'), 'recipe file',
           ('path', 'sha256', 'content', 'executable'))
    relative_file(data['path']); hex_hash(data['sha256'])
    if not isinstance(data['content'], str) or type(data['executable']) is not bool:
        raise ValidationError('Recipe/lock must be UTF-8 text with an executable boolean')
    raw = data['content'].encode('utf-8')
    if len(raw) > 2 * 1024 * 1024 or '\x00' in data['content']:
        raise ValidationError('Recipe text is oversized or binary')
    if hashlib.sha256(raw).hexdigest() != data['sha256']:
        raise ValidationError('Recipe/lock content hash mismatch')
    return data


def validate_build_record(record, base_sha, overlay_sha):
    keys = ('schema_version', 'base_sha256', 'overlay_sha256', 'recipe', 'lock', 'builder', 'software')
    strict(record, keys, 'build record', keys)
    if type(record['schema_version']) is not int or record['schema_version'] != 1:
        raise ValidationError('Unsupported build record schema')
    if (hex_hash(record['base_sha256']) != base_sha or
            hex_hash(record['overlay_sha256']) != overlay_sha):
        raise ValidationError('Build record Base/Overlay does not match execution composition')
    recipe = record['recipe']
    strict(recipe, ('repository', 'commit', 'entrypoint', 'files'), 'recipe',
           ('repository', 'commit', 'entrypoint', 'files'))
    url = urlparse(recipe['repository'] if isinstance(recipe['repository'], str) else '')
    if (url.scheme not in ('https', 'ssh') or not url.hostname or not url.path.strip('/')
            or url.password or url.query or url.fragment or
            (url.scheme == 'https' and url.username)):
        raise ValidationError('Recipe requires an HTTPS or SSH Git URL without credentials/query')
    if not isinstance(recipe['commit'], str) or not re.fullmatch('[0-9a-f]{40}', recipe['commit']):
        raise ValidationError('Recipe requires a full fixed Git commit')
    relative_file(recipe['entrypoint'])
    files = recipe['files']
    if not isinstance(files, list) or not files or len(files) > 128:
        raise ValidationError('Recipe requires a bounded list of source files')
    inventory = {}
    for f in files:
        text_file(f)
        if f['path'] in inventory:
            raise ValidationError('Duplicate recipe file')
        if any(old.startswith(f['path'] + '/') or f['path'].startswith(old + '/') for old in inventory):
            raise ValidationError('Recipe file paths overlap as file/directory')
        inventory[f['path']] = f
    if recipe['entrypoint'] not in inventory:
        raise ValidationError('Recipe entrypoint bytes must be retained')
    lock = text_file(record['lock'])
    if inventory.get(lock['path']) != lock:
        raise ValidationError('Lock must be retained in the same declared Git source closure')
    builder = record['builder']
    strict(builder, ('name', 'version', 'argv', 'parameters'), 'builder',
           ('name', 'version', 'argv', 'parameters'))
    if (any(not isinstance(builder[k], str) or not builder[k] for k in ('name', 'version'))
            or not isinstance(builder['argv'], list) or not builder['argv']
            or any(not isinstance(v, str) or '\x00' in v for v in builder['argv'])
            or not isinstance(builder['parameters'], dict)):
        raise ValidationError('Builder identity, argv and parameter mapping are required')
    software = record['software']
    strict(software, ('status', 'packages'), 'software inventory', ('status', 'packages'))
    if software['status'] not in ('recorded', 'not_collected') or not isinstance(software['packages'], list):
        raise ValidationError('Invalid software inventory status')
    if software['status'] == 'not_collected' and software['packages']:
        raise ValidationError('Uncollected inventory cannot contain packages')
    names = set()
    for package in software['packages']:
        strict(package, ('name', 'version'), 'software package', ('name', 'version'))
        if any(not isinstance(package[k], str) or not package[k] for k in ('name', 'version')):
            raise ValidationError('Invalid software package identity')
        if package['name'] in names:
            raise ValidationError('Duplicate software package')
        names.add(package['name'])
    if len(json_bytes(record)) > MAX_METADATA // 2:
        raise ValidationError('Build record exceeds metadata size limit')
    check_no_secrets(record)
    return record


def validate_validations(records, base_sha, overlay_sha, runtimes=None):
    if not isinstance(records, list) or len(records) > 32:
        raise ValidationError('Validation records must be a bounded list')
    for rec in records:
        keys = ('base_sha256', 'overlay_sha256', 'runtime', 'scope', 'status', 'exit_code', 'summary')
        strict(rec, keys, 'validation record', keys)
        if rec['base_sha256'] != base_sha or rec['overlay_sha256'] != overlay_sha:
            raise ValidationError('Validation is for a different Base/Overlay pair')
        strict(rec['runtime'], ('name', 'version'), 'validation runtime', ('name', 'version'))
        if any(not isinstance(rec['runtime'][k], str) or not rec['runtime'][k] for k in ('name', 'version')):
            raise ValidationError('Validation runtime identity required')
        if runtimes is not None and rec['runtime']['name'] not in runtimes:
            raise ValidationError('Validation runtime is not in this binding')
        if rec['scope'] not in ('mount', 'software-smoke', 'distributed-smoke', 'performance'):
            raise ValidationError('Unknown validation scope')
        if rec['status'] not in ('passed', 'failed') or type(rec['exit_code']) is not int:
            raise ValidationError('Validation requires an explicit verdict and exit code')
        if (rec['exit_code'] == 0) != (rec['status'] == 'passed'):
            raise ValidationError('Validation exit code contradicts verdict')
        if not isinstance(rec['summary'], dict):
            raise ValidationError('Validation summary must be a mapping')
    check_no_secrets(records)
    return records


def composition(base, overlay, tools, platform, accelerator):
    """No host paths or validation dates in the executable environment identity."""
    return {'base': base, 'overlay': overlay, 'tools': tools,
            'platform': platform, 'accelerator': accelerator}


def make_snapshot(comp, build_record=None, validations=None):
    value = {'schema_version': 1, 'composition': comp,
             'environment_sha256': identity(comp),
             'build_record': build_record,
             'build_record_sha256': identity(build_record) if build_record is not None else None,
             'validations': validations or [],
             'provenance_verification': 'declarative-integrity-only'}
    value['snapshot_sha256'] = identity(value)
    validate_snapshot(value)
    return value


def validate_snapshot(value):
    keys = ('schema_version', 'composition', 'environment_sha256', 'build_record',
            'build_record_sha256', 'validations', 'provenance_verification', 'snapshot_sha256')
    strict(value, keys, 'environment snapshot', keys)
    if type(value['schema_version']) is not int or value['schema_version'] != 1:
        raise ValidationError('Unsupported environment snapshot schema')
    if value['provenance_verification'] != 'declarative-integrity-only':
        raise ValidationError('Unsupported provenance trust claim')
    body = dict(value); recorded = body.pop('snapshot_sha256')
    if identity(body) != recorded:
        raise ValidationError('Environment snapshot hash mismatch')
    comp = value['composition']
    strict(comp, ('base', 'overlay', 'tools', 'platform', 'accelerator'), 'composition',
           ('base', 'overlay', 'tools', 'platform', 'accelerator'))
    if comp['platform'] not in ('linux/arm64', 'linux/amd64') or comp['accelerator'] not in ('none', 'nvidia', 'amd'):
        raise ValidationError('Unsupported composition platform/accelerator')
    container_tools(comp['tools'])
    strict(comp['base'], ('kind', 'digest'), 'base identity', ('kind', 'digest'))
    if comp['base']['kind'] not in ('sif', 'oci'):
        raise ValidationError('Invalid base kind')
    digest_value(comp['base']['digest'])
    if identity(comp) != value['environment_sha256']:
        raise ValidationError('Environment composition identity mismatch')
    overlay = comp['overlay']
    if overlay is not None:
        strict(overlay, ('format', 'sha256', 'readonly'), 'overlay identity', ('format', 'sha256', 'readonly'))
        if comp['base']['kind'] != 'sif' or overlay['format'] != 'squashfs' or overlay['readonly'] is not True:
            raise ValidationError('Only one immutable read-only squashfs Overlay over SIF is supported')
        hex_hash(overlay['sha256'])
        validate_build_record(value['build_record'], comp['base']['digest'][7:], overlay['sha256'])
        if value['build_record_sha256'] != identity(value['build_record']):
            raise ValidationError('Build record hash mismatch')
        validate_validations(value['validations'], comp['base']['digest'][7:], overlay['sha256'])
    elif value['build_record'] is not None or value['build_record_sha256'] is not None or value['validations']:
        raise ValidationError('This version records build/validation metadata for Overlay bindings only')
    check_no_secrets(value)
    return value


def validate_environment_binding(base):
    """Validate saved plan data without reopening any external metadata or bytes."""
    snapshot = base.get('environment_snapshot')
    if snapshot is None:
        if 'overlay' in base:
            raise ValidationError('Overlay requires a complete environment snapshot')
        return None
    validate_snapshot(snapshot)
    comp = snapshot['composition']
    expected = ('sha256:' + base.get('sif_sha256', '')) if base.get('kind') == 'sif' else base.get('digest')
    if (comp['base'] != {'kind': base.get('kind'), 'digest': expected}
            or comp['platform'] != base.get('platform') or comp['tools'] != base.get('tools')
            or comp['accelerator'] != base.get('accelerator')):
        raise ValidationError('Plan base differs from frozen environment composition')
    overlay = base.get('overlay')
    if comp['overlay'] is None:
        if overlay is not None:
            raise ValidationError('Unexpected Overlay locator')
    else:
        if not isinstance(overlay, dict):
            raise ValidationError('Missing Overlay locator')
        strict(overlay, ('format', 'sha256', 'readonly', 'source', 'managed'), 'overlay locator',
               ('format', 'sha256', 'readonly', 'source', 'managed'))
        if {k: overlay[k] for k in ('format', 'sha256', 'readonly')} != comp['overlay']:
            raise ValidationError('Overlay locator differs from frozen composition')
        strict(overlay['managed'], ('store_kind', 'root', 'relative_path'), 'overlay store',
               ('store_kind', 'root', 'relative_path'))
        if overlay['managed']['store_kind'] != 'filesystem':
            raise ValidationError('Unknown Overlay store')
    return snapshot


def check_squashfs(path):
    """Conservative SquashFS v4 header/size check, not an image safety audit.

    The final read-only distribution artifact must be a real SquashFS header,
    not an ext3 image renamed to .squashfs. Read only; never mount or repair.
    """
    p = Path(path)
    if p.is_symlink() or not stat.S_ISREG(p.stat().st_mode):
        raise ValidationError('SquashFS Overlay must be a regular file')
    size = p.stat().st_size
    if size < 96:
        raise ValidationError('SquashFS Overlay is too small')
    with p.open('rb') as stream:
        header = stream.read(96)
    if header[:4] != b'hsqs':
        raise ValidationError('Overlay is not a SquashFS image')
    inodes = struct.unpack_from('<I', header, 4)[0]
    block_size = struct.unpack_from('<I', header, 12)[0]
    compression = struct.unpack_from('<H', header, 20)[0]
    block_log = struct.unpack_from('<H', header, 22)[0]
    ids = struct.unpack_from('<H', header, 26)[0]
    major, minor = struct.unpack_from('<HH', header, 28)
    used = struct.unpack_from('<Q', header, 40)[0]
    if (major != 4 or minor != 0 or not inodes or not ids
            or block_size < 4096 or block_size > 1048576
            or block_size & (block_size - 1)
            or block_log != block_size.bit_length() - 1
            or compression not in range(1, 7)
            or used < 96 or used > size):
        raise ValidationError('SquashFS Overlay has invalid v4 superblock fields')
    return {'format': 'squashfs', 'header': 'checked-not-fully-parsed', 'bytes': size}

def verify_overlay(overlay):
    m = overlay['managed']
    p = regular_path(m['root'], m['relative_path'])
    if str(p) != overlay['source'] or sha256(p) != overlay['sha256']:
        raise ValidationError('Managed Overlay content or path changed')
    if overlay['readonly'] is not True or overlay['format'] != 'squashfs':
        raise ValidationError('Only read-only squashfs Overlay is permitted')
    check_squashfs(p)
    return {'source': str(p), 'sha256': overlay['sha256'], 'format': 'squashfs', 'readonly': True}


def verify_environment_files(base):
    snapshot = validate_environment_binding(base)
    if snapshot is None:
        return None
    # Deferred import avoids an image_store/environment import cycle.
    from .image_store import verify_managed
    verify_managed(base)
    overlay = verify_overlay(base['overlay']) if base.get('overlay') is not None else None
    return {'status': 'verified', 'environment_sha256': snapshot['environment_sha256'],
            'snapshot_sha256': snapshot['snapshot_sha256'],
            'base': snapshot['composition']['base'], 'overlay': overlay}
