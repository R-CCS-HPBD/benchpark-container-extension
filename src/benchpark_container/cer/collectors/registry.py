# SPDX-License-Identifier: Apache-2.0
"""CER additive collection v1: hardware + container identity."""
from __future__ import annotations
import copy
from .base import CollectorContext, CommandRunner, merge_additive, merge_file_metadata
from .hardware import collect_hardware
from .container import collect_container


def _state(name, result=None, error=None):
    if error is not None:
        return {'name':name,'status':'partial','errors':[type(error).__name__+': '+str(error)]}
    return {'name':name,'status':result.status,'errors':list(result.errors)}


def collect_v1(attempt, record, runner=None):
    """Return additive metadata. Never raise into benchmark finalization.

    Collector data is checked against fields already present in the CER. A
    collision is reported as partial collection and the original field wins.
    """
    ctx=CollectorContext(attempt)
    runner=runner or CommandRunner()
    observed={}
    files={}
    states=[]
    existing_observed=copy.deepcopy(record.get('observed') or {})
    existing_files=copy.deepcopy(record.get('files') or {})

    for name, fn in (('hardware', lambda: collect_hardware(ctx,runner)),
                     ('container', lambda: collect_container(record))):
        try:
            result=fn()
            trial=copy.deepcopy(existing_observed)
            merge_additive(trial,result.observed)
            merge_additive(observed,result.observed)
            merge_additive(existing_observed,result.observed)
            merge_file_metadata(existing_files,result.files)
            merge_file_metadata(files,result.files)
            states.append(_state(name,result=result))
        except Exception as exc:
            states.append(_state(name,error=exc))

    overall='partial' if any(x['status']=='partial' for x in states) else 'complete'
    return {'observed':observed,'files':files,
            'collection':{'schema_version':1,'status':overall,'non_fatal':True,
                          'capture_phase':'post-execution-finalization',
                          'identity_semantics':'excluded-from-condition-id-and-plan-sha256',
                          'collectors':states}}
