# SPDX-License-Identifier: Apache-2.0
"""Additive collection with private outputs and isolated external collectors.

This retains explicit source-pinned v1 selection. Capability-based automatic
selection is a separate, not-yet-implemented design contract.
"""
from __future__ import annotations
import copy
from .base import CollectorContext, CommandRunner, CollectorResult, merge_additive, merge_file_metadata
from .hardware import collect_hardware
from .container import collect_container
from .publication import private_stage, publish_batch, snapshot_inputs
from .isolated import execute_external


def _state(name, result=None, error=None):
    if error is not None:
        return {'name':name,'status':'partial','errors':[type(error).__name__+': '+str(error)]}
    return {'name':name,'status':result.status,'errors':list(result.errors)}


def collect_hardware_safely(ctx, runner=None):
    """Node/serial hardware share the same private-publication boundary.

    Builtin Python normalization is trusted synchronous code; each CLI is
    separately bounded. This is not the future Evidence Type selector.
    """
    runner = runner or CommandRunner()
    with private_stage(ctx.attempt) as control:
        stage = control/'output'; stage.mkdir(mode=0o700)
        child = CollectorContext(stage, env=ctx.env, max_evidence_bytes=ctx.max_evidence_bytes,
                                 capture_phase=ctx.capture_phase)
        result = collect_hardware(child, runner)
        publish_batch(ctx.attempt, stage, result, 'hardware', {}, {})
        return result


def collect_v1(attempt, record, runner=None):
    ctx=CollectorContext(attempt)
    runner=runner or CommandRunner()
    observed={}; files={}; states=[]
    existing_observed=copy.deepcopy(record.get('observed') or {})
    existing_files=copy.deepcopy(record.get('files') or {})
    collectors=[('hardware', None), ('container', None)]
    from ...providers import validate_descriptors
    if 'node_evidence' in record.get('observed', {}) and 'execution_mapping' in record.get('observed', {}):
        collectors = [item for item in collectors if item[0] != 'hardware']
    descriptors=record.get('resolved', {}).get('runtime', {}).get('providers', [])
    try:
        validate_descriptors(descriptors)
        names={'hardware', 'container'}
        for descriptor in descriptors:
            if descriptor['kind'] == 'collector':
                if descriptor['name'] in names:
                    raise ValueError('External collector shadows reserved identity')
                names.add(descriptor['name'])
                collectors.append((descriptor['name'], descriptor))
    except Exception as exc:
        collectors=[item for item in collectors if item[1] is None]
        states.append(_state('provider-registry',error=exc))
    for name, descriptor in collectors:
        receipt=None
        try:
            with private_stage(attempt) as control:
                stage=control/'output';stage.mkdir(mode=0o700)
                child=CollectorContext(stage,env=ctx.env,max_evidence_bytes=ctx.max_evidence_bytes,
                                       capture_phase=ctx.capture_phase)
                if descriptor is not None:
                    # Preserve documented relative reads of registered evidence
                    # without handing v1 collectors the live writable directory.
                    snapshot_inputs(attempt, stage, existing_files)
                    result,receipt=execute_external(descriptor, child, copy.deepcopy(record), control)
                elif name == 'hardware':
                    result=collect_hardware(child,runner)
                else:
                    result=collect_container(copy.deepcopy(record))
                trial,trial_files=publish_batch(attempt,stage,result,name,existing_observed,existing_files)
                new_observed,new_files=copy.deepcopy(observed),copy.deepcopy(files)
                merge_additive(new_observed,result.observed)
                merge_file_metadata(new_files,result.files)
                observed,files=new_observed,new_files
                existing_observed,existing_files=trial,trial_files
                state=_state(name,result=result)
        except Exception as exc:
            state=_state(name,error=exc)
        if receipt is not None:
            state['execution']=receipt
        states.append(state)
    overall='partial' if any(x['status']=='partial' for x in states) else 'complete'
    return {'observed':observed,'files':files,
            'collection':{'schema_version':1,'status':overall,'non_fatal':True,
                          'capture_phase':'post-execution-finalization',
                          'identity_semantics':'excluded-from-condition-id-and-plan-sha256',
                          'collectors':states}}
