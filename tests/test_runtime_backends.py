# SPDX-License-Identifier: Apache-2.0
"""Three-runtime contract/CLI/lifecycle tests. ALL daemon responses are doubles.
These tests do not establish that an actual container image boots or a GPU works.
"""
from contextlib import contextmanager
import copy
import csv
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from benchpark_container.backends import ExecutionRequest, ImageRef, Mount, RuntimeBackend
from benchpark_container.backends.registry import BACKENDS, backend_class, create_backend
from benchpark_container.backends.apptainer import ApptainerBackend
from benchpark_container.backends.singularity import SingularityBackend
from benchpark_container.backends.docker import DockerBackend
from benchpark_container.backends.oci import LABEL, canonical_reference
from benchpark_container.contracts import runtime_settings
from benchpark_container.util import ValidationError, sha256
from benchpark_container.preparation import _container_run, _capture, EnvironmentBuildError

DIGEST = 'a1' * 32
IMAGE_ID = 'sha256:' + 'b2' * 32
CONTAINER = 'c3' * 32
REFERENCE = 'registry.example/team/base@sha256:' + DIGEST


def settings(tmp_path, name='apptainer', gpu='none', **options):
    result = {'runtime': name, 'executable': name, 'worker_python': sys.executable,
              'image_cache': str(tmp_path / 'image-cache'), 'gpu': gpu}
    if options:
        result['backend_options'] = options
    return result


def base():
    return {'kind': 'oci', 'uri': REFERENCE, 'platform': 'linux/amd64'}


def request(tmp_path, accelerator='none', environment=None, command=('python3', '-c', 'print(1)')):
    return ExecutionRequest(ImageRef('oci', IMAGE_ID, REFERENCE),
        (Mount(str(tmp_path / 'scratch'), '/bpce', False), Mount(str(tmp_path / 'inputs'), '/bpce/inputs', True)),
        environment or {'TEST': 'two words;$(must-not-run)'}, '/bpce/work', command, accelerator)


class DaemonDouble:
    def __init__(self, runtime):
        self.runtime = runtime
        self.calls = []
        self.containers = {}
        self.image_info = {'Id': IMAGE_ID, 'RepoDigests': [REFERENCE], 'Os': 'linux',
                           'Architecture': 'amd64', 'Config': {'Env': [], 'Volumes': None}}
        self.image_missing = False
        self.inspect_error = None
        self.create_timeout = False
        self.created_count = 0
        self.create_image = None
        self.rm_error = False
        self.daemon = {'OSType': 'linux', 'Architecture': 'x86_64', 'ServerVersion': 'test-double', 'SecurityOptions': []}

    def run(self, command, **kwargs):
        self.calls.append(list(command))
        args = command[len(self.runtime.cli()):]
        code, stdout, stderr = 0, '', ''
        if args[:1] == ['info']:
            stdout = json.dumps(self.daemon)
        elif args[:2] == ['image', 'inspect']:
            if self.inspect_error:
                code, stderr = 1, self.inspect_error
            elif self.image_missing:
                code, stderr = 1, 'Error response from daemon: No such image: pinned'
            else:
                stdout = json.dumps([self.image_info])
        elif args[:1] == ['pull']:
            self.image_missing = False
            stdout = 'Pulled test-double image'
        elif args[:1] == ['create']:
            get = lambda flag: args[args.index(flag) + 1]
            name = get('--name')
            ep = args.index('--entrypoint')
            mounts = []
            env = list(self.image_info['Config'].get('Env', []))
            for i, value in enumerate(args):
                if value == '--mount':
                    fields = dict(x.split('=', 1) if '=' in x else (x, True) for x in args[i+1].split(','))
                    mounts.append({'Type': 'bind', 'Source': fields['src'], 'Destination': fields['dst'], 'RW': 'readonly' not in fields})
                if value == '--env':
                    env.append(args[i+1])
            container_id = '%064x' % (int(CONTAINER,16) + self.created_count)
            self.created_count += 1
            data = {'Id': container_id, 'Name': '/' + name, 'Image': self.create_image or args[ep+2],
                    'Config': {'Labels': {LABEL: get('--label').split('=', 1)[1]},
                               'Entrypoint': [args[ep+1]], 'Cmd': args[ep+3:], 'Env': env},
                    'Mounts': mounts, 'State': {'Status': 'created', 'Running': False, 'ExitCode': 0}}
            self.containers[name] = data
            if self.create_timeout:
                raise subprocess.TimeoutExpired(command, kwargs.get('timeout', 30))
            stdout = container_id + '\n'
        elif args[:2] == ['container', 'inspect']:
            target = args[-1]
            found = next((data for name, data in self.containers.items() if target in (name, data['Id'])), None)
            if found is None:
                code, stderr = 1, 'Error: No such container: ' + target
            else:
                stdout = json.dumps([found])
        elif args[:1] == ['rm']:
            if self.rm_error:
                code, stderr = 1, 'daemon refused cleanup'
            else:
                target = args[-1]
                self.containers = {k:v for k,v in self.containers.items() if v['Id'] != target}
                stdout = target
        else:
            raise AssertionError('Unexpected daemon-double command: ' + repr(args))
        return subprocess.CompletedProcess(command, code, stdout, stderr)


@pytest.fixture
def docker(tmp_path, monkeypatch):
    monkeypatch.setattr(shutil, 'which', lambda _: '/declared/docker')
    monkeypatch.setattr('benchpark_container.backends.oci.platform.machine', lambda: 'x86_64')
    rt = DockerBackend(settings(tmp_path, 'docker'))
    daemon = DaemonDouble(rt)
    monkeypatch.setattr(subprocess, 'run', daemon.run)
    monkeypatch.setattr(rt, 'version', lambda: 'Docker version test-double')
    yield rt, daemon
    try:
        rt.close()
    except ValidationError:
        pass


@pytest.mark.parametrize('name,expected', [('apptainer', ApptainerBackend), ('singularity', SingularityBackend), ('docker', DockerBackend)])
def test_system_accepts_three_runtimes_without_loading_an_executable(tmp_path, name, expected, monkeypatch):
    monkeypatch.setattr(shutil, 'which', lambda _: pytest.fail('setup must not probe compute-node executables'))
    assert runtime_settings(settings(tmp_path, name))['runtime'] == name
    assert backend_class(name) is expected


@pytest.mark.parametrize('name', ['podman', 'missing', '', None, ['docker']])
def test_unknown_backend_is_explicit_failure(tmp_path, name):
    with pytest.raises(ValidationError, match='Unknown container runtime'):
        runtime_settings(settings(tmp_path, name))


def test_incompatible_backend_api_and_duplicate_identity_rejected(tmp_path, monkeypatch):
    class Bad(RuntimeBackend):
        name = 'bad'
        api_version = 999
    monkeypatch.setitem(BACKENDS, 'bad', Bad)
    with pytest.raises(ValidationError, match='Incompatible'):
        runtime_settings(settings(tmp_path, 'bad'))
    monkeypatch.setitem(BACKENDS, 'bad', ApptainerBackend)
    with pytest.raises(ValidationError, match='Incompatible'):
        runtime_settings(settings(tmp_path, 'bad'))


@pytest.mark.parametrize('name', ['apptainer', 'singularity', 'docker'])
def test_missing_declared_runtime_never_falls_back(tmp_path, name, monkeypatch):
    monkeypatch.setattr(shutil, 'which', lambda _: None)
    with pytest.raises(ValidationError, match='not found'):
        create_backend(settings(tmp_path, name))


@pytest.mark.parametrize('options', [
    {'random_flag': '--privileged'}, {'endpoint':'tcp://remote:2375'}, {'endpoint':'ssh://remote'},
    {'endpoint':'unix:///../socket'}, {'group_add':['video']}, {'gpu_devices':['0']},
    {'control_timeout_seconds':0}, {'control_timeout_seconds':True}, {'shm_size':'-1'},
    {'ipc':'host','shm_size':'1g'}, {'network':'unknown'}, {'seccomp':'arbitrary'},
    {'config_dir':'relative'}])
def test_invalid_docker_options_are_rejected(tmp_path, options):
    with pytest.raises(ValidationError):
        runtime_settings(settings(tmp_path, 'docker', **options))


@pytest.mark.parametrize('name', ['apptainer','singularity'])
def test_sif_runtime_rejects_undeclared_backend_options(tmp_path, name):
    with pytest.raises(ValidationError):
        runtime_settings(settings(tmp_path,name, extra='unsafe'))


@pytest.mark.parametrize('name,prefix', [('apptainer','APPTAINER_'), ('singularity','SINGULARITY_')])
@pytest.mark.parametrize('gpu,flag', [('none',None),('nvidia','--nv'),('amd','--rocm')])
def test_sif_cli_identity_environment_and_devices(tmp_path, monkeypatch, name, prefix, gpu, flag):
    monkeypatch.setattr(shutil,'which',lambda _: '/declared/' + name)
    monkeypatch.setenv('APPTAINER_BIND','/host:/evil')
    monkeypatch.setenv('SINGULARITYENV_X','injected')
    monkeypatch.setenv('CUDA_VISIBLE_DEVICES','0,2')
    rt = create_backend(settings(tmp_path,name,gpu))
    scratch, inputs = tmp_path/'scratch space',tmp_path/'inputs space'
    scratch.mkdir();inputs.mkdir()
    command = rt.argv('fixed.sif',scratch,inputs,[],['python3','-c','print(1)'],{'X':'$(literal),two'})
    assert command[0] == '/declared/' + name
    assert ('--nv' in command) is (gpu == 'nvidia')
    assert ('--rocm' in command) is (gpu == 'amd')
    assert '--cleanenv' in command and '--containall' in command and '--no-eval' in command
    assert str(inputs)+':/bpce/inputs:ro' in command
    values = [next(csv.reader([command[i+1]]))[0] for i,arg in enumerate(command) if arg=='--env']
    assert 'CUDA_VISIBLE_DEVICES=0,2' in values and 'X=$(literal),two' in values
    env = rt.host_env()
    assert prefix+'CACHEDIR' in env
    assert 'APPTAINER_BIND' not in env and 'SINGULARITYENV_X' not in env
    assert (('SINGULARITY_CACHEDIR' in env) == (name=='singularity'))


@pytest.mark.parametrize('name', ['apptainer','singularity'])
def test_sif_hash_is_verified_and_observed_with_legacy_keys(tmp_path, monkeypatch, name):
    monkeypatch.setattr(shutil,'which',lambda _: '/declared/' + name)
    rt=create_backend(settings(tmp_path,name))
    path=tmp_path/'image.sif';path.write_bytes(b'not-a-real-sif-test-fixture')
    pinned={'kind':'sif','source':str(path),'sif_sha256':sha256(path)}
    image=rt.resolve_image(pinned,10)
    seen=rt.observe_image(image)
    assert seen['kind']=='sif' and seen['sif_path']==str(path) and seen['sif_sha256']==sha256(path)
    assert seen['identity']=='sha256:'+sha256(path)
    assert rt.image(pinned,10)==path
    path.write_bytes(b'changed')
    with pytest.raises(ValidationError,match='changed'):
        rt.resolve_image(pinned,10)


@pytest.mark.parametrize('name', ['apptainer','singularity'])
def test_runtime_version_mismatch_is_not_mislabelled(tmp_path, monkeypatch, name):
    monkeypatch.setattr(shutil,'which',lambda _: '/declared/runtime')
    monkeypatch.setattr(subprocess,'check_output',lambda *a,**k:'other runtime 1.0')
    rt=create_backend(settings(tmp_path,name))
    with pytest.raises(ValidationError,match='does not identify'):
        rt.observe_runtime()


@pytest.mark.parametrize('name', ['apptainer','singularity'])
def test_sif_pull_cache_verifies_metadata_and_does_not_repull(tmp_path, monkeypatch, name):
    monkeypatch.setattr(shutil,'which',lambda _: '/declared/' + name)
    calls=[]
    def pull(argv,**kwargs):
        calls.append(argv);Path(argv[2]).write_bytes(b'converted-test-double-sif')
        return subprocess.CompletedProcess(argv,0)
    monkeypatch.setattr(subprocess,'run',pull)
    rt=create_backend(settings(tmp_path,name))
    first=rt.resolve_image(base(),10);second=rt.resolve_image(base(),10)
    assert first==second and len(calls)==1 and calls[0][-1]=='docker://'+REFERENCE
    Path(first.reference).write_bytes(b'tampered')
    with pytest.raises(ValidationError,match='Cached SIF has changed'):
        rt.resolve_image(base(),10)


@pytest.mark.parametrize('name', ['apptainer','singularity','docker'])
def test_mutable_tag_is_rejected_at_runtime_boundary(tmp_path, monkeypatch, name):
    monkeypatch.setattr(shutil,'which',lambda _: '/declared/' + name)
    rt=create_backend(settings(tmp_path,name))
    with pytest.raises(ValidationError,match='repository@sha256'):
        rt.resolve_image({'kind':'oci','uri':'repo:latest'},10)
    rt.close()


def test_docker_pinned_identity_and_missing_image_pull(docker):
    rt,daemon=docker
    daemon.image_missing=True
    image=rt.resolve_image(base(),20)
    assert image.reference==IMAGE_ID and image.identity==REFERENCE
    assert image.details['platform']=='linux/amd64'
    assert sum('pull' in c for c in daemon.calls)==1
    rt.resolve_image(base(),20)
    assert sum('pull' in c for c in daemon.calls)==1


@pytest.mark.parametrize('changes', [
    {'Id':'mutable'}, {'RepoDigests':['foreign/repository@sha256:'+DIGEST]},
    {'RepoDigests':[]}, {'Architecture':'arm64'}, {'Os':'windows'},
    {'Config':{'Volumes':{'/usr':{}}}}])
def test_docker_rejects_wrong_identity_platform_or_hidden_volumes(docker, changes):
    rt,daemon=docker;daemon.image_info.update(changes)
    with pytest.raises(ValidationError):
        rt.resolve_image(base(),20)


def test_daemon_permission_failure_is_not_treated_as_cache_miss(docker):
    rt,daemon=docker;daemon.inspect_error='permission denied connecting to Docker daemon'
    with pytest.raises(ValidationError,match='permission denied'):
        rt.resolve_image(base(),20)
    assert not any('pull' in c for c in daemon.calls)


def test_docker_sif_rejected_without_daemon_access(docker):
    rt,daemon=docker
    with pytest.raises(ValidationError,match='SIF'):
        rt.resolve_image({'kind':'sif','source':'/base.sif'},20)
    assert not daemon.calls


@pytest.mark.parametrize('ref,expected', [('python','docker.io/library/python'),('library/python','docker.io/library/python'),
    ('docker.io/python','docker.io/library/python'),('index.docker.io/library/python','docker.io/library/python'),
    ('localhost:5000/team/base','localhost:5000/team/base')])
def test_dockerhub_alias_normalization(ref,expected):
    assert canonical_reference(ref+'@sha256:'+DIGEST)==expected+'@sha256:'+DIGEST


def test_docker_create_verify_start_cleanup_and_command_semantics(tmp_path,docker):
    rt,daemon=docker
    rt.attach_attempt(tmp_path)
    req=request(tmp_path)
    command=rt.build_command(req)
    event=rt.events[-1]
    assert not daemon.calls # building command has no daemon side effects
    assert command[-3:-1]==['start','--attach']
    create=event['creation_command']
    assert '--read-only' in create and '--pull' in create and 'never' in create
    assert create[create.index('--entrypoint')+1:]==['python3',IMAGE_ID,'-c','print(1)']
    assert str(os.getuid())+':'+str(os.getgid()) in create
    assert 'type=bind,src='+str(tmp_path/'inputs')+',dst=/bpce/inputs,readonly' in create
    with rt.command_scope(command):
        assert event['image_verified_before_start'] is True
        assert event['container_id']==CONTAINER
        assert daemon.containers
    assert event['cleanup']=='removed' and not daemon.containers
    assert event['environment']['NVIDIA_VISIBLE_DEVICES']=='void'
    rt.close()
    assert json.loads((tmp_path/'runtime-invocations.json').read_text())[0]['cleanup']=='removed'
    assert len(list(tmp_path.glob('runtime-event-*.json'))) >= 3
    with pytest.raises(ValidationError,match='reused'):
        with rt.command_scope(command):
            pytest.fail('reused invocation')


@pytest.mark.parametrize('error', [subprocess.TimeoutExpired(['fixture'],0.1),KeyboardInterrupt(),RuntimeError('failure')])
def test_docker_cleanup_runs_for_timeout_interrupt_and_error(tmp_path,docker,error):
    rt,daemon=docker;command=rt.build_command(request(tmp_path))
    with pytest.raises(type(error)):
        with rt.command_scope(command):
            raise error
    assert rt.events[-1]['cleanup']=='removed' and not daemon.containers


def test_docker_image_mismatch_blocks_start_but_cleans_owned_container(tmp_path,docker):
    rt,daemon=docker;daemon.create_image='sha256:'+'f0'*32
    command=rt.build_command(request(tmp_path))
    with pytest.raises(ValidationError,match='image ID differs'):
        with rt.command_scope(command):
            pytest.fail('unverified image must never start')
    assert not daemon.containers and rt.events[-1]['cleanup']=='removed'


def test_docker_create_timeout_finds_owned_container_by_name(tmp_path,docker):
    rt,daemon=docker;daemon.create_timeout=True
    command=rt.build_command(request(tmp_path))
    with pytest.raises(subprocess.TimeoutExpired):
        with rt.command_scope(command):
            pytest.fail('create timeout must not start')
    assert rt.events[-1]['container_id']==CONTAINER and not daemon.containers


def test_docker_foreign_label_never_deleted(tmp_path,docker):
    rt,daemon=docker;command=rt.build_command(request(tmp_path))
    with pytest.raises(ValidationError,match='ownership label'):
        with rt.command_scope(command):
            next(iter(daemon.containers.values()))['Config']['Labels'][LABEL]='foreign'
    assert daemon.containers and not any('rm' in c for c in daemon.calls)
    assert rt.events[-1]['cleanup']=='failed'


def test_docker_cleanup_error_not_success_and_original_error_preserved(tmp_path,docker):
    rt,daemon=docker;daemon.rm_error=True
    command=rt.build_command(request(tmp_path))
    with pytest.raises(ValidationError,match='refused cleanup'):
        with rt.command_scope(command):
            pass
    assert rt.events[-1]['cleanup']=='failed'
    command=rt.build_command(request(tmp_path))
    with pytest.raises(subprocess.TimeoutExpired):
        with rt.command_scope(command):
            raise subprocess.TimeoutExpired(command,0.1)
    assert rt.events[-1]['cleanup']=='failed'


def test_docker_client_environment_cannot_redirect_or_inject(tmp_path,monkeypatch):
    monkeypatch.setattr(shutil,'which',lambda _: '/declared/docker')
    monkeypatch.setenv('DOCKER_HOST','tcp://remote:2375')
    monkeypatch.setenv('DOCKER_CONTEXT','unsafe')
    monkeypatch.setenv('DOCKER_DEFAULT_PLATFORM','linux/arm64')
    rt=DockerBackend(settings(tmp_path,'docker'))
    assert rt.cli()[1:3]==['--host','unix:///var/run/docker.sock']
    assert not any(k.startswith('DOCKER_') for k in rt.host_env())
    directory=rt.config_dir
    assert json.loads((directory/'config.json').read_text())=={}
    rt.close();assert not directory.exists()


def test_docker_client_proxy_injection_rejected(tmp_path,monkeypatch):
    monkeypatch.setattr(shutil,'which',lambda _: '/declared/docker')
    config=tmp_path/'docker-config';config.mkdir()
    (config/'config.json').write_text(json.dumps({'proxies':{'default':{'httpProxy':'secret'}}}))
    with pytest.raises(ValidationError,match='inject'):
        DockerBackend(settings(tmp_path,'docker',config_dir=str(config)))


@pytest.mark.parametrize('security', [['name=rootless'],['name=userns']])
def test_unvalidated_uid_mapping_fails_explicitly(docker,security):
    rt,daemon=docker;daemon.daemon['SecurityOptions']=security
    with pytest.raises(ValidationError,match='Rootless/userns'):
        rt.observe_runtime()


@pytest.mark.parametrize('visible,inside', [('2','0'),('2,5','0,1'),('GPU-abc','GPU-abc')])
def test_docker_cuda_selection_and_ordinal_mapping(tmp_path,monkeypatch,visible,inside):
    monkeypatch.setattr(shutil,'which',lambda _: '/declared/docker')
    monkeypatch.delenv('SLURM_JOB_ID',raising=False)
    monkeypatch.setenv('CUDA_VISIBLE_DEVICES',visible)
    rt=DockerBackend(settings(tmp_path,'docker','nvidia'))
    rt.build_command(request(tmp_path,'nvidia',{'CUDA_VISIBLE_DEVICES':visible}))
    event=rt.events[-1];create=event['creation_command']
    gpu=next(csv.reader([create[create.index('--gpus')+1]]))[0]
    assert gpu=='device='+visible
    assert event['environment']['CUDA_VISIBLE_DEVICES']==inside
    rt.close()


@pytest.mark.parametrize('visible', ['', '-1','none'])
def test_cuda_empty_allocation_does_not_expand_to_all(tmp_path,monkeypatch,visible):
    monkeypatch.setattr(shutil,'which',lambda _: '/declared/docker')
    monkeypatch.setenv('CUDA_VISIBLE_DEVICES',visible)
    rt=DockerBackend(settings(tmp_path,'docker','nvidia'))
    with pytest.raises(ValidationError,match='no GPU'):
        rt.build_command(request(tmp_path,'nvidia'))
    rt.close()


def test_docker_slurm_numeric_ordinals_are_not_assumed_host_ids(tmp_path,monkeypatch):
    monkeypatch.setattr(shutil,'which',lambda _: '/declared/docker')
    monkeypatch.setenv('SLURM_JOB_ID','123');monkeypatch.setenv('CUDA_VISIBLE_DEVICES','0')
    rt=DockerBackend(settings(tmp_path,'docker','nvidia'))
    with pytest.raises(ValidationError,match='UUID'):
        rt.build_command(request(tmp_path,'nvidia'))
    rt.close()


def test_rocm_devices_groups_security_are_system_declared(tmp_path,monkeypatch):
    monkeypatch.setattr(shutil,'which',lambda _: '/declared/docker')
    rt=DockerBackend(settings(tmp_path,'docker','amd',gpu_devices=['/dev/dri/renderD128'],group_add=[44,109],seccomp='unconfined'))
    rt.build_command(request(tmp_path,'amd'))
    create=rt.events[-1]['creation_command']
    assert '/dev/kfd' in create and '/dev/dri/renderD128' in create and '/dev/dri' not in create
    assert '44' in create and '109' in create and 'seccomp=unconfined' in create
    assert '--privileged' not in create
    rt.close()


def test_restricted_rocm_requires_explicit_render_nodes(tmp_path,monkeypatch):
    monkeypatch.setattr(shutil,'which',lambda _: '/declared/docker')
    monkeypatch.setenv('ROCR_VISIBLE_DEVICES','0')
    rt=DockerBackend(settings(tmp_path,'docker','amd'))
    with pytest.raises(ValidationError,match='render-node'):
        rt.build_command(request(tmp_path,'amd'))
    rt.close()


@pytest.mark.parametrize('env', [{'BAD=KEY':'x'}, {'X':'bad\nvalue'}, {'1BAD':'x'}, {'OK':None}])
def test_environment_encoding_is_validated(tmp_path,env):
    with pytest.raises(ValidationError):
        request(tmp_path,environment=env)


@pytest.mark.parametrize('path', ['/tmp/a,b','/tmp/a:b','/tmp/a\n','relative','/tmp/../root'])
def test_mount_encoding_is_rejected_before_runtime(tmp_path,path):
    with pytest.raises(ValidationError):
        Mount(path,'/data',True)


def test_common_preparation_scope_preserves_probe_stdout_stderr_and_cleans(tmp_path):
    events=[]
    class ScopedProbe:
        def argv(self,*args,**kwargs):
            return [sys.executable,'-c','import sys; print("{\\"ok\\":true}"); print("runtime warning",file=sys.stderr)']
        def host_env(self):return os.environ.copy()
        @contextmanager
        def command_scope(self,command):
            events.append('entered')
            try:yield
            finally:events.append('cleaned')
    proc,_=_container_run(ScopedProbe(),None,tmp_path,tmp_path,[],['ignored'],10)
    assert json.loads(proc.stdout)=={'ok':True} and 'runtime warning' in proc.stderr
    assert events==['entered','cleaned']


def test_common_preparation_timeout_keeps_logs_and_runs_cleanup(tmp_path):
    events=[]
    class ScopedProbe:
        def argv(self,*args,**kwargs):
            return [sys.executable,'-u','-c','import time,sys; print("partial stdout"); print("partial stderr",file=sys.stderr); time.sleep(30)']
        def host_env(self):return os.environ.copy()
        @contextmanager
        def command_scope(self,command):
            try:yield
            finally:events.append('cleaned')
    stem=tmp_path/'probe'
    with pytest.raises(EnvironmentBuildError,match='timed out'):
        _capture(ScopedProbe(),None,tmp_path,tmp_path,[],['ignored'],1.5,{},stem)
    assert events==['cleaned']
    assert 'partial stdout' in stem.with_suffix('.stdout.log').read_text()
    assert 'partial stderr' in stem.with_suffix('.stderr.log').read_text()
    assert json.loads(stem.with_suffix('.command.json').read_text())['timed_out'] is True


def test_docker_start_uses_verified_id_not_mutable_name(tmp_path,docker):
    rt,daemon=docker
    command=rt.build_command(request(tmp_path))
    assert command[-1].startswith('bpce-')
    with rt.command_scope(command):
        assert command[-1]==CONTAINER
        assert rt.events[-1]['command'][-1]==CONTAINER


def test_docker_undeclared_environment_injection_blocks_start(tmp_path,docker,monkeypatch):
    rt,daemon=docker
    original=daemon.run
    def inject(command,**kwargs):
        response=original(command,**kwargs)
        if 'create' in command:
            next(iter(daemon.containers.values()))['Config']['Env'].append('HTTP_PROXY=untracked-proxy')
        return response
    monkeypatch.setattr(subprocess,'run',inject)
    command=rt.build_command(request(tmp_path))
    with pytest.raises(ValidationError,match='environment differs'):
        with rt.command_scope(command):
            pytest.fail('injected environment must not start')
    assert not daemon.containers


def test_inflight_create_without_observation_is_not_a_cleanup_pass(tmp_path,docker,monkeypatch):
    rt,daemon=docker
    original=daemon.run
    def unknown(command,**kwargs):
        if 'create' in command:
            raise subprocess.TimeoutExpired(command,30)
        return original(command,**kwargs)
    monkeypatch.setattr(subprocess,'run',unknown)
    command=rt.build_command(request(tmp_path))
    with pytest.raises(subprocess.TimeoutExpired):
        with rt.command_scope(command):
            pytest.fail('unknown create must not start')
    assert rt.events[-1]['cleanup']=='failed'
    assert 'outcome is unknown' in rt.events[-1]['cleanup_error']


def test_runtime_snapshots_never_overwrite_and_close_is_idempotent(tmp_path,docker):
    rt,daemon=docker;rt.attach_attempt(tmp_path)
    command=rt.build_command(request(tmp_path))
    with rt.command_scope(command):
        first={p.name:p.read_bytes() for p in tmp_path.glob('runtime-event-*.json')}
    assert all((tmp_path/name).read_bytes()==data for name,data in first.items())
    rt.close()
    summary=(tmp_path/'runtime-invocations.json').read_bytes()
    rt.close()
    assert (tmp_path/'runtime-invocations.json').read_bytes()==summary


@pytest.mark.parametrize('name', ['apptainer','singularity','docker'])
@pytest.mark.parametrize('scenario', ['success','nonzero','timeout','preparation-failed','interrupted'])
def test_full_execution_cer_and_retry_bookkeeping_with_backend_doubles(context,tmp_path,monkeypatch,name,scenario):
    from test_storage_runtime import publish
    from benchpark_container import runtime as driver
    from benchpark_container.cli import load_record
    import signal
    contribution,source,_=publish(context,tmp_path)
    spec=driver.concrete_plan(contribution.payload,{'model':'model-a','size':'16'},['echo fixture'])
    spec['runtime']=settings(tmp_path,name)
    image_path=tmp_path/'local.sif';image_path.write_bytes(b'fixture-not-a-container')
    if name=='docker':
        spec['base'].update(kind='oci',uri=REFERENCE,platform='linux/amd64')
    else:
        spec['base'].update(kind='sif',source=str(image_path),sif_sha256=sha256(image_path))
    monkeypatch.setattr(shutil,'which',lambda _: '/declared/' + name)
    monkeypatch.setattr('benchpark_container.cer.recording.platform.machine',lambda:'x86_64')
    runtimes=[]
    def make(value):
        rt=create_backend(value)
        monkeypatch.setattr(rt,'version',lambda: name+' version test-double')
        if name=='docker':
            daemon=DaemonDouble(rt)
            monkeypatch.setattr(subprocess,'run',daemon.run)
            rt.fixture_daemon=daemon
        runtimes.append(rt)
        return rt
    monkeypatch.setattr(driver,'create_backend',make)
    def prepare(rt,image,scratch,inputs,mounts,spec,attempt,timeout):
        state={'state':'ready','tools':{'python':'python3','shell':'bash'},
               'runtime_environment':{'PATH':'/bpce/tools/bin:/usr/bin','PYTHONPATH':'/bpce/python','BPCE_PREFIX':'/bpce/tools'}}
        if scenario=='preparation-failed':
            state['state']='failed';state['error']='fixture failure'
        (attempt/'environment.json').write_text(json.dumps(state))
        if scenario=='preparation-failed':
            raise EnvironmentBuildError('fixture failure')
        if scenario=='interrupted':
            raise driver._ExecutionSignal(signal.SIGTERM)
        return state
    def logged(command,log,timeout,env):
        Path(log).write_text('fixture_metric=1\n')
        code,status={'success':(0,'COMPLETED'),'nonzero':(7,'FAILED'),'timeout':(124,'TIMEOUT')}[scenario]
        rt=runtimes[-1]
        if name=='docker':
            data=next(iter(rt.fixture_daemon.containers.values()))
            data['State'].update(Status='running' if code==124 else 'exited',Running=code==124,ExitCode=code)
            assert command[-1]==data['Id']
        return code,status
    monkeypatch.setattr(driver,'prepare_environment',prepare)
    monkeypatch.setattr(driver,'run_logged',logged)
    root=tmp_path/'runs'
    expected={'success':0,'nonzero':7,'timeout':124,'preparation-failed':1,'interrupted':143}[scenario]
    assert driver.execute(spec,source/'.benchpark-extensions/resources',root)==expected
    record_path=next(root.glob('*/cer.json'));record=load_record(record_path)
    original=record_path.read_bytes()
    assert record['schema_version']==2 and record['observed']['runtime']['name']==name
    assert record['observed']['runtime']['executable']=='/declared/'+name
    assert record['observed']['image']['kind']==('oci' if name=='docker' else 'sif')
    assert record['result']['exit_code']==expected
    assert record['status']=={'success':'COMPLETED','nonzero':'FAILED','timeout':'TIMEOUT',
                              'preparation-failed':'PREPARATION_FAILED','interrupted':'INTERRUPTED'}[scenario]
    if name=='docker' and scenario in ('success','nonzero','timeout'):
        assert record['observed']['runtime_invocations'][0]['cleanup']=='removed'
        assert 'runtime-invocations.json' in record['files']
        assert not runtimes[-1].fixture_daemon.containers
    assert driver.execute(spec,source/'.benchpark-extensions/resources',root)==expected
    assert record_path.read_bytes()==original and len(list(root.glob('*/cer.json')))==2


def test_cleanup_failure_never_returns_success_from_execute(context,tmp_path,monkeypatch):
    from test_storage_runtime import publish
    from benchpark_container import runtime as driver
    from benchpark_container.cli import load_record
    contribution,source,_=publish(context,tmp_path)
    spec=driver.concrete_plan(contribution.payload,{'model':'model-a','size':'16'},['true'])
    spec['runtime']=settings(tmp_path,'docker');spec['base'].update(kind='oci',uri=REFERENCE,platform='linux/amd64')
    monkeypatch.setattr(shutil,'which',lambda _: '/declared/docker')
    monkeypatch.setattr('benchpark_container.cer.recording.platform.machine',lambda:'x86_64')
    rt=DockerBackend(spec['runtime']);daemon=DaemonDouble(rt)
    monkeypatch.setattr(subprocess,'run',daemon.run);monkeypatch.setattr(rt,'version',lambda:'Docker fixture')
    monkeypatch.setattr(driver,'create_backend',lambda _:rt)
    monkeypatch.setattr(driver,'prepare_environment',lambda *a,**k:{'state':'ready','tools':{'shell':'bash'},'runtime_environment':{}})
    def logged(command,log,timeout,env):
        Path(log).write_text('fixture success\n');daemon.rm_error=True
        return 0,'COMPLETED'
    monkeypatch.setattr(driver,'run_logged',logged)
    assert driver.execute(spec,source/'.benchpark-extensions/resources',tmp_path/'runs')==1
    record=load_record(next((tmp_path/'runs').glob('*/cer.json')))
    assert record['status']=='EXECUTION_ERROR' and record['result']['runtime_cleanup_failed'] is True
