# SPDX-License-Identifier: Apache-2.0
"""Validation of explicit application observations, never inferred MPI success."""
from ..util import ValidationError


def validate_worker_bindings(workers, plan, binding, node_id):
    """v2 checks local identity without assuming c10d rank == outer task rank."""
    local = []
    groups = set()
    for worker in workers:
        if (worker.get('node_id') != node_id or worker.get('task_rank') != binding['rank']
                or type(worker.get('pid')) is not int or worker['pid'] <= 0
                or worker.get('pid_scope') != 'container'
                or type(worker.get('local_rank')) is not int or worker['local_rank'] < 0):
            raise ValidationError('Probe worker/task/node identity mismatch')
        local.append(worker['local_rank'])
        if 'group_rank' in worker:
            if type(worker['group_rank']) is not int or not 0 <= worker['group_rank'] < plan['tasks']:
                raise ValidationError('Invalid framework group rank')
            groups.add(worker['group_rank'])
    if plan['spawn_owner'] == 'launcher':
        if local != [binding['local_rank']]:
            raise ValidationError('MPI local rank differs from outer task binding')
    else:
        if sorted(local) != list(range(plan['workers_per_task'])):
            raise ValidationError('Application local worker membership mismatch')
        if any(w.get('local_world_size') != plan['workers_per_task'] for w in workers):
            raise ValidationError('Application local world size mismatch')
        if groups and (len(groups) != 1 or any('group_rank' not in w for w in workers)):
            raise ValidationError('Framework group rank inconsistent within an agent')


def validate_framework_groups(results, plan):
    workers = [w for result in results for w in result['mapping'].get('workers', [])]
    if not any('group_rank' in w for w in workers):
        return
    if any('group_rank' not in w for w in workers):
        raise ValidationError('Incomplete framework group evidence')
    groups = [set(w['group_rank'] for w in r['mapping'].get('workers', [])) for r in results]
    if any(len(g) != 1 for g in groups) or sorted(next(iter(g)) for g in groups) != list(range(plan['tasks'])):
        raise ValidationError('Framework group membership mismatch')
    # No arithmetic mapping of c10d group/global ranks to Scheduler task ranks.
