# SPDX-License-Identifier: Apache-2.0
"""Actual git apply/undo against local tracked fixtures; never production data."""
from pathlib import Path
import shutil
import subprocess
import sys
import pytest


def run(*args,check=True):
    return subprocess.run([str(x) for x in args],capture_output=True,text=True,check=check)


def repo(repository,tmp_path):
    root=tmp_path/'checkout'
    shutil.copytree(repository/'tests/fixtures/upstream_like',root)
    (root/'site').mkdir();(root/'site/system.py').write_text('custom = 1\n')
    run('git','init','-q',root)
    run('git','-C',root,'add','.')
    run('git','-C',root,'-c','user.name=Test','-c','user.email=test@example.invalid','commit','-qm','fixture baseline')
    return root


def test_inspect_apply_reverse_preserves_custom_assets(repository,tmp_path):
    root=repo(repository,tmp_path)
    site=root/'site/system.py';site.write_text('custom = 2\n')
    untracked=root/'site/input.txt';untracked.write_text('preserve untracked input\n')
    before=run('git','-C',root,'diff').stdout
    patch=tmp_path/'review.patch'
    cmd=[sys.executable,repository/'tools/patch_core.py',root,'--output',patch]
    run(*cmd)
    assert run('git','-C',root,'diff').stdout==before
    assert not (root/'lib/benchpark/plugins.py').exists()
    run('git','-C',root,'apply','--check',patch)
    run('git','-C',root,'apply',patch)
    assert (root/'lib/benchpark/plugins.py').is_file()
    assert site.read_text()=='custom = 2\n' and untracked.read_text()=='preserve untracked input\n'
    run(sys.executable,repository/'tools/patch_core.py',root,'--output',patch,'--reverse','--apply')
    assert run('git','-C',root,'diff').stdout==before
    assert not (root/'lib/benchpark/plugins.py').exists()
    assert untracked.is_file()


def test_modified_core_target_is_not_overwritten(repository,tmp_path):
    root=repo(repository,tmp_path)
    p=root/'lib/main.py';p.write_text(p.read_text()+'\n# site custom core edit\n')
    before=run('git','-C',root,'diff').stdout
    patch=tmp_path/'review.patch'
    r=run(sys.executable,repository/'tools/patch_core.py',root,'--output',patch,'--apply',check=False)
    assert r.returncode!=0 and 'Modified Core targets' in r.stderr
    assert not patch.exists() and run('git','-C',root,'diff').stdout==before


def test_patch_output_may_not_overwrite_core_file(repository,tmp_path):
    root=repo(repository,tmp_path);before=(root/'lib/main.py').read_bytes()
    r=run(sys.executable,repository/'tools/patch_core.py',root,'--output',root/'lib/main.py',check=False)
    assert r.returncode!=0 and 'outside the Benchpark' in r.stderr
    assert (root/'lib/main.py').read_bytes()==before


def test_existing_review_evidence_is_not_overwritten(repository,tmp_path):
    root=repo(repository,tmp_path);patch=tmp_path/'review.patch';patch.write_text('keep reviewed patch')
    r=run(sys.executable,repository/'tools/patch_core.py',root,'--output',patch,check=False)
    assert r.returncode!=0 and patch.read_text()=='keep reviewed patch'
    assert not run('git','-C',root,'diff').stdout


def test_changed_upstream_anchor_fails_with_no_partial_apply(repository,tmp_path):
    root=repo(repository,tmp_path)
    path=root/'lib/benchpark/cmd/experiment.py'
    path.write_text(path.read_text().replace('experiment.system_spec = system_spec','experiment.system_spec = renamed_system'))
    run('git','-C',root,'add','.')
    run('git','-C',root,'-c','user.name=Test','-c','user.email=test@example.invalid','commit','-qm','changed upstream')
    r=run(sys.executable,repository/'tools/patch_core.py',root,'--output',tmp_path/'patch','--apply',check=False)
    assert r.returncode!=0 and 'Unsupported upstream layout' in r.stderr
    assert not (root/'lib/benchpark/plugins.py').exists()
    assert not run('git','-C',root,'diff').stdout


def test_patch_and_metadata_must_not_share_filename(repository,tmp_path):
    root=repo(repository,tmp_path);patch=tmp_path/'review.json'
    r=run(sys.executable,repository/'tools/patch_core.py',root,'--output',patch,check=False)
    assert r.returncode!=0 and 'reserved for metadata' in r.stderr
    assert not patch.exists() and not run('git','-C',root,'diff').stdout
