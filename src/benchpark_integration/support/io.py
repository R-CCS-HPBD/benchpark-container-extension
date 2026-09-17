# SPDX-License-Identifier: Apache-2.0
"""Plain-data snapshots and collision-safe file publication."""
import hashlib
import json
import os
from pathlib import Path
import re
import tempfile
from benchpark_integration.api import ExtensionError, digest, plain
STATE_DIR = ".benchpark-extensions"
SNAPSHOT = "system.extensions.json"
NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*$")

def file_hash(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()

def relative_path(value):
    p = Path(value)
    if p.is_absolute() or not p.parts or any(x in ("..", ".") for x in p.parts):
        raise ExtensionError("Unsafe relative resource path: " + str(value))
    if any("\x00" in part for part in p.parts):
        raise ExtensionError("NUL in resource path")
    return p

def contained(root, relative):
    root = Path(root).resolve()
    path = root / relative_path(relative)
    if root not in path.resolve().parents:
        raise ExtensionError("Resource escapes its root: " + str(path))
    return path

def write_new_json(path, data):
    """Publish a complete new file, atomically and without replacing any file."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(prefix=".write-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(plain(data), f, sort_keys=True, indent=2, allow_nan=False)
            f.write("\n"); f.flush(); os.fsync(f.fileno())
        os.link(temp, path)  # Fails even when an existing symlink is dangling.
    finally:
        os.unlink(temp)

def save_system_snapshot(settings, dest):
    if not settings:
        return
    data = plain(settings)
    if not isinstance(data, dict) or not all(NAME.fullmatch(n) for n in data):
        raise ExtensionError("extension_settings must be a namespaced mapping")
    body = {"schema_version": 1, "settings": data}
    write_new_json(Path(dest) / SNAPSHOT, dict(body, sha256=digest(body)))

def read_system_snapshot(dest):
    path = Path(dest) / SNAPSHOT
    if not path.is_file():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    checksum = data.pop("sha256", None)
    if data.get("schema_version") != 1 or digest(data) != checksum:
        raise ExtensionError("System snapshot checksum/schema mismatch: " + str(path))
    return data["settings"]
