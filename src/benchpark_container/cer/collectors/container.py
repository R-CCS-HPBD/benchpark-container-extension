# SPDX-License-Identifier: Apache-2.0
"""Normalize already-observed container execution identity without duplication."""
from __future__ import annotations
from pathlib import Path
from .base import CollectorResult
from ...util import sha256


def _gpu_passthrough(command):
    command=[str(x) for x in (command or [])]
    if '--nv' in command: return 'nvidia'
    if '--rocm' in command: return 'amd'
    return 'none-or-runtime-default'


def collect_container(record):
    r=CollectorResult('container')
    observed=record.get('observed',{})
    resolved=record.get('resolved',{})
    runtime=dict(observed.get('runtime') or {})
    image=dict(observed.get('image') or {})
    base=resolved.get('base') or {}
    selection=resolved.get('image_selection') or {}
    command=observed.get('command') or []
    executable=runtime.get('executable')
    runtime_exe_sha=None
    if executable:
        try:
            p=Path(executable)
            if p.is_file(): runtime_exe_sha=sha256(p)
        except (OSError, ValueError):
            pass
    software=observed.get('software_environment') or {}
    constructed=software.get('constructed') or software.get('base') or {}
    imports=constructed.get('imports') or {}
    r.observed={'container':{
        'schema_version':1,
        'runtime':{
            'name':runtime.get('name'), 'version':runtime.get('version'),
            'executable':executable, 'executable_sha256':runtime_exe_sha,
            'backend_api_version':runtime.get('backend_api_version'),
        },
        'image':{
            'kind':image.get('kind') or base.get('kind'),
            'identity':image.get('identity'),
            'sif_sha256':image.get('sif_sha256') or base.get('sif_sha256'),
            'logical_name':base.get('logical_name'), 'release':base.get('release'),
            'platform':base.get('platform'),
            'catalog_alias':selection.get('catalog_alias'),
            'catalog_entry_sha256':selection.get('entry_sha256'),
            'managed':selection.get('managed'),
        },
        'gpu_passthrough':_gpu_passthrough(command),
        'mount_count':len(observed.get('mounts') or []),
        'environment_names':sorted((observed.get('execution_environment') or {}).keys()),
        'software_imports':{k:{'version':v.get('version'),'file_sha256':v.get('file_sha256')}
                            for k,v in imports.items() if isinstance(v,dict)},
    }}
    return r
