# SPDX-License-Identifier: Apache-2.0
"""Explicit additive evidence registration. Trusted collectors cannot overwrite records."""
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
import copy
import re
from ..util import ValidationError, sha256
from .collectors.base import merge_additive, merge_file_metadata


@dataclass(frozen=True)
class EvidenceContribution:
    owner: str
    observed: dict = field(default_factory=dict)
    files: dict = field(default_factory=dict)
    scope: dict = field(default_factory=dict)
    captured_at: str = ""


def validate_files(directory, files):
    root = Path(directory).resolve()
    if not isinstance(files, dict):
        raise ValidationError("Evidence files must be a mapping")
    for name, meta in files.items():
        if not isinstance(name, str):
            raise ValidationError("Evidence path must be a string")
        rel = PurePosixPath(name)
        if (rel.is_absolute() or not rel.parts or any(x in ("..", ".") for x in name.split("/"))
                or "\\" in name or "\x00" in name or rel.parts[0] in ("cer.json", "started.json", "scratch", ".cer-internal", "evidence-integrity-failure.json")):
            raise ValidationError("Unsafe/reserved evidence path: " + name)
        path = root / name
        for parent in (path, *path.parents):
            if parent == root: break
            if parent.is_symlink(): raise ValidationError("Symlink evidence is not permitted: " + name)
        if not path.is_file() or not path.resolve().is_relative_to(root):
            raise ValidationError("Missing or nonregular evidence: " + name)
        if (not isinstance(meta, dict) or not re.fullmatch(r"[0-9a-f]{64}", str(meta.get("sha256", "")))
                or type(meta.get("bytes")) is not int or meta["bytes"] != path.stat().st_size
                or meta["sha256"] != sha256(path)):
            raise ValidationError("Evidence content mismatch: " + name)
    return files


def register_evidence(directory, record, contribution):
    """Validate the entire contribution before committing anything to the CER.

    Call before finish_run. Final records are never reopened. Files must already
    exist below this attempt; references outside it belong in group Evidence.
    """
    if (Path(directory) / "cer.json").exists():
        raise ValidationError("Cannot append to a finalized CER")
    if not isinstance(contribution, EvidenceContribution) or not re.fullmatch(r"[A-Za-z0-9_.-]+", contribution.owner):
        raise ValidationError("Invalid evidence contribution owner")
    validate_files(directory, contribution.files)
    observed = copy.deepcopy(record.get("observed", {}))
    files = copy.deepcopy(record.get("files", {}))
    merge_additive(observed, contribution.observed)
    merge_file_metadata(files, contribution.files)
    record["observed"], record["files"] = observed, files
    references = record.setdefault("evidence_references", [])
    reference = {"owner": contribution.owner, "scope": copy.deepcopy(contribution.scope),
                 "captured_at": contribution.captured_at, "files": sorted(contribution.files)}
    if reference not in references: references.append(reference)
