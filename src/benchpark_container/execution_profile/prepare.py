"""Call existing, explicitly approved setup commands and seal their results.

This module never downloads/unpacks software, runs a solver, or creates an MPI store.
"""
from __future__ import annotations

import fcntl
import os
import selectors
import signal
import stat
import subprocess
import tempfile
import time
from pathlib import Path

from .common import (ProfileError, atomic_new, check_seal, identity, need, read_json,
                     sealed, sha_file, allowed_path)
from .model import host_fingerprint, validate_profile

MAX_LOG = 1024 * 1024


def fingerprints(entries: list[dict], roots: list[str], *, require_hash: bool = True) -> list[dict]:
    found = []
    for item in entries:
        path = Path(item["path"])
        actual = allowed_path(path, roots)
        digest = sha_file(path)
        if require_hash and item["sha256"] is not None:
            need(digest == item["sha256"], "Dependency digest mismatch: " + item["id"])
        st = path.stat()
        need(not st.st_mode & (stat.S_IWGRP | stat.S_IWOTH),
             "Dependency is group/world writable: " + str(path))
        if item["executable"]:
            need(os.access(path, os.X_OK), "Dependency not executable: " + item["id"])
        found.append({"id": item["id"], "path": str(path), "realpath": str(actual),
                      "sha256": digest, "bytes": st.st_size,
                      "executable": item["executable"]})
    return found


def verify_fingerprints(entries: list[dict]) -> None:
    for item in entries:
        path = Path(item["path"])
        need(str(path.resolve()) == item["realpath"], "Dependency link target changed: " + item["id"])
        need(sha_file(path) == item["sha256"], "Prepared dependency changed: " + item["id"])
        need(not path.stat().st_mode & 0o022, "Prepared dependency became writable to others")
        if item["executable"]:
            need(os.access(path, os.X_OK), "Prepared executable not executable")


def verify_receipt(receipt: dict, profile: dict, policy: dict) -> dict:
    validate_profile(profile, policy)
    body = check_seal(receipt)
    need(body.get("schema_version") == 1 and body.get("status") == "PREPARED", "Receipt is not prepared")
    need(body.get("profile_sha256") == identity(profile), "Receipt profile mismatch")
    need(body.get("policy_sha256") == identity(policy), "Receipt site policy mismatch")
    need(body.get("host") == host_fingerprint(), "Receipt host changed")
    expected_inputs = profile["preparation"]["inputs"]
    expected_outputs = profile["preparation"]["outputs"]
    for key, expected in (("inputs", expected_inputs), ("outputs", expected_outputs)):
        entries = body.get(key)
        need(type(entries) is list and len(entries) == len(expected), "Receipt file set changed")
        by_id = {e["id"]: e for e in entries}
        need(len(by_id) == len(entries) and set(by_id) == {e["id"] for e in expected}, "Receipt file IDs differ")
        for original in expected:
            e = by_id[original["id"]]
            need(e["path"] == original["path"] and e["executable"] == original["executable"], "Receipt file contract differs")
            if original["sha256"] is not None:
                need(e["sha256"] == original["sha256"], "Receipt changed pinned digest")
        verify_fingerprints(entries)
    return receipt


def _terminate(proc: subprocess.Popen) -> None:
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        proc.wait(timeout=3)


def bounded_command(argv: list[str], cwd: Path, logfile: Path, env: dict, timeout: int) -> int:
    """Bound stdout/stderr, duration and local process group; never shell=True."""
    fd = os.open(logfile, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    proc = None
    try:
        with os.fdopen(fd, "wb") as log, selectors.DefaultSelector() as selector:
            proc = subprocess.Popen(argv, cwd=cwd, env=env, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, start_new_session=True)
            selector.register(proc.stdout, selectors.EVENT_READ)
            start = time.monotonic()
            size = 0
            while selector.get_map():
                if time.monotonic() - start > timeout:
                    raise ProfileError("Setup timeout; no prepared receipt was published")
                for key, _ in selector.select(0.1):
                    chunk = os.read(key.fileobj.fileno(), 65536)
                    if not chunk:
                        selector.unregister(key.fileobj)
                        continue
                    remaining = MAX_LOG - size
                    log.write(chunk[:max(0, remaining)])
                    size += len(chunk)
                    if size > MAX_LOG:
                        raise ProfileError("Setup log limit exceeded")
            return proc.wait(timeout=max(1, timeout - (time.monotonic() - start)))
    except BaseException:
        if proc is not None:
            _terminate(proc)
        raise
    finally:
        if proc is not None and proc.stdout is not None:
            proc.stdout.close()


def prepare(profile: dict, policy: dict, receipt_path: Path, *, execute: bool = False,
            dry_run: bool = False, lock_timeout: float = 30.0) -> dict:
    """Only --execute authorizes approved setup commands. No implicit production run."""
    validate_profile(profile, policy)
    receipt_path = Path(receipt_path).absolute()
    if receipt_path.exists():
        return verify_receipt(read_json(receipt_path), profile, policy)
    fingerprints(profile["preparation"]["inputs"], policy["allowed_roots"])
    if dry_run:
        return {"status": "NOT_PREPARED", "profile_sha256": identity(profile),
                "commands_would_run": len(profile["preparation"]["commands"]),
                "installation_performed": False, "qualification": "NOT_ASSESSED"}
    need(profile["preparation"]["kind"] == "external" or execute,
         "Missing receipt; explicitly authorize setup with --execute during Software Preparation")
    receipt_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = receipt_path.with_name(receipt_path.name + ".lock")
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        start = time.monotonic()
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                need(time.monotonic() - start < lock_timeout, "Preparation receipt lock timeout")
                time.sleep(0.05)
        if receipt_path.exists():
            return verify_receipt(read_json(receipt_path), profile, policy)
        inputs = fingerprints(profile["preparation"]["inputs"], policy["allowed_roots"])
        run = Path(tempfile.mkdtemp(prefix="prepare-", dir=receipt_path.parent))
        (run / "home").mkdir(mode=0o700)
        (run / "tmp").mkdir(mode=0o700)
        env = {"PATH": "/usr/bin:/bin", "HOME": str(run / "home"),
               "TMPDIR": str(run / "tmp"), "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8"}
        env.update(profile["host_environment"])
        logs = []
        for index, argv in enumerate(profile["preparation"]["commands"]):
            verify_fingerprints(inputs)
            log = run / f"setup-{index}.log"
            rc = bounded_command(argv, run, log, env, profile["preparation"]["timeout_seconds"])
            need(rc == 0, f"Existing setup command failed ({rc}); private log: {log}")
            logs.append({"path": str(log), "sha256": sha_file(log), "exit_code": rc})
        verify_fingerprints(inputs)  # refuse changed lock files or recipe after install
        outputs = fingerprints(profile["preparation"]["outputs"], policy["allowed_roots"])
        receipt = sealed({"schema_version": 1, "status": "PREPARED",
                          "profile_sha256": identity(profile), "policy_sha256": identity(policy),
                          "host": host_fingerprint(), "inputs": inputs, "outputs": outputs,
                          "preparation_logs": logs,
                          "observation_scope": "declared-files-only; not all runtime dlopen dependencies",
                          "mpi_compatibility": "NOT_TESTED", "provenance_strength": "unsigned-content-integrity"})
        atomic_new(receipt_path, receipt)
        return receipt
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)
