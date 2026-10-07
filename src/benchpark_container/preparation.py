# SPDX-License-Identifier: Apache-2.0
"""Container-side environment preparation orchestrated from the host.

The host only launches the declared container runtime.  Every software action
below executes *inside the Common Base container* using the Base's declared
Bash and any tools needed by the declared work. The extension does not inject
Python, pip, a resolver, a
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
from .contracts import (base_tools, container_tools, executable_value, validate_targets,
                        validate_preparation, validate_environment, DEPENDENCY_POLICY, ROOT, INPUTS,
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


_SHELL_PROBE = r"""
test -n "${BASH_VERSION:-}" || exit 2
set -euo pipefail
printf '%s\0%s\0%s\0' "$BASH" "$BASH_VERSION" "${PATH-}"
"""

_TOOL_PROBE = r"""
import json, sys
print(json.dumps({"python": sys.executable,
                  "python_version": list(sys.version_info[:3])}))
"""


def preflight_shell(rt, image, scratch, inputs, mounts, requested, timeout, environment, attempt):
    """Probe the declared Bash with Bash itself; no interpreter injection."""
    shell = container_tools(requested)['shell']
    validate_targets(mounts, (shell,), concrete=True)
    proc, _ = _capture(rt, image, scratch, inputs, mounts,
        [shell, '-c', _SHELL_PROBE], timeout, environment, attempt / 'base-shell')
    fields = (proc.stdout or '').split('\0')
    if len(fields) != 4 or fields[-1] or not fields[0].startswith('/') or not fields[1]:
        raise EnvironmentBuildError('Invalid Bash probe output', 'BASE_SHELL_CAPABILITY_MISSING')
    observed = container_tools({'shell': fields[0]})
    validate_targets(mounts, (observed['shell'],), concrete=True)
    return dict(observed, shell_version=fields[1], path=fields[2])


def preflight_tools(rt, image, scratch, inputs, mounts, requested, timeout, environment, attempt,
                    require_pip=True):
    """Python preparation preflight; callers explicitly decide whether pip is needed."""
    requested = base_tools(requested)
    tools = preflight_shell(rt, image, scratch, inputs, mounts, requested, timeout, environment, attempt)
    python = _capture(rt, image, scratch, inputs, mounts,
        [requested['python'], '-c', _TOOL_PROBE], timeout,
        environment, attempt / 'base-tools', require_json=True)
    if tuple(python.get('python_version', ())) < (3, 8):
        raise EnvironmentBuildError('Common Base Python >=3.8 is required; no interpreter is injected',
                                    'BASE_PYTHON_CAPABILITY_MISSING')
    tools.update(python=executable_value(python.get('python'), 'Observed Base Python'),
                 python_version=python['python_version'])
    validate_targets(mounts, (tools['python'], tools['shell']), concrete=True)
    package_manager = {}
    if require_pip:
        # The selected Base supplies the only package manager. Probe once and
        # carry the observed command forward under one immutable evidence stem.
        pip_command = [tools['python'], '-m', 'pip']
        pip_proc, pip_argv = _capture(rt, image, scratch, inputs, mounts,
            pip_command + ['--version'], timeout, environment, attempt / 'base-pip-version')
        (attempt / 'base-pip-version.log').write_text(pip_proc.stdout or '', encoding='utf-8')
        package_manager = {
            'source': 'common-base', 'command': pip_command,
            'version_output': (pip_proc.stdout or '').strip(), 'probe_command': pip_argv,
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
    """Prepare declared per-experiment additions with tools from the selected Base.

    Bash is common to all execution. Python inspection and pip are requested
    by requirements/protection/import declarations, not by an image's language.
    Scripts share the private tools prefix, not their process environments.
    """
    result = {
        'schema_version': 6,
        'state': 'preparing', 'phase': 'contract-validation',
        'construction': spec.get('dependency_policy'),
        'requirements': spec.get('requirements', []),
        'setup_scripts': spec.get('setup_scripts', []),
        'protected_packages': spec.get('protected_packages', {}),
        'started_at': time.time(), 'install_commands': [], 'setup_commands': [],
        'boundary': 'container-native-tools/declared-preparation',
        'resolved_additions': [], 'validation': {'status': 'not-requested'},
    }
    out = attempt / 'environment.json'
    try:
        needs = validate_preparation(spec)
        result['required_tools'] = needs
        requested = container_tools(spec['base']['tools'])
        validate_environment(spec.get('environment', {}), needs['python'])
        # Frozen old plans retain their pre-change preparation environment.
        env = {} if spec['dependency_policy'] == DEPENDENCY_POLICY else dict(spec.get('environment', {}))
        if needs['python']:
            env.update(PYTHONNOUSERSITE='1', PYTHONDONTWRITEBYTECODE='1')
        tools_dir = scratch / 'tools'
        tools_dir.mkdir()
        result['phase'] = 'tool-preflight'
        if needs['python']:
            tools, package_manager = preflight_tools(
                rt, image, scratch, inputs, mounts, requested, timeout, env, attempt,
                require_pip=needs['pip'])
            if needs['pip']:
                result['package_manager'] = package_manager
        else:
            tools = preflight_shell(rt, image, scratch, inputs, mounts, requested, timeout, env, attempt)
        result['tools'] = tools
        activation = {'BPCE_BASE_SHELL': tools['shell'], 'BPCE_PREFIX': TOOLS_PREFIX}

        if needs['python']:
            python = tools['python']
            activation.update(BPCE_BASE_PYTHON=python,
                              PYTHONNOUSERSITE='1', PYTHONDONTWRITEBYTECODE='1')
            result['phase'] = 'base-inspection'
            protected = spec.get('protected_packages', {})
            before = _inspect(rt, image, scratch, inputs, mounts, python,
                protected.values(), timeout, env, log_stem=attempt / 'base-inspection')
            result['base'] = before
            for package in protected:
                if canonical(package) not in before['packages']:
                    raise EnvironmentBuildError('Required Base package is absent: ' + package, 'PROTECTED_BASE_MISSING')
            base_path = before['path']
        else:
            base_path = tools['path']

        if needs['pip']:
            prefix = scratch / 'python'
            prefix.mkdir()
            pip = package_manager['command']
            result['phase'] = 'base-dependency-baseline'
            baseline = _pip_check(rt, image, scratch, inputs, mounts, pip, timeout, env,
                                  attempt / 'pip-check-before.log')
            result['dependency_check_before'] = baseline

        if spec.get('setup_scripts'):
            activation['PATH'] = TOOLS_PREFIX + '/bin' + (':' + base_path if base_path else '')
        setup_env = dict(env, **activation)
        result['phase'] = 'setup-scripts'
        for index, rel in enumerate(spec.get('setup_scripts', []), 1):
            if not rel.startswith('inputs/') or '..' in Path(rel).parts:
                raise EnvironmentBuildError('Invalid setup script path: ' + rel, 'SETUP_SCRIPT_INVALID')
            command = [tools['shell'], INPUTS + '/' + rel[len('inputs/'):]]
            name = _safe_log_name(index, rel)
            try:
                proc, argv = _container_run(rt, image, scratch, inputs, mounts, command, timeout,
                                           environment=setup_env, pwd=INPUTS, log=attempt / name)
            except subprocess.TimeoutExpired as error:
                result['setup_commands'].append({'resource': rel, 'command': error.cmd,
                                                'log': name, 'timed_out': True})
                raise EnvironmentBuildError('Setup script timed out: ' + rel,
                                            'SETUP_SCRIPT_TIMEOUT') from error
            result['setup_commands'].append({'resource': rel, 'command': argv, 'log': name,
                                            'exit_code': proc.returncode})
            if proc.returncode:
                raise EnvironmentBuildError('Setup script failed: ' + rel, 'SETUP_SCRIPT_FAILED',
                                            {'exit_code': proc.returncode})
        result['tools_layer'] = tree_identity(tools_dir)

        if needs['pip']:
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
            else:  # Only a frozen legacy plan requests pip without requirements.
                (attempt / 'pip-install.log').write_text('No additional Python requirements.\n')
            result['phase'] = 'dependency-layer-validation'
            layer = _inspect(rt, image, scratch, inputs, mounts, python, [], timeout, env,
                             layer=PYTHON_PREFIX, log_stem=attempt / 'prefix-inspection')
            atomic_json(attempt / 'layer-inventory.json', layer)
            result['resolved_additions'] = layer['distributions']
            result['validation'] = validate_layer_inventory(
                before['packages'], layer['distributions'], spec.get('requirement_pins', {}))
            activation.update(_prefix_activation(layer, before))
            active = dict(env, **activation)
            result['phase'] = 'dependency-check'
            after_check = _pip_check(rt, image, scratch, inputs, mounts, pip, timeout, active,
                                     attempt / 'pip-check-after.log')
            result['dependency_check_after'] = after_check
            delta = dependency_check_delta(baseline, after_check)
            result['dependency_check_delta'] = {
                'new_issues': delta, 'baseline_exit_code': baseline['exit_code'],
                'after_exit_code': after_check['exit_code'],
            }
            if delta:
                raise EnvironmentBuildError('Additional requirements introduced dependency conflicts',
                                            'DEPENDENCY_CHECK_FAILED', {'new_issues': delta})
            result['prefix_identity'] = tree_identity(prefix)

        result['runtime_environment'] = activation
        if needs['python']:
            result['phase'] = 'import-validation'
            modules = list(dict.fromkeys(list(protected.values()) + spec.get('smoke_imports', [])))
            after = _inspect(rt, image, scratch, inputs, mounts, python, modules, timeout,
                             dict(env, **activation), log_stem=attempt / 'active-inspection')
            result['constructed'] = after
            for package, module in protected.items():
                if (after['packages'].get(canonical(package)) != before['packages'][canonical(package)]
                        or after['imports'].get(module) != before['imports'].get(module)):
                    raise EnvironmentBuildError('Base package/import changed: ' + package, 'PROTECTED_BASE_SHADOW')
            result['protected_validation'] = 'passed' if protected else 'not-requested'
            result['import_validation'] = {'status': 'passed', 'modules': modules} if modules else {'status': 'not-requested'}
        result.update(state='ready', phase='complete',
                      software_preparation='completed' if any((spec.get('setup_scripts'), needs['python']))
                      else 'not-requested')
        return result
    except BaseException as error:
        result.update(state='failed', error_type=type(error).__name__, error=str(error),
                      error_code=getattr(error, 'code', 'ENVIRONMENT_BUILD_FAILED'),
                      error_details=getattr(error, 'details', {}))
        raise
    finally:
        result['finished_at'] = time.time()
        atomic_json(out, result)
