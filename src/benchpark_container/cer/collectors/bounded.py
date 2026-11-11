# SPDX-License-Identifier: Apache-2.0
"""Bounded command execution for evidence only; never used to launch benchmarks.

This is a reliability boundary for trusted tools, not a security sandbox. The
caller owns a fresh POSIX process group and drains both pipes incrementally.
"""
from __future__ import annotations
from dataclasses import dataclass
import math
import os
from pathlib import Path
import selectors
import signal
import stat
import subprocess
import time

MAX_OUTPUT_BYTES = 8 * 1024 * 1024
KILL_GRACE_SECONDS = 0.2


@dataclass(frozen=True)
class BoundedResult:
    returncode: int
    stdout: bytes
    stderr: bytes
    outcome: str
    duration_seconds: float


def _workspace_within_budget(root, byte_limit, file_limit):
    """Stop scanning at the first limit. Refuse special files and symlinks."""
    if root is None:
        return True
    total = count = 0
    pending = [Path(root)]
    while pending:
        directory = pending.pop()
        with os.scandir(directory) as entries:
            for entry in entries:
                count += 1
                if count > file_limit:
                    return False
                info = entry.stat(follow_symlinks=False)
                if stat.S_ISDIR(info.st_mode):
                    pending.append(Path(entry.path))
                elif stat.S_ISREG(info.st_mode):
                    total += info.st_size
                    if total > byte_limit:
                        return False
                else:
                    return False
    return True


def _stop_group(process, owns_group=True):
    """Terminate owned group, even if its leader already exited with open pipes."""
    try:
        if owns_group:
            os.killpg(process.pid, signal.SIGTERM)
        elif process.poll() is None:
            process.terminate()
    except ProcessLookupError:
        pass
    try:
        process.wait(timeout=KILL_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        pass
    try:
        if owns_group:
            os.killpg(process.pid, signal.SIGKILL)
        elif process.poll() is None:
            process.kill()
    except ProcessLookupError:
        pass
    # A task stuck in an uninterruptible kernel state may not be reapable yet.
    # Do not turn such a provider into an unbounded finalization wait.
    try:
        process.wait(timeout=KILL_GRACE_SECONDS)
    except subprocess.TimeoutExpired:
        return False
    return True


def run_bounded(argv, timeout, *, env=None, cwd=None,
                max_output_bytes=MAX_OUTPUT_BYTES, watch_root=None,
                max_workspace_bytes=64 * 1024 * 1024, max_workspace_files=512,
                owns_process_group=True):
    if (isinstance(timeout, bool) or not isinstance(timeout, (int, float))
            or not math.isfinite(timeout) or timeout <= 0):
        raise ValueError('Evidence command timeout must be finite and positive')
    if type(max_output_bytes) is not int or max_output_bytes <= 0:
        raise ValueError('Evidence output budget must be a positive integer')
    if os.name != 'posix':
        raise OSError('Bounded evidence execution requires a POSIX host')
    start = time.monotonic()
    process = subprocess.Popen(list(argv), stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=env, cwd=cwd,
        start_new_session=owns_process_group, close_fds=True)
    buffers = {'stdout': bytearray(), 'stderr': bytearray()}
    total = 0
    outcome = 'completed'
    try:
        with selectors.DefaultSelector() as selector:
            for label, stream in [('stdout', process.stdout), ('stderr', process.stderr)]:
                os.set_blocking(stream.fileno(), False)
                selector.register(stream, selectors.EVENT_READ, label)
            while selector.get_map() or process.poll() is None:
                remaining = timeout - (time.monotonic() - start)
                if remaining <= 0:
                    outcome = 'timeout'
                    break
                if not _workspace_within_budget(watch_root, max_workspace_bytes, max_workspace_files):
                    outcome = 'workspace-limit-or-unsafe-file'
                    break
                for key, _ in selector.select(min(remaining, 0.025)):
                    try:
                        chunk = os.read(key.fileobj.fileno(), 65536)
                    except BlockingIOError:
                        continue
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    keep = min(len(chunk), max_output_bytes - total)
                    if keep:
                        buffers[key.data].extend(chunk[:keep])
                        total += keep
                    if keep < len(chunk):
                        outcome = 'output-limit'
                        break
                if outcome != 'completed':
                    break
            if outcome == 'completed' and not _workspace_within_budget(
                    watch_root, max_workspace_bytes, max_workspace_files):
                outcome = 'workspace-limit-or-unsafe-file'
    finally:
        reaped = _stop_group(process, owns_process_group)
        process.stdout.close()
        process.stderr.close()
    if not reaped:
        outcome = 'cleanup-unverified'
    code = process.returncode
    if outcome != 'completed':
        code = 124 if outcome == 'timeout' else 125
    return BoundedResult(code if code is not None else 125,
        bytes(buffers['stdout']), bytes(buffers['stderr']), outcome,
        time.monotonic() - start)
