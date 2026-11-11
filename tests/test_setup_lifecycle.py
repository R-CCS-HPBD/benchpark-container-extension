# SPDX-License-Identifier: Apache-2.0
"""Production lifecycle through a real local Bash transport double.

No container/GPU isolation is claimed. Backend argv read-only bindings are
separately checked using the production Apptainer and Docker command builders.
"""
import copy
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
from types import SimpleNamespace as S

import pytest

from benchpark_container import runtime, setup_environment as setup
from benchpark_container import resource_acquisition as acquisition
from benchpark_container.artifacts import content_identity
from benchpark_container.backends.base import ImageRef, Mount
from benchpark_container.backends.apptainer import ApptainerBackend
from benchpark_container.backends.docker import DockerBackend
from benchpark_container.cli import load_record, verify_recorded_files
from benchpark_container.planning import concrete_plan, validate_plan_version
from benchpark_container.resolver import resolve
from benchpark_container.ramble_adapter import prepare_experiment
from benchpark_container.util import ValidationError, sha256, identity
from benchpark_integration.api import plain
from conftest import changed


class BashTransport:
    """Test-only runtime port. Executes real Bash, not a container emulator."""
    def __init__(self):
        self.events = []
        self.calls = []
        self.resolutions = 0
        self.loaded = 0

    def attach_attempt(self, attempt): self.attempt = Path(attempt)
    def observe_runtime(self): return {'name': 'test-transport', 'version': 'not-a-container'}
    def observe_image(self, image): return {'kind': image.kind, 'identity': image.identity, **dict(image.details)}
    def close(self): pass
    def host_env(self): return dict(os.environ)

    def resolve_image(self, base, timeout):
        self.resolutions += 1
        return ImageRef('sif', base['source'], 'sha256:' + base['sif_sha256'],
                        {'sif_sha256': base['sif_sha256']})

    def resolve_local_image(self, base, timeout): return self.resolve_image(base, timeout)

    def load_prepared_image(self, base, saved, timeout):
        self.loaded += 1
        if sha256(saved['reference']) != base['sif_sha256']:
            raise ValidationError('Prepared SIF content changed')
        return ImageRef(saved['kind'], saved['reference'], saved['identity'], saved['details'])

    def argv(self, image, scratch, inputs, mounts, command, environment=None, pwd='/bpce/work'):
        self.calls.append(list(command))
        mappings = [(m.target, m.source) for m in getattr(self, 'prepared_prefixes', ())]
        mappings += [('/bpce/inputs', str(inputs)), ('/bpce', str(scratch))]
        # longest matches first, with no second replacement of resolved paths.
        import re
        aliases = dict(mappings)
        pattern = re.compile('|'.join(re.escape(x) for x in sorted(aliases, key=len, reverse=True)))
        def translate(value): return pattern.sub(lambda m: aliases[m.group()], str(value))
        env = {'PATH': os.environ.get('PATH', '/usr/bin:/bin'), 'LC_ALL': 'C'}
        env.update({k: translate(v) for k, v in (environment or {}).items()})
        return [shutil.which('env'), '-i'] + [k+'='+v for k,v in sorted(env.items())] + [
            shutil.which('bash'), '-c', 'cd -- "$1"; shift; exec "$@"', 'test-port', translate(pwd)
        ] + [translate(x) for x in command]


@pytest.fixture
def sample(context, tmp_path, monkeypatch):
    native = {'aarch64': 'linux/arm64', 'x86_64': 'linux/amd64'}[platform.machine()]
    source = Path(context.source_root)
    (source / 'setup.sh').write_text('set -eu\nprintf prepared > "$BPCE_PREFIX/value"\n')
    image = tmp_path / 'base.sif'; image.write_bytes(b'local test fixture, not SIF')
    q = plain(context.requirements)
    q.update(schema_version=3, default_image={'uri':image.as_uri(), 'managed':False,
        'platform':native, 'tools':{'shell':'/bin/bash'}},
        requirements=[], protected_packages={}, smoke_imports=[], variables=[],
        artifacts=[{'name':'result','kind':'result','target':'/results'}],
        setup_scripts=['setup.sh'])
    system = plain(context.system); system['platform'] = native
    c = resolve(changed(context, requirements=q, system=system))
    resources = tmp_path / 'workspace/.benchpark-extensions/resources'; resources.mkdir(parents=True)
    for item in c.resources:
        p = resources / item.target; p.parent.mkdir(parents=True, exist_ok=True); shutil.copy2(item.source,p)
    spec = concrete_plan(plain(c.payload), {}, ['test "$(cat "$BPCE_PREFIX/value")" = prepared', 'echo SETUP_HANDOFF_OK'], 'test_first', '0')
    builder, worker = BashTransport(), BashTransport()
    monkeypatch.setattr(setup,'create_backend',lambda settings:builder)
    monkeypatch.setattr(runtime,'create_backend',lambda settings:worker)
    # No optional system collectors/network dependence in this contract test.
    monkeypatch.setattr('benchpark_container.cer.recording.collect_v1',
        lambda *a: {'observed':{}, 'files':{}, 'collection':{'status':'not-requested'}})
    return S(spec=spec, resources=resources, builder=builder, worker=worker,
             root=tmp_path, context=changed(context, requirements=q, system=system), contribution=c)


def test_setup_then_on_uses_real_preparation_and_never_reinstalls(sample, monkeypatch):
    result = setup.prepare(sample.spec, sample.resources)
    assert result['state']=='READY'
    assert result['builder']['gpu_runtime_validation']=='not-performed'
    assert len(result['layers'])==1 and result['layers'][0]['name']=='tools'
    monkeypatch.setattr(runtime, 'prepare_environment', lambda *a: pytest.fail('On attempted to build'))
    sample.worker.resolve_image=lambda *a: pytest.fail('On attempted to acquire image')
    assert runtime.execute(sample.spec,sample.resources,sample.root/'runs')==0
    cer_path=sample.worker.attempt/'cer.json'
    cer=load_record(cer_path)
    assert cer['status']=='COMPLETED'
    assert cer['observed']['prepared_environment_integrity_after']=='verified'
    assert verify_recorded_files(cer_path,cer)>0
    assert sample.worker.loaded==2
    assert 'SETUP_HANDOFF_OK' in (sample.worker.attempt/'benchmark.log').read_text()
    assert sample.worker.prepared_prefixes[0].readonly is True
    # Archive interpretation does not require the original prepared store.
    record_root=setup.environment_root(sample.spec,sample.resources)
    moved=record_root.with_name('not-at-original-location')
    record_root.rename(moved)
    assert verify_recorded_files(cer_path,load_record(cer_path))>0


def test_on_missing_record_fails_without_build_or_download(sample,monkeypatch):
    monkeypatch.setattr(runtime,'prepare_environment',lambda *a:pytest.fail('late preparation'))
    sample.worker.resolve_image=lambda *a:pytest.fail('late pull')
    assert runtime.execute(sample.spec,sample.resources,sample.root/'runs')!=0
    cer=load_record(sample.worker.attempt/'cer.json')
    assert 'ENVIRONMENT_NOT_PREPARED' in cer['result']['error']
    assert not (sample.worker.attempt/'benchmark.log').exists()


def test_repeated_setup_is_verified_not_rebuilt(sample):
    a=setup.prepare(sample.spec,sample.resources)
    calls=len(sample.builder.calls)
    b=setup.prepare(sample.spec,sample.resources)
    assert a==b and len(sample.builder.calls)==calls


@pytest.mark.parametrize('target', ['record','prefix','lock','source'])
def test_tamper_blocks_on_and_setup_does_not_repair(sample,target):
    setup.prepare(sample.spec,sample.resources)
    root=setup.environment_root(sample.spec,sample.resources)
    paths={'record':root/'record.json','prefix':root/'layers/tools/value',
           'lock':root/'lock.json','source':sample.resources/'inputs/setup.sh'}
    p=paths[target]; p.chmod(0o600);p.write_text('tampered')
    with pytest.raises((ValidationError,ValueError)):
        setup.prepare(sample.spec,sample.resources)
    assert runtime.execute(sample.spec,sample.resources,sample.root/'runs')!=0
    assert not list((sample.root/'runs').glob('*/benchmark.log'))


def test_setup_failure_publishes_failure_not_ready(sample):
    spec=copy.deepcopy(sample.spec)
    p=sample.resources/'inputs/setup.sh';p.write_text('exit 17\n')
    spec['resources']['inputs/setup.sh']=sha256(p)
    with pytest.raises(Exception,match='Setup script failed'):
        setup.prepare(spec,sample.resources)
    assert not setup.environment_root(spec,sample.resources).exists()
    failures=list((sample.resources.parent/'prepared').glob('failed-*/failure.json'))
    assert len(failures)==1 and json.loads(failures[0].read_text())['state']=='FAILED'


def test_two_conditions_have_independent_prepared_state(sample):
    first=sample.spec;second=copy.deepcopy(first);second['parameters']['x']='two'
    second['condition_id']=identity(second['parameters']);second['ramble_experiment']='test_second'
    a=setup.prepare(first,sample.resources);b=setup.prepare(second,sample.resources)
    assert a['plan_sha256']!=b['plan_sha256']
    assert setup.environment_root(first,sample.resources)!=setup.environment_root(second,sample.resources)


def test_no_schema_downgrade(sample):
    for version in (1,2):
        spec=copy.deepcopy(sample.spec);spec['schema_version']=version
        with pytest.raises(ValidationError):validate_plan_version(spec)
    spec=copy.deepcopy(sample.spec);spec['preparation_lifecycle']='unknown'
    with pytest.raises(ValidationError):validate_plan_version(spec)


def test_platform_is_checked_before_build(sample, monkeypatch):
    native=platform.machine()
    monkeypatch.setattr(setup.platform,'machine',lambda:'aarch64' if native=='x86_64' else 'x86_64')
    with pytest.raises(ValidationError,match='target-architecture'):setup.prepare(sample.spec,sample.resources)
    assert sample.builder.resolutions==0


def test_setup_offline_without_preparation_and_without_network(sample):
    spec=copy.deepcopy(sample.spec);spec['setup']['offline']=True;spec['setup_scripts']=[]
    sample.builder.resolve_local_image=lambda base, timeout: ImageRef('sif',base['source'],
        'sha256:'+base['sif_sha256'],{'sif_sha256':base['sif_sha256']})
    sample.builder.resolve_image=lambda *a:pytest.fail('offline pull')
    result=setup.prepare(spec,sample.resources)
    assert result['state']=='READY'


def test_offline_does_not_claim_arbitrary_scripts_are_hermetic(sample):
    spec=copy.deepcopy(sample.spec);spec['setup']['offline']=True
    with pytest.raises(ValidationError,match='OFFLINE_BUILD_UNSUPPORTED'):setup.prepare(spec,sample.resources)
    assert sample.builder.calls==[]


def test_phase_dry_run_has_no_access_or_writes(tmp_path):
    prepare_experiment(S(_file_path=str(tmp_path/'missing.py')),S(dry_run=True),None)
    assert list(tmp_path.iterdir())==[]


def test_phase_runs_only_matching_concrete_experiment(sample,monkeypatch):
    root=sample.resources.parent
    (root/'plans').mkdir();(root/'plans/container.json').write_text(json.dumps(plain(sample.contribution.payload)))
    (root/'concrete').mkdir()
    for name in ('test_first','test_second'):
        spec=copy.deepcopy(sample.spec);spec['ramble_experiment']=name
        (root/'concrete'/f'{name}.json').write_text(json.dumps(spec))
    calls=[]
    monkeypatch.setattr(subprocess,'run',lambda argv, **kw:calls.append(argv))
    modifier=S(_file_path=str(sample.resources/'modifiers/bpce-execution/modifier.py'))
    app=S(expander=S(expand_var_name=lambda n:'test_first'), repeats=S(is_repeat_base=False))
    prepare_experiment(modifier,S(dry_run=False),app)
    assert len(calls)==1 and '--setup-environment' in calls[0]
    assert calls[0][calls[0].index('--spec')+1].endswith('test_first.json')



def test_phase_ignores_provisional_render_plans(sample, monkeypatch):
    # Fixed Ramble may render one non-executable provisional command before
    # producing the concrete command for each actual experiment. Only concrete
    # plans may consume Setup resources or obtain READY state.
    root = sample.resources.parent
    (root/'plans').mkdir()
    (root/'plans/container.json').write_text(json.dumps(plain(sample.contribution.payload)))
    (root/'concrete').mkdir()
    (root/'provisional').mkdir()
    for name in ('test_first', 'test_second'):
        executable = copy.deepcopy(sample.spec)
        executable['ramble_experiment'] = name
        executable['repeat_index'] = '0'
        provisional = copy.deepcopy(executable)
        provisional['repeat_index'] = 'unknown'
        (root/'concrete'/f'{name}.json').write_text(json.dumps(executable))
        (root/'provisional'/f'{name}.json').write_text(json.dumps(provisional))

    calls = []
    monkeypatch.setattr(subprocess, 'run', lambda argv, **kw: calls.append(argv))
    modifier = S(_file_path=str(sample.resources/'modifiers/bpce-execution/modifier.py'))
    for name in ('test_first', 'test_second'):
        app = S(expander=S(expand_var_name=lambda n, name=name: name), repeats=S(is_repeat_base=False))
        prepare_experiment(modifier, S(dry_run=False), app)

    assert len(calls) == 2
    assert {Path(argv[argv.index('--spec') + 1]).parent.name for argv in calls} == {'concrete'}
    assert {Path(argv[argv.index('--spec') + 1]).stem for argv in calls} == {'test_first', 'test_second'}


def test_phase_rejects_only_provisional_plan(sample):
    root = sample.resources.parent
    (root/'plans').mkdir()
    (root/'plans/container.json').write_text(json.dumps(plain(sample.contribution.payload)))
    (root/'provisional').mkdir()
    old = copy.deepcopy(sample.spec)
    old['ramble_experiment'] = 'test_first'
    old['repeat_index'] = 'unknown'
    (root/'provisional'/'test_first.json').write_text(json.dumps(old))
    modifier = S(_file_path=str(sample.resources/'modifiers/bpce-execution/modifier.py'))
    app = S(expander=S(expand_var_name=lambda n: 'test_first'), repeats=S(is_repeat_base=False))
    with pytest.raises(ValidationError, match='no generated'):
        prepare_experiment(modifier, S(dry_run=False), app)


def test_phase_rejects_unresolved_repeat_in_concrete_path(sample):
    root = sample.resources.parent
    (root/'plans').mkdir()
    (root/'plans/container.json').write_text(json.dumps(plain(sample.contribution.payload)))
    (root/'concrete').mkdir()
    invalid = copy.deepcopy(sample.spec)
    invalid['ramble_experiment'] = 'test_first'
    invalid['repeat_index'] = 'unknown'
    (root/'concrete'/'test_first.json').write_text(json.dumps(invalid))
    modifier = S(_file_path=str(sample.resources/'modifiers/bpce-execution/modifier.py'))
    app = S(expander=S(expand_var_name=lambda n: 'test_first'), repeats=S(is_repeat_base=False))
    with pytest.raises(ValidationError, match='unresolved repeat_index'):
        prepare_experiment(modifier, S(dry_run=False), app)


def test_phase_does_not_fallback_when_concrete_plan_is_missing(sample):
    root=sample.resources.parent;(root/'plans').mkdir()
    (root/'plans/container.json').write_text(json.dumps(plain(sample.contribution.payload)))
    modifier=S(_file_path=str(sample.resources/'modifiers/bpce-execution/modifier.py'))
    app=S(expander=S(expand_var_name=lambda n:'missing'),repeats=S(is_repeat_base=False))
    with pytest.raises(ValidationError,match='no generated'):prepare_experiment(modifier,S(dry_run=False),app)


@pytest.mark.parametrize('backend', ['apptainer','docker'])
def test_production_backends_bind_prepared_prefixes_readonly(sample,backend):
    record=setup.prepare(sample.spec,sample.resources);root=setup.environment_root(sample.spec,sample.resources)
    settings=dict(sample.spec['runtime'],executable=sys.executable,runtime=backend)
    if backend=='apptainer':
        rt=ApptainerBackend(settings); image=ImageRef('sif',sample.spec['base']['source'],
            'sha256:'+sample.spec['base']['sif_sha256'],{'sif_sha256':sample.spec['base']['sif_sha256']})
    else:
        rt=DockerBackend(settings);image=ImageRef('oci','sha256:'+'ab'*32,'registry/x@sha256:'+'ab'*32)
    scratch=sample.root/('exec-'+backend);scratch.mkdir();setup.bind_prepared(rt,scratch,root,record)
    argv=rt.argv(image,scratch,sample.resources/'inputs',[],['/bin/bash','-c','true'])
    if backend=='apptainer':
        assert str(root/'layers/tools')+':/bpce/tools:ro' in argv
    else:
        create=rt.events[-1]['creation_command']
        assert 'type=bind,src='+str(root/'layers/tools')+',dst=/bpce/tools,readonly' in create
    rt.close()


def test_bundle_setup_failure_does_not_fallback(sample,repository):
    # Execute the actual shipped worker CLI: no inherited installed Extension.
    spec=copy.deepcopy(sample.spec)
    # Base timeout / nonexistent runtime: failure must precede any READY publish.
    spec['runtime']['executable']='/nonexistent/bpce-runtime'
    plan=sample.root/'spec.json';plan.write_text(json.dumps(spec))
    argv=[sys.executable,'-I',str(repository/'src/benchpark_container/resources/runtime.pyz'),
          '--spec',str(plan),'--spec-sha256',sha256(plan),'--resources',str(sample.resources),
          '--run-root',str(sample.root/'runs'),'--setup-environment']
    cp=subprocess.run(argv,capture_output=True,text=True,timeout=20)
    assert cp.returncode!=0
    assert not setup.environment_root(spec,sample.resources).exists()
    assert not (sample.root/'runs').exists()


def test_constructed_input_change_is_rejected_at_on(sample, monkeypatch):
    setup.prepare(sample.spec,sample.resources)
    image=Path(sample.spec["base"]["source"])
    image.write_bytes(b"replaced base")
    assert runtime.execute(sample.spec,sample.resources,sample.root/"runs")!=0
    assert not list((sample.root/"runs").glob("*/benchmark.log"))


def test_base_change_during_benchmark_is_not_sealed_as_success(sample, monkeypatch):
    setup.prepare(sample.spec,sample.resources)
    real=runtime.run_logged
    def change(command,log,timeout,env):
        result=real(command,log,timeout,env)
        Path(sample.spec["base"]["source"]).write_bytes(b"changed after running")
        return result
    monkeypatch.setattr(runtime,"run_logged",change)
    assert runtime.execute(sample.spec,sample.resources,sample.root/"runs")!=0
    cer=load_record(sample.worker.attempt/"cer.json")
    assert cer["result"]["application_exit_code"]==0
    assert cer["observed"]["prepared_environment_integrity_after"]=="failed"


def test_dependency_installation_is_rejected_not_masked_by_setup(sample, monkeypatch):
    # The setup orchestrator must not silently drop requirements to obtain PASS.
    def bad(*args):raise ValidationError("BASE_VERSION_CONFLICT")
    monkeypatch.setattr(setup,"prepare_environment",bad)
    with pytest.raises(ValidationError,match="BASE_VERSION_CONFLICT"):
        setup.prepare(sample.spec,sample.resources)
    assert not setup.environment_root(sample.spec,sample.resources).exists()


def test_setup_timeout_is_a_failure_without_ready(sample):
    spec=copy.deepcopy(sample.spec)
    p=sample.resources/"inputs/setup.sh";p.write_text("sleep 20\n")
    spec["resources"]["inputs/setup.sh"]=sha256(p)
    spec["setup"]["timeout_seconds"]=0.2
    with pytest.raises(Exception,match="timed out"):
        setup.prepare(spec,sample.resources)
    assert not setup.environment_root(spec,sample.resources).exists()


def test_setup_signal_stops_child_and_restores_handler(sample):
    import signal, threading
    spec=copy.deepcopy(sample.spec)
    p=sample.resources/"inputs/setup.sh"
    p.write_text("sleep 20\n")
    spec["resources"]["inputs/setup.sh"]=sha256(p)
    before=signal.getsignal(signal.SIGTERM)
    timer=threading.Timer(0.3, lambda:os.kill(os.getpid(),signal.SIGTERM))
    timer.start()
    try:
        with pytest.raises(setup.SetupInterrupted):setup.prepare(spec,sample.resources)
    finally:timer.cancel();timer.join()
    assert signal.getsignal(signal.SIGTERM)==before
    assert not setup.environment_root(spec,sample.resources).exists()


def test_build_record_cannot_rebind_declared_private_path(sample):
    record=setup.prepare(sample.spec,sample.resources)
    data=copy.deepcopy(record)
    # Altering even a publicly recorded source manifest still requires its hash.
    data["plan"]["benchmark"]="other"
    body=dict(data);body.pop("record_sha256");data["record_sha256"]=identity(body)
    with pytest.raises(ValidationError,match="different concrete plan"):
        setup._validate_record(data,sample.spec)


def test_retain_original_setup_logs_and_detect_tampering(sample):
    setup.prepare(sample.spec,sample.resources)
    assert runtime.execute(sample.spec,sample.resources,sample.root/"runs")==0
    cer_path=sample.worker.attempt/"cer.json";cer=load_record(cer_path)
    copied=sample.worker.attempt/"source-evidence/prepared-environment/environment.json"
    assert copied.exists()
    copied.write_text("changed")
    with pytest.raises(ValidationError):verify_recorded_files(cer_path,cer)


def test_old_frozen_phase_does_not_construct(sample):
    root=sample.resources.parent;(root/"plans").mkdir()
    (root/"plans/container.json").write_text(json.dumps({"schema_version":1}))
    modifier=S(_file_path=str(sample.resources/"modifiers/bpce-execution/modifier.py"))
    app=S(repeats=S(is_repeat_base=False))
    assert prepare_experiment(modifier,S(dry_run=False),app) is None
    assert not (root/"prepared").exists()


def test_repeated_setup_checks_live_base_without_repair(sample):
    setup.prepare(sample.spec,sample.resources)
    resolutions=sample.builder.resolutions
    Path(sample.spec["base"]["source"]).write_bytes(b"changed externally")
    with pytest.raises(ValidationError,match="content changed"):
        setup.prepare(sample.spec,sample.resources)
    assert sample.builder.resolutions==resolutions


def test_parallel_setup_same_plan_constructs_once(sample):
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=2) as executor:
        records=list(executor.map(lambda _:setup.prepare(sample.spec,sample.resources),range(2)))
    assert records[0]==records[1]
    assert sample.builder.resolutions==1
