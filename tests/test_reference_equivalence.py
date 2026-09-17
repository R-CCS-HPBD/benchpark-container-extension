# SPDX-License-Identifier: Apache-2.0
"""Differential checks against the original, unedited v0.5.2 archive."""
import ast
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import zipfile


def test_reference_archive_and_checklist_are_unchanged(repository):
    receipt=json.loads((repository/'docs/review/INITIAL_RECEIPT.json').read_text())
    sha=lambda p:hashlib.sha256(p.read_bytes()).hexdigest()
    assert sha(repository/'tests/reference/v0.5.2.zip')==receipt['source_sha256']
    assert sha(repository/'docs/review/COMPLIANCE.ja.md')==receipt['checklist_sha256']


def test_all_runtime_and_environment_algorithms_are_byte_identical(repository):
    with zipfile.ZipFile(repository/'tests/reference/v0.5.2.zip') as z:
        for name in z.namelist():
            prefix='benchpark-container-extension-v0.5.2/src/benchpark_container/'
            if not name.startswith(prefix) or not name.endswith('.py'):continue
            rel=name[len(prefix):]
            if rel=='__init__.py':continue  # explicit release identity only
            original=z.read(name).decode()
            current=(repository/'src/benchpark_container'/rel).read_text()
            expected = original.replace('benchpark.extension_api','benchpark_integration.api')
            approved = json.loads((repository/'docs/review/runtime/AUTHORIZATION.json').read_text())['runtime_changes']
            extra_path=repository/'docs/review/catalog/AUTHORIZATION.json'
            extra=json.loads(extra_path.read_text())['changed_existing_functions'] if extra_path.exists() else {}
            extra_functions=extra.get('src/benchpark_container/'+rel,[])
            if rel not in approved and not extra_functions:
                assert current == expected, rel
            else:
                # Explicit runtime-addition amendment, NOT a blanket exemption.
                # Every unaffected top-level function/constant remains exact AST.
                permitted = {'preparation.py': {'_container_run'},
                             'contracts.py': {'runtime_settings'},
                             'runtime.py': {'Apptainer', 'execute'}}.get(rel,set()) | set(extra_functions)
                old_nodes = {n.name: ast.dump(n) for n in ast.parse(expected).body
                             if isinstance(n, (ast.FunctionDef, ast.ClassDef)) and n.name not in permitted}
                new_nodes = {n.name: ast.dump(n) for n in ast.parse(current).body
                             if isinstance(n, (ast.FunctionDef, ast.ClassDef))}
                assert old_nodes and all(new_nodes.get(k) == v for k,v in old_nodes.items()), rel
                constants = lambda t: [ast.dump(n) for n in ast.parse(t).body if isinstance(n, ast.Assign)]
                assert constants(current) == constants(expected), rel


def test_all_legacy_test_cases_retained_no_wholesale_test_removal(repository):
    import ast
    with zipfile.ZipFile(repository/'tests/reference/v0.5.2.zip') as z:
        for name in z.namelist():
            prefix='benchpark-container-extension-v0.5.2/tests/'
            if not name.startswith(prefix) or not name.endswith('.py') or '/fixtures/' in name:continue
            rel=name[len(prefix):]
            if not Path(rel).name.startswith('test_'):continue
            names=lambda text:{n.name for n in ast.walk(ast.parse(text)) if isinstance(n,ast.FunctionDef) and n.name.startswith('test_')}
            before=names(z.read(name).decode());after=names((repository/'tests'/rel).read_text())
            assert before<=after,(rel,before-after)


def test_old_and_new_request_results_and_fixed_snapshots_are_equal(repository,tmp_path):
    with zipfile.ZipFile(repository/'tests/reference/v0.5.2.zip') as z:
        for info in z.infolist():
            assert (tmp_path/info.filename).resolve().is_relative_to(tmp_path.resolve())
        z.extractall(tmp_path)
    old=tmp_path/'benchpark-container-extension-v0.5.2'
    outputs=[]
    for root in (old,repository):
        r=subprocess.run([sys.executable,str(repository/'tools/reference_probe.py'),str(root)],
                         capture_output=True,text=True,check=True,cwd=tmp_path)
        outputs.append(json.loads(r.stdout))
    assert outputs[0]==outputs[1]
