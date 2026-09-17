# SPDX-License-Identifier: Apache-2.0
import copy
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
import pytest
from benchpark_container.catalog.manager import Catalog, init_catalog, lookup, validate_manifest
from benchpark_container.catalog.config import read_config, update_registration, load_yaml
from benchpark_container.catalog.store import FilesystemStore, Skopeo, store_class, ArtifactStore
from benchpark_container.util import ValidationError, sha256
from benchpark_container.image_store import verify_layout, select_manifest, verify_managed
from catalog_fixtures import oci_layout, sif_declaration


@pytest.fixture
def cat(tmp_path):
    root=tmp_path/'catalog';init_catalog(root,'personal')
    return Catalog(root)


@pytest.fixture
def source(tmp_path):
    p=tmp_path/'image.sif';p.write_bytes(b'non-executable SIF store fixture\n')
    return p


def test_register_copies_bytes_and_survives_source_deletion(cat,source):
    d=sif_declaration(source);r=cat.register(d)
    assert r['status']=='REGISTERED'
    image=cat.image(r['entry']['artifacts'][0])
    assert Path(image['source']).stat().st_ino != source.stat().st_ino
    source.unlink()
    assert cat.validate('torch-base','r1')['status']=='VERIFIED_STORED'
    assert verify_managed(image)['source']==image['source']
    assert cat.list()[0]['storage_present'] is True
    assert 'store/' in (cat.root/'.gitignore').read_text()


def test_same_release_idempotence_and_rewritten_source_refused(cat,source):
    d=sif_declaration(source);first=cat.register(d)
    assert cat.register(d)['status']=='ALREADY_REGISTERED'
    source.write_bytes(b'changed')
    with pytest.raises(ValidationError,match='Immutable'):cat.register(d)
    assert cat.read('torch-base','r1')==first['entry']
    assert cat.register(sif_declaration(source,release='r2'))['status']=='REGISTERED'


def test_dedup_distinct_names_use_one_sif_object(cat,source):
    cat.register(sif_declaration(source,name='a'));cat.register(sif_declaration(source,name='b'))
    a=cat.read('a','r1')['artifacts'][0]['content'];b=cat.read('b','r1')['artifacts'][0]['content']
    assert a['relative_path']==b['relative_path']
    assert len(list((cat.root/'store/objects/sif').iterdir()))==1


@pytest.mark.parametrize('fault',['missing','changed','symlink'])
def test_corrupt_managed_sif_is_never_accepted(cat,source,fault,tmp_path):
    cat.register(sif_declaration(source));a=cat.read('torch-base','r1')['artifacts'][0]
    p=Path(cat.image(a)['source']);p.parent.chmod(0o755);p.chmod(0o644)
    if fault=='missing':p.unlink()
    elif fault=='changed':p.write_bytes(b'changed')
    else:p.unlink();p.symlink_to(source)
    with pytest.raises((ValidationError,OSError)):cat.validate('torch-base','r1')
    if fault=='missing':assert cat.list()[0]['storage_present'] is False


def test_half_import_does_not_publish_catalog_entry(cat,source,tmp_path):
    d=sif_declaration(source)
    second=copy.deepcopy(d['artifacts'][0]);second['platform']='linux/arm64';second['uri']=(tmp_path/'missing.sif').as_uri()
    d['artifacts'].append(second)
    with pytest.raises(OSError):cat.register(d)
    assert cat.list()==[]
    assert not list((cat.root/'store/tmp').glob('*')) if (cat.root/'store/tmp').exists() else True


def test_parallel_registration_serializes_one_immutable_entry(cat,source):
    with ThreadPoolExecutor(max_workers=4) as pool:
        results=list(pool.map(lambda _:cat.register(sif_declaration(source)),range(4)))
    assert sorted(x['status'] for x in results)==['ALREADY_REGISTERED']*3+['REGISTERED']
    assert len(cat.list())==1


def test_mutated_catalog_entry_and_header_mismatch_refused(cat,source):
    cat.register(sif_declaration(source));p=cat.root/'entries/torch-base/r1.json';p.chmod(0o644)
    data=json.loads(p.read_text());data['artifacts'][0]['tools']['python']='different';p.write_text(json.dumps(data))
    with pytest.raises(ValidationError,match='checksum'):cat.read('torch-base','r1')


@pytest.mark.parametrize('fault',['latest','duplicate','tool','unsafe-name','platform','accelerator','secret','both-sources','unknown-field'])
def test_registration_schema_negative_cases(cat,source,fault):
    d=sif_declaration(source);a=d['artifacts'][0]
    if fault=='latest':d['release']='latest'
    elif fault=='duplicate':d['artifacts'].append(copy.deepcopy(a))
    elif fault=='tool':a['tools']['python']='python -m pip'
    elif fault=='unsafe-name':d['name']='../elsewhere'
    elif fault=='platform':a['platform']='arm64'
    elif fault=='accelerator':a['accelerator']='magic'
    elif fault=='secret':a['uri']='https://user:password@example.org/image'
    elif fault=='both-sources':a['oci_layout']='/elsewhere'
    else:d['unexpected']=True
    with pytest.raises(ValidationError):cat.register(d)
    assert cat.list()==[]


def test_bad_sif_hash_and_symlink_source_refused(cat,source,tmp_path):
    d=sif_declaration(source);d['artifacts'][0]['sha256']='01'*32
    with pytest.raises(ValidationError,match='SHA256'):cat.register(d)
    p=tmp_path/'link.sif';p.symlink_to(source)
    with pytest.raises(ValidationError):cat.register(sif_declaration(p))


def test_visible_registration_no_read_side_effects_and_no_shadow(cat,source,tmp_path,monkeypatch):
    config=tmp_path/'visible/settings.yaml';monkeypatch.setenv('BPCE_CONFIG',str(config))
    assert read_config()['catalogs']=={} and not config.parent.exists()
    cat.register(sif_declaration(source))
    update_registration('personal',cat.root)
    assert lookup('torch-base','r1')[0]=='personal'
    other=tmp_path/'shared';init_catalog(other,'shared');Catalog(other).register(sif_declaration(source))
    update_registration('shared',other)
    with pytest.raises(ValidationError,match='Ambiguous'):lookup('torch-base','r1')
    assert lookup('personal:torch-base','r1')[0]=='personal'
    with pytest.raises(ValidationError,match='elsewhere'):update_registration('personal',other)
    update_registration('personal',remove=True)
    assert cat.validate('torch-base','r1')['status']=='VERIFIED_STORED'
    assert lookup('torch-base','r1')[0]=='shared'


def test_duplicate_yaml_key_and_secret_config_rejected(tmp_path):
    p=tmp_path/'catalog.yaml';p.write_text('name: first\nname: second\n')
    with pytest.raises(ValidationError):load_yaml(p)
    p.write_text('password: abc\n')
    with pytest.raises(ValidationError):load_yaml(p)


def test_real_oci_layout_import_offline_preserves_all_platforms(cat,tmp_path):
    src=oci_layout(tmp_path/'oci-source',('linux/amd64','linux/arm64'))
    d={'schema_version':1,'name':'oci-env','release':'r1','artifacts':[{
        'kind':'oci','oci_layout':str(src['root']),'platform':'linux/arm64','accelerator':'nvidia',
        'tools':{'python':'python3','shell':'bash'}}]}
    record=cat.register(d)['entry']['artifacts'][0]
    assert record['content']['stored_digest']==src['digest']
    assert len(verify_layout(cat.root/record['content']['relative_path'])['manifests'])==2
    shutil.rmtree(src['root'])
    assert cat.validate('oci-env','r1')['status']=='VERIFIED_STORED'
    v=verify_managed(cat.image(record));assert v['platform']=='linux/arm64'


@pytest.mark.parametrize('fault',['missing-layer','changed-layer','size','root','symlink','layout-version','ambiguous-platform','config-missing'])
def test_oci_closure_corruption_rejected(tmp_path,fault):
    src=oci_layout(tmp_path/'layout')
    root=src['root'];layer=root/'blobs/sha256'/src['layer']['digest'][7:]
    if fault=='missing-layer':layer.unlink()
    elif fault=='changed-layer':layer.write_bytes(b'changed')
    elif fault=='symlink':layer.unlink();layer.symlink_to(root/'index.json')
    elif fault=='layout-version':(root/'oci-layout').write_text('{"imageLayoutVersion":"9"}')
    elif fault=='config-missing':
        manifest=json.loads((root/'blobs/sha256'/src['manifests'][0]['digest'][7:]).read_text())
        (root/'blobs/sha256'/manifest['config']['digest'][7:]).unlink()
    elif fault=='size':
        index=json.loads((root/'index.json').read_text());index['manifests'][0]['size']+=1;(root/'index.json').write_text(json.dumps(index))
    if fault=='ambiguous-platform':
        v=verify_layout(root);v['manifests']*=2
        with pytest.raises(ValidationError):select_manifest(v,'linux/amd64')
    else:
        with pytest.raises((ValidationError,OSError)):
            verify_layout(root,'sha256:'+'ab'*32 if fault=='root' else None)


def test_store_extension_registry_and_unknown_kind(cat):
    class TestStore(ArtifactStore):kind='test-only'
    from benchpark_container.catalog import store
    try:
        store.STORES['test-only']=TestStore
        assert store_class('test-only') is TestStore
        with pytest.raises(ValidationError):store_class('absent')
    finally:store.STORES.pop('test-only',None)


def test_skopeo_failure_does_not_create_entry(cat,tmp_path,monkeypatch):
    d={'schema_version':1,'name':'env','release':'r1','artifacts':[{
        'kind':'oci','uri':'registry.example/base@sha256:'+'ab'*32,'platform':'linux/amd64',
        'accelerator':'none','tools':{'python':'python3','shell':'bash'}}]}
    monkeypatch.setattr(shutil,'which',lambda _:None)
    with pytest.raises(ValidationError,match='skopeo'):cat.register(d)
    assert cat.list()==[] and not list((cat.root/'store/tmp').iterdir())


def test_store_parent_symlink_rejected_before_outside_directory_creation(tmp_path):
    from benchpark_container.catalog.manager import init_catalog,Catalog
    from catalog_fixtures import sif_declaration
    root=tmp_path/'cat';init_catalog(root,'team');c=Catalog(root)
    outside=tmp_path/'outside';outside.mkdir()
    (root/'store/objects').mkdir();(root/'store/objects/sif').symlink_to(outside,target_is_directory=True)
    source=tmp_path/'source.sif';source.write_bytes(b'sif bytes')
    with pytest.raises(ValidationError,match='symlink'):c.register(sif_declaration(source))
    assert not list(outside.iterdir()) and not c.list()


def test_readonly_oci_source_can_be_registered_twice_without_cleanup_failure(tmp_path):
    from catalog_fixtures import oci_layout
    root=tmp_path/'cat';init_catalog(root,'team');c=Catalog(root)
    src=oci_layout(tmp_path/'source')
    for p in src['root'].rglob('*'):p.chmod(0o555 if p.is_dir() else 0o444)
    src['root'].chmod(0o555)
    d={'schema_version':1,'name':'env','release':'r1','artifacts':[{'kind':'oci','oci_layout':str(src['root']),
       'platform':'linux/amd64','accelerator':'none','tools':{'python':'python3','shell':'bash'}}]}
    c.register(d);d['release']='r2';c.register(d)
    assert len(c.list())==2 and not list((root/'store/tmp').iterdir())
