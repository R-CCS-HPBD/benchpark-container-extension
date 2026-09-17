# SPDX-License-Identifier: Apache-2.0
"""Review harness cannot turn test failure/timeout into a pass or stop at C01."""
import json
from pathlib import Path
import subprocess
import sys
import review_all


def test_timeout_is_a_recorded_failure_not_an_unreviewed_abort(tmp_path,monkeypatch):
    monkeypatch.setattr(review_all,'ROOT',tmp_path)
    def timeout(*args,**kwargs):
        raise subprocess.TimeoutExpired(['test'],240,output=b'partial test output')
    monkeypatch.setattr(subprocess,'run',timeout)
    result=review_all.run('timeout',['test'],tmp_path)
    assert result['exit_code']==124
    assert 'partial test output' in (tmp_path/'timeout.log').read_text()


def test_missing_test_executable_is_recorded(tmp_path,monkeypatch):
    monkeypatch.setattr(review_all,'ROOT',tmp_path)
    def missing(*args,**kwargs):raise FileNotFoundError('missing executable')
    monkeypatch.setattr(subprocess,'run',missing)
    result=review_all.run('missing',['test'],tmp_path)
    assert result['exit_code']==127 and 'missing executable' in (tmp_path/'missing.log').read_text()


def test_full_ordered_checklist_is_emitted_even_on_test_failure(repository,tmp_path,monkeypatch,capsys):
    data=json.loads((repository/'docs/review/checklist.json').read_text())
    (tmp_path/'docs/review').mkdir(parents=True)
    (tmp_path/'docs/review/checklist.json').write_text(json.dumps(data))
    for name in ('pyproject.toml','MANIFEST.in','README.md','docs/ARCHITECTURE.ja.md','docs/PLUGIN_API.ja.md'):
        (tmp_path/name).write_text('harness fixture')
    notes={row['id']:{'status':'PASS_LOCAL','evidence':'test-harness evidence only'} for row in data['items']}
    notes['C23']['status']='PENDING-EXTERNAL'
    note=tmp_path/'notes.json';note.write_text(json.dumps(notes))
    monkeypatch.setattr(review_all,'ROOT',tmp_path)
    monkeypatch.setattr(sys,'argv',['review_all.py','--round','test','--notes',str(note)])
    monkeypatch.setattr(review_all,'static_checks',lambda:{
        'core_stdlib_only':True,'core_forbidden_literals':[],'external_imports_of_core':[],
        'changed_runtime_files':[],'checklist_unchanged':True,'reference_unchanged':True})
    monkeypatch.setattr(review_all,'run',lambda label,command,out:{'exit_code':1 if label=='pytest' else 0})
    assert review_all.main()==1
    report=json.loads((tmp_path/'results/reviews/test/review.json').read_text())
    assert [r['id'] for r in report['items']]==[r['id'] for r in data['items']]
    assert len(report['items'])==26
    assert all(r['status']=='FAIL' for r in report['items'] if r['id']!='C23')
    printed=capsys.readouterr().out
    assert printed.index('C01')<printed.index('C26')
