# SPDX-License-Identifier: Apache-2.0
"""Generated-condition adapter tests. Ramble objects are explicit test doubles."""
import importlib.util
import json
from pathlib import Path
import re
import shlex
import sys
from types import SimpleNamespace as S
import types
import pytest
from benchpark_integration.api import plain
from benchpark_container.resolver import resolve
from benchpark_container.ramble_adapter import wrap_executable
from benchpark_container.util import ValidationError
from conftest import changed
from test_core_patch import native_spec
from patch_core import transform


class Expander:
    def __init__(self,values):self.values=values
    def expand_var_name(self,key):return self.values.get(key,'{'+key+'}')
    def expand_var(self,text,extra_vars=None):
        values=dict(self.values);values.update(extra_vars or {})
        return re.sub(r'\{(\w+)\}',lambda m:str(values.get(m[1],m[0])),text)


def setup_adapter(context,tmp_path):
    c=resolve(context);state=tmp_path/'workspace'/'.benchpark-extensions';state.mkdir(parents=True)
    plan=state/'plans/container.json';plan.parent.mkdir();plan.write_text(json.dumps(c.payload))
    resources=state/'resources';resources.mkdir();(resources/'runtime.pyz').write_bytes(b'placeholder-only-adapter-not-executed')
    run=tmp_path/'workspace/experiments/a';run.mkdir(parents=True)
    values={'bpce_plan':str(plan),'bpce_resources':str(resources),'experiment_run_dir':str(run),
            'experiment_name':'a','model':'model-a','size':'16','repeat_index':'1','n_nodes':'1','n_ranks':'1'}
    modifier_file=resources/'modifiers/bpce-execution/modifier.py'
    modifier_file.parent.mkdir(parents=True);modifier_file.write_text('# test fixture\n')
    return S(expander=Expander(values), _file_path=str(modifier_file)),run,resources


def test_adapter_concrete_command_and_logs(context,tmp_path):
    m,run,resources=setup_adapter(context,tmp_path)
    executable=S(template=['python /bench/run.py --size {size}'],variables={},mpi=False,run_in_background=False,redirect='original.log',output_capture='>>')
    assert wrap_executable(m,'benchmark',executable)==([],[])
    argv=shlex.split(executable.template[0]);spec=json.loads(Path(argv[argv.index('--spec')+1]).read_text())
    assert spec['parameters']['size']=='16' and spec['command']==['python /bench/run.py --size 16']
    run_root=Path(argv[argv.index('--run-root')+1])
    assert run not in run_root.parents  # allocation modifier may clear run dir!
    assert resources.parent in run_root.parents
    assert executable.redirect=='original.log' and executable.output_capture=='>>'
    before=list(executable.template);wrap_executable(m,'benchmark',executable)
    assert before==executable.template

@pytest.mark.parametrize('mpi,background,ranks',[(True,False,'1'),(False,True,'1'),(False,False,'2')])
def test_unsupported_launcher_modes_fail(context,tmp_path,mpi,background,ranks):
    m,_,_=setup_adapter(context,tmp_path);m.expander.values['n_ranks']=ranks
    e=S(template=['true'],variables={},mpi=mpi,run_in_background=background)
    with pytest.raises(ValidationError):wrap_executable(m,'benchmark',e)


def test_unselected_executable_left_alone(context,tmp_path):
    m,_,_=setup_adapter(context,tmp_path);e=S(template=['host prep'])
    assert wrap_executable(m,'unrelated-host-action',e)==([],[]) and e.template==['host prep']


def test_adapter_skips_ramble_repeat_base(context,tmp_path):
    m,_,resources=setup_adapter(context,tmp_path)
    executable=S(template=['python /bench/run.py --size {size}'],variables={},mpi=False,run_in_background=False)
    app=S(expander=m.expander,repeats=S(is_repeat_base=True))
    original=list(executable.template)
    assert wrap_executable(m,'benchmark',executable,app)==([],[])
    assert executable.template==original
    concrete=resources.parent/'concrete'
    assert not concrete.exists() or not list(concrete.glob('*.json'))


def test_adapter_materializes_ramble_repeat_child(context,tmp_path):
    m,_,resources=setup_adapter(context,tmp_path)
    executable=S(template=['python /bench/run.py --size {size}'],variables={},mpi=False,run_in_background=False)
    app=S(expander=m.expander,repeats=S(is_repeat_base=False))
    assert wrap_executable(m,'benchmark',executable,app)==([],[])
    assert len(list((resources.parent/'concrete').glob('*.json')))==1


def test_provider_affects_config_app_and_software_consistently(repository,native_spec,installed,monkeypatch,context):
    # Execute the actual patched fixture Experiment generation methods.
    def mod(name):
        m=types.ModuleType(name);monkeypatch.setitem(sys.modules,name,m)
        if '.' in name:
            parent,child=name.rsplit('.',1);monkeypatch.setattr(sys.modules[parent],child,m,raising=False)
        return m
    mod('ramble');mod('ramble.language');mod('ramble.language.language_base');mod('ramble.language.language_helpers')
    d=mod('benchpark.directives');d.variant=lambda *a,**kw:None;d.ExperimentSystemBase=type('DummyBase',(),{})
    v=mod('benchpark.variables');exec((repository/'tests/fixtures/upstream_like/lib/benchpark/variables.py').read_text(),v.__dict__)
    e=mod('benchpark.experiment');exec(transform('lib/benchpark/experiment.py',(repository/'tests/fixtures/upstream_like/lib/benchpark/experiment.py').read_text()),e.__dict__)
    from benchpark.variant import Variant
    native_spec.klass.variants[next(iter(native_spec.klass.variants))].update({
        'n_repeats':Variant('n_repeats','1','',values=str),'allocation':Variant('allocation','standard','',values=str)})
    spec=native_spec.spec.ExperimentSpec('smoke +container').concretize()
    class Fixture(e.Experiment):
        def compute_applications_section(self):
            self.add_experiment_variable('size',['16','32'],named=True,matrixed=True)
        def check_required_variables(self):return None
        def compute_package_section(self):raise AssertionError('Container provider must precede application software build')
    x=Fixture.__new__(Fixture)
    x.spec=spec;x.helpers=[];x.name='fixture';x._ramble_name=None;x._spack_name=None;x.workload=('smoke',);x.package_specs={};x._expr_vars=v.VariableDict()
    x.extension_contributions=(resolve(context),)
    result=x.compute_ramble_dict()['ramble']
    assert result['software']=={'packages':{},'environments':{}}
    app=result['applications']['fixture']['workloads']['smoke']['experiments']
    generated=next(iter(app.values()))
    assert generated['variants']['package_manager']=='user-managed'
    assert generated['matrix']==['size']
    assert 'bpce_plan' not in generated['variables']
    assert 'spack_flags' not in result['config']
    assert spec.variants['package_manager']==('user-managed',)
    assert 'package_manager' not in spec.plugin_state['extension_explicit']
