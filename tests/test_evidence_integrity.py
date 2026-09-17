# SPDX-License-Identifier: Apache-2.0
import json
import os
from pathlib import Path
from types import SimpleNamespace as S

import pytest

from benchpark_container import preparation as prep
from benchpark_container.preparation import EnvironmentBuildError
from benchpark_container.util import atomic_json_verified, ValidationError


class CountingRuntime:
    def __init__(self):
        self.argv_calls = 0
    def argv(self, image, scratch, inputs, mounts, command, environment=None, pwd='/bpce/work'):
        self.argv_calls += 1
        return [
            __import__('sys').executable, '-c',
            'print("ok")'
        ]
    def host_env(self):
        return os.environ.copy()


def test_capture_rejects_reused_evidence_stem_before_second_command(tmp_path):
    rt = CountingRuntime()
    stem = tmp_path / 'probe'
    first, _ = prep._capture(rt, None, tmp_path, tmp_path, [], ['ignored'], 30, {}, stem)
    assert first.returncode == 0
    assert rt.argv_calls == 1
    with pytest.raises(EnvironmentBuildError) as error:
        prep._capture(rt, None, tmp_path, tmp_path, [], ['ignored-again'], 30, {}, stem)
    assert error.value.code == 'EVIDENCE_STEM_REUSED'
    assert rt.argv_calls == 1


def test_preflight_probes_common_base_pip_exactly_once(tmp_path, monkeypatch):
    stems = []
    def fake_capture(rt, image, scratch, inputs, mounts, command, timeout, environment, stem, **kwargs):
        stems.append(Path(stem).name)
        if Path(stem).name == 'base-tools':
            return {'python':'/opt/base/python','shell':'/opt/base/bash','python_version':[3,10,12]}
        if Path(stem).name == 'base-pip-version':
            return S(returncode=0, stdout='pip 22.0.2 from /base/pip\n', stderr=''), ['apptainer','exec','base','/opt/base/python','-m','pip','--version']
        return S(returncode=0, stdout='5.1.0\n', stderr=''), ['apptainer','exec','base']
    monkeypatch.setattr(prep, '_capture', fake_capture)
    monkeypatch.setattr(prep, 'validate_targets', lambda *a, **k: None)
    tools, package_manager = prep.preflight_tools(
        None, None, tmp_path, tmp_path, [],
        {'python':'/declared/python','shell':'/declared/bash'}, 30, {}, tmp_path)
    assert tools['python'] == '/opt/base/python'
    assert package_manager['command'] == ['/opt/base/python','-m','pip']
    assert stems.count('base-pip-version') == 1
    assert (tmp_path/'base-pip-version.log').read_text() == 'pip 22.0.2 from /base/pip\n'


def test_immutable_json_accepts_existing_identical_content(tmp_path):
    p = tmp_path / 'concrete.json'
    value = {'a': 1, 'nested': {'x': 'y'}}
    assert atomic_json_verified(p, value) == 'created'
    assert atomic_json_verified(p, value) == 'existing-identical'
    assert json.loads(p.read_text()) == value


def test_immutable_json_rejects_existing_different_content(tmp_path):
    p = tmp_path / 'concrete.json'
    assert atomic_json_verified(p, {'a': 1}) == 'created'
    with pytest.raises(ValidationError, match='Immutable JSON content changed'):
        atomic_json_verified(p, {'a': 2})

def test_architecture_audit_detects_duplicate_attempt_evidence_stem(repository, tmp_path):
    import shutil
    from audit_architecture import audit
    for folder in ('src', 'core', 'examples'):
        shutil.copytree(repository/folder, tmp_path/folder,
                        ignore=shutil.ignore_patterns('__pycache__', '*.egg-info'))
    p = tmp_path/'src/benchpark_container/preparation.py'
    p.write_text(p.read_text() + '''\n\ndef duplicate_evidence_probe(rt, image, scratch, inputs, mounts, timeout, env, attempt):\n    return _capture(rt, image, scratch, inputs, mounts, ['true'], timeout, env, attempt / 'base-pip-version')\n''')
    result = audit(tmp_path)
    assert any('duplicate immutable evidence stem base-pip-version' in i['rule']
               for i in result['issues'])
