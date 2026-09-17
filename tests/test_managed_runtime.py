# SPDX-License-Identifier: Apache-2.0
"""Runtime ports are test doubles here; real filesystem integrity checks run."""
import copy
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import pytest
from benchpark_container.catalog.manager import init_catalog,Catalog
from benchpark_container.image_store import verify_managed, verify_layout
from benchpark_container.util import ValidationError,json_bytes
from benchpark_container.backends.docker import DockerBackend
from benchpark_container.backends.apptainer import ApptainerBackend
from benchpark_container.backends.singularity import SingularityBackend
from test_runtime_backends import settings
from catalog_fixtures import oci_layout


@pytest.fixture
def managed(tmp_path):
    src=oci_layout(tmp_path/'source');root=tmp_path/'catalog';init_catalog(root,'team');c=Catalog(root)
    d={'schema_version':1,'name':'env','release':'r1','artifacts':[{'kind':'oci','oci_layout':str(src['root']),
       'platform':'linux/amd64','accelerator':'none','tools':{'python':'python3','shell':'bash'}}]}
    a=c.register(d)['entry']['artifacts'][0];base=c.image(a);base['managed']['skopeo']='skopeo'
    shutil.rmtree(src['root'])
    return c,base


@pytest.mark.parametrize('fault',[None,'id','rootfs','platform','copy-failed','load-failed'])
def test_managed_docker_offline_load_verified_before_execution(managed,tmp_path,monkeypatch,fault):
    cat,base=managed;verified=verify_managed(base);calls=[]
    monkeypatch.setattr(shutil,'which',lambda name:'/tools/'+name)
    monkeypatch.setattr('benchpark_container.backends.oci.platform.machine',lambda:'x86_64')
    rt=DockerBackend(settings(tmp_path,'docker'))
    info={'Id':verified['config_digest'],'Os':'linux','Architecture':'amd64','Config':{'Env':[]},
          'RepoDigests':[], 'RootFS':{'Layers':verified['config']['rootfs']['diff_ids']}}
    if fault=='id':info['Id']='sha256:'+'cd'*32
    if fault=='rootfs':info['RootFS']['Layers']=[]
    if fault=='platform':info['Architecture']='arm64'
    def external(cmd,**kwargs):
        calls.append(cmd)
        if cmd==['/tools/skopeo','--version']:
            return subprocess.CompletedProcess(cmd,0,'skopeo test-double\n','')
        if cmd[:2]==['/tools/skopeo','copy']:
            assert cmd[2].startswith('oci:') and cmd[3].startswith('docker-archive:')
            verify_layout(cmd[2][4:])
            return subprocess.CompletedProcess(cmd,1 if fault=='copy-failed' else 0,b'',b'')
        raise AssertionError(cmd)
    monkeypatch.setattr(subprocess,'run',external)
    def run(cmd,timeout=None,check=True):
        calls.append(cmd)
        assert cmd[len(rt.cli())]=='load'
        if fault=='load-failed':raise ValidationError('load failed')
        return subprocess.CompletedProcess(cmd,0,'Loaded image ID\n','')
    monkeypatch.setattr(rt,'_run',run)
    monkeypatch.setattr(rt,'_object',lambda cmd,*a,**k:(calls.append(cmd),info)[1])
    if fault:
        with pytest.raises(ValidationError):rt.resolve_image(base,30)
    else:
        image=rt.resolve_image(base,30)
        assert image.reference==verified['config_digest']
        assert image.identity==base['managed']['stored_digest']
        assert image.details['repo_digests']==[] and image.details['managed'] is True
    assert all('docker://' not in str(c) and 'pull' not in c for c in calls)
    assert all('create' not in c and 'start' not in c for c in calls)
    assert not list(rt.cache.glob('bpce-oci-restore-*'))
    assert rt.events[0]['network_source_used'] is False
    rt.private_config.cleanup()


@pytest.mark.parametrize('cls',[ApptainerBackend,SingularityBackend])
def test_managed_oci_to_sif_uses_store_not_registry_and_detects_cache_change(managed,tmp_path,monkeypatch,cls):
    cat,base=managed;calls=[]
    monkeypatch.setattr(shutil,'which',lambda n:'/tools/'+n)
    rt=cls(settings(tmp_path,cls.name));monkeypatch.setattr(rt,'version',lambda:cls.name+' test-double')
    def build(cmd,**kw):
        calls.append(cmd);assert cmd[1]=='build' and cmd[-1].startswith('oci:')
        verify_layout(cmd[-1][4:]);Path(cmd[2]).write_bytes(b'derived SIF fixture, not executed')
        return subprocess.CompletedProcess(cmd,0)
    monkeypatch.setattr(subprocess,'run',build)
    image=rt.resolve_image(base,30)
    assert image.details['source_oci_digest']==base['managed']['stored_digest']
    assert rt.resolve_image(base,30).identity==image.identity and len(calls)==1
    Path(image.reference).write_bytes(b'changed')
    with pytest.raises(ValidationError,match='identity'):rt.resolve_image(base,30)


@pytest.mark.parametrize('fault',[None,'wrong-digest','changed-payload','copy-failed'])
def test_registry_schema2_normalization_preserves_execution_semantics_and_records_both_identities(tmp_path,monkeypatch,fault):
    src=oci_layout(tmp_path/'normalized')
    stored_manifest=json.loads((src['root']/'blobs/sha256'/src['manifests'][0]['digest'][7:]).read_text())
    stored_config=(src['root']/'blobs/sha256'/stored_manifest['config']['digest'][7:]).read_bytes()
    source_config=json.loads(stored_config)
    if fault=='changed-payload':
        source_config['config']['Env']=['SEMANTICALLY_DIFFERENT=1']
    source_config_raw=json_bytes(source_config)
    source_config_digest='sha256:'+hashlib.sha256(source_config_raw).hexdigest()

    # Model a Docker schema-2 source which is normalized by skopeo to OCI.
    # The source compressed-layer descriptor is deliberately different from the
    # stored OCI descriptor. dev4's approved policy makes the stored artifact
    # canonical, so source config differences are no longer registration errors.
    # Historical test name is retained; this is not an equivalence certificate.
    original=copy.deepcopy(stored_manifest)
    original['mediaType']='application/vnd.docker.distribution.manifest.v2+json'
    original['config']['mediaType']='application/vnd.docker.container.image.v1+json'
    original['config']['digest']=source_config_digest
    original['config']['size']=len(source_config_raw)
    original['layers'][0]['mediaType']='application/vnd.docker.image.rootfs.diff.tar.gzip'
    original['layers'][0]['digest']='sha256:'+'ef'*32
    original['layers'][0]['size']+=7
    raw=json_bytes(original);source_digest='sha256:'+hashlib.sha256(raw).hexdigest()
    uri='registry.example/team/base@'+source_digest
    if fault=='wrong-digest':uri='registry.example/team/base@sha256:'+'ab'*32
    calls=[]
    monkeypatch.setattr(shutil,'which',lambda _: '/tools/skopeo')
    def run(cmd,**kwargs):
        calls.append(cmd)
        if '--version' in cmd:return subprocess.CompletedProcess(cmd,0,b'skopeo test-double',b'')
        if 'inspect' in cmd:
            if '--config' in cmd:return subprocess.CompletedProcess(cmd,0,source_config_raw,b'')
            return subprocess.CompletedProcess(cmd,0,raw,b'')
        assert cmd[1:5]==['copy','--all','--format','oci']
        if fault=='copy-failed':return subprocess.CompletedProcess(cmd,1,b'',b'private detail not persisted')
        dest=cmd[-1][4:].rsplit(':',1)[0];shutil.copytree(src['root'],dest)
        return subprocess.CompletedProcess(cmd,0,b'',b'')
    monkeypatch.setattr(subprocess,'run',run)
    root=tmp_path/'catalog';init_catalog(root,'team');c=Catalog(root)
    d={'schema_version':1,'name':'env','release':'r1','artifacts':[{'kind':'oci','uri':uri,
       'platform':'linux/amd64','accelerator':'none','tools':{'python':'python3','shell':'bash'}}]}
    if fault in ('wrong-digest', 'copy-failed'):
        with pytest.raises(ValidationError):c.register(d)
        assert c.list()==[]
    else:
        result=c.register(d)['entry']['artifacts'][0]['content']
        assert result['source_digest']==source_digest
        assert result['stored_digest']==src['digest'] and result['stored_digest']!=source_digest
        proofs=[root/x['relative_path'] for x in result['source_proof_blobs']]
        assert any(x.read_bytes()==raw for x in proofs)
        assert len(proofs) == 1  # Only raw source root manifest is retained.
        assert not any('--config' in cmd for cmd in calls)
        assert result['source_equivalence'] == 'NOT_ASSERTED'
        assert result['identity_basis'] == 'managed-artifact'
        assert 'managed-canonical' in result['transfer']['mode']
        assert c.validate('env','r1')['status']=='VERIFIED_STORED'
    assert not list((root/'store/tmp').iterdir())


def test_corrupt_store_prevents_any_runtime_or_network_operation(managed,tmp_path,monkeypatch):
    cat,base=managed;v=verify_managed(base)
    blob=Path(v['layout'])/'blobs/sha256'/v['layers'][0]['digest'][7:]
    blob.chmod(0o644);blob.write_bytes(b'corrupt')
    monkeypatch.setattr(shutil,'which',lambda _: '/tools/docker')
    rt=DockerBackend(settings(tmp_path,'docker'))
    monkeypatch.setattr(subprocess,'run',lambda *a,**k:pytest.fail('Store corruption must be caught before external calls'))
    with pytest.raises(ValidationError):rt.resolve_image(base,30)
    rt.private_config.cleanup()


def test_declared_registry_digest_must_match_source(tmp_path,monkeypatch):
    init_catalog(tmp_path/'cat','team');cat=Catalog(tmp_path/'cat')
    d={'schema_version':1,'name':'bad','release':'r1','artifacts':[{'kind':'oci',
       'uri':'registry.example/repo@sha256:'+'aa'*32,'digest':'sha256:'+'bb'*32,
       'platform':'linux/amd64','accelerator':'none','tools':{'python':'python3','shell':'bash'}}]}
    monkeypatch.setattr(subprocess,'run',lambda *a,**k:pytest.fail('Mismatch must fail before external calls'))
    with pytest.raises(ValidationError,match='digest differs'):cat.register(d)
    assert cat.list()==[]


def test_selected_multiplatform_layout_freezes_only_selected_manifest(tmp_path):
    from benchpark_container.image_store import select_manifest,selected_layout
    src=oci_layout(tmp_path/'source',platforms=('linux/amd64','linux/arm64'))
    data=verify_layout(src['root']);selected=select_manifest(data,'linux/arm64')
    selected['layout']=str(src['root'])
    view=selected_layout(selected,tmp_path/'view')
    check=verify_layout(view,selected['manifest_digest'])
    assert len(check['manifests'])==1
    assert check['manifests'][0]['platform']=='linux/arm64'
