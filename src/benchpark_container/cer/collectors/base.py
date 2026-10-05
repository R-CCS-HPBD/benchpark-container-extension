# SPDX-License-Identifier: Apache-2.0
"""Small, non-fatal CER collection primitives.

Collectors add evidence only after benchmark execution. They MUST NOT affect
condition_id, plan_sha256, command construction, or benchmark success/failure.
"""
from __future__ import annotations
import dataclasses
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
from typing import Any, Mapping, Sequence


@dataclasses.dataclass(frozen=True)
class CommandResult:
    argv: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self):
        return self.returncode == 0


class CommandRunner:
    def which(self, name):
        return shutil.which(name)

    def run(self, argv: Sequence[str], timeout=15.0):
        try:
            cp = subprocess.run(list(argv), capture_output=True, text=True,
                                timeout=timeout, check=False)
            return CommandResult(tuple(argv), cp.returncode, cp.stdout, cp.stderr)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return CommandResult(tuple(argv), 127, '', str(exc))


@dataclasses.dataclass
class CollectorContext:
    attempt: Path
    env: Mapping[str, str] = dataclasses.field(default_factory=lambda: dict(os.environ))
    max_evidence_bytes: int = 8 * 1024 * 1024

    def __post_init__(self):
        self.attempt = Path(self.attempt)

    @property
    def evidence(self):
        return self.attempt / 'cer-evidence'


@dataclasses.dataclass
class CollectorResult:
    name: str
    status: str = 'complete'  # complete | partial | unavailable
    observed: dict[str, Any] = dataclasses.field(default_factory=dict)
    files: dict[str, dict[str, Any]] = dataclasses.field(default_factory=dict)
    errors: list[str] = dataclasses.field(default_factory=list)

    def add_error(self, message):
        self.errors.append(str(message))
        if self.status == 'complete':
            self.status = 'partial'


def _sha(data):
    return hashlib.sha256(data).hexdigest()


def write_evidence(ctx, result, relative, data):
    rel = Path(relative)
    if rel.is_absolute() or '..' in rel.parts:
        raise ValueError('unsafe evidence path: ' + str(relative))
    raw = data.encode('utf-8') if isinstance(data, str) else data
    if len(raw) > ctx.max_evidence_bytes:
        raw = raw[:ctx.max_evidence_bytes]
        result.add_error('evidence truncated: ' + str(relative))
    path = ctx.evidence / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    key = str(path.relative_to(ctx.attempt))
    result.files[key] = {'sha256': _sha(raw), 'bytes': len(raw)}
    return key


def record_command(ctx, result, runner, stem, argv, timeout=15.0):
    write_evidence(ctx, result, stem + '.command.json',
                   json.dumps({'argv': list(argv), 'timeout_seconds': timeout,
                               'shell': False}, indent=2, sort_keys=True) + '\n')
    cp = runner.run(argv, timeout=timeout)
    write_evidence(ctx, result, stem + '.stdout.log', cp.stdout)
    write_evidence(ctx, result, stem + '.stderr.log', cp.stderr)
    if not cp.ok:
        result.add_error('%s failed rc=%s' % (' '.join(argv), cp.returncode))
    return cp


def json_maybe(text):
    try:
        return json.loads(text)
    except (TypeError, json.JSONDecodeError):
        return None


def merge_additive(dst, src, path=""):
    """Recursively add collector metadata without overwriting existing meaning."""
    for key, value in src.items():
        here = f"{path}.{key}" if path else str(key)
        if key not in dst:
            dst[key] = value
            continue
        current = dst[key]
        if isinstance(current, dict) and isinstance(value, dict):
            merge_additive(current, value, here)
        elif current == value:
            continue
        else:
            raise ValueError(f"CER collector field collision: {here}")


def merge_file_metadata(dst, src):
    """Add file metadata without replacing an existing path with different data."""
    for key, value in src.items():
        if key in dst and dst[key] != value:
            raise ValueError(f"CER collector evidence collision: {key}")
        dst[key] = value
