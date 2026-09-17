# SPDX-License-Identifier: Apache-2.0
"""The deployed loader/ports, not only external helpers, exercise these gates."""
import argparse
import ast
import json
from pathlib import Path
from types import SimpleNamespace as S
import pytest
from benchpark import plugins
from benchpark_integration.api import (API_VERSION, ConfigurationContribution,
                                      ExtensionDescriptor, ExtensionError, OptionSpec)
from benchpark_integration.hooks import provider
from benchpark_integration.support.generation import resolve_experiment
from benchpark_integration.support.io import save_system_snapshot
from benchpark_integration.support.storage import STATE_DIR
from conftest import EP, EPs, changed
from test_core_patch import native_spec
from test_storage_runtime import publish
from patch_core import transform


def test_core_is_single_stdlib_loader_not_a_relocated_core_framework(repository):
    files = sorted((repository/'core/files/lib/benchpark').rglob('*.py'))
    assert [p.name for p in files] == ['plugins.py']
    assert len(files[0].read_text().splitlines()) <= 180  # Bounded seam, not a LOC optimization objective.
    tree = ast.parse(files[0].read_text())
    import sys
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            assert all(a.name.split('.')[0] in sys.stdlib_module_names for a in node.names)
        if isinstance(node, ast.ImportFrom):
            assert node.module.split('.')[0] in sys.stdlib_module_names
    assert not (repository/'core/files/lib/benchpark/extension_api.py').exists()
    assert not (repository/'core/files/lib/benchpark/extension_support').exists()


def test_no_live_core_import_from_any_external_implementation(repository):
    for p in (repository/'src').rglob('*.py'):
        for n in ast.walk(ast.parse(p.read_text())):
            if isinstance(n, ast.ImportFrom) and n.module:
                assert not n.module.startswith('benchpark.'), p
            if isinstance(n, ast.Import):
                assert not any(a.name == 'benchpark' or a.name.startswith('benchpark.') for a in n.names), p


def test_native_concretization_algorithm_ast_identical(repository):
    p='lib/benchpark/spec.py'
    before=(repository/'tests/fixtures/upstream_like'/p).read_text()
    after=transform(p,before)
    def methods(text):
        tree=ast.parse(text)
        for cls in tree.body:
            if isinstance(cls,ast.ClassDef) and cls.name=='ConcreteSpec':
                return {m.name:ast.dump(m) for m in cls.body if isinstance(m,ast.FunctionDef)}
    assert methods(before)==methods(after)


def test_same_named_native_option_not_hijacked_by_provider(native_spec,installed):
    # An upstream-native option is authoritative. Merely installing a similarly
    # named provider may not silently switch its meaning.
    from benchpark.variant import Variant
    group=next(iter(native_spec.klass.variants.values()))
    group['container']=Variant('container',False,'native option',values=(True,False))
    result=native_spec.spec.ExperimentSpec('smoke +container').concretize()
    assert result.variants['package_manager']==('spack',)
    assert installed[0].loads==0


def test_no_provider_installed_native_path_does_not_load_anything(native_spec,monkeypatch):
    monkeypatch.setattr(plugins.importlib.metadata,'entry_points',lambda:EPs())
    before=native_spec.spec.ExperimentSpec('smoke').concretize()
    assert before.variants['package_manager']==('spack',)
    assert plugins.invoke('generation',(),default='original')=='original'


def test_broken_provider_is_not_imported_for_native_or_help(native_spec,installed,capsys):
    def broken():raise AssertionError('Do not import inactive provider')
    for ep in installed:
        if ep.group=='benchpark.lifecycle.v1':ep.factory=broken
    s=native_spec.spec.ExperimentSpec('smoke').concretize()
    assert s.variants['package_manager']==('spack',)
    plugins.print_extension_help()
    assert 'container' in capsys.readouterr().out
    with pytest.raises(plugins.PluginError,match='Cannot load'):
        native_spec.spec.ExperimentSpec('smoke +container').concretize()


def test_missing_plugin_cannot_destroy_old_workspace(context,tmp_path,installed,monkeypatch):
    _,source,_=publish(context,tmp_path)
    output=tmp_path/'out';output.mkdir();sentinel=output/'previous-result';sentinel.write_text('keep')
    owners=plugins.owners_at(source,output/'workspace')
    assert owners==['container']
    monkeypatch.setattr(plugins.importlib.metadata,'entry_points',lambda:EPs())
    with pytest.raises(plugins.PluginError,match='Expected one lifecycle'):
        plugins.invoke('workspace',owners,source=str(source),output=str(output))
    assert sentinel.read_text()=='keep'


def test_installed_plugin_guard_runs_before_deletion(context,tmp_path,installed):
    _,source,_=publish(context,tmp_path)
    out=tmp_path/'out';out.mkdir();sentinel=out/'result';sentinel.write_text('keep')
    with pytest.raises(ExtensionError,match='Refusing'):
        plugins.invoke('workspace',plugins.owners_at(source,out/'workspace'),source=str(source),output=str(out))
    assert sentinel.read_text()=='keep'


@pytest.mark.parametrize('body',['{}','{"owners": []}','{"owners": "container"}','{"owners": ["../bad"]}','{'])
def test_corrupt_routing_marker_fails_before_native_deletion(tmp_path,body):
    p=tmp_path/STATE_DIR/'manifest.json';p.parent.mkdir();p.write_text(body)
    with pytest.raises(plugins.PluginError):plugins.owners_at(tmp_path)


def test_provider_api_mismatch_fails_closed(installed):
    for ep in installed:
        if ep.group=='benchpark.lifecycle.v1':ep.factory=lambda:S(api_version=999)
    with pytest.raises(plugins.PluginError,match='Unsupported plugin API'):
        plugins.invoke('request',['container'])


def test_duplicate_route_fails_closed(installed):
    installed.append(installed[3])
    with pytest.raises(plugins.PluginError,match='found 2'):
        plugins.invoke('request',['container'])


def test_distinct_coordinators_fail_not_last_wins(installed):
    installed.append(EP('other','benchpark.lifecycle.v1',lambda:S(api_version=1)))
    with pytest.raises(plugins.PluginError,match='Conflicting lifecycle coordinators'):
        plugins.invoke('request',['container','other'])


def test_independent_noncontainer_provider_uses_same_core_loader(monkeypatch):
    calls=[]
    class ReportProvider:
        api_version=1
        def handle(self,event,context):
            calls.append((event,context))
            return {'report_format':context['format']}
    eps=EPs([EP('report','benchpark.lifecycle.v1',ReportProvider)])
    monkeypatch.setattr(plugins.importlib.metadata,'entry_points',lambda:eps)
    assert plugins.invoke('resolve',['report'],format='json')=={'report_format':'json'}
    assert calls==[('resolve',{'format':'json'})]


def test_independent_cli_provider_and_future_native_name_priority(monkeypatch):
    loaded=[]
    def command():
        loaded.append(True)
        return S(api_version=1,help='External report',setup_parser=lambda p:p.add_argument('--format',default='json'),handler=lambda a:0)
    eps=EPs([EP('report','benchpark.cli.v1',command)])
    monkeypatch.setattr(plugins.importlib.metadata,'entry_points',lambda:eps)
    p=argparse.ArgumentParser();sub=p.add_subparsers(dest='action');actions={}
    plugins.register_commands(sub,actions,['report','--format','text'])
    assert p.parse_args(['report','--format','text']).format=='text'
    assert loaded==[True]
    p=argparse.ArgumentParser();sub=p.add_subparsers(dest='action');sub.add_parser('report')
    native=lambda a:17;actions={'report':native};loaded.clear()
    plugins.register_commands(sub,actions,['report'])
    assert actions['report'] is native and loaded==[]


def test_two_features_compose_over_same_broker(native_spec,installed):
    installed.append(EP('observe','benchpark.extensions',lambda:ExtensionDescriptor(
        'observe',API_VERSION,(OptionSpec('sample_rate','10',kind='integer'),),
        lambda c:ConfigurationContribution('observe',{},variables={'observe_rate':'10'}),
        requires_system=False,requires_declaration=False)))
    installed.append(EP('observe','benchpark.lifecycle.v1',provider))
    spec=native_spec.spec.ExperimentSpec('smoke +container +observe sample_rate=12').concretize()
    assert spec.plugin_state['extension_selection']==['container','observe']
    assert spec.variants['sample_rate']==('12',)


@pytest.mark.parametrize('value',['abc','1,2'])
def test_extension_option_validation_not_delegated_to_native_schema(native_spec,installed,value):
    installed.append(EP('observe','benchpark.extensions',lambda:ExtensionDescriptor(
        'observe',API_VERSION,(OptionSpec('sample_rate','10',kind='integer'),),lambda c:None)))
    installed.append(EP('observe','benchpark.lifecycle.v1',provider))
    with pytest.raises(ExtensionError):
        native_spec.spec.ExperimentSpec('smoke +observe sample_rate='+value).concretize()


def test_resolution_port_receives_data_not_native_object(context,tmp_path,installed):
    system=tmp_path/'system';system.mkdir()
    from benchpark_integration.api import plain
    save_system_snapshot({'container':plain(context.system)},system)
    source=context.source_root
    input_data=dict(name='common-base-smoke',variants=plain(context.variants),
        state={'extension_selection':['container'],'extension_explicit':{'container':[True]},'preparation_records':{}},
        system_dir=str(system),settings={'container':plain(context.requirements)},source_root=source)
    snapshot=json.dumps(input_data,sort_keys=True)
    result=plugins.invoke('experiment_resolve',['container'],**input_data)
    assert len(result)==1 and result[0].payload['base']['logical_name']=='direct'
    assert json.dumps(input_data,sort_keys=True)==snapshot


def test_native_experiment_methods_unchanged_except_generation_boundary(repository):
    path='lib/benchpark/experiment.py'
    before=(repository/'tests/fixtures/upstream_like'/path).read_text()
    after=transform(path,before)
    def methods(source):
        for cls in ast.parse(source).body:
            if isinstance(cls,ast.ClassDef) and cls.name=='Experiment':
                return {n.name:ast.dump(n) for n in cls.body if isinstance(n,ast.FunctionDef)}
        raise AssertionError('Fixture Experiment class is missing')
    left,right=methods(before),methods(after)
    assert set(left)==set(right)
    assert {name for name in left if left[name]!=right[name]}=={'compute_ramble_dict'}
