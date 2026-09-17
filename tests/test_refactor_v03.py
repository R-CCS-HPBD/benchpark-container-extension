from ports import generation_port
# SPDX-License-Identifier: Apache-2.0
"""Regression tests for the reviewed architecture, not a real GPU claim."""
import argparse
import ast
from dataclasses import replace
import json
from pathlib import Path
import shutil
from types import SimpleNamespace as S
import pytest
from benchpark_integration import discovery as manager
from benchpark_integration.api import (API_VERSION, ExtensionDescriptor, ConfigurationContribution,
    SoftwareProvider, ExtensionError, plain)
from benchpark_integration.support.generation import (RambleGeneration, validate_contributions,
    resolve_experiment)
from benchpark_integration.support import storage
from benchpark_container.resolver import resolve
from benchpark_container.requirements import pinned_requirements
from benchpark_container.runtime import validate_layer_inventory, EnvironmentBuildError
from benchpark_container.util import ValidationError
from conftest import EP, changed
from test_core_patch import native_spec
from patch_core import transform


def test_manager_has_only_discovery(repository):
    tree=ast.parse((repository/'src/benchpark_integration/discovery.py').read_text())
    assert {n.name for n in tree.body if isinstance(n,ast.FunctionDef)}=={'entries','load_unique'}


def test_native_package_manager_reads_are_not_rewritten(repository):
    name='lib/benchpark/experiment.py'
    before=(repository/'tests/fixtures/upstream_like'/name).read_text()
    after=transform(name,before)
    expression='self.spec.variants["package_manager"][0]'
    assert before.count(expression)==after.count(expression)>=4
    before_tree=ast.parse(before);after_tree=ast.parse(after)
    b=next(n for n in before_tree.body if isinstance(n,ast.ClassDef) and n.name=='Experiment')
    a=next(n for n in after_tree.body if isinstance(n,ast.ClassDef) and n.name=='Experiment')
    for method in b.body:
        if isinstance(method,ast.FunctionDef) and method.name!='compute_ramble_dict':
            other=next(n for n in a.body if isinstance(n,ast.FunctionDef) and n.name==method.name
                       and ast.dump(ast.Module(body=n.decorator_list, type_ignores=[]))==ast.dump(ast.Module(body=method.decorator_list,type_ignores=[])))
            assert ast.dump(method)==ast.dump(other),method.name


def test_core_has_no_tuning_policy_or_container_constants(repository):
    for f in (repository/'core/files').rglob('*.py'):
        tree=ast.parse(f.read_text())
        imports=[n.module for n in ast.walk(tree) if isinstance(n,ast.ImportFrom)]
        assert all(not n or not n.startswith(('benchpark_container','benchpark_tuning')) for n in imports)
        literals={n.value for n in ast.walk(tree) if isinstance(n,ast.Constant) and isinstance(n.value,str)}
        assert not ({'apptainer','docker','container','tune','tuning_policy','user-managed','spack'} & literals),f


def test_required_native_option_is_set_once_without_changing_input(native_spec,installed):
    request=native_spec.spec.ExperimentSpec('smoke +container')
    s=request.concretize()
    assert 'package_manager' not in request.variants
    assert s.variants['package_manager']==('user-managed',)
    assert 'package_manager' not in s.plugin_state.get("extension_explicit", {})
    assert request.variants.get("container") == (True,)
    assert s.satisfies('package_manager=user-managed')


def test_explicit_matching_package_manager_accepted(native_spec,installed):
    s=native_spec.spec.ExperimentSpec('smoke +container package_manager=user-managed').concretize()
    assert s.variants['package_manager']==('user-managed',)


def test_explicit_conflicting_pm_rejected_before_native_build(native_spec,installed):
    with pytest.raises(ExtensionError,match='Explicit option conflicts'):
        native_spec.spec.ExperimentSpec('smoke +container package_manager=spack').concretize()


def test_two_independent_extensions_can_coexist(native_spec,installed):
    installed.append(EP('probe','benchpark.extensions',lambda:ExtensionDescriptor(
        'probe',API_VERSION,(),lambda c: ConfigurationContribution('probe',{}),
        requires_system=False,requires_declaration=False)))
    from benchpark_integration.hooks import provider
    installed.append(EP('probe','benchpark.lifecycle.v1',provider))
    s=native_spec.spec.ExperimentSpec('smoke +container +probe').concretize()
    assert s.plugin_state.get("extension_selection", {})==['container','probe']


def test_two_extensions_disagreeing_native_defaults_rejected(native_spec,installed):
    installed.append(EP('probe','benchpark.extensions',lambda:ExtensionDescriptor(
        'probe',API_VERSION,(),lambda c:None,required_variants={'package_manager':'spack'})))
    from benchpark_integration.hooks import provider
    installed.append(EP('probe','benchpark.lifecycle.v1',provider))
    with pytest.raises(ExtensionError,match='native-option conflict'):
        native_spec.spec.ExperimentSpec('smoke +container +probe').concretize()


def test_multiple_contributions_and_single_exclusive_software_owner(context,tmp_path):
    c=resolve(context)
    extra=ConfigurationContribution('observer',{'enabled':True},variables={'observe':'1'})
    assert validate_contributions((c,extra))==c.software_provider
    with pytest.raises(ExtensionError,match='software-section providers'):
        validate_contributions((c,replace(extra,software_provider=SoftwareProvider({}))))
    source=tmp_path/'source';source.mkdir();(source/'ramble.yaml').write_text('ramble: {}\n')
    system=tmp_path/'system';system.mkdir()
    storage.publish_experiment((c,extra),source,system)
    manifest=storage.verify_manifest(source)
    assert manifest['owners']==['container','observer']
    assert (source/storage.STATE_DIR/'plans/observer.json').is_file()


def test_disabled_helper_empty_named_environment_not_a_dependency(context):
    ex=S(extension_contributions=(resolve(context),),helpers=[S(compute_package_section=lambda:
             {'packages':{},'environments':{'affinity':{'packages':[]}}})])
    assert generation_port(ex).software(lambda:pytest.fail('must not build'))=={'packages':{},'environments':{}}


def test_active_helper_environment_not_discarded(context):
    ex=S(extension_contributions=(resolve(context),),helpers=[S(compute_package_section=lambda:
             {'packages':{},'environments':{'profiler':{'packages':['profiler']}}})])
    with pytest.raises(ExtensionError,match='host/helper'):
        generation_port(ex).software(lambda:pytest.fail('must not build'))


def test_tune_works_without_container(native_spec,installed):
    (native_spec.root/'tuning/x.yaml').write_text('schema_version: 1\noverrides: {size: 32}\n')
    s=native_spec.spec.ExperimentSpec('smoke tune=x').concretize()
    assert s.variants['size']==('32',) and s.variants['package_manager']==('spack',)
    assert s.plugin_state.get("preparation_records", {})['tune']['overrides']=={'size':32}
    assert installed[0].loads==0


def test_cli_uses_actual_native_registry_even_future_commands(installed):
    from benchpark_integration.support.cli import register_commands
    p=argparse.ArgumentParser();sub=p.add_subparsers(dest='subcommand')
    sub.add_parser('cer')  # Future upstream takes this name; native must win.
    native=lambda args:17;actions={'cer':native}
    register_commands(sub,actions,['cer'])
    assert actions['cer'] is native and installed[1].loads==0


def test_selected_command_passes_normal_config(installed):
    from benchpark_integration.support.cli import register_commands
    p=argparse.ArgumentParser();p.add_argument('-C','--config')
    sub=p.add_subparsers(dest='subcommand');sub.add_parser('system');actions={}
    register_commands(sub,actions,['-C','/chosen','cer','list','/tmp/runs'])
    a=p.parse_args(['-C','/chosen','cer','list','/tmp/runs'])
    assert a.config=='/chosen' and a.cer_action=='list'
    assert installed[0].loads==0 and installed[1].loads==1


def test_unknown_native_feature_does_not_load_external_module(native_spec,installed):
    with pytest.raises(Exception,match='not a valid variant'):
        native_spec.spec.ExperimentSpec('smoke +not_installed').concretize()
    assert all(e.loads==0 for e in installed if e.group != "benchpark.lifecycle.v1")


@pytest.mark.parametrize('requirement',['vllm','vllm>=0.1','vllm==0.1.*','vllm~=0.1','vllm @ https://x/pkg.whl'])
def test_unpinned_requirement_rejected(context,requirement):
    p=Path(context.source_root)/'requirements.txt';p.write_text(requirement+'\n')
    with pytest.raises(ValidationError):resolve(context)


def test_fixed_requirements_and_nested_constraints_are_retained(context):
    root=Path(context.source_root)
    (root/'requirements.txt').write_text('demo-pkg==1.2.3\n-c lock.txt\n')
    (root/'lock.txt').write_text('transitive-dep==4.5.6\n')
    p=resolve(context).payload
    assert p['requirement_pins']=={'demo-pkg':['1.2.3'],'transitive-dep':['4.5.6']}
    assert 'inputs/lock.txt' in p['resources']


def test_marker_versions_are_not_resolved_on_host(context):
    root=Path(context.source_root)
    (root/'requirements.txt').write_text('demo==1.0; platform_machine == "aarch64"\ndemo==2.0; platform_machine == "x86_64"\n')
    assert pinned_requirements(root,['requirements.txt'])=={'demo':['1.0','2.0']}


def test_unpinned_transitive_install_rejected(tmp_path):
    layer=[{'name':'Demo','version':'1.0','content_sha256':'a'*64},
           {'name':'New_dep','version':'7.0','content_sha256':'b'*64}]
    with pytest.raises(EnvironmentBuildError,match='Unpinned additional/transitive'):
        validate_layer_inventory({},layer,{'demo':['1.0']})
    validate_layer_inventory({},layer,{'demo':['1.0'],'new-dep':['7.0']})


def test_configure_examples_writes_relative_repo_paths(tmp_path):
    import yaml
    from configure_examples import create_config
    root=tmp_path/'bp';(root/'config').mkdir(parents=True)
    groups={kind:['../'+kind] for kind in ('systems','experiments','applications','packages')}
    (root/'config/repos.yaml').write_text(yaml.safe_dump({'repos':groups}))
    output=tmp_path/'cfg';create_config(root,output,tmp_path/'bootstrap')
    values=yaml.safe_load((output/'repos.yaml').read_text())['repos']
    for kind, paths in values.items():
        assert all(not Path(x).is_absolute() for x in paths)
        assert (output/paths[-1]).resolve()==root/kind


def test_stream_baseline_does_not_assume_openmp(repository):
    text=(repository/'tools/verify_upstream.py').read_text()
    assert 'stream +openmp' not in text
    assert text.count('stream package_manager=user-managed')==2


def test_single_generation_stage_applies_to_every_template(context):
    c=resolve(context)
    data={'ramble':{'modifiers':[], 'applications':{'a':{'workloads':{'w':{'experiments':{
        'one':{'variables':{'size':'16'},'env_vars':{'set':{}}},
        'two':{'variables':{'size':'32'},'env_vars':{'set':{}}}}}}}}}}
    result=generation_port(S(extension_contributions=(c,),helpers=[])).finish(data)
    values=result['ramble']['applications']['a']['workloads']['w']['experiments']
    assert all('bpce_plan' not in e['variables'] for e in values.values())
    assert {'name': 'bpce-execution', 'mode': 'standard'} in result['ramble']['modifiers']
    assert values['one']['variables']['size']=='16' and values['two']['variables']['size']=='32'


def test_concrete_plan_survives_native_experiment_cleanup(context,tmp_path):
    import shlex
    from test_generation_adapter import setup_adapter
    from benchpark_container.ramble_adapter import wrap_executable
    modifier,run,resources=setup_adapter(context,tmp_path)
    e=S(template=['python /bench/run.py --size {size}'],variables={},mpi=False,run_in_background=False)
    wrap_executable(modifier,'benchmark',e)
    args=shlex.split(e.template[0]);spec=Path(args[args.index('--spec')+1])
    before=spec.read_bytes()
    shutil.rmtree(run)
    assert spec.read_bytes()==before and run not in spec.parents


def test_anonymous_spec_preserves_native_error(native_spec,installed):
    with pytest.raises(Exception,match='anonymous'):
        native_spec.spec.ExperimentSpec().concretize()


def test_legacy_tune_declaration_cannot_silently_run_base(context):
    v=plain(context.variants);v['tune']=['inference-mode']
    with pytest.raises(ValidationError,match='Tune was not prepared'):
        resolve(changed(context,variants=v))


def test_requirements_cannot_enable_unpinned_source_build(context):
    p=Path(context.source_root)/'requirements.txt'
    p.write_text('--only-binary :none:\ndemo==1.0\n')
    with pytest.raises(ValidationError,match='binary wheels'):
        resolve(context)
