# SPDX-License-Identifier: Apache-2.0
"""Profiler declaration/frozen-resource adapter. Runs only at experiment initialization.

This file is intentionally NOT included in the stdlib-only execution-node zipapp.
"""
from __future__ import annotations
from importlib import metadata
from pathlib import Path, PurePosixPath
import re
from .util import ValidationError, sha256
from .profiler_interop import BUNDLE_RESOURCE

CAPABILITIES = ("profiler-init", "profiler-inner-worker", "attempt-evidence-before-seal",
                "profiler-link-after-seal")


def interop_descriptor():
    return {"api_version": 2, "capabilities": list(CAPABILITIES),
            "scope": "opt-in single attempt; CPU; GPU/parallel pending E2E"}


def _api():
    eps = list(metadata.entry_points().select(group="benchpark.container.decorators.v2", name="profiler"))
    if len(eps) != 1:
        raise ValidationError("Profiler v2 requires exactly one installed decorator entry point")
    descriptor = eps[0].load()()
    if (not isinstance(descriptor, dict) or descriptor.get("api_version") != 2
            or descriptor.get("requires_execution_owner") != "container"
            or any(not callable(descriptor.get(k)) for k in
                   ("freeze_request", "decorate", "collect", "finalize_link"))):
        raise ValidationError("Profiler decorator does not satisfy API v2")
    return descriptor


def stage_frozen_bundle(source_root, declaration, executable, image_tools, runtime):
    """Stage an already frozen, verified Profiler bundle into immutable resources."""
    from benchpark_integration.api import ResourceSpec
    from .contracts import executable_value
    if not isinstance(declaration, dict) or not set(declaration) <= {
            "enabled", "bundle", "python", "capture_id"}:
        raise ValidationError("Profiler declaration must contain only enabled/bundle/python/capture_id")
    if type(declaration.get("enabled")) is not bool:
        raise ValidationError("Profiler enabled must be a boolean")
    if not declaration["enabled"]:
        if len(declaration) != 1:
            raise ValidationError("Disabled profiler declaration cannot carry other fields")
        return None, []
    if runtime.get("gpu", "none") != "none":
        raise ValidationError("Profiler interop P0 is CPU-only; GPU allocation identity needs separate validation")
    bundle_rel = declaration.get("bundle")
    if (not isinstance(bundle_rel, str) or not bundle_rel or PurePosixPath(bundle_rel).is_absolute()
            or any(part in ("..", ".") for part in bundle_rel.split("/"))
            or "\\" in bundle_rel or "\x00" in bundle_rel):
        raise ValidationError("Profiler bundle must be a normalized source-relative directory")
    root = Path(source_root).resolve()
    bundle = root / bundle_rel
    if not bundle.is_dir() or not bundle.resolve().is_relative_to(root):
        raise ValidationError("Profiler frozen bundle is absent/outside experiment source")
    try:
        from benchpark_profiler.bundle import verify_bundle
        request = verify_bundle(bundle)
    except ImportError as exc:
        raise ValidationError("Install benchpark-profiler-extension to initialize profiling") from exc
    target = request["profile"]["target"]
    if target["executable"] != executable or target["process_role"] != "benchmark":
        raise ValidationError("Profiler target must match the single Benchpark executable/benchmark role")
    python = declaration.get("python", image_tools.get("python"))
    if python is None:
        raise ValidationError("Profiler requires a declared Python executable inside the selected image")
    executable_value(python, "Profiler image Python")
    capture = declaration.get("capture_id", "capture-1")
    if not isinstance(capture, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+", capture):
        raise ValidationError("Invalid Profiler capture_id")
    files = []
    for path in sorted(bundle.rglob("*")):
        if path.is_symlink():
            raise ValidationError("Profiler bundle contains a symlink")
        if path.is_file():
            relative = path.relative_to(bundle).as_posix()
            files.append(ResourceSpec(target=f"{BUNDLE_RESOURCE}/{relative}",
                                      source=str(path), sha256=sha256(path)))
    from benchpark_profiler.common import digest
    return ({"enabled": True, "bundle_resource": BUNDLE_RESOURCE,
             "manifest_sha256": sha256(bundle / "manifest.json"),
             "request_sha256": digest(request), "capture_id": capture,
             "python": python, "executable_id": executable}, files)
