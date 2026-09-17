# SPDX-License-Identifier: Apache-2.0
"""Static validation and snapshot discovery for pip requirement files.

Architecture rule: Python dependency *descriptions* may cross the container
boundary; Python installers and package payloads do not.  The actual install is
performed later by the Common Base container's own ``python -m pip``.

This reader therefore snapshots only requirement/constraint text files.  Local
wheel/sdist/path/VCS inputs are rejected here; if an experiment needs a custom
build/install procedure it must be expressed as a Git-managed setup script.
"""
from pathlib import Path
import re
import shlex

from packaging.requirements import Requirement, InvalidRequirement
from packaging.utils import canonicalize_name
from packaging.version import Version, InvalidVersion

from .util import ValidationError, inside, check_no_secrets, sha256


def scan_requirements(root, requested):
    root = Path(root).resolve()
    visited, visiting, pins, files, entries = set(), set(), {}, {}, []

    def locate(path):
        try:
            relative = Path(path).resolve().relative_to(root)
        except ValueError as e:
            raise ValidationError("Requirements file escapes experiment source: " + str(path)) from e
        return inside(root, str(relative), must_exist=True)

    def add_file(path):
        path = locate(path)
        if not path.is_file():
            raise ValidationError("Requirements input must be a regular file: " + str(path))
        # Requirement resources are text declarations only.  Never snapshot a
        # wheel/sdist/binary as part of the pip path.
        if path.suffix.lower() in ('.whl', '.zip', '.gz', '.bz2', '.xz', '.tar'):
            raise ValidationError("Python package payloads are not mounted; use a requirement pin or setup script: " + str(path))
        files[str(path.relative_to(root))] = sha256(path)
        return path

    def add_pin(name, version, marker, origin):
        name = canonicalize_name(name)
        try:
            normalized = str(Version(version))
        except InvalidVersion as e:
            raise ValidationError("Invalid fixed version: " + str(version)) from e
        pins.setdefault(name, set()).add(normalized)
        entries.append({"name": name, "version": normalized, "marker": marker, "source": origin})

    def option_value(line, short, long):
        if line.startswith(long):
            tail = line[len(long):]
            if tail and not tail[0].isspace() and tail[0] != "=":
                return None
            tail = tail.lstrip("= ")
        elif line.startswith(short):
            tail = line[len(short):].strip()
        else:
            return None
        parts = shlex.split(tail)
        if len(parts) != 1:
            raise ValidationError("Expected one include path: " + line)
        return parts[0]

    def visit(path):
        p = locate(path)
        if p in visiting:
            raise ValidationError("Cyclic requirements include: " + str(p.relative_to(root)))
        if p in visited:
            return
        visiting.add(p)
        add_file(p)
        text = p.read_text(encoding="utf-8")
        check_no_secrets(text)
        body = re.sub(r"\\\r?\n", " ", text)
        for line_no, raw in enumerate(body.splitlines(), 1):
            line = re.split(r"\s+#", raw.strip(), maxsplit=1)[0].strip()
            if not line or line.startswith("#"):
                continue
            origin = str(p.relative_to(root)) + ":" + str(line_no)
            token = option_value(line, "-r", "--requirement")
            if token is None:
                token = option_value(line, "-c", "--constraint")
            if token is not None:
                if "${" in token or Path(token).is_absolute():
                    raise ValidationError("Environment-dependent/absolute include path: " + origin)
                visit(p.parent / token)
                continue

            # Hashes are metadata in the text file, not payloads.  Accept only
            # SHA-256 hashes so the static contract stays deterministic.
            chunks = re.split(r"\s+--hash(?:=|\s+)", line)
            requirement = chunks[0].strip()
            for h in chunks[1:]:
                if not re.fullmatch(r"sha256:[0-9a-fA-F]{64}", h.strip()):
                    raise ValidationError("Invalid requirement SHA-256: " + origin)

            if line.startswith("-"):
                parts = shlex.split(line)
                option = parts[0].split("=")[0]
                flags = {"--no-index", "--require-hashes", "--prefer-binary"}
                valued = {"--index-url", "--extra-index-url", "--find-links", "--only-binary"}
                if option in flags:
                    if len(parts) != 1 or "=" in parts[0]:
                        raise ValidationError("Unexpected option argument: " + origin)
                elif option in valued:
                    values = ([parts[0].split("=", 1)[1]] + parts[1:]) if "=" in parts[0] else parts[1:]
                    if len(values) != 1 or not values[0] or "${" in values[0]:
                        raise ValidationError("Invalid pip option value: " + origin)
                    value = values[0]
                    if option == "--only-binary":
                        if value != ":all:":
                            raise ValidationError("Reproducible dependencies require binary wheels (:all:)")
                    elif not value.startswith("https://"):
                        raise ValidationError("Package locations in requirements must be HTTPS; local payloads belong in setup scripts: " + origin)
                else:
                    raise ValidationError("Unsupported reproducible pip option: " + option)
                continue

            # Explicitly reject local paths, VCS and direct URLs.  The only
            # software crossing this path is the requirements text itself.
            lowered = requirement.lower()
            if (requirement.startswith(("./", "../", "/", "file:")) or
                    lowered.startswith(("git+", "hg+", "svn+", "bzr+"))):
                raise ValidationError("Local/VCS Python package payloads are not mounted; use a setup script: " + origin)
            try:
                req = Requirement(requirement)
            except InvalidRequirement as e:
                raise ValidationError("Invalid pinned requirement at " + origin) from e
            if req.url:
                raise ValidationError("Direct-reference Python payloads are not part of the requirement-only boundary: " + origin)
            versions = list(req.specifier)
            if len(versions) != 1 or versions[0].operator != "==" or "*" in versions[0].version:
                raise ValidationError("Pin each Python dependency with name==version (no latest/ranges): " + origin + " " + requirement)
            add_pin(req.name, versions[0].version, str(req.marker) if req.marker else None, origin)
        visiting.remove(p)
        visited.add(p)

    if not isinstance(requested, (list, tuple)) or not all(isinstance(r, str) for r in requested):
        raise ValidationError("requirements must be a list of source-relative files")
    for rel in requested:
        visit(inside(root, rel, must_exist=True))

    unconditional = {}
    for entry in entries:
        if entry["marker"] is None:
            unconditional.setdefault(entry["name"], set()).add(Version(entry["version"]))
    for name, versions in unconditional.items():
        if len(versions) > 1:
            raise ValidationError("Conflicting unconditional fixed versions for " + name)

    return {
        "pins": {n: sorted(v) for n, v in sorted(pins.items())},
        "files": dict(sorted(files.items())),
        "entries": entries,
        "transitive_resolution": "common-base-pip-at-runtime",
        "boundary": "requirements-text-only",
    }


def pinned_requirements(root, requested):
    return scan_requirements(root, requested)["pins"]
