# SPDX-License-Identifier: Apache-2.0
"""One coordinator per attempt. Task spawning/stopping is delegated upstream."""
import json
import os
from pathlib import Path
import sys
import uuid
from ..util import ValidationError, atomic_json, identity, sha256
from .contract import validate_resolved
from .session import session_provider
from .evidence import create_group, finalize_group, TaskContext


def launch(spec, spec_path, resources, run_root, helper):
    from ..runtime import run_logged
    plan=validate_resolved(spec['parallel'])
    provider=session_provider(plan,spec['runtime'].get('providers',[]))
    timeout=float(spec['timeout_seconds'])
    if not 0<timeout<=7*86400:raise ValidationError('Invalid timeout_seconds')
    group=create_group(run_root,spec)
    worker=[spec['runtime']['worker_python'],str(helper), '--spec',str(spec_path),
            '--spec-sha256',sha256(spec_path),'--resources',str(resources),'--run-root',str(group/'containers'),
            '--parallel-task',str(group)]
    code,state=1,'EXECUTION_ERROR'
    try:
        if plan['schema_version'] == 2:
            from .admission import freeze_runtime_inputs
            freeze_runtime_inputs(group, spec, dict(os.environ))
        command=provider.command(plan,worker)
        if not isinstance(command,(list,tuple)) or not command or not all(isinstance(x,str) and '\x00' not in x for x in command):
            raise ValidationError('Session provider must return a host argv')
        atomic_json(group/'launcher.json',{'command':command,'worker_argv':worker,
                   'provider':plan['provider'],'ownership':('container-local-direct' if plan['provider']=='container-local-v1' else 'existing-upstream-launcher')})
        code,state=run_logged(command,group/'launcher.log',timeout,dict(os.environ))
    except BaseException as exc:
        atomic_json(group/'launcher-error.json',{'type':type(exc).__name__,'error':str(exc)})
    finally:
        code,record=finalize_group(group,code,state)
        print('BPCE_GROUP='+str(group/'group.json'),file=sys.stderr)
    return code


def task(spec,resources,run_root,group):
    from ..runtime import execute
    try:
        context=TaskContext(group,spec)
        return execute(spec,resources,run_root,execution_context=context)
    except BaseException as exc:
        directory=Path(group)/'rejections'
        if directory.is_dir():
            atomic_json(directory/(uuid.uuid4().hex+'.json'),{'type':type(exc).__name__,'error':str(exc)})
        print('BPCE parallel task failed: '+str(exc),file=sys.stderr)
        return 1
