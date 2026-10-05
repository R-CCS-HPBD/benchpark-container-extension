#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Architecture regression guard for the current production boundary.

This is deliberately a *regression guard*, not a proof of correctness.  It
checks the invariants that previously regressed in real-machine testing:
container-native Python/pip/Bash, requirement-text-only Python inputs, no
host-native environment builder, and no injected installer payload.
"""
import argparse
import ast
import hashlib
import json
from pathlib import Path
from zipfile import ZipFile

ROOT = Path(__file__).resolve().parents[1]


def audit(root):
    root = Path(root)
    issues, files = [], []

    def issue(path, line, rule):
        issues.append({'file': str(path.relative_to(root)), 'line': line, 'rule': rule})

    for folder in ('src', 'core/files'):
        base = root / folder
        if not base.exists():
            continue
        for path in sorted(base.rglob('*.py')):
            if '__pycache__' in path.parts:
                continue
            text = path.read_text(encoding='utf-8')
            tree = ast.parse(text, filename=str(path))
            files.append({'path': str(path.relative_to(root)),
                          'sha256': hashlib.sha256(path.read_bytes()).hexdigest(),
                          'syntax': 'passed'})
            is_core = path.parts[len(root.parts)] == 'core' if len(path.parts) > len(root.parts) else False
            for node in ast.walk(tree):
                if is_core and isinstance(node, (ast.Import, ast.ImportFrom)):
                    names = [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module or '']
                    if any(n.startswith(('benchpark_container', 'benchpark_tuning', 'apptainer', 'docker')) for n in names):
                        issue(path, node.lineno, 'INV-CORE: Core imports concrete extension/runtime')
                if isinstance(node, ast.Call):
                    if isinstance(node.func, ast.Attribute) and node.func.attr == 'get' and len(node.args) > 1:
                        key, default = node.args[:2]
                        if isinstance(key, ast.Constant) and key.value in ('python','shell','worker_python','executable','runtime'):
                            if isinstance(default, ast.Constant) and default.value in ('python','python3','bash','/bin/bash','apptainer'):
                                issue(path, node.lineno, 'INV-TOOLS: executable fallback inside production')
                    if any(k.arg == 'shell' and isinstance(k.value, ast.Constant) and k.value.value is True for k in node.keywords):
                        issue(path, node.lineno, 'INV-HOST: implicit host shell execution')

    resources = root / 'src/benchpark_container/resources'
    for path in sorted(resources.rglob('*')):
        if not path.is_file() or '__pycache__' in path.parts:
            continue
        rel = path.relative_to(resources)
        if path.name in ('pip.pyz', 'environment.py', 'sitecustomize.py') or 'installer' in rel.parts:
            issue(path, 0, 'INV-INJECT: injected installer/activation resource')

    runtime_zip = resources / 'runtime.pyz'
    if runtime_zip.exists():
        with ZipFile(runtime_zip) as z:
            allowed = {'__main__.py','bpce_node/__init__.py','bpce_node/artifacts.py','bpce_node/contracts.py',
                       'bpce_node/image_store.py', 'bpce_node/preparation.py','bpce_node/reproducibility_rules.py','bpce_node/cer/__init__.py',
                       'bpce_node/cer/recording.py','bpce_node/ramble_adapter.py','bpce_node/runtime.py','bpce_node/util.py'}
            for name in z.namelist():
                backend_module = (name.startswith('bpce_node/backends/') and name.endswith('.py')
                    and '..' not in Path(name).parts
                    and (root / 'src/benchpark_container' / name.removeprefix('bpce_node/')).is_file())
                collector_module = (name.startswith('bpce_node/cer/collectors/') and name.endswith('.py')
                    and '..' not in Path(name).parts
                    and (root / 'src/benchpark_container' / name.removeprefix('bpce_node/')).is_file())
                worker_module = backend_module or collector_module
                if worker_module:
                    source = root / 'src/benchpark_container' / name.removeprefix('bpce_node/')
                    if z.read(name) != source.read_bytes():
                        issue(runtime_zip, 0, 'INV-BUNDLE: worker source/bundle mismatch ' + name)
                    for node in ast.walk(ast.parse(z.read(name).decode('utf-8'))):
                        if isinstance(node, ast.Import):
                            for alias in node.names:
                                if alias.name.split('.')[0] not in __import__('sys').stdlib_module_names:
                                    issue(runtime_zip, 0, 'INV-BUNDLE: non-stdlib worker dependency ' + alias.name)
                        elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                            if node.module.split('.')[0] not in __import__('sys').stdlib_module_names:
                                issue(runtime_zip, 0, 'INV-BUNDLE: non-stdlib worker dependency ' + node.module)
                if name not in allowed and not worker_module:
                    issue(runtime_zip, 0, 'INV-INJECT: unexpected host worker bundle member ' + name)

    prep_path = root / 'src/benchpark_container/preparation.py'
    prep = prep_path.read_text(encoding='utf-8')
    if "pip_command = [tools['python'], '-m', 'pip']" not in prep or "pip = package_manager['command']" not in prep:
        issue(prep_path, 0, 'INV-PIP: package manager is not carried from the Common Base python -m pip preflight')
    for token in ('--no-deps', '--target', '--ignore-installed', '--isolated', 'PIP_CONFIG_FILE', 'ensurepip', 'venv.create', 'virtualenv'):
        if token in prep:
            issue(prep_path, 0, 'INV-PIP: extension imposes/injects package-manager environment: ' + token)
    if "pip + ['install', '--prefix', PYTHON_PREFIX]" not in prep:
        issue(prep_path, 0, 'INV-PIP: install path is not minimal Base-pip + private-prefix contract')

    # Evidence files inside one attempt are immutable. A probe must not reuse a
    # literal evidence stem from another probe, even in a different helper
    # called by prepare_environment. This catches the v0.5.1 real-machine
    # failure where base-pip-version was executed twice into the same path.
    prep_tree = ast.parse(prep, filename=str(prep_path))
    stems = {}
    def attempt_literal(expr):
        if (isinstance(expr, ast.BinOp) and isinstance(expr.op, ast.Div)
                and isinstance(expr.left, ast.Name) and expr.left.id == 'attempt'
                and isinstance(expr.right, ast.Constant) and isinstance(expr.right.value, str)):
            return expr.right.value.removesuffix('.log')
        return None
    for node in ast.walk(prep_tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
            continue
        value = None
        if node.func.id == '_capture':
            if len(node.args) > 8:
                value = attempt_literal(node.args[8])
            if value is None:
                for kw in node.keywords:
                    if kw.arg == 'stem': value = attempt_literal(kw.value)
        elif node.func.id == '_inspect':
            for kw in node.keywords:
                if kw.arg == 'log_stem': value = attempt_literal(kw.value)
        elif node.func.id == '_pip_check' and len(node.args) > 8:
            value = attempt_literal(node.args[8])
        if value:
            if value in stems:
                issue(prep_path, node.lineno, 'INV-EVIDENCE: duplicate immutable evidence stem ' + value)
            else:
                stems[value] = node.lineno

    req_path = root / 'src/benchpark_container/requirements.py'
    req = req_path.read_text(encoding='utf-8')
    if 'parse_wheel_filename' in req or 'InvalidWheelFilename' in req:
        issue(req_path, 0, 'INV-INPUT: requirement scanner handles package payloads')
    if 'Python package payloads are not mounted' not in req:
        issue(req_path, 0, 'INV-INPUT: local Python payload rejection missing')

    # No current example may carry a Python package payload.  Historical audit
    # evidence under docs/history/results/history is intentionally out of scope.
    examples = root / 'examples'
    for path in sorted(examples.rglob('*')):
        if path.is_file() and path.suffix.lower() in ('.whl', '.tar', '.gz', '.zip'):
            issue(path, 0, 'INV-INPUT: example mounts/distributes Python package payload')

    # A host-native environment-construction test previously masked the actual
    # container boundary.  Never reintroduce it as production validation.
    for folder in ('src', 'tests', 'tools'):
        base = root / folder
        if not base.exists():
            continue
        for path in base.rglob('*.py'):
            if path.name == 'audit_architecture.py':
                continue
            tree = ast.parse(path.read_text(encoding='utf-8'), filename=str(path))
            for node in ast.walk(tree):
                if isinstance(node, ast.ClassDef) and node.name == 'NativeRuntime':
                    issue(path, node.lineno, 'INV-HOST: host-native environment construction path')
                if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == 'native_case':
                    issue(path, node.lineno, 'INV-HOST: host-native environment construction path')

    runtime_path = root / 'src/benchpark_container/runtime.py'
    runtime_text = runtime_path.read_text(encoding='utf-8')
    if ':/bpce/resources' in runtime_text:
        issue(runtime_path, 0, 'INV-MOUNT: extension resource tree is mounted into container')

    adapter = root / 'src/benchpark_container/ramble_adapter.py'
    if adapter.exists():
        adapter_text = adapter.read_text(encoding='utf-8')
        if 'atomic_json_verified(spec_path, concrete)' not in adapter_text:
            issue(adapter, 0, 'INV-EVIDENCE: content-addressed concrete plan publication is not create-or-verify')

    modifier = resources / 'modifiers/bpce-execution/modifier.py'
    if modifier.exists() and any(isinstance(n, ast.Name) and n.id == '__file__'
                                 for n in ast.walk(ast.parse(modifier.read_text(encoding='utf-8')))):
        issue(modifier, 0, 'INV-RAMBLE: loader assumes __file__')

    application = root / 'examples/applications/common-base-smoke/application.py'
    if application.exists():
        text = application.read_text(encoding='utf-8')
        if '{bpce_python} /bench/benchmark.py' not in text or 'python3 /bench/' in text or 'python /bench/' in text:
            issue(application, 0, 'INV-TOOLS: application bypasses declared Base Python')

    return {
        'status': 'PASSED_RULES' if not issues else 'FAILED',
        'production_files': files,
        'issues': issues,
        'scope': 'current production boundary regression guards',
        'not_proven': ['real Apptainer/Singularity/Docker compatibility', 'arbitrary setup-script safety', 'absence of unknown bugs'],
    }


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--root', type=Path, default=ROOT)
    p.add_argument('--report', type=Path)
    a = p.parse_args()
    r = audit(a.root)
    text = json.dumps(r, indent=2) + '\n'
    if a.report:
        a.report.write_text(text, encoding='utf-8')
    print('architecture rules: ' + r['status'] + '; ' + str(len(r['production_files'])) + ' Python files; ' + str(len(r['issues'])) + ' issues')
    for i in r['issues']:
        print(i)
    return 0 if not r['issues'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
