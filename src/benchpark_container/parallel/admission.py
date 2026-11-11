# SPDX-License-Identifier: Apache-2.0
"""Fixed-membership admission and coordinator-owned inputs, not a scheduler.

All paths are in the already-created shared attempt directory. No network daemon,
node selection, shell execution, GPU discovery or application imports live here.
"""
import json
import time
from pathlib import Path
from ..util import ValidationError, atomic_json, identity, check_no_secrets

MAX_RUNTIME_INPUT_BYTES = 65536
MAX_TASK_RECORD_BYTES = 65536


def _read(path, limit):
    path = Path(path)
    if path.is_symlink() or not path.is_file() or path.stat().st_size > limit:
        raise ValidationError('Invalid shared record: ' + str(path))
    try:
        value = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, ValueError) as exc:
        raise ValidationError('Unreadable shared record: ' + str(path)) from exc
    if not isinstance(value, dict):
        raise ValidationError('Shared record must be an object')
    return value


def freeze_runtime_inputs(group, spec, environment):
    """Called once by coordinator after allocation, before the outer launcher."""
    group = Path(group)
    names = spec['parallel'].get('coordinator_environment', [])
    values = {}
    for name in names:
        value = environment.get(name)
        if not isinstance(value, str) or not value.strip() or any(c in value for c in '\x00\r\n'):
            raise ValidationError('Missing/invalid coordinator runtime input: ' + name)
        values[name] = value
    check_no_secrets(values)
    data = {'schema_version': 1, 'group_attempt_id': group.name,
            'spec_sha256': identity(spec), 'environment': values}
    data['record_sha256'] = identity(data)
    if len(json.dumps(data).encode('utf-8')) > MAX_RUNTIME_INPUT_BYTES:
        raise ValidationError('Coordinator runtime input exceeds size limit')
    atomic_json(group / 'runtime-inputs.json', data)
    return data


def load_runtime_inputs(group, spec, environment=None):
    group = Path(group)
    data = _read(group / 'runtime-inputs.json', MAX_RUNTIME_INPUT_BYTES)
    expected_hash = data.pop('record_sha256', None)
    if (identity(data) != expected_hash or type(data.get('schema_version')) is not int or data['schema_version'] != 1
            or data.get('group_attempt_id') != group.name or data.get('spec_sha256') != identity(spec)):
        raise ValidationError('Coordinator runtime input identity mismatch')
    values = data.get('environment')
    if not isinstance(values, dict) or set(values) != set(spec['parallel'].get('coordinator_environment', [])):
        raise ValidationError('Coordinator runtime input names mismatch')
    for name, value in values.items():
        if not isinstance(value, str) or not value.strip() or any(c in value for c in '\x00\r\n'):
            raise ValidationError('Invalid coordinator runtime input value')
        # The filesystem is authoritative. A launcher may omit an env var, but
        # must not substitute a conflicting value for another task.
        if environment is not None and name in environment and environment[name] != value:
            raise ValidationError('Task/coordinator runtime input differs: ' + name)
    check_no_secrets(values)
    return dict(values)


def validate_membership(plan, records, group_id):
    """Validate observed task/host placement, independent of launcher syntax."""
    if len(records) != plan['tasks']:
        raise ValidationError('Admission task count mismatch')
    ranks, by_node = [], {}
    for record in records:
        rank, local_rank = record.get('task_rank'), record.get('task_local_rank')
        if (type(rank) is not int or type(local_rank) is not int or rank < 0 or local_rank < 0
                or type(record.get('task_world_size')) is not int or record['task_world_size'] != plan['tasks']
                or record.get('group_attempt_id') != group_id
                or record.get('spawn_owner') != plan['spawn_owner']
                or not isinstance(record.get('node_id'), str) or not record['node_id']):
            raise ValidationError('Invalid admission task record')
        ranks.append(rank)
        by_node.setdefault(record['node_id'], []).append(local_rank)
    if sorted(ranks) != list(range(plan['tasks'])):
        raise ValidationError('Admission duplicate/missing task ranks')
    if len(by_node) != plan['nodes']:
        raise ValidationError('Admission participating-node-count-mismatch')
    for local in by_node.values():
        if sorted(local) != list(range(len(local))):
            raise ValidationError('Admission local task ranks are not unique/contiguous')
        if plan.get('placement') == 'one-task-per-node' and local != [0]:
            raise ValidationError('Admission expected exactly one task per node')
    return {'task_count': len(records), 'node_count': len(by_node),
            'placement': plan.get('placement', 'task')}


def wait_for_members(group, plan):
    """All tasks pass the same immutable snapshot, or fail within a fixed bound."""
    group = Path(group)
    deadline = time.monotonic() + float(plan['startup_timeout_seconds'])
    while True:
        if (group / 'closed.json').exists():
            raise ValidationError('Group closed during admission')
        if any((group / 'rejections').glob('*.json')):
            raise ValidationError('A task was rejected during admission')
        entries = list((group / 'members').iterdir())
        for entry in entries:
            if entry.is_symlink() or not entry.is_dir() or not entry.name.isdecimal() or not 0 <= int(entry.name) < plan['tasks']:
                raise ValidationError('Invalid admission slot')
        paths = [group / 'members' / str(rank) / 'task-started.json' for rank in range(plan['tasks'])]
        if all(p.is_file() for p in paths):
            records = [_read(p, MAX_TASK_RECORD_BYTES) for p in paths]
            for rank, record in enumerate(records):
                if record.get('task_rank') != rank:
                    raise ValidationError('Admission slot/rank mismatch')
            result = validate_membership(plan, records, group.name)
            return result
        if time.monotonic() >= deadline:
            raise ValidationError('Admission timeout: not all expected tasks arrived')
        time.sleep(min(0.1, max(0, deadline - time.monotonic())))
