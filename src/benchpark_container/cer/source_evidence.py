# SPDX-License-Identifier: Apache-2.0
"""Retain declared benchmark source bytes as attempt evidence, without Git/network."""
from pathlib import Path
import shutil
from ..util import ValidationError, atomic_json, inside, sha256


def retain_source_evidence(resources, attempt, spec):
    manifest = spec.get("benchmark_source")
    if manifest is None:
        return None
    if manifest.get("schema_version") != 1:
        raise ValidationError("Unknown benchmark source manifest")
    target_root = Path(attempt) / "source-evidence"
    target_root.mkdir(exist_ok=False)
    count = 0
    for rel, item in manifest["files"].items():
        src = inside(resources, item["resource"], must_exist=True)
        if src.is_symlink() or not src.is_file() or sha256(src) != item["sha256"]:
            raise ValidationError("Benchmark source resource mismatch: " + rel)
        if bool(src.stat().st_mode & 0o111) != item["executable"]:
            raise ValidationError("Benchmark source executable mode mismatch: " + rel)
        dst = inside(target_root, "repository/" + rel)
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dst)
        dst.chmod(0o555 if item["executable"] else 0o444)
        if sha256(dst) != item["sha256"] or dst.stat().st_size != item["bytes"]:
            raise ValidationError("Benchmark source copy mismatch: " + rel)
        count += 1
    atomic_json(target_root / "manifest.json", manifest)
    return {"status": "verified-and-retained", "scope": manifest["scope"],
            "content_sha256": manifest["content_sha256"], "file_count": count,
            "manifest": "source-evidence/manifest.json",
            "repository": manifest["repository"], "file_open_tracing": False}
