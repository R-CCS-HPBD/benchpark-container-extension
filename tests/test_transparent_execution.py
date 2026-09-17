# SPDX-License-Identifier: Apache-2.0
"""Explicit opt-in with no extra loader/activation required from benchmark users."""
from types import SimpleNamespace as S
from pathlib import Path
import json
import shutil
import sys
import types
import pytest
from benchpark_integration.api import ExtensionError
from benchpark_integration import discovery as extensions
from test_core_patch import native_spec
from test_generation_adapter import setup_adapter, Expander
from benchpark_container.ramble_adapter import wrap_executable


def test_legacy_default_cannot_enable_container(native_spec, installed):
    native_spec.klass.extension_defaults = {'container': True}
    request = native_spec.spec.ExperimentSpec('smoke size=32')
    assert "container" not in request.variants
    result = request.concretize()
    assert result.variants['package_manager'] == ('spack',)
    assert 'container' not in result.variants
    assert all(ep.loads == 0 for ep in installed if ep.group != "benchpark.lifecycle.v1")


def test_explicit_disable_keeps_spack(native_spec, installed):
    native_spec.klass.extension_defaults = {'container': True}
    result = native_spec.spec.ExperimentSpec('smoke ~container').concretize()
    assert result.variants['container'] == (False,)
    assert result.variants['package_manager'] == ('spack',)
    assert not result.plugin_state.get("extension_selection", {})
    assert all(ep.loads == 0 for ep in installed if ep.group != "benchpark.lifecycle.v1")


def test_missing_unselected_plugin_does_not_affect_native(native_spec, installed, monkeypatch):
    native_spec.klass.extension_defaults = {'container': True}
    monkeypatch.setattr(extensions.importlib.metadata, 'entry_points', lambda: {})
    result = native_spec.spec.ExperimentSpec('smoke').concretize()
    assert result.variants['package_manager'] == ('spack',)
    assert 'container' not in result.variants


def test_only_explicit_enable_selects_container(native_spec, installed):
    native_spec.klass.extension_defaults = {'container': True}
    a = native_spec.spec.ExperimentSpec('smoke').concretize()
    b = native_spec.spec.ExperimentSpec('smoke +container').concretize()
    c = native_spec.spec.ExperimentSpec('smoke').concretize()
    assert a.variants['package_manager'] == c.variants['package_manager'] == ('spack',)
    assert b.variants['container'] == (True,)
    assert b.variants['package_manager'] == ('user-managed',)
    assert b.plugin_state.get("extension_explicit", {}) == {'container': [True]}
    assert dict(a.variants.items()) == dict(c.variants.items())


@pytest.mark.parametrize('selection', ['', ' ~container'])
def test_native_explicit_package_manager_preserved(native_spec, installed, selection):
    result = native_spec.spec.ExperimentSpec('smoke package_manager=user-managed' + selection).concretize()
    assert result.variants['package_manager'] == ('user-managed',)
    assert all(ep.loads == 0 for ep in installed if ep.group != "benchpark.lifecycle.v1")


def test_unconfigured_native_unchanged(native_spec, installed):
    request = native_spec.spec.ExperimentSpec('smoke')
    assert "container" not in request.variants
    assert 'container' not in request.concretize().variants
    assert all(ep.loads == 0 for ep in installed if ep.group != "benchpark.lifecycle.v1")


def test_application_expander_not_modifier_workspace_variables(context, tmp_path):
    modifier, _, resources = setup_adapter(context, tmp_path)
    app = S(expander=modifier.expander)
    modifier.expander = Expander({'bpce_resources': '{workspace_root}/invalid'})
    executable = S(template=['python /bench/run.py --size {size}'], variables={}, mpi=False, run_in_background=False)
    wrap_executable(modifier, 'benchmark', executable, app)
    assert '{workspace_root}' not in executable.template[0]
    assert str(resources / 'runtime.pyz') in executable.template[0]


def test_staged_modifier_loader_does_not_require_dunder_file(repository, context, tmp_path, monkeypatch):
    modifier, _, resources = setup_adapter(context, tmp_path)
    src = repository / 'src/benchpark_container/resources'
    shutil.copyfile(src / 'runtime.pyz', resources / 'runtime.pyz')
    file = Path(modifier._file_path)
    shutil.copyfile(src / 'modifiers/bpce-execution/modifier.py', file)
    fake = types.ModuleType('ramble.modkit')
    fake.BasicModifier = type('BasicModifier', (), {})
    fake.mode = fake.default_mode = fake.executable_modifier = lambda *a, **k: None
    fake.__all__ = ['BasicModifier', 'mode', 'default_mode', 'executable_modifier']
    monkeypatch.setitem(sys.modules, 'ramble', types.ModuleType('ramble'))
    monkeypatch.setitem(sys.modules, 'ramble.modkit', fake)
    scope = {'__name__': 'test_modifier_no_file'}  # no __file__ on purpose
    exec(compile(file.read_text(), str(file), 'exec'), scope)
    m = scope['BpceExecution']()
    m._file_path, m.expander = modifier._file_path, modifier.expander
    executable = S(template=['true'], variables={}, mpi=False, run_in_background=False)
    assert m.wrap_benchmark('benchmark', executable) == ([], [])
    assert 'runtime.pyz' in executable.template[0]


def test_repo_layout_and_resource_snapshot_contract(repository):
    import yaml
    resources = repository / 'src/benchpark_container/resources'
    assert yaml.safe_load((resources/'modifiers/repo.yaml').read_text())['repo']['subdirectory'] == '.'
    storage = (repository/'src/benchpark_integration/support/storage.py').read_text()
    assert '"modifier_repos.yaml"' in storage and 'extension-repositories.yaml' not in storage


def test_explicit_disable_of_declared_feature_without_plugin(native_spec, installed, monkeypatch):
    native_spec.klass.extension_defaults = {'container': True}
    monkeypatch.setattr(extensions.importlib.metadata, 'entry_points', lambda: {})
    spec = native_spec.spec.ExperimentSpec('smoke ~container').concretize()
    assert spec.variants['container'] == (False,)
    assert spec.variants['package_manager'] == ('spack',)


@pytest.mark.parametrize('statuses,valid', [
    (['COMPLETED'] * 4, True),
    (['COMPLETED'] * 3 + ['PREPARATION_FAILED'], False),
    (['COMPLETED'] * 3, False),
    ([], False),
])
def test_real_verifier_does_not_count_failed_or_missing_retry_as_success(tmp_path, statuses, valid):
    import json
    from verify_upstream import require_completed_attempts
    paths = []
    for index, status in enumerate(statuses):
        p = tmp_path / ('cer-%d.json' % index)
        p.write_text(json.dumps({'status': status}))
        paths.append(p)
    if valid:
        require_completed_attempts(paths)
    else:
        with pytest.raises(RuntimeError):
            require_completed_attempts(paths)


def test_common_base_smoke_uses_declared_python_contract(repository):
    """The shipped real-runtime fixture must use the Common Base Python contract."""
    app = (repository / 'examples/applications/common-base-smoke/application.py').read_text()
    assert "'{bpce_python} /bench/benchmark.py" in app
    assert "'python3 /bench/benchmark.py" not in app
    assert "'python /bench/benchmark.py" not in app
