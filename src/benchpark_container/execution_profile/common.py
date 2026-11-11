"""Bounded JSON, content identities and write-once evidence.

Hashes establish consistency, not authenticity. Site policy is an operator trust root.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import tempfile
from pathlib import Path
from typing import Any

MAX_JSON = 4 * 1024 * 1024
SHA = re.compile(r"[0-9a-f]{64}\Z")
REVISION = re.compile(r"[0-9a-f]{40}\Z")
NAME = re.compile(r"[A-Za-z][A-Za-z0-9_.-]{0,95}\Z")
SECRET = re.compile(r"password|passwd|secret|token|credential|private.?key", re.I)


class ProfileError(ValueError):
    pass


def need(condition: Any, message: str) -> None:
    if not condition:
        raise ProfileError(message)


def canonical(value: Any) -> bytes:
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode("utf-8")


def identity(value: Any) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


def strict(value: Any, keys: set[str], label: str, optional: set[str] | None = None) -> dict:
    need(type(value) is dict, f"{label}: expected object")
    need(not set(value) - keys, f"{label}: unknown fields {sorted(set(value) - keys)}")
    need(not keys - set(value) - (optional or set()),
         f"{label}: missing fields {sorted(keys - set(value) - (optional or set()))}")
    return value


def _unique(pairs: list[tuple[str, Any]]) -> dict:
    out = {}
    for k, v in pairs:
        need(k not in out, "JSON duplicate key: " + k)
        out[k] = v
    return out


def read_json(path: str | Path) -> dict:
    path = Path(path)
    need(path.is_file(), "JSON missing: " + str(path))
    with path.open("rb") as f:
        raw = f.read(MAX_JSON + 1)
    need(len(raw) <= MAX_JSON, "JSON exceeds size bound")
    try:
        value = json.loads(raw, object_pairs_hook=_unique,
                           parse_constant=lambda x: (_ for _ in ()).throw(ProfileError("Nonfinite JSON")))
    except (json.JSONDecodeError, UnicodeDecodeError) as exc:
        raise ProfileError("Invalid JSON: " + str(path)) from exc
    need(type(value) is dict, "Top-level JSON must be an object")
    return value


def sha_file(path: str | Path) -> str:
    path = Path(path)
    # Hash the opened file and reject replacement/change during the read.
    with path.open("rb") as f:
        before = os.fstat(f.fileno())
        need(stat.S_ISREG(before.st_mode), "Not a regular file: " + str(path))
        h = hashlib.sha256()
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
        after = os.fstat(f.fileno())
    now = path.stat()
    fields = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns)
    need(fields(before) == fields(after) == fields(now), "File changed while hashing: " + str(path))
    return h.hexdigest()


def absolute(value: Any) -> Path:
    need(type(value) is str and value and not any(c in value for c in "\x00\n\r"), "Invalid path")
    p = Path(value)
    need(p.is_absolute() and ".." not in p.parts, "Absolute path without '..' required: " + value)
    return p


def allowed_path(path: str | Path, roots: list[str]) -> Path:
    p = absolute(str(path)).resolve()
    need(any(p == Path(r).resolve() or Path(r).resolve() in p.parents for r in roots),
         "Path outside site-approved roots: " + str(path))
    return p


def atomic_new(path: str | Path, value: dict) -> None:
    """Publish complete JSON without replacing an existing file; mode 0600."""
    path = Path(path)
    raw = canonical(value) + b"\n"
    need(len(raw) <= MAX_JSON, "Evidence exceeds JSON budget")
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".profile-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(raw)
            f.flush()
            os.fsync(f.fileno())
        # link is atomic and fails if path already exists; os.replace would overwrite.
        os.link(tmp, path)
        dfd = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    finally:
        os.unlink(tmp)


def sealed(body: dict) -> dict:
    need("record_sha256" not in body, "Already sealed")
    return {**body, "record_sha256": identity(body)}


def check_seal(record: dict) -> dict:
    need(type(record) is dict, "Record must be an object")
    digest = record.get("record_sha256")
    body = {k: v for k, v in record.items() if k != "record_sha256"}
    need(digest == identity(body), "Record content digest mismatch")
    return body


def check_digest(value: Any, label: str) -> None:
    need(type(value) is str and SHA.fullmatch(value), label + ": expected SHA-256")
