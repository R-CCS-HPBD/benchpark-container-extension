# SPDX-License-Identifier: Apache-2.0
"""Explicit dev1 invariants: code protection and bounded schema-test migration."""
import ast
import hashlib
import json
from pathlib import Path
from zipfile import ZipFile
import subprocess
import sys


def test_dev1_protected_code_and_old_test_names_are_retained(repository):
    area=repository/'docs/review/catalog'
    receipt=json.loads((area/'INPUT_RECEIPT.json').read_text())
    auth=json.loads((area/'AUTHORIZATION.json').read_text())
    migrations=json.loads((area/'TEST_MIGRATION.json').read_text())
    with ZipFile(repository/'tests/reference/v1.0.0.dev1.zip') as archive:
        prefix=archive.namelist()[0].split('/')[0]+'/'
        for name in archive.namelist():
            rel=name[len(prefix):]
            if not rel.endswith('.py'):continue
            if rel.startswith('tests/'):
                current=repository/rel
                assert current.is_file(),rel
                old=archive.read(name).decode();new=current.read_text()
                def names(text):
                    return {n.name for n in ast.walk(ast.parse(text)) if isinstance(n,(ast.FunctionDef,ast.AsyncFunctionDef)) and n.name.startswith('test_')}
                assert names(old)<=names(new),rel
                if rel[6:] not in migrations:assert old==new,rel
            protected=rel in auth['protected_files'] or any(rel.startswith(d+'/') for d in auth['protected_directories'])
            if protected: assert archive.read(name)==(repository/rel).read_bytes(),rel
        for rel,changed in auth['changed_existing_functions'].items():
            old=ast.parse(archive.read(prefix+rel).decode());new=ast.parse((repository/rel).read_text())
            funcs=lambda tree:{n.name:ast.dump(n) for n in tree.body if isinstance(n,(ast.FunctionDef,ast.ClassDef))}
            a,b=funcs(old),funcs(new)
            for name,value in a.items():
                if name not in changed:assert b.get(name)==value,(rel,name)
        # Inside authorized backend classes, unchanged methods stay identical.
        methods_changed={'sif.py':{'resolve_image'},'oci.py':{'resolve_image'},'base.py':set()}
        for file,changed in methods_changed.items():
            rel='src/benchpark_container/backends/'+file
            old=ast.parse(archive.read(prefix+rel).decode());new=ast.parse((repository/rel).read_text())
            methods=lambda tree:{(c.name,n.name):ast.dump(n) for c in tree.body if isinstance(c,ast.ClassDef) for n in c.body if isinstance(n,ast.FunctionDef)}
            a,b=methods(old),methods(new)
            for key,value in a.items():
                if key[1] not in changed:assert b.get(key)==value,(file,key)


def test_no_catalog_dependency_in_generic_execution_or_worker(repository):
    for name in ('runtime.py','preparation.py','ramble_adapter.py'):
        text=(repository/'src/benchpark_container'/name).read_text()
        assert 'container_image' not in text and 'catalog.manager' not in text
    with ZipFile(repository/'src/benchpark_container/resources/runtime.pyz') as archive:
        assert not any('/catalog/' in n or 'image_selection.py' in n or 'container_cli.py' in n for n in archive.namelist())
        assert 'bpce_node/image_store.py' in archive.namelist()
    code='import sys; sys.path.insert(0,sys.argv[1]); from bpce_node.image_store import verify_managed; print("stdlib-node-ok")'
    p=subprocess.run([sys.executable,'-I','-S','-c',code,str(repository/'src/benchpark_container/resources/runtime.pyz')],capture_output=True,text=True)
    assert p.returncode==0,p.stderr


def test_catalog_selection_fourth_backend_uses_existing_store(multi,monkeypatch):
    # Imported fixture aliases expose the same complete System/Catalog.
    from benchpark_container.backends.sif import SIFRuntimeBackend
    from benchpark_container.backends.registry import BACKENDS
    from benchpark_container.image_selection import select_image,select_runtime
    from benchpark_integration.api import plain
    class Fourth(SIFRuntimeBackend):
        name='fourth-fixture'
    monkeypatch.setitem(BACKENDS,Fourth.name,Fourth)
    context,_,_=multi
    system=plain(context.system)
    system['runtimes'][Fourth.name]={'executable':'fourth'}
    variants={'container_runtime':[Fourth.name]}
    runtime,_=select_runtime(system,variants)
    image,selection=select_image(system,plain(context.requirements),variants,runtime)
    assert image['kind']=='sif' and selection['managed'] is True


from test_catalog_selection import multi  # pytest fixture only
