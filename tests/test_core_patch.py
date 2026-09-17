# SPDX-License-Identifier: Apache-2.0
"""Native parser/variant code with labeled dependency doubles, NOT upstream E2E."""
import ast
from collections.abc import MutableMapping
import copy
import importlib.util
import json
from pathlib import Path
import pickle
import shutil
import subprocess
import sys
import types
import pytest
from benchpark_integration import discovery as ext
from benchpark_integration.api import ExtensionError
from patch_core import make_patch, transform, TARGETS, API_FILES

class HashableMap(MutableMapping):
    def __init__(self):self.dict={}
    def __getitem__(self,k):return self.dict[k]
    def __setitem__(self,k,v):self.dict[k]=v
    def __delitem__(self,k):del self.dict[k]
    def __iter__(self):return iter(self.dict)
    def __len__(self):return len(self.dict)
    def __hash__(self):return hash(tuple(sorted(self.dict.items())))
    def __eq__(self,other):return hasattr(other,'dict') and self.dict==other.dict

@pytest.fixture
def native_spec(repository,monkeypatch,tmp_path):
    # Real parser and concretizer from the documented fixture. Only missing
    # repository/dependency infrastructure is doubled. Never called upstream.
    def module(name):
        m=types.ModuleType(name);monkeypatch.setitem(sys.modules,name,m)
        if '.' in name:
            parent,attr=name.rsplit('.',1)
            monkeypatch.setattr(sys.modules[parent],attr,m,raising=False)
        return m
    module('llnl');module('llnl.util');lang=module('llnl.util.lang')
    lang.HashableMap=HashableMap;lang.dedupe=lambda seq:list(dict.fromkeys(seq))
    import benchpark
    repo=module('benchpark.repo');repo.ObjectTypes=types.SimpleNamespace(experiments='experiments',systems='systems')
    classes={}
    lookup=types.SimpleNamespace(get_obj_class=lambda n:classes[n],filename_for_object_name=lambda n:str(tmp_path/n/'experiment.py'))
    repo.paths={'experiments':lookup,'systems':lookup}
    for name in ('error','variant','spec'):
        m=module('benchpark.'+name)
        text=(repository/'tests/fixtures/upstream_like/lib/benchpark'/f'{name}.py').read_text()
        if name=='spec':text=transform('lib/benchpark/spec.py',text)
        exec(compile(text,'fixture_'+name+'.py','exec'),m.__dict__)
    spec=sys.modules['benchpark.spec'];V=sys.modules['benchpark.variant'].Variant
    C=type('FixtureExperiment',(),{'namespace':'fixture','variants':{spec.Spec():{
        'workload':V('workload','main','',values=('main',)),
        'package_manager':V('package_manager','spack','',values=('spack','user-managed')),
        'size':V('size','16','',values=int,multi=True),
        'tune':V('tune','none','',values=str),
        'backend':V('backend','fixture','',values=('fixture',)),
        }},'extension_request_settings':{'tune':{'allowed':{'size':{'min':1,'max':64}}}}})
    classes['smoke']=C
    (tmp_path/'smoke'/'tuning').mkdir(parents=True)
    return types.SimpleNamespace(spec=spec,klass=C,root=tmp_path/'smoke',classes=classes)


def test_native_variants_no_container_plugin_loaded(native_spec,installed):
    s=native_spec.spec.ExperimentSpec('smoke size=16').concretize()
    assert s.variants['size']==('16',) and 'container' not in s.variants
    assert all(e.loads==0 for e in installed if e.group != "benchpark.lifecycle.v1")


def test_container_preconcretize_schema_not_global(native_spec,installed):
    E=native_spec.spec.ExperimentSpec
    a=E('smoke +container').concretize();b=E('smoke').concretize()
    assert a.variants['container']==(True,)
    assert a.variants['container_image']==('default',)
    assert 'container' not in b.variants
    assert 'container' not in next(iter(native_spec.klass.variants.values()))
    assert a.plugin_state.get("extension_explicit", {})=={'container':[True]}
    with pytest.raises(TypeError):a.variants['container']=False


def test_disabled_extension_no_load(native_spec,installed):
    s=native_spec.spec.ExperimentSpec('smoke ~container').concretize()
    assert s.variants['container']==(False,)
    assert not s.plugin_state.get("extension_selection", {}) and all(e.loads==0 for e in installed if e.group != "benchpark.lifecycle.v1")


def test_saved_extension_schema_can_read_without_package(native_spec,installed,monkeypatch):
    E=native_spec.spec.ExperimentSpec
    a=E('smoke +container').concretize()
    raw=pickle.dumps(a)
    monkeypatch.setattr(ext.importlib.metadata,'entry_points',lambda:{})
    b=pickle.loads(raw)
    assert b.variants['container']==(True,)
    assert b.plugin_state.get("extension_schema", {})==a.plugin_state.get("extension_schema", {})


def test_missing_extension_rejects_container(native_spec,installed,monkeypatch):
    monkeypatch.setattr(ext.importlib.metadata,'entry_points',lambda:{})
    with pytest.raises(Exception,match='not a valid variant'):
        native_spec.spec.ExperimentSpec('smoke +container').concretize()


def test_fixed_tune_merge_and_explicit_provenance(native_spec,installed):
    (native_spec.root/'tuning'/'large.yaml').write_text('schema_version: 1\noverrides:\n  size: 32\n')
    s=native_spec.spec.ExperimentSpec('smoke +container tune=large').concretize()
    assert s.variants['size']==('32',) and 'size' not in s.plugin_state.get("extension_explicit", {})
    assert s.plugin_state.get("preparation_records", {})['tune']['overrides']=={'size':32}

@pytest.mark.parametrize('contents,spec_text',[
    ('schema_version: 1\noverrides: {backend: other}\n','smoke tune=x'),
    ('schema_version: 1\noverrides: {size: 1000}\n','smoke tune=x'),
    ('schema_version: 1\noverrides: {size: 32}\n','smoke tune=x size=16'),
    ('schema_version: 1\nsearch: {}\n','smoke tune=x'),
    ('schema_version: 1\napplies_to: {backend: missing}\noverrides: {size: 32}\n','smoke tune=x')])
def test_tune_conflict_and_policy(native_spec,installed,contents,spec_text):
    (native_spec.root/'tuning'/'x.yaml').write_text(contents)
    with pytest.raises(ExtensionError):native_spec.spec.ExperimentSpec(spec_text).concretize()


def test_tune_equal_cli_accepted(native_spec,installed):
    (native_spec.root/'tuning'/'x.yaml').write_text('schema_version: 1\noverrides: {size: 32}\n')
    s=native_spec.spec.ExperimentSpec('smoke tune=x size=32').concretize()
    assert s.variants['size']==('32',)


def test_patch_roundtrip_and_source_unchanged(repository,tmp_path):
    source=repository/'tests/fixtures/upstream_like'; target=tmp_path/'checkout';shutil.copytree(source,target)
    before={n:(target/n).read_bytes() for n in TARGETS}
    subprocess.run(['git','init','-q',str(target)],check=True)
    patch,records=make_patch(target)
    assert len(records)==len(TARGETS)+len(API_FILES)
    assert all((target/n).read_bytes()==v for n,v in before.items())
    subprocess.run(['git','-C',str(target),'apply','--check','-'],input=patch,text=True,check=True)
    subprocess.run(['git','-C',str(target),'apply','-'],input=patch,text=True,check=True)
    for n in TARGETS:ast.parse((target/n).read_text())
    subprocess.run(['git','-C',str(target),'apply','--reverse','--check','-'],input=patch,text=True,check=True)
    subprocess.run(['git','-C',str(target),'apply','--reverse','-'],input=patch,text=True,check=True)
    assert all((target/n).read_bytes()==v for n,v in before.items())
    assert not (target/'lib/benchpark/plugins.py').exists()



def test_fn_apps_non_spack_setup_initializes_spack_env_cmd_once(repository):
    """Model FN_apps: initializer references spack_env_cmd outside Spack branch."""
    path = repository / 'tests/fixtures/upstream_like/lib/benchpark/cmd/setup.py'
    original = path.read_text()
    modeled = original.replace(
        '    if "spack" in pkg_manager:\n',
        '    if "spack" in pkg_manager:\n        spack_env_cmd = "/site/spack-mirror.sh"\n',
        1,
    ).replace(
        '. {per_workspace_setup.ramble_location}/share/ramble/setup-env.sh\n""")',
        '. {per_workspace_setup.ramble_location}/share/ramble/setup-env.sh\n{spack_env_cmd}\n""")',
        1,
    )
    assert '{spack_env_cmd}' in modeled
    patched = transform('lib/benchpark/cmd/setup.py', modeled)
    needle = '    spack_env_cmd = ""\n    if "spack" in pkg_manager:\n'
    assert patched.count(needle) == 1
    ast.parse(patched)


def test_non_spack_setup_compatibility_does_not_touch_upstream_without_reference(repository):
    path = repository / 'tests/fixtures/upstream_like/lib/benchpark/cmd/setup.py'
    original = path.read_text()
    assert '{spack_env_cmd}' not in original
    patched = transform('lib/benchpark/cmd/setup.py', original)
    assert '    spack_env_cmd = ""\n    if "spack" in pkg_manager:\n' not in patched

def test_patch_cli_help_does_not_bootstrap(repository,tmp_path):
    source=repository/'tests/fixtures/upstream_like';target=tmp_path/'checkout';shutil.copytree(source,target)
    # Main imports paths and bootstrap helpers even before command init. This
    # test intentionally checks only patch syntax, not a fake full bootstrap.
    for n in TARGETS:
        p=target/n;p.write_text(transform(n,p.read_text()))
    main=(target/'lib/main.py').read_text()
    assert main.index('register_commands(subparsers, actions, sys.argv[1:])') > main.index('    init_commands(subparsers, actions)')
    assert 'import benchpark_container' not in main


def test_unknown_layout_fails_closed(repository,tmp_path):
    target=tmp_path/'checkout';shutil.copytree(repository/'tests/fixtures/upstream_like',target)
    p=target/'lib/benchpark/cmd/experiment.py';before=p.read_text();p.write_text(before.replace('experiment.system_spec = system_spec','experiment.system_spec = new_system'))
    snapshot=p.read_bytes()
    with pytest.raises(ValueError,match='Unsupported upstream layout'):make_patch(target)
    assert p.read_bytes()==snapshot


def test_core_does_not_import_container_implementation(repository):
    for p in (repository/'core/files').rglob('*.py'):
        tree=ast.parse(p.read_text())
        imports=[n.module for n in ast.walk(tree) if isinstance(n,ast.ImportFrom)]
        imports += [a.name for n in ast.walk(tree) if isinstance(n,ast.Import) for a in n.names]
        assert not any(n and ('benchpark_container' in n or 'apptainer' in n) for n in imports)


def test_existing_native_tune_not_hijacked(native_spec,installed):
    native_spec.klass.extension_request_settings={}
    s=native_spec.spec.ExperimentSpec('smoke tune=custom-native-behavior')
    assert "container" not in s.variants
    assert s.concretize().variants['tune']==('custom-native-behavior',)
