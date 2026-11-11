# SPDX-License-Identifier: Apache-2.0
"""Group membership, per-node evidence and container links; never a Node CER tree.

Filesystem publication is deliberately the initial transport contract. All
participating nodes must see the same group/resources directory. Each member
owns a directory and immutable records; only the coordinator finalizes a group.
"""
import copy
import fcntl
import json
import os
from pathlib import Path
import shutil
import socket
import time
import uuid
from ..util import ValidationError, atomic_json, identity, inside, sha256, check_no_secrets
from ..cer.recording import now
from ..cer.collectors.base import CollectorContext, CommandRunner
from ..cer.collectors.registry import collect_hardware_safely
from .contract import validate_resolved
from .session import session_provider

MAX_PROBE_BYTES=1024*1024


def node_identity():
    try:boot=Path('/proc/sys/kernel/random/boot_id').read_text().strip()
    except OSError:boot='unavailable'
    data={'hostname':socket.gethostname(),'boot_id':boot}
    return identity(data),data


def reference(path,root):
    path=Path(path); root=Path(root)
    rel=path.relative_to(root)
    current=root
    for part in rel.parts:
        current=current/part
        if current.is_symlink():raise ValidationError('Symlink in evidence reference')
    if not path.is_file():raise ValidationError('Invalid evidence reference')
    return {'path':path.relative_to(root).as_posix(),'sha256':sha256(path),'bytes':path.stat().st_size}


def create_group(root,spec):
    plan=validate_resolved(spec['parallel'])
    root=Path(root).resolve();root.mkdir(parents=True,exist_ok=True)
    group=root/uuid.uuid4().hex;group.mkdir(mode=0o700)
    for name in ('members','nodes','shared-outputs','rejections'):(group/name).mkdir()
    data={'schema_version':1,'kind':'benchpark-execution-group','attempt_id':group.name,
          'condition_id':spec['condition_id'],'repeat_index':spec.get('repeat_index'),
          'ramble_experiment':spec.get('ramble_experiment'),'started_at':now(),
          'spec_sha256':identity(spec),'resolved':plan,'status':'RUNNING',
          'filesystem_contract':'shared-visible-paths','scientific_validity':'not-assessed',
          'measurement_validity':'apps-owned'}
    atomic_json(group/'started.json',data)
    return group


def _node_evidence(group):
    node_id,node=node_identity();directory=group/'nodes'/node_id;directory.mkdir(exist_ok=True)
    with (directory/'collection.lock').open('a+b') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        target=directory/'node.json'
        if not target.exists():
            data={'schema_version':1,'kind':'benchpark-node-evidence','node_id':node_id,
                  'group_attempt_id':group.name,'identity':node,'capture_phase':'pre-container-execution',
                  'captured_at':now(), 'scope':'host-visible; device visibility may be allocation-restricted'}
            try:
                ctx=CollectorContext(directory,capture_phase='pre-container-execution')
                result=collect_hardware_safely(ctx,CommandRunner())
                data.update(observed=result.observed,files=result.files,status=result.status,errors=result.errors)
            except Exception as exc:
                data.update(status='partial',errors=[type(exc).__name__+': '+str(exc)])
            data['record_sha256']=identity(data);atomic_json(target,data)
        saved=json.loads(target.read_text());expected=saved.pop('record_sha256')
        if identity(saved)!=expected or saved['node_id']!=node_id or saved['group_attempt_id']!=group.name:
            raise ValidationError('Node evidence identity mismatch')
    return node_id,reference(target,group)


class TaskContext:
    def __init__(self,group,spec):
        self.group=Path(group).resolve(strict=True);self.spec=spec;self.plan=validate_resolved(spec['parallel'])
        data=json.loads((self.group/'started.json').read_text())
        if data['spec_sha256']!=identity(spec) or data['attempt_id']!=self.group.name:
            raise ValidationError('Group/spec mismatch')
        if (self.group/'closed.json').exists():raise ValidationError('Group is already closed')
        provider=session_provider(self.plan,spec['runtime'].get('providers',[]))
        self.binding=provider.binding(self.plan,dict(os.environ))
        # Validate external providers' output too; shape does not imply app MPI success.
        if (set(self.binding)!= {'rank','size','local_rank'} or
                any(type(v) is not int or v<0 for v in self.binding.values()) or
                self.binding['size']!=self.plan['tasks'] or self.binding['rank']>=self.plan['tasks'] or
                self.binding['local_rank']>=self.plan['tasks']):
            raise ValidationError('Invalid provider task binding')
        self.slot=self.group/'members'/str(self.binding['rank'])
        self.slot.mkdir(mode=0o700,exist_ok=False) # duplicate rank is never last-wins
        self.node_id,self.node_ref=_node_evidence(self.group)
        self.mapping={'schema_version':1,'group_attempt_id':self.group.name,
            'node_id':self.node_id,'role_id':self.plan['role'],'session_id':self.plan['session'],
            'task_rank':self.binding['rank'],'task_world_size':self.binding['size'],
            'task_local_rank':self.binding['local_rank'],'spawn_owner':self.plan['spawn_owner'],
            'host_helper_pid':os.getpid(),'host_affinity':sorted(os.sched_getaffinity(0)) if hasattr(os,'sched_getaffinity') else None,
            'device_visibility':{k:os.environ[k] for k in ('CUDA_VISIBLE_DEVICES','ROCR_VISIBLE_DEVICES','HIP_VISIBLE_DEVICES') if k in os.environ},
            'worker_observation':'not-yet-collected','generation':0}
        atomic_json(self.slot/'task-started.json',self.mapping)
        self.shared_environment = {}
        if self.plan['schema_version'] == 2:
            from .admission import load_runtime_inputs, wait_for_members
            self.shared_environment = load_runtime_inputs(self.group, spec, dict(os.environ))
            admitted = wait_for_members(self.group, self.plan)
            atomic_json(self.slot/'admission.json', admitted)

    def started(self,attempt,record):
        record['observed']['execution_mapping']=copy.deepcopy(self.mapping)
        record['observed']['node_evidence']=dict(self.node_ref,group_path=str(self.group))
        atomic_json(self.slot/'container-started.json',{'attempt_id':record['attempt_id'],
             'directory':Path(attempt).relative_to(self.group).as_posix(),
             'group_attempt_id':self.group.name,'node_id':self.node_id})

    def environment(self):
        names=set(self.plan['environment'])|set(self.plan['rank_environment'].values())
        values={k:v for k,v in os.environ.items() if k in names or any(k.startswith(p) for p in self.plan['environment_prefixes'])}
        check_no_secrets(values)
        reserved={'BPCE_GROUP_ATTEMPT_ID':self.group.name,'BPCE_TASK_RANK':str(self.binding['rank']),
                  'BPCE_TASK_COUNT':str(self.binding['size']),'BPCE_TASK_LOCAL_RANK':str(self.binding['local_rank']),
                  'BPCE_EXPECTED_WORKERS':str(self.plan['expected_workers']),
                  'BPCE_WORKERS_PER_TASK':str(self.plan['workers_per_task']),
                  'BPCE_NODE_ID':self.node_id,'BPCE_PARALLEL_PROBE':'/bpce/work/parallel-probe.json'}
        if set(values)&set(reserved):raise ValidationError('Site environment collides with BPCE ownership')
        if self.plan['schema_version'] == 2:
            reserved.update(BPCE_NODE_COUNT=str(self.plan['nodes']),
                            BPCE_TASK_PLACEMENT=self.plan['placement'])
            from .admission import load_runtime_inputs
            shared = load_runtime_inputs(self.group, self.spec, dict(os.environ))
            if shared != self.shared_environment:
                raise ValidationError('Coordinator inputs changed after admission')
            values.update(shared)
        values.update(reserved);return values

    def capture_probe(self,attempt,record):
        source=Path(attempt)/'scratch/work/parallel-probe.json'
        if not source.exists():
            record['result']['parallel_probe']='missing' if self.plan['probe_required'] else 'not-requested'
            return not self.plan['probe_required']
        if source.is_symlink() or not source.is_file() or source.stat().st_size>MAX_PROBE_BYTES:
            raise ValidationError('Invalid parallel probe file')
        probe=json.loads(source.read_text())
        if type(probe.get('schema_version')) is not int or probe['schema_version']!=self.plan['schema_version'] or probe.get('group_attempt_id')!=self.group.name:
            raise ValidationError('Parallel probe schema/group mismatch')
        workers=probe.get('workers')
        if not isinstance(workers,list) or len(workers)!=self.plan['workers_per_task']:
            raise ValidationError('Parallel probe worker count mismatch')
        ranks=[]
        for worker in workers:
            if (not isinstance(worker,dict) or type(worker.get('rank')) is not int or
                    not 0<=worker['rank']<self.plan['expected_workers'] or
                    type(worker.get('world_size')) is not int or worker.get('world_size')!=self.plan['expected_workers']):
                raise ValidationError('Parallel probe rank/world mismatch')
            ranks.append(worker['rank'])
        if len(set(ranks))!=len(ranks):raise ValidationError('Parallel probe duplicate ranks')
        if self.plan['spawn_owner']=='launcher' and ranks!=[self.binding['rank']]:
            raise ValidationError('Application rank differs from launcher task rank')
        if self.plan['schema_version'] == 2:
            from .probe import validate_worker_bindings
            validate_worker_bindings(workers, self.plan, self.binding, self.node_id)
        if probe.get('collective_ok') is not True:raise ValidationError('Application collective probe failed')
        check_no_secrets(probe)
        target=Path(attempt)/'parallel-probe.json';shutil.copyfile(source,target)
        record['observed']['execution_mapping'].update(workers=workers,worker_observation='application-probe')
        record['result']['parallel_probe']='passed'
        return True

    def completed(self,attempt,record,code):
        atomic_json(self.slot/'result.json',{'schema_version':1,'group_attempt_id':self.group.name,
            'node_id':self.node_id,'task_rank':self.binding['rank'],'exit_code':code,
            'container_attempt_id':record['attempt_id'],'cer':reference(Path(attempt)/'cer.json',self.group),
            'mapping':copy.deepcopy(record['observed'].get('execution_mapping',{})),
            'parallel_probe':record['result'].get('parallel_probe','missing')})


def finalize_group(group,launcher_code,launcher_state):
    group=Path(group)
    if (group/'group.json').exists():raise ValidationError('Execution group is already finalized')
    data=json.loads((group/'started.json').read_text());plan=data['resolved']
    atomic_json(group/'closed.json',{'closed_at':now(),'launcher_exit_code':launcher_code})
    results=[];errors=[];ranks=[];nodes=set();workers=[]
    for path in sorted((group/'members').glob('*/result.json')):
        try:
            result=json.loads(path.read_text());ref=result['cer'];cer=inside(group,ref['path'],must_exist=True)
            checked=reference(cer,group)
            if checked != ref:raise ValidationError('Child CER hash/size mismatch')
            if result['group_attempt_id']!=group.name:raise ValidationError('Child group mismatch')
            child=json.loads(cer.read_text());h=child.pop('record_sha256')
            if identity(child)!=h or child['attempt_id']!=result['container_attempt_id'] or child['condition_id']!=data['condition_id']:
                raise ValidationError('Child CER identity mismatch')
            from ..cer.evidence import validate_files
            validate_files(cer.parent, child.get('files', {}))
            mapping=child['observed']['execution_mapping']
            if (type(result['task_rank']) is not int or str(result['task_rank'])!=path.parent.name
                    or mapping!=result['mapping'] or mapping['node_id']!=result['node_id']
                    or mapping['task_rank']!=result['task_rank']
                    or mapping['group_attempt_id']!=group.name
                    or child['result'].get('exit_code')!=result['exit_code']):
                raise ValidationError('Child task/mapping/result mismatch')
            node_ref=dict(child['observed']['node_evidence']);node_ref.pop('group_path',None)
            node_path=inside(group,node_ref['path'],must_exist=True)
            if reference(node_path,group)!=node_ref:raise ValidationError('Node evidence reference mismatch')
            ranks.append(result['task_rank']);nodes.add(result['node_id'])
            workers.extend(result['mapping'].get('workers',[]));results.append(result)
            if result['exit_code']!=0:errors.append('task-failed:'+str(result['task_rank']))
        except Exception as exc:errors.append(str(path.name)+': '+str(exc))
    if sorted(ranks)!=list(range(plan['tasks'])):errors.append('incomplete-or-duplicate-task-membership')
    if len(nodes)!=plan['nodes']:errors.append('participating-node-count-mismatch')
    if list((group/'rejections').glob('*.json')):errors.append('rejected-task-entry')
    probe='unverified'
    if len(results)==plan['tasks'] and all(x['parallel_probe']=='passed' for x in results):
        if sorted(x['rank'] for x in workers)==list(range(plan['expected_workers'])):
            probe='application-probe-passed'
        else:errors.append('application-world-membership-mismatch');probe='failed'
    elif plan['probe_required']:
        errors.append('required-application-probe-missing-or-failed');probe='failed'
    runtime_inputs_ref = None
    if plan['schema_version'] == 2:
        from .admission import validate_membership
        from .probe import validate_framework_groups
        try:
            # Values are observed, not folded into the immutable condition ID.
            runtime_data = json.loads((group/'runtime-inputs.json').read_text())
            runtime_hash = runtime_data.pop('record_sha256', None)
            if (identity(runtime_data) != runtime_hash
                    or runtime_data.get('group_attempt_id') != group.name
                    or runtime_data.get('spec_sha256') != data['spec_sha256']):
                raise ValidationError('Coordinator runtime input identity mismatch')
            runtime_inputs_ref = reference(group/'runtime-inputs.json', group)
            validate_membership(plan, [r['mapping'] for r in results], group.name)
            if probe == 'application-probe-passed':
                validate_framework_groups(results, plan)
        except Exception as exc:
            errors.append('v2-contract: '+str(exc))
    node_refs=[]; collection_partial=False
    from ..cer.evidence import validate_files
    for p in sorted((group/'nodes').glob('*/node.json')):
        try:
            node=json.loads(p.read_text());h=node.pop('record_sha256')
            if identity(node)!=h:raise ValidationError('Node record integrity mismatch')
            validate_files(p.parent,node.get('files',{}))
            node_refs.append(reference(p,group))
            collection_partial |= node.get('status')!='complete'
        except Exception as exc:
            errors.append('node-evidence-integrity: '+str(exc));collection_partial=True
    shared_files=[]
    for p in sorted((group/'shared-outputs').rglob('*')):
        if p.is_symlink():
            errors.append('shared-output-symlink: '+p.relative_to(group).as_posix())
        elif p.is_file():
            try:shared_files.append(reference(p,group))
            except Exception as exc:errors.append('shared-output-integrity: '+str(exc))
    code=launcher_code if launcher_code else (1 if errors else 0)
    status='COMPLETED' if code==0 else (launcher_state if launcher_state in ('TIMEOUT','INTERRUPTED') else 'FAILED')
    incomplete='incomplete-or-duplicate-task-membership' in errors
    if incomplete and launcher_code==0:status='INCOMPLETE'
    data.update(status=status,finished_at=now(),launcher_status=launcher_state,exit_code=code,
        members=results,nodes=node_refs,shared_outputs=shared_files,
        errors=errors,parallel_validation=probe,
        cleanup_status='unverified-missing-members' if incomplete else 'child-runtime-status-recorded',
        evidence_completeness='partial' if incomplete or collection_partial else 'membership-records-complete',
        resource_binding_validation='host-visible-only; no new enforcement',
        communication_validation='application-reported' if probe=='application-probe-passed' else 'unverified')
    if plan['schema_version'] == 2:
        data['runtime_inputs'] = runtime_inputs_ref
        data['placement_validation'] = 'passed' if not errors else 'failed'
    data['record_sha256']=identity(data);atomic_json(group/'group.json',data)
    return code,data
