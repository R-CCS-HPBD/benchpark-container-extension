#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Run the frozen checklist IN ORDER, not only the last failed test.

PASS_LOCAL is NOT a container/upstream E2E certificate. No external evidence is
invented by this tool. Manual semantic review is recorded separately per round.
The checks test bounded invariants, not the absence of every unknown defect.
"""
import argparse
import ast
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time
import zipfile

ROOT = Path(__file__).resolve().parents[1]


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def run(label, command, out):
    start = time.monotonic()
    try:
        proc = subprocess.run(command, cwd=ROOT, text=True, stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, timeout=240)
        code, output = proc.returncode, proc.stdout
    except subprocess.TimeoutExpired as exc:
        output = exc.stdout or ''
        if isinstance(output, bytes):
            output = output.decode('utf-8', errors='replace')
        code, output = 124, output + '\nReview command timed out; not a pass.\n'
    except OSError as exc:
        code, output = 127, str(exc) + '\n'
    log = out / (label + '.log')
    log.write_text(output)
    return {'exit_code': code, 'seconds': time.monotonic()-start,
            'command': command, 'log': str(log.relative_to(ROOT))}


def static_checks():
    core = sorted((ROOT / 'core/files').rglob('*.py'))
    prohibited = {'container', 'apptainer', 'docker', 'cer', 'pip', 'tune', 'spack', 'user-managed'}
    imports, literals = [], set()
    for path in core:
        tree = ast.parse(path.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import): imports.extend(a.name.split('.')[0] for a in node.names)
            if isinstance(node, ast.ImportFrom) and node.module: imports.append(node.module.split('.')[0])
            if isinstance(node, ast.Constant) and isinstance(node.value, str): literals.add(node.value)
    foreign = []
    for path in (ROOT/'src').rglob('*.py'):
        for node in ast.walk(ast.parse(path.read_text())):
            if isinstance(node, ast.ImportFrom) and node.module and node.module.startswith('benchpark.'):
                foreign.append(str(path.relative_to(ROOT))+':'+node.module)
            if isinstance(node, ast.Import):
                foreign.extend(str(path.relative_to(ROOT))+':'+a.name for a in node.names if a.name=='benchpark' or a.name.startswith('benchpark.'))
    frozen = ROOT/'docs/review/INITIAL_RECEIPT.json'
    receipt = json.loads(frozen.read_text())
    preserved = ['preparation.py','runtime.py','contracts.py','artifacts.py','util.py',
                 'provenance.py','requirements.py','reproducibility.py','reproducibility_rules.py',
                 'ramble_adapter.py','cer/recording.py','cer/legacy.py', 'cer/__init__.py']
    changed = []
    with zipfile.ZipFile(ROOT/'tests/reference/v0.5.2.zip') as z:
        for rel in preserved:
            old = z.read('benchpark-container-extension-v0.5.2/src/benchpark_container/'+rel)
            if old != (ROOT/'src/benchpark_container'/rel).read_bytes(): changed.append(rel)
    approval_dir = ROOT / 'docs/review/runtime'
    permitted, protected_changed, amendment_valid = set(), [], True
    if (approval_dir / 'AUTHORIZATION.json').is_file():
        frozen = json.loads((approval_dir / 'FROZEN.json').read_text())
        amendment_valid = all((approval_dir / name).is_file() and sha(approval_dir / name) == expected
                              for name, expected in frozen.items())
        approval = json.loads((approval_dir / 'AUTHORIZATION.json').read_text())
        permitted = set(approval['runtime_changes']) if amendment_valid else set()
        inputs = json.loads((approval_dir / 'INPUT_RECEIPT.json').read_text())['files']
        protected = set(approval['protected_files'])
        for directory in approval['protected_directories']:
            protected.update(name for name in inputs if name.startswith(directory + '/'))
            protected.update(str(path.relative_to(ROOT)) for path in (ROOT / directory).rglob('*')
                             if path.is_file() and '__pycache__' not in path.parts)
        protected_changed = [name for name in sorted(protected) if not (ROOT/name).is_file()
                             or name not in inputs or sha(ROOT/name) != inputs[name]]
    catalog_dir = ROOT / 'docs/review/catalog'
    catalog_changed, catalog_valid = [], True
    if (catalog_dir / 'AUTHORIZATION.json').is_file():
        catalog_frozen=json.loads((catalog_dir/'FROZEN.json').read_text())
        catalog_valid=all((catalog_dir/name).is_file() and sha(catalog_dir/name)==expected
                          for name,expected in catalog_frozen.items())
        catalog_approval=json.loads((catalog_dir/'AUTHORIZATION.json').read_text())
        if catalog_valid:
            permitted.update(catalog_approval['additional_legacy_runtime_changes'])
            protected_changed=[p for p in protected_changed if p not in catalog_approval['unfreeze_only']]
        receipt2=json.loads((catalog_dir/'INPUT_RECEIPT.json').read_text())
        protected=set(catalog_approval['protected_files'])
        for directory in catalog_approval['protected_directories']:
            protected.update(p for p in receipt2['files'] if p.startswith(directory+'/'))
            protected.update(str(p.relative_to(ROOT)) for p in (ROOT/directory).rglob('*')
                             if p.is_file() and '__pycache__' not in p.parts)
        catalog_changed=[p for p in sorted(protected) if not (ROOT/p).is_file()
                         or sha(ROOT/p)!=receipt2['files'].get(p)]
    return {'catalog_amendment_valid':catalog_valid, 'catalog_protected_changes':catalog_changed,
        'unexpected_runtime_changes': sorted(set(changed) - permitted),
        'protected_changes': protected_changed, 'runtime_amendment_valid': amendment_valid,
        'core_files':[str(p.relative_to(ROOT)) for p in core],
        'core_lines':sum(len(p.read_text().splitlines()) for p in core),
        'core_stdlib_only':set(imports)<=set(sys.stdlib_module_names),
        'core_forbidden_literals':sorted(prohibited & literals),
        'external_imports_of_core':foreign, 'unchanged_runtime_files':len(preserved)-len(changed),
        'changed_runtime_files':changed,
        'checklist_unchanged':sha(ROOT/'docs/review/COMPLIANCE.ja.md')==receipt['checklist_sha256'],
        'reference_unchanged':sha(ROOT/'tests/reference/v0.5.2.zip')==receipt['source_sha256']}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--round',required=True)
    parser.add_argument('--notes',type=Path,required=True,help='Manual per-item semantic review JSON')
    args=parser.parse_args()
    out=ROOT/'results/reviews'/args.round
    out.mkdir(parents=True,exist_ok=False)
    notes=json.loads(args.notes.read_text())
    checks=json.loads((ROOT/'docs/review/checklist.json').read_text())['items']
    runtime_checklist = ROOT/'docs/review/runtime/checklist.json'
    if runtime_checklist.is_file():
        checks += json.loads(runtime_checklist.read_text())['items']
    catalog_dir=ROOT/'docs/review/catalog'
    if (catalog_dir/'checklist.json').is_file():
        amendments=json.loads((catalog_dir/'AMENDMENTS.json').read_text())
        for item in checks:
            if item['id'] in amendments:
                item['historical_requirement']=item['requirement']
                item['requirement']=amendments[item['id']]
        checks += json.loads((catalog_dir/'checklist.json').read_text())['items']
    dev3_dir=ROOT/'docs/review/dev3'
    if (dev3_dir/'AMENDMENTS.json').is_file():
        frozen=json.loads((dev3_dir/'FROZEN.json').read_text())
        if not all((dev3_dir/name).is_file() and sha(dev3_dir/name)==expected for name,expected in frozen.items()):
            raise ValueError('dev3 review amendment is not frozen/valid')
        amendments=json.loads((dev3_dir/'AMENDMENTS.json').read_text())
        for item in checks:
            if item['id'] in amendments:
                item['historical_requirement']=item.get('historical_requirement',item['requirement'])
                item['requirement']=amendments[item['id']]
    dev4_dir = ROOT / 'docs/review/dev4'
    if (dev4_dir / 'AMENDMENTS.json').is_file():
        frozen = json.loads((dev4_dir / 'FROZEN.json').read_text())
        if not all((dev4_dir / name).is_file() and sha(dev4_dir / name) == expected
                   for name, expected in frozen.items()):
            raise ValueError('dev4 review amendment is not frozen/valid')
        amendments = json.loads((dev4_dir / 'AMENDMENTS.json').read_text())
        for item in checks:
            if item['id'] in amendments:
                item['historical_requirement'] = item.get('historical_requirement', item['requirement'])
                item['requirement'] = amendments[item['id']]
        checks += json.loads((dev4_dir / 'checklist.json').read_text())['items']
    if set(notes)!=set(row['id'] for row in checks): raise ValueError('Every checklist item needs a review note')
    allowed = {'PASS_LOCAL', 'FAIL', 'PENDING-EXTERNAL'}
    for item in checks:
        note = notes[item['id']]
        if note.get('status') not in allowed or not str(note.get('evidence', '')).strip():
            raise ValueError('Each item needs an explicit supported status and nonempty evidence: ' + item['id'])
    sources = {}
    for directory in ('core', 'src', 'tools', 'tests', 'examples', 'docs', '.github'):
        for path in sorted((ROOT / directory).rglob('*')):
            if path.is_file() and '__pycache__' not in path.parts and not any(p.endswith('.egg-info') for p in path.parts):
                sources[str(path.relative_to(ROOT))] = sha(path)
    for name in ('pyproject.toml', 'MANIFEST.in', 'README.md', 'docs/ARCHITECTURE.ja.md', 'docs/PLUGIN_API.ja.md'):
        sources[name] = sha(ROOT / name)
    (out / 'source-manifest.json').write_text(json.dumps(sources, sort_keys=True, indent=2) + '\n')
    for path in sorted((ROOT/'docs/review/runtime').glob('*')):
        if path.is_file():
            sources[str(path.relative_to(ROOT))] = sha(path)
    (out / 'source-manifest.json').write_text(json.dumps(sources, sort_keys=True, indent=2) + '\n')
    findings=static_checks()
    findings['source_manifest_sha256'] = sha(out / 'source-manifest.json')
    test_files=sorted(str(p.relative_to(ROOT)) for p in (ROOT/'tests').glob('test_*.py'))
    split=len(test_files)//2
    runs={name:run(name,[sys.executable,*cmd],out) for name,cmd in (
        ('bundle',['tools/build_runtime.py','--check']),
        ('architecture',['tools/audit_architecture.py']),
        ('pytest-a',['-m','pytest','-q',*test_files[:split]]),
        ('pytest-b',['-m','pytest','-q',*test_files[split:]]),
        ('pytest',['-m','pytest','-q','tests/legacy']))}
    tests_ok=all(r['exit_code']==0 for r in runs.values())
    basics=(findings['core_stdlib_only'] and not findings['core_forbidden_literals'] and
            not findings['external_imports_of_core'] and
            not findings.get('unexpected_runtime_changes', findings['changed_runtime_files']) and
            not findings.get('protected_changes', []) and findings.get('runtime_amendment_valid', True) and
            findings['checklist_unchanged'] and findings['reference_unchanged'] and
            findings.get('catalog_amendment_valid',True) and not findings.get('catalog_protected_changes',[]))
    rows=[]
    for item in checks:  # Fixed order, including all previously passing items.
        note=notes[item['id']]
        status=note['status']
        if status=='PASS_LOCAL' and (not tests_ok or not basics): status='FAIL'
        row=dict(item, status=status, evidence=note['evidence'], scope=note.get('scope','static/unit'))
        rows.append(row)
        print(item['id'],status,item['title'])
    data={'round':args.round,'snapshot':findings,'tests':runs,'items':rows,
          'local_review':'PASS' if basics and tests_ok and not any(x['status']=='FAIL' for x in rows) else 'FAIL',
          'external_e2e':'PENDING-EXTERNAL','notes_sha256':sha(args.notes)}
    (out/'review.json').write_text(json.dumps(data,ensure_ascii=False,indent=2)+'\n')
    text='# '+args.round+' 全項目レビュー\n\n外部E2Eは未実施。PASS_LOCALは実機合格を意味しない。\n\n|ID|判定|根拠|\n|---|---|---|\n'
    for x in rows: text+=f"|{x['id']}|{x['status']}|{x['evidence']}|\n"
    (out/'REVIEW.ja.md').write_text(text)
    return 0 if data['local_review']=='PASS' else 1


if __name__=='__main__':sys.exit(main())
