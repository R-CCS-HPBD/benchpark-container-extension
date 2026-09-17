# SPDX-License-Identifier: Apache-2.0
"""Bounded dev3→dev4 change authorization and preservation of earlier tests."""
import ast
import hashlib
import json


def test_dev4_changes_are_within_approved_scope(repository):
    area = repository / 'docs/review/dev4'
    frozen = json.loads((area / 'FROZEN.json').read_text())
    for name, expected in frozen.items():
        assert hashlib.sha256((area / name).read_bytes()).hexdigest() == expected, name
    receipt = json.loads((area / 'INPUT_RECEIPT.json').read_text())['files']
    auth = json.loads((area / 'AUTHORIZATION.json').read_text())
    allowed = set(auth['allowed_changed_files'])
    prefixes = tuple(auth['allowed_new_prefixes'])
    paths = set()
    for folder in ('core', 'src', 'tools', 'tests', 'examples', 'docs', '.github'):
        paths.update(str(p.relative_to(repository)) for p in (repository / folder).rglob('*')
                     if p.is_file() and '__pycache__' not in p.parts and not any(x.endswith('.egg-info') for x in p.parts))
    paths.update(p for p in receipt if p.split('/')[0] in ('core', 'src', 'tools', 'tests', 'examples', 'docs', '.github'))
    for path in sorted(paths):
        if path in allowed or path.startswith(prefixes):
            continue
        p = repository / path
        assert p.is_file() and path in receipt, path
        assert hashlib.sha256(p.read_bytes()).hexdigest() == receipt[path], path


def test_dev3_test_functions_preserved(repository):
    baseline = json.loads((repository / 'docs/review/dev4/INPUT_TEST_NAMES.json').read_text())
    for path, names in baseline.items():
        tree = ast.parse((repository / path).read_text())
        actual = {n.name for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name.startswith('test_')}
        assert set(names) <= actual, path


def test_ordered_dev4_checklist_extends_all_previous_items(repository):
    import review_all
    # Requirements are frozen per amendment, not silently replaced in originals.
    checks = []
    for rel in ('docs/review/checklist.json', 'docs/review/runtime/checklist.json',
                'docs/review/catalog/checklist.json', 'docs/review/dev4/checklist.json'):
        checks.extend(json.loads((repository / rel).read_text())['items'])
    ids = [x['id'] for x in checks]
    assert len(ids) == len(set(ids)) == 92
    assert ids[:26] == [f'C{i:02}' for i in range(1, 27)]
    assert ids[26:50] == [f'R{i:02}' for i in range(1, 25)]
    assert ids[50:80] == [f'M{i:02}' for i in range(1, 31)]
    assert ids[80:] == [f'I{i:02}' for i in range(1, 13)]
    assert 'dev4_dir' in (repository / 'tools/review_all.py').read_text()


def test_harness_migration_preserves_every_original_assert(repository):
    saved = json.loads((repository / 'docs/review/dev4/INPUT_HARNESS_ASSERTS.json').read_text())
    current = ast.parse((repository / saved['file']).read_text())
    assert [ast.dump(n) for n in ast.walk(current) if isinstance(n, ast.Assert)] == saved['asserts']
