# SPDX-License-Identifier: Apache-2.0
"""Source-boundary and distribution-level fourth-backend extension proofs."""
import ast
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
from zipfile import ZipFile

import pytest
import review_all


def test_input_061_archive_and_runtime_checklist_remain_frozen(repository):
    root=repository/'docs/review/runtime'
    inputs=json.loads((root/'INPUT_RECEIPT.json').read_text())
    sha=lambda p:hashlib.sha256(p.read_bytes()).hexdigest()
    assert sha(repository/'tests/reference/v0.6.1.zip')==inputs['source_sha256']
    for name,expected in json.loads((root/'FROZEN.json').read_text()).items():
        assert sha(root/name)==expected,name
    report=review_all.static_checks()
    assert report['runtime_amendment_valid'] is True
    assert report['protected_changes']==[]
    assert report['unexpected_runtime_changes']==[]


def test_original_061_tests_retained_and_old_assertions_preserved(repository):
    changed={'test_contract.py','test_storage_runtime.py','test_reference_equivalence.py'}
    migration=repository/'docs/review/catalog/TEST_MIGRATION.json'
    if migration.exists(): changed.update(json.loads(migration.read_text()))
    prefix='benchpark-container-extension-v0.6.1/tests/'
    with ZipFile(repository/'tests/reference/v0.6.1.zip') as z:
        for name in z.namelist():
            if not name.startswith(prefix) or not name.endswith('.py'):continue
            relative=name[len(prefix):]
            current=repository/'tests'/relative
            old=z.read(name).decode()
            assert current.is_file(),relative
            new=current.read_text()
            if relative not in changed:
                assert old==new,relative
            old_tests={n.name:n for n in ast.walk(ast.parse(old)) if isinstance(n,ast.FunctionDef) and n.name.startswith('test_')}
            new_tests={n.name:n for n in ast.walk(ast.parse(new)) if isinstance(n,ast.FunctionDef) and n.name.startswith('test_')}
            assert set(old_tests)<=set(new_tests),relative
            if relative=='test_storage_runtime.py':
                get_asserts=lambda n:[ast.dump(a) for a in ast.walk(n) if isinstance(a,ast.Assert)]
                for key in old_tests:
                    assert get_asserts(old_tests[key])==get_asserts(new_tests[key]),key


def test_no_concrete_runtime_switch_in_common_execution_or_contract(repository):
    for relative in ['src/benchpark_container/runtime.py','src/benchpark_container/contracts.py','src/benchpark_container/preparation.py']:
        tree=ast.parse((repository/relative).read_text())
        for node in ast.walk(tree):
            if isinstance(node,ast.If):
                constants={n.value for n in ast.walk(node.test) if isinstance(n,ast.Constant) and isinstance(n.value,str)}
                assert not constants & {'docker','singularity','apptainer','podman'},(relative,node.lineno)


def test_fourth_backend_only_requires_module_registry_tests_and_bundle_regeneration(repository,tmp_path):
    project=tmp_path/'project';project.mkdir()
    for directory in ['src','tools','core']:
        shutil.copytree(repository/directory,project/directory,ignore=shutil.ignore_patterns('__pycache__','*.egg-info'))
    before={str(p.relative_to(project)):p.read_bytes() for d in ('src','tools','core') for p in (project/d).rglob('*') if p.is_file()}
    backend=project/'src/benchpark_container/backends/fourth.py'
    backend.write_text('from .sif import SIFRuntimeBackend\nclass FourthBackend(SIFRuntimeBackend):\n    name="fourth-fixture"\n    env_prefix="FOURTH_"\n')
    registry=project/'src/benchpark_container/backends/registry.py'
    registry.write_text(registry.read_text().replace('BACKENDS = {','from .fourth import FourthBackend\n\nBACKENDS = {\n    "fourth-fixture": FourthBackend,'))
    subprocess.run([sys.executable,str(project/'tools/build_runtime.py')],check=True,capture_output=True)
    bundle=project/'src/benchpark_container/resources/runtime.pyz'
    with ZipFile(bundle) as z:
        assert 'bpce_node/backends/fourth.py' in z.namelist()
        assert z.read('bpce_node/backends/fourth.py')==backend.read_bytes()
    # -I -S deliberately excludes editable installs, site-packages and caller cwd.
    script='''import sys,json
sys.path.insert(0,sys.argv[1])
from bpce_node.backends.registry import backend_class
from bpce_node.contracts import runtime_settings
s=runtime_settings({"runtime":"fourth-fixture","executable":"declared-fourth","worker_python":sys.executable,"image_cache":"/declared/cache"})
print(json.dumps({"runtime":s["runtime"],"backend":backend_class(s["runtime"]).__name__}))
'''
    result=subprocess.run([sys.executable,'-I','-S','-c',script,str(bundle)],capture_output=True,text=True,cwd=tmp_path)
    assert result.returncode==0,result.stderr
    assert json.loads(result.stdout)=={'runtime':'fourth-fixture','backend':'FourthBackend'}
    changed={name for name,value in before.items() if (project/name).read_bytes()!=value}
    assert changed=={'src/benchpark_container/backends/registry.py','src/benchpark_container/resources/runtime.pyz'}
    assert all((project/name).read_bytes()==data for name,data in before.items() if name not in changed)


def test_review_reports_all_50_items_even_when_tests_fail(repository,tmp_path,monkeypatch,capsys):
    source=repository/'docs/review'
    destination=tmp_path/'docs/review';shutil.copytree(source,destination)
    for path in ('pyproject.toml','MANIFEST.in','README.md','docs/ARCHITECTURE.ja.md','docs/PLUGIN_API.ja.md'):
        (tmp_path/path).write_text('review test fixture')
    rows=json.loads((destination/'checklist.json').read_text())['items']+json.loads((destination/'runtime/checklist.json').read_text())['items']
    catalog=destination/'catalog/checklist.json'
    if catalog.exists():rows+=json.loads(catalog.read_text())['items']
    imports=destination/'dev4/checklist.json'
    if imports.exists():rows+=json.loads(imports.read_text())['items']
    notes={r['id']:{'status':'PENDING-EXTERNAL' if r['id'] in ('C23','R21','R22','R23') else 'PASS_LOCAL','evidence':'test-double-only'} for r in rows}
    note=tmp_path/'notes.json';note.write_text(json.dumps(notes))
    monkeypatch.setattr(review_all,'ROOT',tmp_path)
    monkeypatch.setattr(sys,'argv',['review_all','--round','all-50','--notes',str(note)])
    monkeypatch.setattr(review_all,'static_checks',lambda:{'core_stdlib_only':True,'core_forbidden_literals':[],
        'external_imports_of_core':[],'changed_runtime_files':['runtime.py'],'unexpected_runtime_changes':[],
        'protected_changes':[],'runtime_amendment_valid':True,'checklist_unchanged':True,'reference_unchanged':True})
    monkeypatch.setattr(review_all,'run',lambda *a,**k:{'exit_code':1})
    assert review_all.main()==1
    data=json.loads((tmp_path/'results/reviews/all-50/review.json').read_text())
    assert [i['id'] for i in data['items']]==[i['id'] for i in rows]
    assert len(data['items'])==len(rows)
    assert sum(i['status']=='FAIL' for i in data['items'])==len(rows)-4
    assert sum(i['status']=='PENDING-EXTERNAL' for i in data['items'])==4
