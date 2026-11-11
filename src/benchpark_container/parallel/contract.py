# SPDX-License-Identifier: Apache-2.0
"""Versioned, scheduler-neutral parallel declarations.

v2 fixes outer-task accounting and adds explicit placement/admission and shared
runtime inputs. It does not infer resource counts, launchers or device policy.
"""
import copy
import re
from ..util import ValidationError, strict, expand, check_no_secrets

SITE_KEYS = ('provider', 'rank_environment', 'environment', 'environment_prefixes')
V1_APP_KEYS = ('schema_version', 'nodes', 'tasks', 'workers_per_task', 'spawn_owner',
               'role', 'session', 'probe_required', 'shared_outputs', 'rank_accounting')
V2_KEYS = ('placement', 'coordinator_environment', 'startup_timeout_seconds')
APP_KEYS = (*V1_APP_KEYS, *V2_KEYS)
RESOLVED_KEYS = (*SITE_KEYS, *APP_KEYS, 'launcher', 'expected_workers')
NAME = re.compile(r'[A-Za-z_][A-Za-z0-9_]*')
REFERENCE = re.compile(r'\{[A-Za-z_][A-Za-z0-9_]*\}')


def positive(value, name):
    if isinstance(value, bool) or not re.fullmatch(r'[1-9][0-9]*', str(value)):
        raise ValidationError('Parallel ' + name + ' must be a positive integer')
    return int(value)


def _names(values, label):
    if (not isinstance(values, list) or not all(isinstance(x, str) for x in values)
            or len(set(values)) != len(values)):
        raise ValidationError(label + ' must be a unique list')
    if any(not NAME.fullmatch(x) for x in values):
        raise ValidationError('Invalid name in ' + label)


def declaration(app, site):
    if not isinstance(app, dict) or type(app.get('schema_version')) is not int or app['schema_version'] not in (1, 2):
        raise ValidationError('Unsupported parallel schema')
    version = app['schema_version']
    strict(app, V1_APP_KEYS if version == 1 else APP_KEYS, 'parallel application',
           ('schema_version', 'nodes', 'tasks', 'spawn_owner'))
    strict(site, SITE_KEYS, 'parallel site', ('rank_environment',))
    rank_env = site['rank_environment']
    strict(rank_env, ('rank', 'size', 'local_rank'), 'rank environment', ('rank', 'size', 'local_rank'))
    if any(not isinstance(v, str) or not NAME.fullmatch(v) for v in rank_env.values()):
        raise ValidationError('Rank bindings must name environment variables')
    if len(set(rank_env.values())) != len(rank_env):
        raise ValidationError('Rank/size/local_rank require distinct environment variables')
    result = {'provider': 'ramble-launcher-v1', 'workers_per_task': 1,
              'role': 'main', 'session': 'main', 'probe_required': False,
              'shared_outputs': [], 'environment': [], 'environment_prefixes': []}
    if version == 2:
        result.update(placement='task', coordinator_environment=[], startup_timeout_seconds=60)
    result.update(copy.deepcopy(site))
    result.update(copy.deepcopy(app))
    if result['spawn_owner'] not in ('launcher', 'application'):
        raise ValidationError('Unknown spawn owner; use launcher or application, never both')
    scope = result.get('rank_accounting', 'launcher-task')
    if scope not in ('launcher-task', 'application-worker'):
        raise ValidationError('Invalid upstream MPI rank accounting')
    if scope == 'application-worker':
        if result['spawn_owner'] != 'application':
            raise ValidationError('Application-worker rank accounting requires application spawn owner')
        if version == 2 or result['provider'] != 'container-local-v1':
            raise ValidationError('Application-worker accounting is only valid for legacy container-local-v1; '
                                  'external launcher n_ranks must count outer tasks')
    for key in ('role', 'session', 'provider'):
        if not isinstance(result[key], str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', result[key]):
            raise ValidationError('Invalid parallel ' + key)
    for key in ('nodes', 'tasks', 'workers_per_task'):
        value = result[key]
        if not isinstance(value, str) or not REFERENCE.fullmatch(value):
            positive(value, key)
    for key in ('environment', 'environment_prefixes', 'shared_outputs'):
        _names(result[key], key)
    if any(not name.endswith('_') for name in result['environment_prefixes']):
        raise ValidationError('Environment prefix must end in underscore')
    if type(result['probe_required']) is not bool:
        raise ValidationError('probe_required must be boolean')
    if version == 2:
        if result['placement'] not in ('task', 'one-task-per-node'):
            raise ValidationError('Unknown parallel placement')
        _names(result['coordinator_environment'], 'coordinator_environment')
        for name in result['coordinator_environment']:
            check_no_secrets({name: 'declared'})
            permitted = name in result['environment'] or any(name.startswith(p) for p in result['environment_prefixes'])
            if not permitted or name in rank_env.values() or name in RESERVED_BINDINGS:
                raise ValidationError('Coordinator environment is not site-authorized or is task-owned: ' + name)
        if positive(result['startup_timeout_seconds'], 'startup_timeout_seconds') > 600:
            raise ValidationError('startup_timeout_seconds must not exceed 600')
    check_no_secrets(result)
    return result


RESERVED_BINDINGS = frozenset((
    'BPCE_GROUP_ATTEMPT_ID', 'BPCE_TASK_RANK', 'BPCE_TASK_COUNT', 'BPCE_TASK_LOCAL_RANK',
    'BPCE_EXPECTED_WORKERS', 'BPCE_WORKERS_PER_TASK', 'BPCE_NODE_ID', 'BPCE_PARALLEL_PROBE',
    'BPCE_NODE_COUNT', 'BPCE_TASK_PLACEMENT',
))


def variables(plan):
    names = {'mpi_command', 'n_nodes', 'n_ranks'}
    for key in ('nodes', 'tasks', 'workers_per_task'):
        names.update(re.findall(r'\{([A-Za-z_]\w*)\}', str(plan[key])))
    return names


def resolve(plan, values):
    result = copy.deepcopy(plan)
    for key in ('nodes', 'tasks', 'workers_per_task'):
        result[key] = positive(expand(plan[key], values), key)
    if 'n_nodes' not in values or positive(values['n_nodes'], 'n_nodes') != result['nodes']:
        raise ValidationError('Parallel nodes must match upstream n_nodes')
    if 'n_ranks' not in values:
        raise ValidationError('Missing upstream n_ranks')
    rank_count = positive(values['n_ranks'], 'n_ranks')
    scope = result.get('rank_accounting', 'launcher-task')
    if scope == 'application-worker':
        if result['provider'] != 'container-local-v1' or result['schema_version'] != 1:
            raise ValidationError('External launcher n_ranks must count outer tasks')
        if result['spawn_owner'] != 'application' or rank_count != result['tasks'] * result['workers_per_task']:
            raise ValidationError('Application MPI ranks must match all spawned workers')
    elif rank_count != result['tasks']:
        raise ValidationError('Parallel tasks must match upstream n_ranks; worker count is separate')
    if result['tasks'] < result['nodes']:
        raise ValidationError('Fixed profile requires at least one task per participating node')
    if result['spawn_owner'] == 'launcher' and result['workers_per_task'] != 1:
        raise ValidationError('External rank owner cannot also spawn internal workers')
    if result.get('placement') == 'one-task-per-node' and result['tasks'] != result['nodes']:
        raise ValidationError('one-task-per-node requires tasks == nodes')
    if result['provider'] == 'container-local-v1':
        if result['nodes'] != 1 or result['tasks'] != 1 or result['spawn_owner'] != 'application':
            raise ValidationError('Container-local requires one node/task and application-owned workers')
        launcher = 'container-local-direct'
    else:
        launcher = values.get('mpi_command')
    if (not isinstance(launcher, str) or not launcher.strip() or '\x00' in launcher
            or launcher.strip().lower() in ('placeholder', 'none', 'default', 'null')
            or re.search(r'(?<!\$)\{[A-Za-z_]\w*\}', launcher)):
        raise ValidationError('Upstream mpi_command is not concrete')
    check_no_secrets(launcher)
    result['launcher'] = launcher
    result['expected_workers'] = result['tasks'] * result['workers_per_task']
    return result


def validate_resolved(plan):
    version = plan.get('schema_version') if isinstance(plan, dict) else None
    keys = RESOLVED_KEYS if version == 2 else tuple(k for k in RESOLVED_KEYS if k not in V2_KEYS)
    strict(plan, keys, 'resolved parallel', tuple(k for k in keys if k != 'rank_accounting'))
    app = {k: plan[k] for k in APP_KEYS if k in plan}
    site = {k: plan[k] for k in SITE_KEYS}
    expected = resolve(declaration(app, site), {
        'n_nodes': plan['nodes'],
        'n_ranks': plan['expected_workers'] if plan.get('rank_accounting') == 'application-worker' else plan['tasks'],
        'mpi_command': plan['launcher'],
    })
    if expected != plan:
        raise ValidationError('Resolved parallel contract is inconsistent')
    return plan
