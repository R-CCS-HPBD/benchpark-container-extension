# SPDX-License-Identifier: Apache-2.0
"""Isolated execution adapter for the existing source-pinned collector API v1.

No provider discovery or runtime/session behavior changes. Import, factory and
collect all run in the child; the parent accepts only bounded JSON and files.
"""
from __future__ import annotations
import dataclasses
import hashlib
import importlib
import json
import os
from pathlib import Path
import sys

from .bounded import run_bounded
from .publication import (MAX_FILE_BYTES, MAX_BATCH_BYTES, MAX_FILES,
                          atomic_new, read_regular)

PROTOCOL_VERSION = 1
EXTERNAL_TIMEOUT_SECONDS = 10.0
MEMORY_LIMIT_BYTES = 512 * 1024 * 1024


def _unique(pairs):
    out = {}
    for k, v in pairs:
        if k in out:
            raise ValueError('Duplicate key in collector protocol')
        out[k] = v
    return out


def _json(raw):
    return json.loads(raw, object_pairs_hook=_unique,
                      parse_constant=lambda x: (_ for _ in ()).throw(ValueError('Nonfinite JSON')))


def _import_location():
    # A file imported from runtime.pyz has that archive as an ancestor. Do not
    # guess the package to import from ambient PYTHONPATH or the work directory.
    path = Path(__file__)
    for parent in path.parents:
        if parent.name.endswith('.pyz') and parent.is_file():
            return parent, __package__.split('.')[0]
    return path.parents[3], __package__.split('.')[0]


def execute_external(descriptor, ctx, record, control_dir):
    from .base import CollectorResult
    from ...providers import validate_descriptors
    validate_descriptors([descriptor])
    control_dir = Path(control_dir)
    request_path, response_path = control_dir/'request.json', control_dir/'response.json'
    request = {'protocol_version': PROTOCOL_VERSION, 'descriptor': descriptor,
               'attempt': str(ctx.attempt), 'env': dict(ctx.env), 'record': record,
               'capture_phase': ctx.capture_phase,
               'max_evidence_bytes': ctx.max_evidence_bytes}
    payload = json.dumps(request, sort_keys=True, allow_nan=False).encode()
    if len(payload) > MAX_BATCH_BYTES:
        raise ValueError('External collector request exceeds budget')
    atomic_new(request_path, payload)
    location, package = _import_location()
    bootstrap = ('import sys,importlib;sys.path.insert(0,sys.argv[1]);'
                 'm=importlib.import_module(sys.argv[2]);'
                 'm.child_main(sys.argv[3],sys.argv[4])')
    env = dict(ctx.env)
    env.pop('PYTHONPATH', None)
    env.pop('PYTHONHOME', None)
    cp = run_bounded([sys.executable, '-I', '-S', '-c', bootstrap,
          str(location), package+'.cer.collectors.isolated',
          str(request_path), str(response_path)], EXTERNAL_TIMEOUT_SECONDS,
          cwd=ctx.attempt, env=env, max_output_bytes=MAX_FILE_BYTES,
          watch_root=control_dir, max_workspace_bytes=3*MAX_BATCH_BYTES,
          max_workspace_files=3*MAX_FILES)
    receipt = {'schema_version': 1, 'protocol_version': PROTOCOL_VERSION,
               'provider_api_version': descriptor['api_version'],
               'provider_sha256': descriptor['sha256'], 'isolated': True,
               'outcome': cp.outcome, 'exit_code': cp.returncode,
               'duration_seconds': cp.duration_seconds,
               'timeout_seconds': EXTERNAL_TIMEOUT_SECONDS,
               'max_output_bytes': MAX_FILE_BYTES,
               'memory_limit_bytes': MEMORY_LIMIT_BYTES,
               'stdout_sha256': hashlib.sha256(cp.stdout).hexdigest(),
               'stderr_sha256': hashlib.sha256(cp.stderr).hexdigest(),
               'stdout_captured_bytes': len(cp.stdout),
               'stderr_captured_bytes': len(cp.stderr),
               'console_output_retention': 'not-retained; hashes of bounded captured bytes only',
               'context_contract': 'v1-private-registered-input-snapshot'}
    if cp.outcome != 'completed' or cp.returncode != 0:
        return CollectorResult(descriptor['name'], 'partial', errors=[
            'External collector '+cp.outcome+' exit='+str(cp.returncode)]), receipt
    try:
        data = _json(read_regular(response_path))
        if not isinstance(data, dict) or set(data) != {'protocol_version', 'result'} or data['protocol_version'] != 1:
            raise ValueError('Collector protocol mismatch')
        result = data['result']
        if not isinstance(result, dict) or set(result) != {'name','status','observed','files','errors'}:
            raise ValueError('Invalid collector result envelope')
        return CollectorResult(**result), receipt
    except (OSError, ValueError, TypeError) as exc:
        receipt['outcome'] = 'invalid-result'
        return CollectorResult(descriptor['name'], 'partial', errors=[
            'External collector invalid result: '+type(exc).__name__]), receipt


def child_main(request_path, response_path):
    # Install limits and restore normal termination BEFORE importing provider
    # code. Parent runtime may ignore termination while finalizing its CER.
    import resource
    import signal
    signal.signal(signal.SIGTERM, signal.SIG_DFL)
    signal.signal(signal.SIGINT, signal.SIG_DFL)
    os.umask(0o077)
    resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
    resource.setrlimit(resource.RLIMIT_AS, (MEMORY_LIMIT_BYTES, MEMORY_LIMIT_BYTES))
    resource.setrlimit(resource.RLIMIT_FSIZE, (MAX_BATCH_BYTES, MAX_BATCH_BYTES))
    request = _json(read_regular(Path(request_path), MAX_BATCH_BYTES))
    if request['protocol_version'] != PROTOCOL_VERSION:
        raise ValueError('Unsupported collector IPC version')
    from ...providers import load_provider
    from .base import CollectorContext, CollectorResult, CommandRunner
    ctx = CollectorContext(Path(request['attempt']), env=request['env'],
        max_evidence_bytes=request['max_evidence_bytes'], capture_phase=request['capture_phase'])
    result = load_provider(request['descriptor'])(ctx, request['record'], CommandRunner(owns_process_group=False))
    if not isinstance(result, CollectorResult):
        raise ValueError('Provider did not return CollectorResult')
    body = {'protocol_version': PROTOCOL_VERSION, 'result': dataclasses.asdict(result)}
    raw = json.dumps(body, sort_keys=True, allow_nan=False).encode()
    if len(raw) > MAX_FILE_BYTES:
        raise ValueError('Collector result JSON exceeds budget')
    atomic_new(Path(response_path), raw)
