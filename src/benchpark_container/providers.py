# SPDX-License-Identifier: Apache-2.0
"""Explicit, content-pinned external modules, not ambient plugin discovery.

A provider source defines factory(api). Its transitive dependencies must be
stdlib or the supplied API. It is TRUSTED host code, not a security sandbox.
The source is frozen into the resolved plan, so runtime.pyz requires no pip.
"""
import hashlib
import re
from pathlib import Path
from types import SimpleNamespace
from .util import ValidationError, strict

API_VERSION = 1
MAX_SOURCE_BYTES = 1024 * 1024


def provider_from_file(kind, name, path):
    code = Path(path).read_text(encoding='utf-8')
    value = {'schema_version': 1, 'api_version': API_VERSION, 'kind': kind,
             'name': name, 'code': code,
             'sha256': hashlib.sha256(code.encode('utf-8')).hexdigest()}
    validate_descriptors([value])
    return value


def validate_descriptors(values):
    if not isinstance(values, list): raise ValidationError('providers must be a list')
    seen = set()
    for value in values:
        strict(value, ('schema_version','api_version','kind','name','code','sha256'), 'provider',
               ('schema_version','api_version','kind','name','code','sha256'))
        if type(value['schema_version']) is not int or value['schema_version'] != 1 or type(value['api_version']) is not int or value['api_version'] != API_VERSION:
            raise ValidationError('Unsupported provider API/schema')
        if value['kind'] not in ('runtime','collector','session') or not isinstance(value['name'], str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*',value['name']):
            raise ValidationError('Invalid provider kind/name')
        key = (value['kind'], value['name'])
        if key in seen: raise ValidationError('Duplicate provider: '+str(key))
        seen.add(key)
        code = value['code']
        if not isinstance(code, str) or len(code.encode('utf-8')) > MAX_SOURCE_BYTES:
            raise ValidationError('Provider source missing or too large')
        if hashlib.sha256(code.encode('utf-8')).hexdigest() != value['sha256']:
            raise ValidationError('Provider source hash mismatch')
    return values


def load_provider(descriptor):
    validate_descriptors([descriptor])
    from .backends.base import RuntimeBackend, RuntimeCapabilities, ExecutionRequest, ImageRef, Mount
    from .cer.collectors.base import CollectorResult
    api = SimpleNamespace(version=API_VERSION, RuntimeBackend=RuntimeBackend,
        RuntimeCapabilities=RuntimeCapabilities, ExecutionRequest=ExecutionRequest,
        ImageRef=ImageRef, Mount=Mount, CollectorResult=CollectorResult,
        ValidationError=ValidationError)
    namespace = {'__name__': '_bpce_provider_' + descriptor['sha256'], '__builtins__': __builtins__}
    exec(compile(descriptor['code'], '<bpce-provider:'+descriptor['sha256']+'>', 'exec'), namespace)
    factory = namespace.get('factory')
    if not callable(factory): raise ValidationError('Provider must define factory(api)')
    result = factory(api)
    if not callable(result): raise ValidationError('Provider factory must return a callable')
    return result
