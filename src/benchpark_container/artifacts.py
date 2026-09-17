# SPDX-License-Identifier: Apache-2.0
"""Materialize per-attempt mounts; do not download models implicitly."""
import itertools
import json
import re
import os
from pathlib import Path
from .util import ValidationError, inside, sha256, identity, expand
from .contracts import validate_targets


def verify_tree_links(source):
    source = source.resolve()
    for p in source.rglob("*"):
        if p.is_symlink():
            actual = p.resolve(strict=True)
            if actual != source and source not in actual.parents:
                raise ValidationError("Symlink outside mounted artifact: %s; mount the enclosing snapshot/cache instead" % p)



def tree_identity(root):
    """Content identity including symlink targets; never silently follow outside links."""
    root = Path(root)
    rows = []
    for p in sorted(root.rglob("*")):
        name = str(p.relative_to(root))
        if p.is_symlink():
            rows.append({"path": name, "symlink": os.readlink(p)})
        elif p.is_file():
            rows.append({"path": name, "sha256": sha256(p), "executable": bool(p.stat().st_mode & 0o111)})
        elif p.is_dir():
            rows.append({"path": name, "directory": True})
        else:
            raise ValidationError("Unrecordable special file in artifact: " + str(p))
    return {"files": sum("sha256" in row for row in rows), "entries": len(rows), "sha256": identity(rows)}


def content_identity(path):
    path = Path(path)
    if path.is_file():
        return {"kind": "file", "sha256": sha256(path)}
    if path.is_dir():
        verify_tree_links(path)
        return dict(tree_identity(path), kind="directory")
    raise ValidationError("Input must be a regular file or directory: " + str(path))


def pin_external_artifacts(artifacts, variants):
    """Pin source-visible candidates before init completes, without downloading.

    Missing Cartesian candidates may be invalid zip pairings. Each *actual*
    concrete path must still have an init-time identity or execution fails.
    """
    output = []
    for artifact in artifacts:
        a = dict(artifact)
        if a.get("location") == "external":
            names = sorted(set(re.findall(r"\{([A-Za-z_]\w*)\}", a["path"])))
            choices = []
            for name in names:
                if name not in variants:
                    raise ValidationError("No candidates for artifact variable: " + name)
                choices.append(variants[name])
            if any(len(values) == 0 for values in choices):
                raise ValidationError("Empty artifact candidates")
            count = 1
            for values in choices:
                count *= len(values)
            if count > 100000:
                raise ValidationError("Too many artifact candidates; use paired explicit paths")
            pins = {}
            for values in itertools.product(*choices):
                relative = expand(a["path"], dict(zip(names, values)))
                path = inside(a["host_root"], relative)
                if path.exists():
                    pins[relative] = content_identity(path)
            if not pins:
                raise ValidationError("No source-visible fixed artifact: " + a["name"])
            a["pinned_inputs"] = pins
        output.append(a)
    return output

def materialize(artifacts, resources, attempt):
    validate_targets(artifacts, concrete=True)
    mounts = []
    for a in artifacts:
        record = dict(a)
        if a["location"] == "attempt":
            source = inside(attempt / "outputs", a["name"])
            source.mkdir(parents=True)
            record["verification"] = "writable-output"
        else:
            if a["location"] == "resource":
                source = inside(resources, a["resource"], must_exist=True)
            else:
                source = inside(a["host_root"], a["path"], must_exist=True)
                pins = a.get("pinned_inputs")
                if pins is not None:
                    expected = pins.get(a["path"])
                    if expected is None or expected != content_identity(source):
                        raise ValidationError("Fixed external input changed/not pinned at init: " + a["name"])
            if source.is_file():
                h = sha256(source)
                if a.get("sha256") and h != a["sha256"]:
                    raise ValidationError("Input checksum mismatch: " + a["name"])
                record.update(observed_sha256=h, verification="sha256")
            elif source.is_dir():
                verify_tree_links(source)
                record.update(observed_tree=tree_identity(source), content_verified=True,
                              declared_revision_verified=False)
                if a.get("manifest"):
                    manifest = inside(source, a["manifest"], must_exist=True)
                    if a.get("manifest_sha256") and sha256(manifest) != a["manifest_sha256"]:
                        raise ValidationError("Pinned manifest changed: " + a["name"])
                    data = json.loads(manifest.read_text(encoding="utf-8"))
                    for item in data["files"]:
                        p = inside(source, item["path"], must_exist=True)
                        if not p.is_file() or sha256(p) != item["sha256"]:
                            raise ValidationError("Artifact manifest mismatch: " + str(p))
                    record.update(observed_manifest_sha256=sha256(manifest),
                                  verification="listed-files-sha256", unlisted_files="not-verified")
                else:
                    record.update(verification="tree-sha256", declared_revision_verified=False)
            else:
                raise ValidationError("Input must be a regular file or directory")
        if any(x in str(source) for x in ":,\n\r\x00"):
            raise ValidationError("Apptainer bind cannot encode this host path: " + str(source))
        record["resolved_source"] = str(source.resolve())
        mounts.append(record)
    return mounts
