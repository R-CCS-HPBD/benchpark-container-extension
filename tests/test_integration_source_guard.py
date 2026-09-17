# SPDX-License-Identifier: Apache-2.0
"""Stale broker detection in the real-upstream verifier, before it can bootstrap."""
import importlib
import importlib.metadata
from pathlib import Path
from types import SimpleNamespace
import pytest
from verify_upstream import verify_installed_source


@pytest.mark.parametrize('stale', [None, 'benchpark_container', 'benchpark_integration', 'benchpark_tuning'])
def test_verifier_checks_all_deployed_python_packages(tmp_path, monkeypatch, stale):
    modules = {}
    root = tmp_path / 'source'
    for name in ('benchpark_container', 'benchpark_integration', 'benchpark_tuning'):
        source = root / 'src' / name
        source.mkdir(parents=True)
        (source / '__init__.py').write_text('version = 1\n')
        target = tmp_path / 'installed' / name
        target.mkdir(parents=True)
        (target / '__init__.py').write_text('version = 0\n' if name == stale else 'version = 1\n')
        modules[name] = SimpleNamespace(__file__=str(target / '__init__.py'), __version__='test')
    monkeypatch.setattr(importlib, 'import_module', lambda name: modules[name])
    monkeypatch.setattr(importlib.metadata, 'version', lambda name: 'test')
    if stale:
        with pytest.raises(RuntimeError, match=stale):
            verify_installed_source(root)
    else:
        result = verify_installed_source(root)
        assert result['source_match'] == 'passed'
        assert set(result['integration_origins']) == set(modules)
