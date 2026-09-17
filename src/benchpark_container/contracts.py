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
DEPENDENCY_POLICY = 'common-base-pip-requirements-v1'
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


def base_tools(value):
    strict(value, ('python', 'shell'), 'Common Base tools', ('python', 'shell'))
    return {k: executable_value(value[k], 'Common Base tools.' + k) for k in ('python', 'shell')}


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
