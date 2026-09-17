# SPDX-License-Identifier: Apache-2.0
"""Managed-canonical imports: real local OCI bytes, mocked registry/copy tool.

These tests do not claim real Skopeo, Docker, SIF or GPU execution. Source config
changes model conversion; stored-graph integrity is independently checked.
"""
import copy
import hashlib
import json
from pathlib import Path
import shutil
import subprocess

import pytest

from benchpark_container.catalog.manager import Catalog, init_catalog
from benchpark_container.catalog.store import remote_reference, registry_repository
from benchpark_container.image_store import verify_managed
from benchpark_container.util import ValidationError, json_bytes
from catalog_fixtures import oci_layout, sif_declaration


@pytest.fixture
def importer(tmp_path, monkeypatch):
    stored = oci_layout(tmp_path / 'tool-output', ('linux/amd64', 'linux/arm64'))
    top = json.loads((stored['root'] / 'index.json').read_text())['manifests'][0]
    raw_top = (stored['root'] / 'blobs/sha256' / top['digest'][7:]).read_bytes()
    source = json.loads(raw_top)
    source['mediaType'] = 'application/vnd.docker.distribution.manifest.list.v2+json'
    raw = json_bytes(source)
    digest = 'sha256:' + hashlib.sha256(raw).hexdigest()
    state = {'raw': raw, 'digest': digest, 'fault': None, 'calls': [],
             'all_sources': {digest: raw}, 'tag': 'registry.example/team/base:trial',
             'repository': 'registry.example/team/base', 'stored': stored}
    monkeypatch.setattr(shutil, 'which', lambda _: '/tools/skopeo')

    def run(cmd, **kwargs):
        state['calls'].append(cmd)
        if '--version' in cmd:
            return subprocess.CompletedProcess(cmd, 0, b'skopeo fixture-ONLY\n', b'')
        if cmd[1:3] == ['inspect', '--raw']:
            assert '--config' not in cmd
            ref = cmd[-1].removeprefix('docker://')
            if '@' in ref:
                response = state['all_sources'][ref.rsplit('@', 1)[1]]
            else:
                response = state['raw']
                # After returning A to the resolver the tag can already point B.
                if state['fault'] == 'move-after-resolution':
                    newer = dict(json.loads(response), annotations={'tag-moved': 'true'})
                    newraw = json_bytes(newer)
                    state['raw'] = newraw
                    state['digest'] = 'sha256:' + hashlib.sha256(newraw).hexdigest()
                    state['all_sources'][state['digest']] = newraw
            if state['fault'] == 'wrong-source-bytes':
                response += b' '
            return subprocess.CompletedProcess(cmd, 0, response, b'')
        assert cmd[1:5] == ['copy', '--all', '--format', 'oci']
        assert '@sha256:' in cmd[-2], 'Never copy by a mutable tag'
        assert cmd[-2].rsplit('@', 1)[1] in state['all_sources']
        if state['fault'] == 'copy-failed':
            return subprocess.CompletedProcess(cmd, 1, b'', b'credentials must not be persisted')
        destination = Path(cmd[-1][4:].rsplit(':', 1)[0])
        shutil.copytree(stored['root'], destination)
        blob = destination / 'blobs/sha256' / stored['layer']['digest'][7:]
        if state['fault'] == 'missing-layer':
            blob.unlink()
        if state['fault'] == 'corrupt-layer':
            blob.write_bytes(b'wrong bytes')
        if state['fault'] == 'bad-index':
            (destination / 'index.json').write_text('{broken')
        return subprocess.CompletedProcess(cmd, 0, b'', b'')

    monkeypatch.setattr(subprocess, 'run', run)
    root = tmp_path / 'catalog'
    init_catalog(root, 'personal')
    state['catalog'] = Catalog(root)
    state['declaration'] = {
        'schema_version': 1, 'name': 'base', 'release': 'r1',
        'artifacts': [{'kind': 'oci', 'uri': state['tag'],
                       'platform': 'linux/arm64', 'accelerator': 'nvidia',
                       'runtimes': ['docker'], 'tools': {'python': 'python3', 'shell': 'bash'}}]}
    return state


def test_tag_resolved_before_copy_and_managed_identity_is_canonical(importer):
    t = importer
    entry = t['catalog'].register(t['declaration'])['entry']
    a = entry['artifacts'][0]['content']
    assert a['origin'] == t['tag']
    assert a['source_digest'] == t['digest']
    assert a['stored_digest'] == t['stored']['digest'] != t['digest']
    assert a['identity'] == a['stored_digest']
    assert a['identity_basis'] == 'managed-artifact'
    assert a['source_equivalence'] == 'NOT_ASSERTED'
    assert a['transfer']['resolved_reference'] == t['repository'] + '@' + t['digest']
    assert a['transfer']['source_reference_type'] == 'tag'
    assert entry['import_policy'] == 'managed-canonical-v1' and entry['registered_at']
    copy_cmd = next(c for c in t['calls'] if c[1] == 'copy')
    assert copy_cmd[-2] == 'docker://' + t['repository'] + '@' + t['digest']
    # Registration leaves caller-owned declaration untouched.
    assert 'digest' not in t['declaration']['artifacts'][0]


def test_tag_moving_during_import_does_not_retarget_copy(importer):
    t = importer
    original = t['digest']
    t['fault'] = 'move-after-resolution'
    result = t['catalog'].register(t['declaration'])
    assert t['digest'] != original
    assert result['entry']['artifacts'][0]['content']['source_digest'] == original
    copy_cmd = next(c for c in t['calls'] if c[1] == 'copy')
    assert copy_cmd[-2].endswith('@' + original)
    tag_reads = [c for c in t['calls'] if c[1] == 'inspect' and '@' not in c[-1]]
    assert len(tag_reads) == 1


def test_same_tag_resolved_identity_is_idempotent_but_moved_tag_conflicts(importer):
    t = importer
    first = t['catalog'].register(t['declaration'])
    count = sum(c[1] == 'copy' for c in t['calls'])
    again = t['catalog'].register(t['declaration'])
    assert again['status'] == 'ALREADY_REGISTERED' and again['entry'] == first['entry']
    assert sum(c[1] == 'copy' for c in t['calls']) == count
    newer = dict(json.loads(t['raw']), annotations={'revision': 'next'})
    t['raw'] = json_bytes(newer)
    t['digest'] = 'sha256:' + hashlib.sha256(t['raw']).hexdigest()
    t['all_sources'][t['digest']] = t['raw']
    with pytest.raises(ValidationError, match='Immutable'):
        t['catalog'].register(t['declaration'])
    assert t['catalog'].read('base', 'r1') == first['entry']
    d = copy.deepcopy(t['declaration']); d['release'] = 'r2'
    assert t['catalog'].register(d)['status'] == 'REGISTERED'
    assert t['catalog'].read('base', 'r1') == first['entry']


@pytest.mark.parametrize('fault', ['copy-failed', 'missing-layer', 'corrupt-layer', 'bad-index'])
def test_import_fault_is_not_registered_and_other_release_survives(importer, fault):
    t = importer
    previous = t['catalog'].register(t['declaration'])['entry']
    t['fault'] = fault
    d = copy.deepcopy(t['declaration']); d['release'] = 'broken'
    with pytest.raises((ValidationError, OSError, ValueError)):
        t['catalog'].register(d)
    assert [r['release'] for r in t['catalog'].list()] == ['r1']
    assert t['catalog'].read('base', 'r1') == previous
    assert not list((t['catalog'].root / 'store/tmp').iterdir())


@pytest.mark.parametrize('declared_matches', [True, False])
def test_optional_tag_expected_digest_is_checked(importer, declared_matches):
    t = importer
    t['declaration']['artifacts'][0]['digest'] = t['digest'] if declared_matches else 'sha256:' + 'ab' * 32
    if declared_matches:
        assert t['catalog'].register(t['declaration'])['status'] == 'REGISTERED'
    else:
        with pytest.raises(ValidationError, match='requested digest'):
            t['catalog'].register(t['declaration'])
        assert not any(c[1] == 'copy' for c in t['calls'])
        assert t['catalog'].list() == []


def test_digest_input_keeps_digest_validation(importer):
    t = importer
    t['declaration']['artifacts'][0]['uri'] = t['repository'] + '@' + t['digest']
    t['fault'] = 'wrong-source-bytes'
    with pytest.raises(ValidationError, match='requested digest'):
        t['catalog'].register(t['declaration'])
    assert not any(c[1] == 'copy' for c in t['calls'])
    assert t['catalog'].list() == []


def test_declared_platform_not_in_managed_image_refused(importer):
    t = importer
    # Stored fixture has amd64/arm64; remove the arm64 child before copy.
    src = oci_layout(t['stored']['root'].parent / 'wrong-platform', ('linux/amd64',))
    shutil.rmtree(t['stored']['root'])
    shutil.copytree(src['root'], t['stored']['root'])
    with pytest.raises(ValidationError, match='platform'):
        t['catalog'].register(t['declaration'])
    assert t['catalog'].list() == []


def test_validate_and_runtime_locator_do_not_need_registry_or_source_proof(importer, monkeypatch):
    t = importer
    entry = t['catalog'].register(t['declaration'])['entry']
    image = t['catalog'].image(entry['artifacts'][0])
    shutil.rmtree(t['stored']['root'])
    # Source proof is provenance, not part of the executable managed graph.
    for proof in entry['artifacts'][0]['content']['source_proof_blobs']:
        (t['catalog'].root / proof['relative_path']).unlink()
    monkeypatch.setattr(subprocess, 'run', lambda *a, **k: pytest.fail('must remain offline'))
    monkeypatch.setattr(shutil, 'which', lambda *a: pytest.fail('no tool required to validate'))
    report = t['catalog'].validate('base', 'r1')
    assert report['status'] == 'VERIFIED_STORED' and report['source_accessed'] is False
    assert report['runtime_validation'] == 'NOT_PERFORMED'
    assert verify_managed(image)['root_digest'] == entry['artifacts'][0]['content']['identity']


def test_sif_first_and_failed_oci_cannot_publish_partial_release(importer, tmp_path):
    t = importer
    sif = tmp_path / 'test.sif'; sif.write_bytes(b'SIF copy unit fixture; not executed')
    d = copy.deepcopy(t['declaration'])
    d['artifacts'].insert(0, sif_declaration(sif)['artifacts'][0])
    t['fault'] = 'copy-failed'
    with pytest.raises(ValidationError):
        t['catalog'].register(d)
    assert t['catalog'].list() == []
    assert list((t['catalog'].root / 'store/objects/sif').iterdir())


@pytest.mark.parametrize('ref', [
    'nvcr.io/nvidia/pytorch:24.01-py3', 'docker.io/library/python:3.10',
    'registry.example:5000/team/img:latest', 'docker://registry.example/base:trial',
    '[::1]:5000/team/base:abc', 'reg.io/a__b:V1',
    'registry.example/team/base@sha256:' + 'ab' * 32,
])
def test_import_reference_accepts_explicit_tags_and_digest(ref):
    parsed = remote_reference(ref)
    assert parsed == ref.removeprefix('docker://')
    assert '/' in registry_repository(parsed)


@pytest.mark.parametrize('ref', [
    'python:3.10', 'python', 'reg.example/base', 'reg.example:5000/base',
    'reg.example/base:', 'reg.example/base:tag with spaces', 'reg.example/base:tag;id',
    'reg.example/Base:tag', 'https://reg.example/base:tag',
    'reg.example/../base:tag', 'reg.example/base:tag?token=secret',
    'user:password@reg.example/base:tag', 'reg.example/base:tag@sha256:' + 'aa' * 32,
    'reg.example/base@sha256:short', 'reg.example/base@sha512:' + 'a' * 128,
    'reg.example/base:tag\n', 'reg.example/base@sha256:' + 'AB' * 32,
])
def test_unsafe_or_implicit_registry_reference_rejected(ref):
    with pytest.raises(ValidationError):
        remote_reference(ref)


def test_tag_not_allowed_for_direct_runtime_even_after_catalog_change():
    from benchpark_container.reproducibility import pin_image
    with pytest.raises(ValidationError):
        pin_image({'uri': 'registry.example/base:latest'})


def test_inline_register_tag_cli_uses_same_import_policy(importer, monkeypatch, capsys, tmp_path):
    from benchpark_container.container_cli import main
    from benchpark_container.catalog.config import update_registration
    t = importer
    cfg = tmp_path / 'visible-catalogs.yaml'
    update_registration('personal', t['catalog'].root, config=cfg)
    import sys
    monkeypatch.setattr(sys, 'argv', ['container', '--catalog-config', str(cfg), 'register',
        '--catalog', 'personal', '--name', 'base', '--release', 'r1', '--kind', 'oci',
        '--source', t['tag'], '--platform', 'linux/arm64', '--accelerator', 'nvidia'])
    assert main() == 0
    record = json.loads(capsys.readouterr().out)
    assert record['status'] == 'REGISTERED'
    assert record['entry']['artifacts'][0]['content']['origin'] == t['tag']


def test_canonical_import_keeps_requirement_deltas_separate_from_base(importer, context, tmp_path, monkeypatch):
    from benchpark_container.catalog.config import update_registration
    from benchpark_container.resolver import resolve
    from benchpark_container import image_selection
    from benchpark_integration.api import plain
    from conftest import changed
    t = importer
    entry = t['catalog'].register(t['declaration'])['entry']
    cfg = tmp_path / 'catalogs.yaml'
    update_registration('personal', t['catalog'].root, config=cfg)
    monkeypatch.setenv('BPCE_CONFIG', str(cfg))
    monkeypatch.setattr(image_selection, 'source_provenance', lambda *a, **k: {'git_status': 'test-fixture'})
    monkeypatch.setattr('benchpark_container.resolver.source_provenance',
                        lambda *a, **k: {'git_status': 'test-fixture'})
    s = plain(context.system); s['platform'] = 'linux/arm64'
    s['runtimes'] = {'docker': {'executable': 'docker'}}; s['default_runtime'] = 'docker'
    q = plain(context.requirements); q['default_image'] = 'personal:base'; q['default_release'] = 'r1'
    q['setup_scripts'] = ['setup.sh']
    source = Path(context.source_root)
    (source / 'setup.sh').write_text('#!/bin/bash\nmkdir -p "$BPCE_PREFIX/bin"\n')
    (source / 'requirements.txt').write_text('additional-demo==1.0\n')
    ctx = changed(context, system=s, requirements=q)
    first = plain(resolve(ctx).payload)
    (source / 'requirements.txt').write_text('additional-demo==2.0\n')
    second = plain(resolve(ctx).payload)
    assert first['requirement_pins'] == {'additional-demo': ['1.0']}
    assert second['requirement_pins'] == {'additional-demo': ['2.0']}
    assert first['resources']['inputs/requirements.txt'] != second['resources']['inputs/requirements.txt']
    assert first['base'] == second['base']
    assert first['base']['managed']['stored_digest'] == entry['artifacts'][0]['content']['identity']
    assert first['setup_scripts'] == second['setup_scripts'] == ['inputs/setup.sh']
    assert first['protected_packages'] == second['protected_packages'] == q['protected_packages']
    assert first['image_selection']['entry_snapshot']['import_policy'] == 'managed-canonical-v1'
    assert t['catalog'].read('base', 'r1') == entry


def test_legacy_catalog_entry_remains_readable_without_rewriting_policy(tmp_path):
    from benchpark_container.util import identity
    root = tmp_path / 'cat'; init_catalog(root, 'old')
    cat = Catalog(root)
    source = tmp_path / 'original.sif'; source.write_bytes(b'legacy storage-only fixture')
    declaration = sif_declaration(source)
    entry = cat.register(declaration)['entry']
    # Recreate the dev3 entry's additive-field-free shape, with its valid checksum.
    entry.pop('import_policy'); entry.pop('registered_at'); entry.pop('entry_sha256')
    for artifact in entry['artifacts']:
        artifact['content'].pop('identity_basis'); artifact['content'].pop('import_policy')
    entry['entry_sha256'] = identity(entry)
    p = root / 'entries/torch-base/r1.json'; p.chmod(0o644); p.write_bytes(json_bytes(entry))
    before = p.read_bytes()
    assert cat.validate('torch-base', 'r1')['status'] == 'VERIFIED_STORED'
    assert cat.register(declaration)['status'] == 'ALREADY_REGISTERED'
    assert p.read_bytes() == before
