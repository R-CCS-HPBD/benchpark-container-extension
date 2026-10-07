# SPDX-License-Identifier: Apache-2.0
"""Execution contract, independent of Benchpark/Ramble and site defaults.

Paths below are versioned private container ABI, not host filesystem guesses.
Tool *values* come from System/Base declarations, never from fallback names.
"""
from pathlib import PurePosixPath
import re
from .util import ValidationError, strict

CONTRACT_VERSION = 1
ROOT = '/bpce'
INPUTS = ROOT + '/inputs'
PYTHON_PREFIX = ROOT + '/python'
TOOLS_PREFIX = ROOT + '/tools'
WORK = ROOT + '/work'
BENCHMARK_SCRIPT = ROOT + '/benchmark.sh'
DEPENDENCY_POLICY = 'common-base-pip-requirements-v1'  # Frozen legacy plans only.
PREPARATION_POLICY = 'declared-preparation-v1'
BASE_PROTECTED_ROOTS = ('/usr', '/bin', '/sbin', '/lib', '/lib64', '/etc', '/proc', '/sys', '/dev')


def executable_value(value, label):
    if (not isinstance(value, str) or not value or value.startswith('-')
            or any(x in value for x in ('\n', '\r', '\x00', '{', '}', '$', '`'))):
        raise ValidationError(label + ' must be a declared executable name or absolute path (not shell code)')
    if '/' in value and not PurePosixPath(value).is_absolute():
        raise ValidationError(label + ' must not depend on the current directory')
    if '/' not in value and any(c.isspace() for c in value):
        raise ValidationError(label + ' is one executable, not command+arguments')
    if '..' in PurePosixPath(value).parts:
        raise ValidationError(label + ' must be normalized')
    return value


def container_tools(value):
    """Generic execution needs a declared Bash, not a Python environment."""
    strict(value, ('python', 'shell'), 'Container tools', ('shell',))
    return {key: executable_value(tool, 'Container tools.' + key)
            for key, tool in value.items()}


def base_tools(value):
    """Strict Python/Common Base contract; no implicit interpreter fallback."""
    strict(value, ('python', 'shell'), 'Common Base tools', ('python', 'shell'))
    return container_tools(value)


def preparation_needs(spec):
    """Derive prerequisites from declared work, never from a benchmark name.

    The old policy is recognized only for frozen pre-change plans. New plans
    use PREPARATION_POLICY and need pip only when requirements are declared.
    """
    for key in ('requirements', 'setup_scripts', 'smoke_imports'):
        values = spec.get(key, [])
        if not isinstance(values, list) or any(not isinstance(v, str) or not v for v in values):
            raise ValidationError(key + ' must be a list of nonempty strings')
    for key in ('protected_packages', 'requirement_pins'):
        if not isinstance(spec.get(key, {}), dict):
            raise ValidationError(key + ' must be a mapping')
    if spec.get('requirement_pins') and not spec.get('requirements'):
        raise ValidationError('Requirement pins without requirements')
    legacy = spec.get('dependency_policy') == DEPENDENCY_POLICY
    pip = legacy or bool(spec.get('requirements'))
    python = pip or bool(spec.get('protected_packages') or spec.get('smoke_imports'))
    return {'python': python, 'pip': pip}


def validate_preparation(spec):
    """Validate a frozen preparation contract at every execution boundary."""
    if spec.get('dependency_policy') not in (PREPARATION_POLICY, DEPENDENCY_POLICY):
        raise ValidationError('Unknown/missing dependency policy; generate a new experiment')
    needs = preparation_needs(spec)
    tools = container_tools(spec['base']['tools'])
    if needs['python']:
        base_tools(tools)
    return needs


def validate_environment(environment, python_required):
    if not isinstance(environment, dict):
        raise ValidationError('environment must be a mapping')
    for key, value in environment.items():
        if (not isinstance(key, str) or not re.fullmatch(r'[A-Za-z_]\w*', key)
                or not isinstance(value, (str, int, float))):
            raise ValidationError('Invalid environment declaration')
        if key.startswith(('BPCE_', 'APPTAINER', 'SINGULARITY')):
            raise ValidationError('Container environment ownership conflict: ' + key)
        if python_required and (
            key.startswith('PIP_') or key in (
                'PATH', 'PYTHONPATH', 'PYTHONHOME', 'VIRTUAL_ENV',
                'PYTHONNOUSERSITE', 'PYTHONDONTWRITEBYTECODE')
        ):
            raise ValidationError('Python environment ownership conflict: ' + key)



def runtime_settings(value):
    strict(value, ('runtime', 'executable', 'worker_python', 'image_cache', 'gpu', 'backend_options'),
           'System execution', ('runtime', 'executable', 'worker_python', 'image_cache'))
    from .backends.registry import backend_class
    backend = backend_class(value['runtime'])
    if not isinstance(value.get('backend_options', {}), dict):
        raise ValidationError('backend_options must be a System mapping')
    for key in ('executable', 'worker_python'):
        executable_value(value[key], 'System execution.' + key)
    if not isinstance(value['image_cache'], str) or not PurePosixPath(value['image_cache']).is_absolute():
        raise ValidationError('image_cache must be an absolute System path')
    if value.get('gpu', 'none') not in ('none', 'nvidia', 'amd'):
        raise ValidationError('Unknown GPU passthrough policy')
    result = dict(value, gpu=value.get('gpu', 'none'))
    backend.validate_config(result)
    if result['gpu'] not in backend.capabilities.accelerators:
        raise ValidationError('Selected runtime does not support the requested GPU policy')
    return result


def validate_targets(artifacts, tools=(), concrete=False):
    targets = []
    for artifact in artifacts:
        target = artifact['target']
        if (not isinstance(target, str) or not target.startswith('/') or target == '/'
                or '..' in PurePosixPath(target).parts or '//' in target
                or any(c in target for c in ':,\n\r\x00')):
            raise ValidationError('Invalid mount target: ' + repr(target))
        if concrete and re.search(r'\{[A-Za-z_]\w*\}', target):
            raise ValidationError('Mount target is not concrete: ' + target)
        target = str(PurePosixPath(target))
        for protected in (ROOT,) + BASE_PROTECTED_ROOTS:
            if target == protected or target.startswith(protected + '/') or protected.startswith(target + '/'):
                raise ValidationError('Mount would replace a reserved/Base filesystem region: ' + target)
        for tool in tools:
            if tool.startswith('/') and (tool == target or tool.startswith(target + '/')):
                raise ValidationError('Mount would hide a declared Base tool: ' + tool)
        if any(target == old or target.startswith(old + '/') or old.startswith(target + '/') for old in targets):
            raise ValidationError('Overlapping mount targets: ' + target)
        targets.append(target)


def validate_accelerator_contract(plan):
    """Recheck the v1.3 selection after expansion and before running a backend.

    Older saved plans without this field keep their original execution contract.
    A present field cannot silently contradict the concrete plan parameters.
    """
    if 'accelerator_selection' not in plan:
        return  # Legacy plan; do not pretend it declared the v1.3 contract.
    selection = plan['accelerator_selection']
    parameters = plan.get('parameters', {})
    if not isinstance(parameters, dict):
        raise ValidationError('Accelerator parameters must be a mapping')

    def boolean(value):
        if type(value) is bool:
            return value
        # Ramble may expand booleans into their textual representation.
        if isinstance(value, str) and value.lower() in ('true', 'false'):
            return value.lower() == 'true'
        raise ValidationError('Accelerator flag must be boolean or an expanded true/false string')

    enabled = [name for name in ('cuda', 'rocm')
               if name in parameters and boolean(parameters[name])]
    if selection is None:
        if enabled or 'accelerator_backend' in parameters:
            raise ValidationError('Accelerator parameters have no recorded accelerator selection')
        return
    if not isinstance(selection, dict):
        raise ValidationError('Accelerator selection must be a mapping')
    backend = selection.get('backend')
    if not isinstance(backend, str):
        raise ValidationError('Accelerator backend must be a string')
    vendor = {'cuda': 'nvidia', 'rocm': 'amd'}.get(backend)
    if vendor is None or selection.get('accelerator') != vendor:
        raise ValidationError('Invalid accelerator selection/backend pair')
    if enabled != [backend]:
        raise ValidationError('Concrete accelerator flags differ from the resolved selection')
    if 'accelerator_backend' in parameters and parameters['accelerator_backend'] != backend:
        raise ValidationError('Exported accelerator_backend differs from the resolved selection')
    runtime, image = plan.get('runtime'), plan.get('image_selection')
    if not isinstance(runtime, dict) or not isinstance(image, dict):
        raise ValidationError('Accelerator runtime and image selections must be mappings')
    if (selection.get('system_gpu') != vendor or runtime.get('gpu') != vendor):
        raise ValidationError('System/runtime accelerator differs from the resolved selection')
    if image.get('accelerator') != vendor:
        raise ValidationError('Image accelerator differs from the resolved selection')
