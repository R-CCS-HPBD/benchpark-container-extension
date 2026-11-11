# SPDX-License-Identifier: Apache-2.0
"""Non-overwriting publication of bounded, private CollectorResult outputs.

Only the parent publishes files. Private staging is accidental-write isolation,
not a sandbox against trusted code that deliberately uses an absolute path.
"""
from __future__ import annotations
from contextlib import contextmanager
import copy
import fcntl
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import stat
import tempfile

MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_BATCH_BYTES = 32 * 1024 * 1024
MAX_FILES = 256
INTERNAL = '.cer-internal'
FAILURE_RECORD = 'evidence-integrity-failure.json'


def safe_relative(name):
    if (not isinstance(name, str) or not name or '\\' in name or '\x00' in name
            or any(x in ('', '.', '..') for x in name.split('/'))):
        raise ValueError('Unsafe evidence path: ' + repr(name))
    rel = PurePosixPath(name)
    if rel.is_absolute() or rel.parts[0] in ('cer.json', 'started.json', 'scratch', INTERNAL, FAILURE_RECORD):
        raise ValueError('Unsafe/reserved evidence path: ' + repr(name))
    return rel


def checked_path(root, name, *, mkdir=False):
    rel = safe_relative(name)
    current = Path(root)
    if current.is_symlink() or not current.is_dir():
        raise ValueError('Evidence root must be a real directory')
    for part in rel.parts[:-1]:
        current = current / part
        if mkdir:
            current.mkdir(mode=0o700, exist_ok=True)
        if current.is_symlink() or (current.exists() and not current.is_dir()):
            raise ValueError('Symlink or non-directory evidence parent: ' + name)
    path = current / rel.name
    if path.is_symlink():
        raise ValueError('Symlink evidence: ' + name)
    return path


def read_regular(path, limit=MAX_FILE_BYTES):
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_size > limit:
            raise ValueError('Nonregular/oversized evidence: ' + str(path))
        chunks = []
        count = 0
        while True:
            raw = os.read(fd, min(65536, limit + 1 - count))
            if not raw:
                break
            count += len(raw)
            if count > limit:
                raise ValueError('Evidence grew beyond its budget')
            chunks.append(raw)
        end = os.fstat(fd)
        if (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns, st.st_ctime_ns) != (
                end.st_dev, end.st_ino, end.st_size, end.st_mtime_ns, end.st_ctime_ns):
            raise ValueError('Evidence changed while reading')
        return b''.join(chunks)
    finally:
        os.close(fd)


def atomic_new(path, raw):
    """Publish a whole file via an atomic no-replace link; never truncate target."""
    path = Path(path)
    fd, temp = tempfile.mkstemp(prefix='.cer-publish-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as out:
            out.write(raw)
            out.flush()
            os.fsync(out.fileno())
        os.link(temp, path, follow_symlinks=False)
    finally:
        os.unlink(temp)


def internal_root(root):
    path = Path(root) / INTERNAL
    if path.is_symlink():
        raise ValueError('Symlink collector control directory')
    path.mkdir(mode=0o700, exist_ok=True)
    if not path.is_dir():
        raise ValueError('Non-directory collector control path')
    return path


@contextmanager
def publication_lock(root):
    lock = internal_root(root) / 'publish.lock'
    fd = os.open(lock, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError('Invalid publisher lock')
        # Shared publishers only hold this while publishing already bounded data.
        # Never wait indefinitely on a buggy owner.
        import time
        deadline = time.monotonic() + 2.0
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise TimeoutError('Evidence publisher lock timeout')
                time.sleep(0.01)
        yield
    finally:
        os.close(fd)


@contextmanager
def private_stage(root):
    with tempfile.TemporaryDirectory(prefix='stage-', dir=internal_root(root)) as name:
        yield Path(name)


def validate_batch(stage, result, expected_name):
    from .base import CollectorResult
    if not isinstance(result, CollectorResult) or result.name != expected_name:
        raise ValueError('Collector result name/type mismatch')
    if result.status not in ('complete', 'partial', 'unavailable'):
        raise ValueError('Invalid collector collection status')
    if not isinstance(result.observed, dict) or not isinstance(result.files, dict):
        raise ValueError('Collector output must be mappings')
    if not isinstance(result.errors, list) or not all(isinstance(x, str) for x in result.errors):
        raise ValueError('Invalid collector error list')
    # Refuse NaN, foreign objects and excessive metadata before merger/encoding.
    raw = json.dumps(vars(result), sort_keys=True, allow_nan=False).encode()
    if len(raw) > MAX_FILE_BYTES or len(result.files) > MAX_FILES:
        raise ValueError('Collector metadata/file-count budget exceeded')
    content = {}
    total = 0
    for name, meta in result.files.items():
        path = checked_path(stage, name)
        data = read_regular(path)
        total += len(data)
        if total > MAX_BATCH_BYTES:
            raise ValueError('Collector batch budget exceeded')
        if (not isinstance(meta, dict) or type(meta.get('bytes')) is not int
                or meta['bytes'] != len(data)
                or meta.get('sha256') != hashlib.sha256(data).hexdigest()):
            raise ValueError('Collector content/hash mismatch: ' + name)
        content[name] = data
    # A file must not also be the parent of another returned file.
    for name in content:
        if any('/'.join(PurePosixPath(name).parts[:i]) in content
               for i in range(1, len(PurePosixPath(name).parts))):
            raise ValueError('Collector file/directory collision')
    return content


def publish_batch(root, stage, result, expected_name, existing_observed, existing_files):
    from .base import merge_additive, merge_file_metadata
    from ..evidence import validate_files
    content = validate_batch(stage, result, expected_name)
    # Validate both merges BEFORE publishing any output.
    trial_observed = copy.deepcopy(existing_observed)
    trial_files = copy.deepcopy(existing_files)
    merge_additive(trial_observed, result.observed)
    merge_file_metadata(trial_files, result.files)
    with publication_lock(root):
        if (Path(root) / 'cer.json').exists():
            raise ValueError('Cannot publish into a finalized CER')
        validate_files(root, existing_files)
        destinations = {}
        for name, raw in content.items():
            path = checked_path(root, name)
            if path.exists():
                # Same registered content/metadata is idempotent. Unregistered
                # files and different bytes are never adopted or overwritten.
                if name not in existing_files or read_regular(path) != raw:
                    raise ValueError('Existing evidence collision: ' + name)
            else:
                destinations[name] = path
        created = []
        try:
            for name, path in destinations.items():
                path = checked_path(root, name, mkdir=True)
                atomic_new(path, content[name])
                st = path.lstat()
                created.append((path, st.st_dev, st.st_ino))
            validate_files(root, trial_files)
        except BaseException:
            # Remove only files published by this failed batch, never a prior
            # provider's evidence or a file that was replaced outside this API.
            for path, dev, ino in reversed(created):
                try:
                    st = path.lstat()
                    if (st.st_dev, st.st_ino) == (dev, ino):
                        path.unlink()
                except FileNotFoundError:
                    pass
            raise
    return trial_observed, trial_files


def snapshot_inputs(root, stage, files):
    """v1 compatibility: bounded COPIES of registered files, never hard links."""
    from ..evidence import validate_files
    validate_files(root, files)
    if len(files) > MAX_FILES:
        raise ValueError('Legacy collector input snapshot has too many files')
    total = 0
    for name in sorted(files):
        src = checked_path(root, name)
        raw = read_regular(src)
        total += len(raw)
        if total > MAX_BATCH_BYTES:
            raise ValueError('Legacy collector input snapshot exceeds budget')
        target = checked_path(stage, name, mkdir=True)
        atomic_new(target, raw)
        target.chmod(0o400)
