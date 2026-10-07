# SPDX-License-Identifier: Apache-2.0
"""Declared external benchmark sources; no native Benchpark imports or Git writes.

This module captures a selected source closure, not every file opened by a process.
A checkout may follow a branch. The observed commit AND captured bytes are retained.
"""
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
from urllib.parse import urlsplit, urlunsplit

from .api import ExtensionError, ResourceSpec, digest

INDEX = "benchmarks.yaml"
PREFIX = "benchmark-source/repository"
MAX_FILE_BYTES = 16 * 1024 * 1024
MAX_TOTAL_BYTES = 128 * 1024 * 1024
IGNORED_DIRS = {".git", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache"}
NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")


@dataclass(frozen=True)
class SourceSnapshot:
    manifest: dict
    resources: tuple
    application_repositories: tuple


def _relative(value):
    if not isinstance(value, str) or not value or "\\" in value:
        raise ExtensionError("Source path must be a nonempty POSIX relative path")
    p = PurePosixPath(value)
    if p.is_absolute() or any(x in ("..", ".git") for x in p.parts) or str(p) != value:
        raise ExtensionError("Unsafe/noncanonical source path: " + repr(value))
    if any(ord(c) < 32 for c in value) or value in (".", ""):
        raise ExtensionError("Unsafe source path: " + repr(value))
    return p


def checked_path(root, rel):
    root = Path(root).resolve()
    p = root
    for part in _relative(rel).parts:
        p = p / part
        if p.is_symlink():
            raise ExtensionError("Source symlinks are not supported: " + rel)
    if not p.exists():
        raise ExtensionError("Declared source is missing: " + rel)
    return p


def _read_declared_index(root):
    import yaml
    class UniqueLoader(yaml.SafeLoader):
        pass
    def mapping(loader, node, deep=False):
        out = {}
        for key_node, value_node in node.value:
            key = loader.construct_object(key_node, deep=deep)
            if not isinstance(key, str) or key in out:
                raise ExtensionError("Duplicate/non-string key in benchmarks.yaml")
            out[key] = loader.construct_object(value_node, deep=deep)
        return out
    UniqueLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, mapping)
    path = checked_path(root, INDEX)
    if path.stat().st_size > MAX_FILE_BYTES:
        raise ExtensionError("Oversized benchmark index")
    data = yaml.load(path.read_text(encoding="utf-8"), Loader=UniqueLoader)
    if not isinstance(data, dict) or data.get("schema_version") != 1:
        raise ExtensionError("benchmark index schema_version=1 is required")
    if set(data) - {"schema_version", "name", "benchmarks"}:
        raise ExtensionError("Unknown benchmark index field")
    entries = data.get("benchmarks")
    if not isinstance(entries, dict):
        raise ExtensionError("benchmark index must declare benchmarks")
    for name, item in entries.items():
        if not NAME.fullmatch(name) or not isinstance(item, dict):
            raise ExtensionError("Invalid benchmark entry")
        if set(item) - {"experiment", "application", "package", "shared"}:
            raise ExtensionError("Unknown benchmark source field: " + name)
        for key, prefix in (("experiment", "experiments/"), ("application", "repos/ramble_applications/"),
                            ("package", "repos/spack_repo/")):
            rel = item.get(key)
            if rel is None and key == "package":
                continue
            _relative(rel)
            if not rel.startswith(prefix):
                raise ExtensionError("Use Benchpark standard source paths: " + str(rel))
        shared = item.get("shared", [])
        if not isinstance(shared, list):
            raise ExtensionError("shared must be a list of declared files/directories")
        for rel in shared:
            _relative(rel)
    return data


def read_index(root):
    """Infer standard per-benchmark directories, then apply optional closure hints.

    Adding a benchmark does not require changing the index, registration, or an
    installed Python distribution. Shared sources still need explicit hints.
    """
    root = Path(root).resolve()
    index = (_read_declared_index(root) if (root / INDEX).exists() or (root / INDEX).is_symlink()
             else {"schema_version": 1, "name": root.name, "benchmarks": {}})
    entries = dict(index["benchmarks"])
    for path in sorted((root / "experiments").glob("*/experiment.py")):
        name = path.parent.name
        if not NAME.fullmatch(name):
            raise ExtensionError("Invalid benchmark directory: " + name)
        if name in entries:
            continue
        item = {"experiment": "experiments/" + name,
                "application": "repos/ramble_applications/" + name}
        # Fail at validation/init, not silently by falling back to another app.
        checked_path(root, item["application"] + "/application.py")
        candidates = ["repos/spack_repo/benchpark/packages/" + n
                      for n in dict.fromkeys((name.replace("-", "_"), name))]
        packages = [rel for rel in candidates if (root / rel / "package.py").is_file()]
        if len(packages) > 1:
            raise ExtensionError("Ambiguous package for benchmark: " + name)
        if packages:
            item["package"] = packages[0]
        entries[name] = item
    index["benchmarks"] = entries
    return index


def _index_bytes(root):
    p = Path(root) / INDEX
    return checked_path(root, INDEX).read_bytes() if p.exists() or p.is_symlink() else None


def find_source_root(experiment_dir):
    directory = Path(experiment_dir).resolve()
    for p in (directory, *directory.parents):
        if (p / INDEX).exists():
            return p
        if (p / "experiments/repo.yaml").is_file() and (p / "repos/ramble_applications/repo.yaml").is_file():
            from .repository_provider import registered_roots
            if p in registered_roots():
                return p
        # Do not accidentally adopt a manifest outside this checkout.
        if (p / ".git").exists():
            break
    return None


def _git(root, *args):
    try:
        cp = subprocess.run(["git", "-C", str(root), *args], capture_output=True,
                            text=True, timeout=15, check=False)
        return cp.stdout.strip() if cp.returncode == 0 else None
    except (OSError, subprocess.TimeoutExpired):
        return None


def _public_remote(value):
    if not value:
        return None
    if "://" in value:
        p = urlsplit(value)
        host = p.hostname or ""
        if p.port:
            host += ":" + str(p.port)
        return urlunsplit((p.scheme, host, p.path, "", ""))
    # SSH scp-style user@host:path contains no password; do not dump .git/config.
    return value


def git_identity(root):
    top = _git(root, "rev-parse", "--show-toplevel")
    commit = _git(root, "rev-parse", "--verify", "HEAD^{commit}")
    if top is None or commit is None:
        return {"status": "not-available", "url": None, "commit": None,
                "requested_ref": None, "observed_ref": None, "dirty": None}
    requested = None
    gitdir = _git(root, "rev-parse", "--absolute-git-dir")
    if gitdir:
        receipt = Path(gitdir) / "benchpark-acquisition.json"
        if receipt.is_file():
            try:
                acquired = json.loads(receipt.read_text())
                if acquired.get("resolved_commit") == commit:
                    requested = acquired.get("requested_ref")
            except (OSError, ValueError):
                pass
    status = _git(root, "status", "--porcelain=v1", "--untracked-files=all")
    return {"status": "available", "url": _public_remote(_git(root, "remote", "get-url", "origin")),
            "commit": commit, "requested_ref": requested,
            "observed_ref": _git(root, "symbolic-ref", "--quiet", "--short", "HEAD"),
            "dirty": None if status is None else bool(status),
            "subdirectory": os.path.relpath(Path(root).resolve(), Path(top).resolve())}


def _files(root, rel):
    p = checked_path(root, rel)
    if p.is_file():
        yield p
        return
    if not p.is_dir():
        raise ExtensionError("Source is not a regular file/directory: " + rel)
    for parent, dirs, names in os.walk(p, followlinks=False):
        for d in dirs:
            if (Path(parent) / d).is_symlink():
                raise ExtensionError("Source directory symlink: " + str(Path(parent) / d))
        dirs[:] = sorted(d for d in dirs if d not in IGNORED_DIRS and not d.endswith(".egg-info"))
        for name in sorted(names):
            if name == ".DS_Store" or name.endswith((".pyc", ".pyo")):
                continue
            yield Path(parent) / name


def capture_source(repository, name):
    root = Path(repository).resolve()
    index_before = _index_bytes(root)
    index = read_index(root)
    if name not in index["benchmarks"]:
        raise ExtensionError("Benchmark not declared in index: " + name)
    entry = index["benchmarks"][name]
    # Include repository descriptors so snapshot application lookup is self-contained.
    roots = [entry["experiment"], entry["application"], "experiments/repo.yaml", "repos/ramble_applications/repo.yaml"]
    if entry.get("package"):
        roots += [entry["package"], str(PurePosixPath(entry["package"]).parent.parent / "repo.yaml")]
    roots += entry.get("shared", [])
    for key, filename in (("experiment", "experiment.py"), ("application", "application.py"), ("package", "package.py")):
        if entry.get(key):
            checked_path(root, entry[key] + "/" + filename)
    git_before = git_identity(root)
    paths = {}
    for rel in roots:
        for p in _files(root, rel):
            paths[p.relative_to(root).as_posix()] = p
    # A Git submodule is a separate source repository. Do not silently omit an
    # uninitialized gitlink or report the parent commit as its complete identity.
    links = _git(root, "ls-files", "--stage", "-z")
    if links:
        for item in links.split("\0"):
            if item.startswith("160000 ") and "\t" in item:
                sub = item.split("\t", 1)[1]
                if any(sub == r or sub.startswith(r + "/") or r.startswith(sub + "/") for r in roots):
                    raise ExtensionError("Declare/materialize submodule sources separately: " + sub)
    files, resources, total = {}, [], 0
    for rel, p in sorted(paths.items()):
        p = checked_path(root, rel)
        st = p.stat()
        if not stat.S_ISREG(st.st_mode):
            raise ExtensionError("Nonregular source file: " + rel)
        if p.name == ".env" or p.suffix in (".sif", ".squashfs", ".safetensors"):
            raise ExtensionError("Use external artifacts, not source snapshots: " + rel)
        if st.st_size > MAX_FILE_BYTES:
            raise ExtensionError("Oversized source file; use an external artifact: " + rel)
        raw = p.read_bytes()
        if raw.startswith(b"version https://git-lfs.github.com/spec/v1\n"):
            raise ExtensionError("Unmaterialized Git LFS pointer is not source bytes: " + rel)
        total += len(raw)
        if total > MAX_TOTAL_BYTES:
            raise ExtensionError("Source closure exceeds snapshot limit")
        sha = hashlib.sha256(raw).hexdigest()
        executable = bool(st.st_mode & 0o111)
        target = PREFIX + "/" + rel
        files[rel] = {"sha256": sha, "bytes": len(raw), "executable": executable, "resource": target}
        resources.append(ResourceSpec(target=target, source=str(p), sha256=sha, executable=executable))
    # Reject changes during capture. Publication rechecks each resource again.
    git_after = git_identity(root)
    if git_before != git_after or _index_bytes(root) != index_before or read_index(root)["benchmarks"].get(name) != entry:
        raise ExtensionError("Source checkout/index changed during capture; retry init")
    after_paths = {p.relative_to(root).as_posix() for rel in roots for p in _files(root, rel)}
    if after_paths != set(paths):
        raise ExtensionError("Source file set changed during capture; retry init")
    for r in resources:
        if (hashlib.sha256(Path(r.source).read_bytes()).hexdigest() != r.sha256
                or bool(Path(r.source).stat().st_mode & 0o111) != r.executable):
            raise ExtensionError("Source bytes changed during capture; retry init")
    content = {"benchmark": name, "declaration": entry,
               "files": {p: {k: m[k] for k in ("sha256", "bytes", "executable")} for p, m in files.items()}}
    manifest = {"schema_version": 1, "scope": "declared-benchmark-source-closure",
                "benchmark": name, "declaration": entry, "repository": git_after,
                "index_sha256": hashlib.sha256(index_before).hexdigest() if index_before is not None else None,
                "content_sha256": digest(content), "files": files,
                "file_open_tracing": False}
    return SourceSnapshot(manifest, tuple(resources), (PREFIX + "/repos/ramble_applications",))


def capture_for_experiment(experiment_dir, name):
    root = find_source_root(experiment_dir)
    if root is None:
        return None
    index = read_index(root)
    matches = [n for n, e in index["benchmarks"].items()
               if checked_path(root, e["experiment"]).resolve() == Path(experiment_dir).resolve()]
    if len(matches) != 1:
        raise ExtensionError("External experiment must map to exactly one benchmark source entry")
    if matches[0] != name:
        raise ExtensionError("External experiment name/index mismatch: " + name)
    return capture_source(root, name)


def compare_sources(old, new):
    a, b = old["files"], new["files"]
    return {"content_changed": old["content_sha256"] != new["content_sha256"],
            "commit_changed": old["repository"].get("commit") != new["repository"].get("commit"),
            "added": sorted(set(b) - set(a)), "removed": sorted(set(a) - set(b)),
            "modified": sorted(p for p in a.keys() & b.keys() if a[p] != b[p])}
