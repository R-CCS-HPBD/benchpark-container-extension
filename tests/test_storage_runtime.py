# SPDX-License-Identifier: Apache-2.0
import copy
from dataclasses import replace
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
from types import SimpleNamespace as S
import pytest
from benchpark_integration.support import storage as core
from benchpark_integration.support.io import save_system_snapshot, read_system_snapshot
from benchpark_integration.api import ExtensionError, plain
from benchpark_container.resolver import resolve
from benchpark_container.runtime import concrete_plan, execute, run_logged, Apptainer
from benchpark_container.artifacts import materialize
from benchpark_container.cer.recording import start_run, finish_run
from benchpark_container.cli import load_record, differences, command
from benchpark_container.util import ValidationError, sha256, identity


def publish(context,tmp_path):
    c=resolve(context);source=tmp_path/'new-experiment';source.mkdir()
    system=tmp_path/'system';system.mkdir()
    (source/'ramble.yaml').write_text('ramble: {}\n')
    (system/'variables.yaml').write_text('variables: {x: 1}\n')
    (system/'execute_experiment.tpl').write_text('#!/bin/bash\n{command}\n')
    core.publish_experiment((c,),source,system)
    return c,source,system


def test_system_snapshot_no_extension_import(tmp_path,installed):
    system=S(extension_settings={'container':{'base':'A'}})
    save_system_snapshot(system.extension_settings,tmp_path)
    system.extension_settings['container']['base']='B'
    assert read_system_snapshot(tmp_path)['container']['base']=='A'
    assert all(e.loads==0 for e in installed if e.group != "benchpark.lifecycle.v1")
    with pytest.raises(FileExistsError):save_system_snapshot(system.extension_settings,tmp_path)


def test_system_snapshot_tamper(tmp_path):
    save_system_snapshot({'container':{'x':1}},tmp_path)
    p=tmp_path/core.SNAPSHOT;data=json.loads(p.read_text());data['settings']['container']['x']=2;p.write_text(json.dumps(data))
    with pytest.raises(ExtensionError):read_system_snapshot(tmp_path)


def test_native_snapshot_and_setup_unchanged(tmp_path):
    save_system_snapshot({},tmp_path)
    assert not (tmp_path/core.SNAPSHOT).exists()
    assert core.guard_workspace(tmp_path,tmp_path/'native') is None
    assert core.stage_workspace(tmp_path,tmp_path/'configs',tmp_path,None,None) is False


def test_fixed_resources_survive_definition_update(context,tmp_path):
    c,source,system=publish(context,tmp_path)
    (Path(context.source_root)/'script.py').write_text('new version')
    (system/'variables.yaml').write_text('variables: {x: 999}\n')
    core.verify_manifest(source)
    configs=tmp_path/'workspace'/'configs';configs.mkdir(parents=True)
    assert core.stage_workspace(source,configs,system,None,lambda n:n.endswith('.yaml'))
    assert not (configs/'variables.yaml').is_symlink()
    assert 'x: 1' in (configs/'variables.yaml').read_text()
    assert 'metric=1' in (configs.parent/core.STATE_DIR/'resources/inputs/script.py').read_text()
    assert (configs/'modifier_repos.yaml').is_file()

@pytest.mark.parametrize('target',['ramble.yaml','.benchpark-extensions/resources/inputs/script.py','.benchpark-extensions/plans/container.json'])
def test_snapshot_tamper_rejected(context,tmp_path,target):
    _,source,_=publish(context,tmp_path);p=source/target;p.chmod(0o644);p.write_text('corrupt')
    with pytest.raises(ExtensionError):core.verify_manifest(source)


def test_protected_workspace_not_replaced(context,tmp_path):
    _,source,_=publish(context,tmp_path);out=tmp_path/'out';out.mkdir();sentinel=out/'results';sentinel.write_text('preserve')
    with pytest.raises(ExtensionError):core.guard_workspace(source,out)
    assert sentinel.read_text()=='preserve'


def test_native_request_cannot_overwrite_extension_workspace(tmp_path):
    native=tmp_path/'native';native.mkdir();out=tmp_path/'out';p=out/'workspace'/core.STATE_DIR;p.mkdir(parents=True);(p/'manifest.json').write_text('{}')
    with pytest.raises(ExtensionError):core.guard_workspace(native,out)


def test_plan_concrete_matrix_and_repeat(context):
    p=dict(resolve(context).payload);p['parameters']['model']=['model-a','model-b'];p['environment']={'MEM':'{size}'}
    a=concrete_plan(p,{'model':'model-a','size':'16'},['python /bench/run.py'],'eA','1')
    b=concrete_plan(p,{'model':'model-b','size':'32'},['python /bench/run.py'],'eB','1')
    repeat=concrete_plan(p,{'model':'model-a','size':'16'},['python /bench/run.py'],'eA.2','2')
    assert a['condition_id']!=b['condition_id']
    assert a['condition_id']==repeat['condition_id']
    assert a['environment']['MEM']=='16' and b['environment']['MEM']=='32'
    assert a['artifacts'][1]['path']=='model-a'
    assert b['artifacts'][1]['path']=='model-b'
    with pytest.raises(ValidationError):concrete_plan(p,{'size':'16'},['true'])


def test_per_attempt_write_no_overwrite(context,tmp_path):
    p=concrete_plan(resolve(context).payload,{'model':'model-a','size':'16'},['true'])
    d1,r1=start_run(tmp_path/'runs',p);r1['status']='FAILED';finish_run(d1,r1)
    d2,r2=start_run(tmp_path/'runs',p);r2['status']='COMPLETED';finish_run(d2,r2)
    assert d1!=d2 and r1['condition_id']==r2['condition_id']
    assert load_record(d1)['status']=='FAILED'
    with pytest.raises(FileExistsError):finish_run(d1,r1)
    assert differences(r1,r2)


def test_unfinished_run_is_not_success(context,tmp_path,capsys):
    p=concrete_plan(resolve(context).payload,{'model':'model-a','size':'16'},['true'])
    start_run(tmp_path,p)
    assert command(S(cer_action='list',root=tmp_path))==0
    assert json.loads(capsys.readouterr().out)[0]['status']=='INCOMPLETE'


def test_cer_hash_validation(context,tmp_path):
    p=concrete_plan(resolve(context).payload,{'model':'model-a','size':'16'},['true'])
    d,r=start_run(tmp_path,p);r['status']='COMPLETED';finish_run(d,r)
    f=d/'cer.json';data=json.loads(f.read_text());data['status']='FAILED';f.write_text(json.dumps(data))
    with pytest.raises(ValidationError):load_record(f)


def test_missing_runtime_is_preserved_as_failure(context,tmp_path,monkeypatch):
    _,source,_=publish(context,tmp_path)
    monkeypatch.setattr(shutil,'which',lambda _:None)
    spec=concrete_plan(resolve(context).payload,{'model':'model-a','size':'16'},['true'])
    assert execute(spec,source/core.STATE_DIR/'resources',tmp_path/'runs')==1
    records=list((tmp_path/'runs').glob('*/cer.json'));assert len(records)==1
    assert load_record(records[0])['status']=='PREPARATION_FAILED'


def test_actual_process_timeout_and_logs(tmp_path):
    p=tmp_path/'run.log'
    code,state=run_logged([sys.executable,'-c','print("unit_metric=1")'],p,20,os.environ.copy())
    assert (code,state)==(0,'COMPLETED') and 'unit_metric=1' in p.read_text()
    code,state=run_logged([sys.executable,'-c','import time;time.sleep(30)'],tmp_path/'timeout.log',0.1,os.environ.copy())
    assert (code,state)==(124,'TIMEOUT')


def test_actual_process_failure(tmp_path):
    code,state=run_logged([sys.executable,'-c','raise SystemExit(7)'],tmp_path/'err.log',20,os.environ.copy())
    assert (code,state)==(7,'FAILED')


def test_apptainer_argv_not_shell_and_no_implicit_bind(tmp_path,monkeypatch):
    monkeypatch.setattr(shutil,'which',lambda _: '/usr/bin/apptainer')
    monkeypatch.setenv('APPTAINER_BIND','/host:/evil');monkeypatch.setenv('APPTAINERENV_PYTHONPATH','/evil')
    a=Apptainer({'runtime':'apptainer','executable':'apptainer','worker_python':sys.executable,'image_cache':str(tmp_path),'gpu':'nvidia'})
    argv=a.argv('base.sif',tmp_path,tmp_path,[],['python3','-c','print(1)'],{'X':'two words'})
    assert argv[1]=='exec' and '--nv' in argv and 'X=two words' in argv
    assert 'APPTAINER_BIND' not in a.host_env()
    assert 'APPTAINERENV_PYTHONPATH' not in a.host_env()
    with pytest.raises(ValidationError):a.argv('b',tmp_path/'a,b',tmp_path,[],['true'])


def test_portable_bundle_has_no_installed_extension_dependency(repository):
    r=repository/'src/benchpark_container/resources/runtime.pyz'
    proc=subprocess.run([sys.executable,str(r),'--help'],capture_output=True,text=True)
    assert proc.returncode==0 and '--spec-sha256' in proc.stdout
    subprocess.run([sys.executable,str(repository/'tools/build_runtime.py'),'--check'],check=True)


def test_directory_manifest_and_private_outputs(context,tmp_path):
    _,source,_=publish(context,tmp_path)
    spec=concrete_plan(resolve(context).payload,{'model':'model-a','size':'16'},['true'])
    attempt=tmp_path/'attempt';attempt.mkdir()
    mounts=materialize(spec['artifacts'],source/core.STATE_DIR/'resources',attempt)
    assert mounts[1]['content_verified'] is True
    assert mounts[1]['declared_revision_verified'] is False
    assert mounts[-1]['resolved_source']==str(attempt/'outputs/result')


@pytest.mark.parametrize('prepare_fails', [False, True])
def test_runtime_consumes_activation_contract_and_retains_partial_environment(context,tmp_path,monkeypatch,prepare_fails):
    """A runtime adapter double: tests job bookkeeping, not real Apptainer."""
    from benchpark_container import runtime as runtime_module
    monkeypatch.setattr("benchpark_container.cer.recording.platform.machine", lambda: "x86_64")
    _,source,_=publish(context,tmp_path)
    image=tmp_path/'base.sif';image.write_bytes(b'fixture-not-a-container')
    observed=[]
    class AdapterDouble(Apptainer):
        def __init__(self, settings):
            self.settings, self.executable, self.events, self.attempt = settings, settings['executable'], [], None
        def resolve_image(self, base, timeout):return image
        def version(self):return 'test-double-not-apptainer'
        def host_env(self):return os.environ.copy()
        def argv(self, image, scratch, inputs, mounts, command, environment=None, pwd='/bpce/work'):
            observed.append(environment)
            return [sys.executable,'-c','print("fixture-metric=1")']
    def fake_prepare(rt,image_arg,scratch,inputs,mounts,spec,attempt,timeout):
        data={'state':'failed' if prepare_fails else 'ready',
              'phase':'dependency-installation' if prepare_fails else 'complete',
              'error':'fixture conflict' if prepare_fails else None,
              'tools':{'python':sys.executable,'shell':shutil.which('bash')},
              'runtime_environment':{'PATH':'/bpce/python/bin:/bpce/tools/bin:/usr/bin',
                   'PYTHONPATH':'/bpce/python','BPCE_PREFIX':'/bpce/tools',
                   'PYTHONNOUSERSITE':'1','PYTHONDONTWRITEBYTECODE':'1'}}
        (attempt/'environment.json').write_text(json.dumps(data))
        if prepare_fails:
            raise RuntimeError('fixture conflict')
        return data
    monkeypatch.setitem(__import__('benchpark_container.backends.registry', fromlist=['BACKENDS']).BACKENDS, 'apptainer', AdapterDouble)
    monkeypatch.setattr(runtime_module,'prepare_environment',fake_prepare)
    spec=concrete_plan(resolve(context).payload,{'model':'model-a','size':'16'},['true'])
    code=execute(spec,source/core.STATE_DIR/'resources',tmp_path/'runs')
    record=load_record(next((tmp_path/'runs').glob('*/cer.json')))
    if prepare_fails:
        assert code==1 and not observed
        assert record['status']=='PREPARATION_FAILED'
        assert record['observed']['software_environment']['error']=='fixture conflict'
    else:
        assert code==0 and record['status']=='COMPLETED'
        assert observed[0]['PYTHONPATH']=='/bpce/python'
        assert observed[0]['BPCE_PREFIX']=='/bpce/tools'
        assert 'VIRTUAL_ENV' not in observed[0]


def test_execute_records_benchmark_failure_phase(context, tmp_path, monkeypatch):
    """A non-zero benchmark process is an execution failure, not a preparation failure."""
    from benchpark_container import runtime as runtime_module
    monkeypatch.setattr("benchpark_container.cer.recording.platform.machine", lambda: "x86_64")
    _, source, _ = publish(context, tmp_path)
    image = tmp_path / "base.sif"
    image.write_bytes(b"fixture-not-a-container")

    class AdapterDouble(Apptainer):
        def __init__(self, settings):
            self.settings, self.executable, self.events, self.attempt = settings, settings['executable'], [], None
        def resolve_image(self, base, timeout): return image
        def version(self): return "test-double-not-apptainer"
        def host_env(self): return os.environ.copy()
        def argv(self, image, scratch, inputs, mounts, command, environment=None, pwd="/bpce/work"):
            return [sys.executable, "-c", "raise SystemExit(127)"]

    def fake_prepare(rt, image_arg, scratch, inputs, mounts, spec, attempt, timeout):
        data = {
            "state": "ready",
            "phase": "complete",
            "tools": {"python":sys.executable,"shell":shutil.which("bash")},
            "runtime_environment": {
                "PATH": "/usr/bin",
                "PYTHONPATH": "/bpce/python",
                "BPCE_PREFIX": "/bpce/tools",
                "PYTHONNOUSERSITE": "1",
                "PYTHONDONTWRITEBYTECODE": "1",
            },
        }
        (attempt / "environment.json").write_text(json.dumps(data))
        return data

    monkeypatch.setitem(__import__('benchpark_container.backends.registry', fromlist=['BACKENDS']).BACKENDS, 'apptainer', AdapterDouble)
    monkeypatch.setattr(runtime_module, "prepare_environment", fake_prepare)
    spec = concrete_plan(resolve(context).payload, {"model": "model-a", "size": "16"}, ["true"])
    code = execute(spec, source/core.STATE_DIR/"resources", tmp_path/"runs")
    record = load_record(next((tmp_path/"runs").glob("*/cer.json")))
    assert code == 127
    assert record["status"] == "FAILED"
    assert record["result"]["failure_phase"] == "benchmark"
