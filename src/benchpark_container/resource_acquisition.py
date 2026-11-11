# SPDX-License-Identifier: Apache-2.0
"""Setup-only acquisition of declared immutable input files.

The portable runtime verifies these bytes; it never downloads them. No dataset
names, ML libraries, credentials, archive extractors or site paths live here.
"""
import contextlib
import fcntl
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import tempfile
import time
import urllib.parse
import urllib.request

from .artifacts import content_identity
from .util import ValidationError, atomic_json, check_no_secrets, identity, inside, sha256, strict

SHA = re.compile(r"[0-9a-f]{64}\Z")
COMMIT = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")


def valid_hash(value):
    if not isinstance(value, str) or not SHA.fullmatch(value):
        raise ValidationError("Resource sha256 must be 64 lowercase hex characters")
    return value


def _relative(value):
    if (not isinstance(value, str) or not value or value in (".", "..")
            or PurePosixPath(value).is_absolute() or ".." in PurePosixPath(value).parts
            or "\\" in value or any(c in value for c in "\x00\r\n")
            or str(PurePosixPath(value)) != value):
        raise ValidationError("Unsafe resource filename: " + repr(value))
    return value


def _https(value):
    if not isinstance(value, str):
        raise ValidationError("Public resource URL must be a string")
    check_no_secrets(value)
    u = urllib.parse.urlsplit(value)
    if u.scheme != "https" or not u.hostname or u.username or u.password or u.fragment:
        raise ValidationError("Public resource requires an HTTPS URL without credentials/fragment")
    return value


def validate_source(source):
    """Pure declaration validation. A HF snapshot has an explicit file closure.

    The files list deliberately excludes arbitrary snapshot discovery and remote
    code execution. Its SHA256s verify the bytes, not merely a claimed revision.
    """
    if not isinstance(source, dict):
        raise ValidationError("acquire must be an object")
    check_no_secrets(source)
    kind = source.get("type")
    if kind == "https":
        strict(source, ("type", "url", "sha256"), "HTTPS resource", ("type", "url", "sha256"))
        _https(source["url"]); valid_hash(source["sha256"])
    elif kind == "huggingface":
        strict(source, ("type", "repository", "revision", "files"), "HF resource",
               ("type", "repository", "revision", "files"))
        repo, rev, files = source["repository"], source["revision"], source["files"]
        if not isinstance(repo, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+", repo):
            raise ValidationError("HF repository must be owner/name")
        if not isinstance(rev, str) or not COMMIT.fullmatch(rev):
            raise ValidationError("HF revision must be a full immutable commit")
        if not isinstance(files, dict) or not files:
            raise ValidationError("HF acquisition requires the explicit filename -> SHA256 closure")
        names = sorted(files)
        for name in names:
            _relative(name); valid_hash(files[name])
            if any(other != name and other.startswith(name + "/") for other in names):
                raise ValidationError("Resource filename conflicts with a directory")
    else:
        raise ValidationError("Unsupported acquisition source type: " + str(kind))
    return source


@contextlib.contextmanager
def resource_lock(path, timeout):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise ValidationError("Lock must not be a symlink")
    with path.open("a+b") as f:
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise ValidationError("Timed out waiting for resource lock: " + str(path))
                time.sleep(0.05)
        try:
            yield
        finally:
            fcntl.flock(f, fcntl.LOCK_UN)


class _HTTPSRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _https(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def download(url, destination, expected, timeout):
    """Stream to a private temporary file. Never publish unverified bytes."""
    _https(url); valid_hash(expected)
    request = urllib.request.Request(url, headers={"User-Agent": "benchpark-resource/1"})
    deadline = time.monotonic() + timeout
    opener = urllib.request.build_opener(_HTTPSRedirect())
    with opener.open(request, timeout=min(timeout, 60)) as response, Path(destination).open("xb") as out:
        while True:
            if time.monotonic() > deadline:
                raise ValidationError("Resource download timed out")
            block = response.read(1024 * 1024)
            if not block:
                break
            out.write(block)
        out.flush(); os.fsync(out.fileno())
    if sha256(destination) != expected:
        raise ValidationError("Downloaded resource checksum mismatch: " + url)


def _expected_files(source):
    if source["type"] == "https":
        return {"payload": source["sha256"]}
    return source["files"]


def _verify(directory, source):
    directory = Path(directory)
    if directory.is_symlink() or not directory.is_dir():
        raise ValidationError("Resource cache is not a regular directory")
    expected = _expected_files(source)
    if any(p.is_symlink() for p in directory.rglob("*")):
        raise ValidationError("Symlink in downloaded resource")
    actual = {p.relative_to(directory).as_posix() for p in directory.rglob("*") if p.is_file()}
    if actual != set(expected):
        raise ValidationError("Downloaded resource file closure differs from declaration")
    for name, digest in expected.items():
        path = inside(directory, name, must_exist=True)
        if not path.is_file() or sha256(path) != digest:
            raise ValidationError("Resource cache integrity failure: " + name)
    return directory / "payload" if source["type"] == "https" else directory


def acquire(source, store, *, offline=False, timeout=3600):
    """Create-or-verify cache, explicit offline behavior, no silent repair."""
    validate_source(source)
    if type(offline) is not bool or not 0 < timeout <= 7 * 86400:
        raise ValidationError("Invalid acquisition policy")
    store = Path(store)
    key = identity(source)
    final = store / "objects" / key
    with resource_lock(store / "locks" / (key + ".lock"), timeout):
        if final.exists() or final.is_symlink():
            return _verify(final, source)
        if offline:
            raise ValidationError("OFFLINE_RESOURCE_MISSING: " + key)
        final.parent.mkdir(parents=True, exist_ok=True)
        temp = Path(tempfile.mkdtemp(prefix=".fetch-", dir=final.parent))
        try:
            for name, digest in _expected_files(source).items():
                dest = inside(temp, name)
                dest.parent.mkdir(parents=True, exist_ok=True)
                if source["type"] == "https":
                    url = source["url"]
                else:
                    url = ("https://huggingface.co/" + source["repository"] + "/resolve/"
                           + source["revision"] + "/" + urllib.parse.quote(name, safe="/"))
                download(url, dest, digest, timeout)
            _verify(temp, source)
            for path in sorted(temp.rglob("*"), reverse=True):
                path.chmod(0o555 if path.is_dir() else 0o444)
            temp.chmod(0o555)
            os.rename(temp, final)
        except BaseException:
            # Keep failed source bytes out of the published namespace. The
            # enclosing setup record retains the error, not secret credentials.
            if temp.exists():
                for path in [temp, *temp.rglob("*")]:
                    if not path.is_symlink():
                        path.chmod(0o700 if path.is_dir() else 0o600)
                shutil.rmtree(temp)
            raise
    return _verify(final, source)


def prepare_artifacts(artifacts, store, *, offline=False, timeout=3600):
    """Resolve downloaded and declared private inputs, leaving outputs per-attempt."""
    prepared = []
    for item in artifacts:
        a = dict(item)
        if a.get("location") == "download":
            source = acquire(a["acquire"], store, offline=offline, timeout=timeout)
            a.update(location="external", host_root=str(source.parent), path=source.name,
                     pinned_inputs={source.name: content_identity(source)})
        elif a.get("location") == "external" and a.get("verify_at_setup"):
            path = inside(a["host_root"], a["path"], must_exist=True)
            actual = content_identity(path)
            if a.get("sha256") != actual["sha256"]:
                raise ValidationError("Provided resource checksum mismatch: " + a["name"])
            a["pinned_inputs"] = {a["path"]: actual}
        prepared.append(a)
    return prepared
