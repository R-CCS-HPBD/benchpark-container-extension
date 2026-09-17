# SPDX-License-Identifier: Apache-2.0
"""Executable checks for the sealed pre-change invariants, not one-SIF fixtures."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from types import SimpleNamespace as S
import zipfile
import pytest
from benchpark_integration.api import plain, ExtensionError
from benchpark_integration.support import storage
from benchpark_container.resolver import resolve
from benchpark_container.runtime import concrete_plan, Apptainer
from benchpark_container.preparation import (_pip_check, dependency_check_delta, EnvironmentBuildError,
    prepare_environment, _capture, _prefix_activation)
from benchpark_container.contracts import DEPENDENCY_POLICY
from benchpark_container.artifacts import materialize, tree_identity
from benchpark_container.util import ValidationError
from conftest import changed
from test_storage_runtime import publish
from test_generation_adapter import setup_adapter
from benchpark_container.ramble_adapter import wrap_executable


@pytest.mark.parametrize('field', ['executable', 'worker_python'])
def test_system_tools_have_no_hidden_fallback(context, field):
    s = plain(context.system); (s['runtimes']['apptainer'] if field=='executable' else s['execution']).pop(field)
    with pytest.raises(ValidationError, match='Missing'): resolve(changed(context, system=s))


@pytest.mark.parametrize('field', ['python', 'shell'])
def test_base_tools_have_no_hidden_fallback(context, field):
    q = plain(context.requirements); q['default_image']['tools'].pop(field)
    with pytest.raises(ValidationError, match='Missing'): resolve(changed(context, requirements=q))


def test_base_releases_can_use_different_python_and_shell(context):
    q = plain(context.requirements)
    q['default_image']['tools'] = {'python':'/opt/new Python/bin/py-entry','shell':'/opt/new Bash/bin/b-shell'}
    q['default_release'] = 'release-b'
    a = resolve(context).payload
    b = resolve(changed(context, requirements=q)).payload
    assert a['runtime'] == b['runtime']  # runtime is not per-image
    assert a['base']['tools'] != b['base']['tools']


@pytest.mark.parametrize('value', ['py -m something', './python', '../bin/python', '${PYTHON}', 'bad\npath'])
def test_invalid_executable_values_fail_before_run(context, value):
    q=plain(context.requirements);q['default_image']['tools']['python']=value
    with pytest.raises(ValidationError):resolve(changed(context,requirements=q))


def test_condition_identity_includes_system_snapshot(context):
    a=resolve(changed(context,provenance={'system_snapshot_sha256':'a'*64})).payload
    b=resolve(changed(context,provenance={'system_snapshot_sha256':'b'*64})).payload
    assert concrete_plan(a,{'model':'model-a','size':'16'},['true'])['condition_id'] != concrete_plan(b,{'model':'model-a','size':'16'},['true'])['condition_id']


def test_external_bytes_change_after_init_is_rejected(context,tmp_path):
    _,src,_=publish(context,tmp_path)
    spec=concrete_plan(resolve(context).payload,{'model':'model-a','size':'16'},['true'])
    (Path(plain(context.system)['artifact_roots']['models'])/'model-a/weights.txt').write_text('changed')
    attempt=tmp_path/'attempt';attempt.mkdir()
    with pytest.raises(ValidationError,match='Fixed external'):materialize(spec['artifacts'],src/storage.STATE_DIR/'resources',attempt)


@pytest.mark.parametrize('target', ['/usr/bin/python', '/bpce/foo', '/etc/config'])
def test_expanded_mount_targets_revalidated(context,target):
    q=plain(context.requirements);q['artifacts'][0]['target']='/{mount_target}'
    p=resolve(changed(context,requirements=q)).payload
    with pytest.raises(ValidationError):concrete_plan(p,{'model':'model-a','size':'16','mount_target':target[1:]},['true'])


def test_injected_new_yaml_is_not_copied_into_fixed_workspace(context,tmp_path):
    _,source,system=publish(context,tmp_path)
    (source/'new-mutable-config.yaml').write_text('variables: {inject: yes}')
    configs=tmp_path/'workspace/configs';configs.mkdir(parents=True)
    storage.stage_workspace(source,configs,system,None,lambda n:n.endswith('.yaml'))
    assert not (configs/'new-mutable-config.yaml').exists()


def test_template_fallback_is_frozen(context,tmp_path):
    c=resolve(context)
    src=tmp_path/'src';src.mkdir();(src/'ramble.yaml').write_text('ramble: {}')
    sysdir=tmp_path/'system';sysdir.mkdir()
    upstream=tmp_path/'upstream';f=upstream/'common-resources/execute_experiment.tpl'
    f.parent.mkdir(parents=True);f.write_text('original template')
    storage.publish_experiment((c,),src,sysdir,upstream_root=upstream)
    f.write_text('changed template')
    storage.copy_template(f,tmp_path/'tpl',src)
    assert (tmp_path/'tpl').read_text()=='original template'


def test_shell_tool_paths_come_from_contract_in_application(context,tmp_path):
    m,_,_=setup_adapter(context,tmp_path)
    e=S(template=['{bpce_python} /bench/run.py'],variables={},mpi=False,run_in_background=False)
    wrap_executable(m,'benchmark',e)
    import shlex
    args=shlex.split(e.template[0])
    data=json.loads(Path(args[args.index('--spec')+1]).read_text())
    assert data['command']==['"$BPCE_BASE_PYTHON" /bench/run.py']
    assert args[0] == plain(context.system)['execution']['worker_python']


@pytest.mark.parametrize('exitcode,stdout,stderr',[(1,'',''),(2,'','ERROR: unknown command'),(1,'demo 1 requires x, which is not installed.','Traceback: crash')])
def test_checker_execution_failure_is_not_an_empty_delta(tmp_path,monkeypatch,exitcode,stdout,stderr):
    from benchpark_container import preparation as m
    monkeypatch.setattr(m,'_capture',lambda *a,**kw:(S(returncode=exitcode,stdout=stdout,stderr=stderr),['declared-pip']))
    with pytest.raises(EnvironmentBuildError) as error:
        _pip_check(None,None,tmp_path,tmp_path,[],[],30,{},tmp_path/'check.log')
    assert error.value.code=='DEPENDENCY_CHECKER_FAILED'


def test_checker_does_not_parse_version_specific_diagnostic_wording(tmp_path,monkeypatch):
    from benchpark_container import preparation as m
    monkeypatch.setattr(m,'_capture',lambda *a,**kw:(S(returncode=1,stdout='future pip diagnostic wording',stderr=''),['declared-pip']))
    result=_pip_check(None,None,tmp_path,tmp_path,[],[],30,{},tmp_path/'check.log')
    assert result['issues']==['future pip diagnostic wording']


def test_success_message_is_not_an_issue(tmp_path,monkeypatch):
    from benchpark_container import preparation as m
    monkeypatch.setattr(m,'_capture',lambda *a,**kw:(S(returncode=0,stdout='No broken requirements found.\n',stderr=''),['pip']))
    result=_pip_check(None,None,tmp_path,tmp_path,[],[],30,{},tmp_path/'check.log')
    assert result['issues']==[]


def test_changed_symlink_affects_tool_identity(tmp_path):
    (tmp_path/'command').symlink_to('/first/base/tool')
    a=tree_identity(tmp_path)
    (tmp_path/'command').unlink();(tmp_path/'command').symlink_to('/second/base/tool')
    assert a!=tree_identity(tmp_path)


def test_unknown_pth_is_explicitly_rejected():
    with pytest.raises(EnvironmentBuildError,match='.pth'):
        _prefix_activation({'prefix_sites':[],'prefix_bins':[],'pth_files':['/bpce/python/x.pth']},{'path':'/base/bin'})


def test_metadata_probe_retains_invalid_stdout_and_stderr(tmp_path,monkeypatch):
    from benchpark_container import preparation as m
    monkeypatch.setattr(m,'_container_run',lambda *a,**kw:(S(returncode=0,stdout='not json',stderr='diagnostic'),['base-python']))
    with pytest.raises(EnvironmentBuildError,match='Invalid probe JSON'):
        _capture(None,None,tmp_path,tmp_path,[],[],30,{},tmp_path/'probe',require_json=True)
    assert (tmp_path/'probe.stderr.log').read_text()=='diagnostic'
    assert (tmp_path/'probe.stdout.log').read_text()=='not json'


def test_probe_timeout_retains_partial_output_and_diagnostic(tmp_path):
    import sys
    from benchpark_container.preparation import _capture, EnvironmentBuildError
    class Local:
        def host_env(self):return dict(__import__('os').environ)
        def argv(self,*args,**kwargs):
            return [sys.executable,'-S','-u','-c','import time,sys; print("started",flush=True); print("diagnostic",file=sys.stderr,flush=True); time.sleep(10)']
    with pytest.raises(EnvironmentBuildError) as error:
        _capture(Local(),None,None,None,[],[],0.5,{},tmp_path/'timeout',require_json=True)
    assert error.value.code=='BASE_PROBE_TIMEOUT'
    assert (tmp_path/'timeout.stdout.log').read_text().strip()=='started'
    assert (tmp_path/'timeout.stderr.log').read_text().strip()=='diagnostic'
    assert json.loads((tmp_path/'timeout.command.json').read_text())['timed_out']


def test_architecture_regression_rules(repository):
    from audit_architecture import audit
    result=audit(repository)
    assert result['issues']==[],result['issues']


def test_architecture_guard_detects_regression_not_just_current_literals(repository,tmp_path):
    import shutil
    from audit_architecture import audit
    for folder in ('src','core','examples'):
        shutil.copytree(repository/folder,tmp_path/folder,ignore=shutil.ignore_patterns('__pycache__','*.egg-info'))
    p=tmp_path/'src/benchpark_container/contracts.py'
    p.write_text(p.read_text()+'\ndef forbidden(settings):\n    return settings.get("python", "python3")\n')
    assert any('fallback' in i['rule'] for i in audit(tmp_path)['issues'])


def test_git_setup_provenance_in_nested_experiment(tmp_path):
    from benchpark_container.provenance import source_provenance
    subprocess.run(['git','init','-q',str(tmp_path)],check=True)
    source=tmp_path/'experiments/one';(source/'setup').mkdir(parents=True)
    (source/'setup/install.sh').write_text('echo reviewed\n')
    subprocess.run(['git','-C',str(tmp_path),'add','.'],check=True)
    subprocess.run(['git','-C',str(tmp_path),'-c','user.name=Test','-c','user.email=test@invalid', 'commit','-qm','fixture'],check=True)
    p=source_provenance(source,['setup/install.sh'])
    assert p['git_status']=='available' and len(p['commit'])==40
    assert p['setup_scripts_tracked']=={'setup/install.sh':True}
    assert not p['worktree_dirty']
    (source/'setup/install.sh').write_text('echo modified\n')
    assert source_provenance(source,['setup/install.sh'])['worktree_dirty']


def test_undeclared_apptainer_controls_do_not_enter_host_runtime(context,monkeypatch):
    monkeypatch.setattr('benchpark_container.runtime.shutil.which',lambda x:'/declared/runtime')
    monkeypatch.setenv('APPTAINER_BIND','/host:/usr')
    monkeypatch.setenv('SINGULARITYENV_PYTHONPATH','/injected')
    monkeypatch.setenv('APPTAINERENV_FOO','untracked')
    runtime=Apptainer(dict(plain(context.system)['execution'],runtime='apptainer',**plain(context.system)['runtimes']['apptainer']))
    result=runtime.host_env()
    assert 'APPTAINER_BIND' not in result and 'SINGULARITYENV_PYTHONPATH' not in result and 'APPTAINERENV_FOO' not in result
    assert result['APPTAINER_CACHEDIR'].endswith('oci-cache')
