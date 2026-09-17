# SPDX-License-Identifier: Apache-2.0
"""Tests for the Common-Base-owned package-manager boundary."""
import json
import os
from pathlib import Path
from types import SimpleNamespace
import pytest

from benchpark_container import preparation as r
from benchpark_container.runtime import Apptainer
from benchpark_container.contracts import DEPENDENCY_POLICY
import sys


class FakeRuntime:
    def argv(self, image, scratch, inputs, mounts, command, environment=None, pwd='/bpce/work'):
        return ['apptainer','exec',str(image),'--'] + list(command)
    def host_env(self):
        return os.environ.copy()


def base_inventory():
    return {'python':'3.10','executable':'/usr/bin/python3','prefix':'/usr',
            'packages':{'torch':'2.11.0+cu128','pip':'22.0.2'},
            'distributions':[],
            'imports':{'torch':{'file':'/base/torch/__init__.py','file_sha256':'1'*64,'version':'2.11.0+cu128'}},
            'site_paths':['/usr/local/lib/python3.10/dist-packages'],'path':'/usr/local/bin:/usr/bin'}


def test_prepare_uses_base_python_m_pip_and_git_setup_script(tmp_path,monkeypatch):
    scratch=tmp_path/'scratch';scratch.mkdir();(scratch/'work').mkdir()
    inputs=tmp_path/'inputs';inputs.mkdir();(inputs/'requirements.txt').write_text('demo==1.0\n')
    (inputs/'setup.sh').write_text('#!/bin/bash\n')
    attempt=tmp_path/'attempt';attempt.mkdir()
    commands=[]

    def fake_inspect(rt,image,scratch,inputs,mounts,python,modules,timeout,environment=None,layer=None,log_stem=None):
        if layer:
            return {'python':'3.10','executable':'/usr/bin/python3','prefix':'/usr',
                    'packages':{'demo':'1.0'},'distributions':[{'name':'demo','version':'1.0','content_sha256':'2'*64}],
                    'imports':{},'site_paths':[],'path':'/usr/bin','prefix_sites':['/bpce/python/custom-site'],'prefix_bins':['/bpce/python/custom-bin']}
        data=base_inventory()
        if environment and environment.get('PYTHONPATH')=='/bpce/python/custom-site':
            data=json.loads(json.dumps(data));data['packages']['demo']='1.0'
        return data

    def fake_run(rt,image,scratch,inputs,mounts,command,timeout,environment=None,pwd='/bpce/work',log=None,check=False):
        commands.append((list(command),dict(environment or {}),pwd,log))
        if command[-1]=='--version':
            proc=SimpleNamespace(returncode=0,stdout='pip 22.0.2 from /usr/lib/python3/dist-packages/pip\n')
        elif command[-2:]==['freeze','--all']:
            proc=SimpleNamespace(returncode=0,stdout='pip==22.0.2\ndemo==1.0\n')
        else:
            proc=SimpleNamespace(returncode=0,stdout='')
        if log:
            Path(log).write_text('ok\n')
        return proc, ['apptainer','exec','base.sif','--']+list(command)

    monkeypatch.setattr(r,'preflight_tools',lambda *a,**k: ({'python':'python3','shell':'/custom/bash'}, {'source':'common-base','command':['python3','-m','pip'],'version_output':'pip 22.0.2','probe_command':['apptainer','exec','base.sif','--','python3','-m','pip','--version']}))
    monkeypatch.setattr(r,'_inspect',fake_inspect)
    monkeypatch.setattr(r,'_container_run',fake_run)
    monkeypatch.setattr(r,'_capture',lambda rt,image,scratch,inputs,mounts,command,timeout,environment,stem,**kw: fake_run(rt,image,scratch,inputs,mounts,command,timeout,environment))
    monkeypatch.setattr(r,'_pip_check',lambda *a,**kw:{'exit_code':0,'issues':[],'command':['python3','-m','pip','check']})
    spec={'runtime':{}, 'base':{'tools':{'python':'python3','shell':'bash'}}, 'dependency_policy':DEPENDENCY_POLICY,'protected_packages':{'torch':'torch'},
          'requirements':['inputs/requirements.txt'],'setup_scripts':['inputs/setup.sh'],
          'requirement_pins':{'demo':['1.0']},'smoke_imports':[]}
    result=r.prepare_environment(FakeRuntime(),Path('base.sif'),scratch,inputs,[],spec,attempt,60)
    assert result['state']=='ready'
    flattened=[c[0] for c in commands]
    assert result['package_manager']['command']==['python3','-m','pip']
    install=next(c for c in flattened if c[:3]==['python3','-m','pip'] and 'install' in c)
    assert install == ['python3','-m','pip','install','--prefix','/bpce/python','--requirement','/bpce/inputs/requirements.txt']
    assert ['/custom/bash','/bpce/inputs/setup.sh'] in flattened
    assert all('pip.pyz' not in ' '.join(c) for c in flattened)
    assert all(flag not in install for flag in ('--no-deps','--target','--ignore-installed','--isolated'))
    assert result['package_manager']['source']=='common-base'


def test_prepare_never_mounts_extension_resource_root(repository,tmp_path,monkeypatch):
    monkeypatch.setattr(__import__('shutil'),'which',lambda _: '/usr/bin/apptainer')
    a=Apptainer({'runtime':'apptainer','executable':'apptainer','worker_python':sys.executable,'image_cache':str(tmp_path)})
    scratch=tmp_path/'scratch';scratch.mkdir();inputs=tmp_path/'resources'/'inputs';inputs.mkdir(parents=True)
    argv=a.argv('base.sif',scratch,inputs,[],['true'])
    joined=' '.join(argv)
    assert str(inputs)+':/bpce/inputs:ro' in joined
    assert str(inputs.parent)+':/bpce/resources:ro' not in joined


def test_container_run_separates_runtime_stderr_from_machine_stdout(tmp_path):
    """Regression: Apptainer INFO on stderr must not corrupt JSON probe stdout."""
    class ProbeRuntime:
        def argv(self, image, scratch, inputs, mounts, command, environment=None, pwd='/bpce/work'):
            return [
                sys.executable, '-c',
                'import sys; print("{\\"ok\\": true}"); print("INFO: gocryptfs not found", file=sys.stderr)'
            ]
        def host_env(self):
            return os.environ.copy()

    proc, _ = r._container_run(
        ProbeRuntime(), Path('base.sif'), tmp_path, tmp_path, [], ['ignored'], 30,
        environment={}, check=True)
    assert json.loads(proc.stdout) == {'ok': True}
    assert 'gocryptfs not found' in proc.stderr
    assert 'gocryptfs' not in proc.stdout
