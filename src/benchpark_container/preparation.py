# SPDX-License-Identifier: Apache-2.0
"""Container-side environment preparation orchestrated from the host.

The host only launches the declared container runtime.  Every software action
below executes *inside the Common Base container* using the Base's declared
Python/pip/Bash.  The extension does not inject Python, pip, a resolver, a
package-manager binary, or a venv bootstrap.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import signal
import subprocess
import time

from .util import ValidationError, atomic_json
from .backends.base import command_scope
from .reproducibility_rules import canonical
from .contracts import (base_tools, validate_targets, DEPENDENCY_POLICY, ROOT, INPUTS,
                        PYTHON_PREFIX, TOOLS_PREFIX, WORK)
from .artifacts import tree_identity


class EnvironmentBuildError(RuntimeError):
    def __init__(self, message, code="ENVIRONMENT_BUILD_FAILED", details=None):
        self.code = code
        self.details = details or {}
        super().__init__(message)


_INSPECT = r'''
import hashlib, importlib, importlib.metadata, json, os, re, site, sys, pathlib
sys.dont_write_bytecode = True
def canonical(n): return re.sub(r"[-_.]+", "-", n).lower()
def fhash(path):
    h=hashlib.sha256()
    with open(path,"rb") as f:
        for b in iter(lambda:f.read(1024*1024),b""): h.update(b)
    return h.hexdigest()
modules=json.loads(sys.argv[1])
mode=sys.argv[2]
search=None
pth=[]
bins=[]
if mode!="environment":
    root=pathlib.Path(sys.argv[3])
    search=sorted({str(p.parent) for p in root.rglob("*.dist-info") if p.is_dir()})
    pth=sorted(str(p) for p in root.rglob("*.pth"))
    bins=sorted(str(p) for p in root.rglob("bin") if p.is_dir())
dists=importlib.metadata.distributions() if search is None else importlib.metadata.distributions(path=search)
packages={}
items=[]
for d in dists:
    n=d.metadata.get("Name")
    if not n: continue
    name=canonical(n); version=str(d.version)
    packages.setdefault(name,version)
    if search is not None:
        hashes=[]
        for f in sorted(d.files or [], key=lambda x:str(x)):
            p=d.locate_file(f)
            try:
                if p.is_file(): hashes.append(str(f)+"\0"+fhash(p))
            except OSError: pass
        h=hashlib.sha256("\n".join(hashes).encode()).hexdigest()
        items.append({"name":name,"version":version,"content_sha256":h})
loaded={}
if mode=="environment":
    for name in modules:
        m=importlib.import_module(name)
        p=os.path.realpath(m.__file__) if getattr(m,"__file__",None) else None
        loaded[name]={"file":p,"file_sha256":fhash(p) if p and os.path.isfile(p) else None,
                      "version":str(getattr(m,"__version__","unknown"))}
paths=[]
for p in list(site.getsitepackages())+list(sys.path):
    if p and os.path.isdir(p) and os.path.basename(p) in ("site-packages","dist-packages"):
        p=os.path.realpath(p)
        if p not in paths: paths.append(p)
print(json.dumps({"python":sys.version,"executable":sys.executable,"prefix":sys.prefix,
                  "packages":packages,"distributions":sorted(items,key=lambda x:(x["name"],x["version"])),
                  "imports":loaded,"site_paths":paths,"path":os.environ.get("PATH",""),
                  "pythonpath":os.environ.get("PYTHONPATH", ""), "pythonhome":os.environ.get("PYTHONHOME", ""),
                  "python_version":list(sys.version_info[:3]),
                  "prefix_sites":search or [], "prefix_bins":bins, "pth_files":pth}))
'''


def _container_run(rt, image, scratch, inputs, mounts, command, timeout,
                   environment=None, pwd=WORK, log=None, check=False):
    """Launch one command through the container runtime; never execute it natively."""
    argv = rt.argv(image, scratch, inputs, mounts, command, environment, pwd=pwd)
    stream = open(log, 'wb') if log is not None else None
    try:
        with command_scope(rt, argv):
            with subprocess.Popen(argv, stdout=stream or subprocess.PIPE,
                                  stderr=subprocess.STDOUT if stream else subprocess.PIPE,
                                  text=stream is None, env=rt.host_env(), start_new_session=True) as child:
                try:
                    stdout, stderr = child.communicate(timeout=timeout)
                except BaseException as error:
                    try:
                        os.killpg(child.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                    stdout, stderr = child.communicate()
                    if isinstance(error, subprocess.TimeoutExpired):
                        error.stdout, error.stderr = stdout, stderr
                    raise
                proc = subprocess.CompletedProcess(argv, child.returncode, stdout, stderr)
    finally:
        if stream is not None:
            stream.close()
    if check and proc.returncode:
        raise subprocess.CalledProcessError(proc.returncode, argv,
            output=getattr(proc, 'stdout', None), stderr=getattr(proc, 'stderr', None))
    return proc, argv


def _capture(rt, image, scratch, inputs, mounts, command, timeout, environment, stem,
             require_json=False, check=True, pwd=WORK):
    stem = Path(stem)
    evidence = [stem.with_suffix('.command.json'), stem.with_suffix('.stdout.log'),
                stem.with_suffix('.stderr.log')]
    existing = [str(path) for path in evidence if path.exists()]
    if existing:
        raise EnvironmentBuildError(
            'Evidence stem reused inside one attempt: ' + str(stem),
            'EVIDENCE_STEM_REUSED', {'stem': str(stem), 'existing': existing})
    try:
        proc, argv = _container_run(rt, image, scratch, inputs, mounts, command, timeout,
                                    environment=environment, pwd=pwd)
    except subprocess.TimeoutExpired as error:
        def text(value):
            return value.decode('utf-8', errors='replace') if isinstance(value, bytes) else value or ''
        stem.with_suffix('.stdout.log').write_text(text(error.stdout), encoding='utf-8')
        stem.with_suffix('.stderr.log').write_text(text(error.stderr), encoding='utf-8')
        atomic_json(stem.with_suffix('.command.json'), {'argv': error.cmd, 'timeout_seconds': timeout, 'timed_out': True})
        raise EnvironmentBuildError('Common Base command timed out; see ' + str(stem),
            'BASE_PROBE_TIMEOUT', {'log': stem.name, 'timeout_seconds': timeout}) from error
    stem.with_suffix('.stdout.log').write_text(proc.stdout or '', encoding='utf-8')
    stem.with_suffix('.stderr.log').write_text(getattr(proc, 'stderr', '') or '', encoding='utf-8')
    atomic_json(stem.with_suffix('.command.json'), {'argv': argv, 'exit_code': proc.returncode})
    if check and proc.returncode:
        raise EnvironmentBuildError('Common Base command failed; see ' + str(stem),
            'BASE_COMMAND_FAILED', {'command': argv, 'exit_code': proc.returncode, 'log': stem.name})
    if require_json:
        try:
            data = json.loads(proc.stdout)
        except (ValueError, TypeError) as error:
            raise EnvironmentBuildError('Invalid probe JSON; see ' + str(stem),
                'BASE_PROBE_INVALID_JSON', {'log': stem.name}) from error
        if not isinstance(data, dict):
            raise EnvironmentBuildError('Base probe result must be an object', 'BASE_PROBE_INVALID_JSON')
        return data
    return proc, argv


def _inspect(rt, image, scratch, inputs, mounts, python, modules, timeout,
             environment=None, layer=None, log_stem=None):
    command = [python, '-c', _INSPECT, json.dumps(list(modules)), 'prefix' if layer else 'environment']
    if layer:
        command.append(layer)
    return _capture(rt, image, scratch, inputs, mounts, command, timeout, environment,
                    log_stem or (Path(scratch).parent / 'inspection'), require_json=True)


_TOOL_PROBE = r'''
import json, os, shutil, sys
shell = shutil.which(sys.argv[1])
if shell is None:
    raise RuntimeError("Declared Base Bash not found: " + sys.argv[1])
print(json.dumps({"python": sys.executable, "shell": os.path.abspath(shell),
                  "python_version": list(sys.version_info[:3])}))
'''


def preflight_tools(rt, image, scratch, inputs, mounts, requested, timeout, environment, attempt):
    requested = base_tools(requested)
    validate_targets(mounts, requested.values(), concrete=True)
    tools = _capture(rt, image, scratch, inputs, mounts,
        [requested['python'], '-c', _TOOL_PROBE, requested['shell']], timeout,
        environment, attempt / 'base-tools', require_json=True)
    if tuple(tools['python_version']) < (3, 8):
        raise EnvironmentBuildError('Common Base Python >=3.8 is required; no interpreter is injected',
                                    'BASE_PYTHON_CAPABILITY_MISSING')
    validate_targets(mounts, (tools['python'], tools['shell']), concrete=True)
    # setup scripts and the benchmark wrapper are Bash by contract.
    _capture(rt, image, scratch, inputs, mounts,
        [tools['shell'], '-c', 'test -n "$BASH_VERSION" && set -euo pipefail && printf "%s\\n" "$BASH_VERSION"'],
        timeout, environment, attempt / 'base-shell')
    # This is the only package manager used by the extension. Probe it exactly
    # once per attempt and carry the observed result forward instead of running
    # a second probe under the same evidence name later in preparation.
    pip_command = [tools['python'], '-m', 'pip']
    pip_proc, pip_argv = _capture(rt, image, scratch, inputs, mounts,
        pip_command + ['--version'], timeout, environment, attempt / 'base-pip-version')
    (attempt / 'base-pip-version.log').write_text(pip_proc.stdout or '', encoding='utf-8')
    package_manager = {
        'source': 'common-base',
        'command': pip_command,
        'version_output': (pip_proc.stdout or '').strip(),
        'probe_command': pip_argv,
    }
    return tools, package_manager


def _pip_check(rt, image, scratch, inputs, mounts, pip_command, timeout, environment, log_path):
    """Run Base pip check and retain raw evidence without parsing version-specific prose."""
    stem = Path(log_path).with_suffix('')
    proc, argv = _capture(rt, image, scratch, inputs, mounts, pip_command + ['check'],
        timeout, environment, stem, check=False, pwd=INPUTS)
    text = proc.stdout or ''
    Path(log_path).write_text(text, encoding='utf-8')
    if proc.returncode == 0:
        return {'exit_code': 0, 'issues': [], 'stdout': text, 'command': argv, 'checker_status': 'ok'}
    stderr = getattr(proc, 'stderr', '') or ''
    if proc.returncode != 1 or 'Traceback' in stderr:
        raise EnvironmentBuildError('Common Base pip check could not complete; see ' + str(log_path),
            'DEPENDENCY_CHECKER_FAILED', {'exit_code': proc.returncode, 'log': Path(log_path).name})
    issues = sorted({line.strip() for line in text.splitlines() if line.strip()})
    if not issues:
        raise EnvironmentBuildError('Common Base pip check failed without dependency diagnostics; see ' + str(log_path),
            'DEPENDENCY_CHECKER_FAILED', {'exit_code': proc.returncode, 'log': Path(log_path).name})
    return {'exit_code': 1, 'issues': issues, 'stdout': text, 'command': argv, 'checker_status': 'issues'}


def dependency_check_delta(before, after):
    return sorted(set(after.get('issues', [])) - set(before.get('issues', [])))


def validate_layer_inventory(base_packages, distributions, pins):
    base = {canonical(k): str(v) for k, v in (base_packages or {}).items()}
    fixed = {canonical(k): [str(x) for x in v] for k, v in (pins or {}).items()}
    conflicts, unpinned = [], []
    for item in distributions:
        name, version = canonical(item['name']), str(item['version'])
        if name in base:
            conflicts.append({'name': name, 'base_version': base[name], 'layer_version': version})
        if version not in fixed.get(name, []):
            unpinned.append({'name': name, 'version': version})
    if conflicts:
        raise EnvironmentBuildError('Python requirements shadow a distribution already supplied by the Common Base',
                                    'BASE_VERSION_CONFLICT', {'conflicts': conflicts})
    if unpinned:
        raise EnvironmentBuildError('Unpinned additional/transitive dependencies installed by Common Base pip',
                                    'UNPINNED_TRANSITIVE_DEPENDENCY', {'packages': unpinned})
    return {'status': 'passed', 'validated_additions': len(distributions),
            'base_shadow_conflicts': [], 'unpinned_additions': []}


def _safe_log_name(index, relative):
    name = re.sub(r'[^A-Za-z0-9_.-]+', '-', Path(relative).name).strip('-') or 'setup'
    return 'setup-%02d-%s.log' % (index, name)


def _prefix_activation(info, before):
    sites, bins = info.get('prefix_sites', []), info.get('prefix_bins', [])
    for path in sites + bins:
        if path != PYTHON_PREFIX and not path.startswith(PYTHON_PREFIX + '/'):
            raise EnvironmentBuildError('Prefix layout escaped private install root', 'INVALID_PREFIX_LAYOUT')
    if info.get('pth_files'):
        raise EnvironmentBuildError('A requirement installed .pth activation that cannot be represented by the private prefix contract; put it in the Common Base or a setup script',
                                    'UNSUPPORTED_PREFIX_ACTIVATION', {'files': info['pth_files']})
    return {
        'PYTHONPATH': ':'.join(sites + ([before['pythonpath']] if before.get('pythonpath') else [])),
        'PATH': ':'.join(bins + [TOOLS_PREFIX + '/bin', before['path']]),
        'BPCE_PREFIX': TOOLS_PREFIX,
        'PYTHONNOUSERSITE': '1',
        'PYTHONDONTWRITEBYTECODE': '1',
    }


def prepare_environment(rt, image, scratch, inputs, mounts, spec, attempt, timeout):
    """Build the run-local delta strictly with tools from the Common Base.

    Boundary:
      * mount requirement text and Git-managed setup scripts;
      * run Base ``python -m pip install --prefix ... -r ...`` inside container;
      * run setup scripts with Base Bash inside container;
      * never invoke host Python/pip as the environment builder;
      * never mount/inject pip, Python, a resolver or package-manager payload.
    """
    if spec.get('dependency_policy') != DEPENDENCY_POLICY:
        raise EnvironmentBuildError('Unknown/missing dependency policy; generate a new experiment', 'UNKNOWN_DEPENDENCY_POLICY')
    requested = base_tools(spec['base']['tools'])
    prefix, tools_dir = scratch / 'python', scratch / 'tools'
    prefix.mkdir()
    tools_dir.mkdir()
    # These only prevent user-site/bytecode leakage; no pip implementation or
    # resolver policy is supplied by the extension.
    env = {'PYTHONNOUSERSITE': '1', 'PYTHONDONTWRITEBYTECODE': '1'}
    result = {
        'schema_version': 5,
        'state': 'preparing',
        'phase': 'tool-preflight',
        'construction': DEPENDENCY_POLICY,
        'requirements': spec.get('requirements', []),
        'setup_scripts': spec.get('setup_scripts', []),
        'protected_packages': spec.get('protected_packages', {}),
        'started_at': time.time(),
        'install_commands': [],
        'setup_commands': [],
        'boundary': 'container-native-tools/requirements-text+setup-scripts',
    }
    out = attempt / 'environment.json'
    try:
        tools, package_manager = preflight_tools(
            rt, image, scratch, inputs, mounts, requested, timeout, env, attempt)
        result['tools'] = tools
        result['package_manager'] = package_manager
        python = tools['python']
        pip = package_manager['command']

        result['phase'] = 'base-inspection'
        before = _inspect(rt, image, scratch, inputs, mounts, python,
            spec.get('protected_packages', {}).values(), timeout, env, log_stem=attempt / 'base-inspection')
        result['base'] = before
        protected = spec.get('protected_packages', {})
        for package in protected:
            if canonical(package) not in before['packages']:
                raise EnvironmentBuildError('Required Base package is absent: ' + package, 'PROTECTED_BASE_MISSING')
        result['phase'] = 'base-dependency-baseline'
        baseline = _pip_check(rt, image, scratch, inputs, mounts, pip, timeout, env, attempt / 'pip-check-before.log')
        result['dependency_check_before'] = baseline

        # Command/tool prerequisites are installed only by Git-managed scripts,
        # executed with the Base Bash.  Their writable destination is /bpce/tools.
        setup_env = dict(env, BPCE_PREFIX=TOOLS_PREFIX, BPCE_BASE_PYTHON=python,
                         BPCE_BASE_SHELL=tools['shell'], PATH=TOOLS_PREFIX + '/bin:' + before['path'])
        result['phase'] = 'setup-scripts'
        for index, rel in enumerate(spec.get('setup_scripts', []), 1):
            if not rel.startswith('inputs/') or '..' in Path(rel).parts:
                raise EnvironmentBuildError('Invalid setup script path: ' + rel, 'SETUP_SCRIPT_INVALID')
            command = [tools['shell'], INPUTS + '/' + rel[len('inputs/'):]]
            name = _safe_log_name(index, rel)
            proc, argv = _container_run(rt, image, scratch, inputs, mounts, command, timeout,
                                       environment=setup_env, pwd=INPUTS, log=attempt / name)
            result['setup_commands'].append({'resource': rel, 'command': argv, 'log': name, 'exit_code': proc.returncode})
            if proc.returncode:
                raise EnvironmentBuildError('Setup script failed: ' + rel, 'SETUP_SCRIPT_FAILED', {'exit_code': proc.returncode})
        result['tools_layer'] = tree_identity(tools_dir)

        # Python dependency semantics come from the requirement files and the
        # selected Common Base pip.  The extension supplies only the writable
        # destination prefix required by an immutable SIF.
        result['phase'] = 'dependency-installation'
        if spec.get('requirements'):
            command = pip + ['install', '--prefix', PYTHON_PREFIX]
            for rel in spec['requirements']:
                if not rel.startswith('inputs/') or '..' in Path(rel).parts:
                    raise EnvironmentBuildError('Invalid staged requirements path: ' + rel)
                command += ['--requirement', INPUTS + '/' + rel[len('inputs/'):]]
            proc, argv = _container_run(rt, image, scratch, inputs, mounts, command, timeout,
                                       environment=env, pwd=INPUTS, log=attempt / 'pip-install.log')
            result['install_commands'].append(argv)
            if proc.returncode:
                raise EnvironmentBuildError('Common Base pip dependency installation failed; see pip-install.log',
                                            'DEPENDENCY_INSTALL_FAILED', {'exit_code': proc.returncode})
        else:
            (attempt / 'pip-install.log').write_text('No additional Python requirements.\n')

        result['phase'] = 'dependency-layer-validation'
        layer = _inspect(rt, image, scratch, inputs, mounts, python, [], timeout, env,
                         layer=PYTHON_PREFIX, log_stem=attempt / 'prefix-inspection')
        atomic_json(attempt / 'layer-inventory.json', layer)
        result['resolved_additions'] = layer['distributions']
        result['validation'] = validate_layer_inventory(before['packages'], layer['distributions'], spec.get('requirement_pins', {}))
        activation = _prefix_activation(layer, before)
        activation.update(BPCE_BASE_PYTHON=python, BPCE_BASE_SHELL=tools['shell'])
        active = dict(env, **activation)
        result['runtime_environment'] = activation

        result['phase'] = 'dependency-check'
        after_check = _pip_check(rt, image, scratch, inputs, mounts, pip, timeout, active, attempt / 'pip-check-after.log')
        result['dependency_check_after'] = after_check
        delta = dependency_check_delta(baseline, after_check)
        result['dependency_check_delta'] = {
            'new_issues': delta,
            'baseline_exit_code': baseline['exit_code'],
            'after_exit_code': after_check['exit_code'],
        }
        if delta:
            raise EnvironmentBuildError('Additional requirements introduced dependency conflicts',
                                        'DEPENDENCY_CHECK_FAILED', {'new_issues': delta})

        result['phase'] = 'import-validation'
        modules = list(dict.fromkeys(list(protected.values()) + spec.get('smoke_imports', [])))
        after = _inspect(rt, image, scratch, inputs, mounts, python, modules, timeout, active,
                         log_stem=attempt / 'active-inspection')
        result['constructed'] = after
        for package, module in protected.items():
            if (after['packages'].get(canonical(package)) != before['packages'][canonical(package)]
                    or after['imports'].get(module) != before['imports'].get(module)):
                raise EnvironmentBuildError('Base package/import changed: ' + package, 'PROTECTED_BASE_SHADOW')

        result['prefix_identity'] = tree_identity(prefix)
        result.update(state='ready', phase='complete', protected_validation='passed')
        return result
    except BaseException as error:
        result.update(state='failed', error_type=type(error).__name__, error=str(error),
                      error_code=getattr(error, 'code', 'ENVIRONMENT_BUILD_FAILED'),
                      error_details=getattr(error, 'details', {}))
        raise
    finally:
        result['finished_at'] = time.time()
        atomic_json(out, result)
