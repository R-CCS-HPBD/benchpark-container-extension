# SPDX-License-Identifier: Apache-2.0
"""Portable, stdlib-only validation and persistence used on execution nodes."""
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile


class ValidationError(ValueError):
    pass


def json_bytes(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"),
                      ensure_ascii=False, allow_nan=False).encode("utf-8")


def identity(value):
    return hashlib.sha256(json_bytes(value)).hexdigest()


def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for part in iter(lambda: f.read(1024 * 1024), b""):
            h.update(part)
    return h.hexdigest()


def atomic_json(path, data, replace=False):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix=".atomic-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(json_bytes(data) + b"\n"); f.flush(); os.fsync(f.fileno())
        if replace:
            os.replace(temp, path)
        else:
            os.link(temp, path)
    finally:
        if os.path.exists(temp):
            os.unlink(temp)


def atomic_json_verified(path, data):
    """Publish immutable JSON; concurrent identical publishers are accepted.

    This is for content-addressed/shared immutable files only. Mutable attempt
    evidence must keep using ``atomic_json`` so accidental reuse remains an
    error instead of being hidden.
    """
    path = Path(path)
    try:
        atomic_json(path, data)
        return 'created'
    except FileExistsError:
        try:
            existing = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError) as error:
            raise ValidationError('Existing immutable JSON is unreadable: ' + str(path)) from error
        if existing != data:
            raise ValidationError('Immutable JSON content changed: ' + str(path))
        return 'existing-identical'


def inside(root, relative, must_exist=False):
    root = Path(root).resolve()
    rel = Path(relative)
    if rel.is_absolute() or ".." in rel.parts or not rel.parts or "\x00" in str(rel):
        raise ValidationError("Unsafe relative artifact path: " + str(relative))
    p = root / rel
    actual = p.resolve(strict=must_exist)
    if root != actual and root not in actual.parents:
        raise ValidationError("Artifact symlink/path escapes root: " + str(p))
    return p


def strict(mapping, keys, label, required=()):
    if not isinstance(mapping, dict):
        raise ValidationError(label + " must be an object")
    if set(mapping) - set(keys):
        raise ValidationError("Unknown %s fields: %s" % (label, sorted(set(mapping)-set(keys))))
    if set(required) - set(mapping):
        raise ValidationError("Missing %s fields: %s" % (label, sorted(set(required)-set(mapping))))


def safe_name(value, label="name"):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", value):
        raise ValidationError("Invalid " + label + ": " + repr(value))
    return value


SECRET = re.compile(r"(?:^|_)(?:TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL|API_KEY|PRIVATE_KEY)(?:_|$)", re.I)


def check_no_secrets(value):
    """Reject credential-looking declarations; runtime does not dump os.environ."""
    if isinstance(value, dict):
        for k, v in value.items():
            if SECRET.search(k):
                raise ValidationError("Secret-bearing configuration is not permitted: " + k)
            check_no_secrets(v)
    elif isinstance(value, list):
        for v in value:
            check_no_secrets(v)
    elif isinstance(value, str):
        if re.search(r"https?://[^/\s]+@", value) or re.search(r"(?i)(token|api_key|password)=", value):
            raise ValidationError("Credential-bearing URI/argument must not be persisted")


def expand(value, variables):
    """Only {identifier} placeholders. Not eval, no attribute or index traversal."""
    if isinstance(value, dict):
        return {k: expand(v, variables) for k, v in value.items()}
    if isinstance(value, list):
        return [expand(v, variables) for v in value]
    if not isinstance(value, str):
        return value
    pat = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")
    def sub(match):
        key = match.group(1)
        if key not in variables or isinstance(variables[key], (dict, list)):
            raise ValidationError("Unresolved concrete variable: " + key)
        return str(variables[key])
    result = pat.sub(sub, value)
    if pat.search(result):
        raise ValidationError("Nested/unresolved placeholder: " + result)
    return result
