# SPDX-License-Identifier: Apache-2.0
"""Stdlib-only execution-node adapter for the immutable Profiler v2 file contract.

No Profiler package is imported on the compute node. The Profiler-produced
worker.pyz remains inside the benchmark container. Only the fixed job envelope,
artifact references and CER owner linkage are materialized here. Compatibility
is checked against the Profiler's public API in separate tests.
"""
from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
import json
import os
import re

from .util import ValidationError, sha256, identity, atomic_json, atomic_json_verified

BUNDLE_RESOURCE = "profiler/bundle"
INNER_BUNDLE = "/bpce-profiler-bundle"
INNER_ATTEMPT = "/bpce-profiler-attempt"
REFERENCE_KEYS = {"schema_version", "kind", "attempt_id", "capture_id", "provider", "profile",
                  "configuration", "status", "profiling_status", "accepted", "measurement_kind",
                  "observation", "artifact_manifest", "cer_writer"}


@dataclass(frozen=True)
class Decoration:
    inner_argv: list[str]
    job: dict
    host_job: str
    host_output: str
    job_sha256: str


def _file(root, relative):
    """No symlink along any path, no escapes, no nonregular evidence."""
    if (not isinstance(relative, str) or not relative or "\\" in relative or "\x00" in relative
            or PurePosixPath(relative).is_absolute()
            or any(p in ("", ".", "..") for p in relative.split("/"))):
        raise ValidationError("Unsafe profiler evidence path: " + str(relative))
    root = Path(root).resolve()
    path = root
    for part in relative.split("/"):
        path = path / part
        if path.is_symlink():
            raise ValidationError("Profiler evidence symlink: " + relative)
    if not path.is_file() or not path.resolve().is_relative_to(root):
        raise ValidationError("Missing profiler evidence: " + relative)
    return path


def _load(path):
    path = Path(path)
    if path.stat().st_size > 32 * 1024 * 1024:
        raise ValidationError("Profiler JSON exceeds metadata limit")
    def pairs(items):
        out = {}
        for key, value in items:
            if key in out:
                raise ValidationError("Duplicate Profiler JSON key: " + key)
            out[key] = value
        return out
    def constant(val):
        raise ValidationError("Invalid profiler JSON constant: " + val)
    return json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=pairs,
                      parse_constant=constant)


def _verify_frozen_bundle(spec, resources):
    p = spec["profiler"]
    if (set(p) != {"enabled", "bundle_resource", "manifest_sha256", "request_sha256",
                   "capture_id", "python", "executable_id"}
            or p["enabled"] is not True or p["bundle_resource"] != BUNDLE_RESOURCE):
        raise ValidationError("Invalid frozen Profiler plan contract")
    root = Path(resources).resolve()
    bundle = root / BUNDLE_RESOURCE
    if bundle.is_symlink() or not bundle.is_dir() or not bundle.resolve().is_relative_to(root):
        raise ValidationError("Frozen Profiler bundle missing or symlinked")
    manifest_path = _file(bundle, "manifest.json")
    if sha256(manifest_path) != p["manifest_sha256"]:
        raise ValidationError("Frozen Profiler manifest hash mismatch")
    manifest = _load(manifest_path)
    if (set(manifest) != {"schema_version", "kind", "request_sha256", "files"}
            or manifest["schema_version"] != 1
            or manifest["kind"] != "benchpark-profiler-bundle"
            or not isinstance(manifest["files"], dict)):
        raise ValidationError("Invalid frozen Profiler manifest")
    for rel, expected in manifest["files"].items():
        path = _file(bundle, rel)
        if (not isinstance(expected, dict) or set(expected) != {"sha256", "bytes"}
                or type(expected["bytes"]) is not int or expected["bytes"] != path.stat().st_size
                or expected["sha256"] != sha256(path)):
            raise ValidationError("Frozen Profiler file changed: " + rel)
    actual = set()
    for path in bundle.rglob("*"):
        if path.is_symlink():
            raise ValidationError("Frozen Profiler bundle symlink")
        if path.is_file():
            actual.add(path.relative_to(bundle).as_posix())
    if actual != set(manifest["files"]) | {"manifest.json"}:
        raise ValidationError("Frozen Profiler bundle membership changed")
    request = _load(_file(bundle, "request.json"))
    if identity(request) != manifest["request_sha256"] or identity(request) != p["request_sha256"]:
        raise ValidationError("Frozen Profiler request digest mismatch")
    if request["profile"]["target"]["executable"] != p["executable_id"]:
        raise ValidationError("Frozen Profiler target mismatch")
    return bundle


def _frozen_bundle(spec, resources):
    if spec.get("profiler", {}).get("enabled") is not True:
        return None
    return _verify_frozen_bundle(spec, resources)


def prepare_capture(spec, resources, attempt, image, benchmark_argv, runtime_mounts):
    if not spec.get("profiler", {}).get("enabled"):
        return None, []
    if spec.get("parallel"):
        raise ValidationError("Profiler P0 cannot run parallel groups/ranks")
    if spec["runtime"].get("gpu", "none") != "none":
        raise ValidationError("Profiler P0 GPU allocation is not yet supported")
    bundle = _verify_frozen_bundle(spec, resources)
    p = spec["profiler"]
    if not re.fullmatch(r"[A-Za-z0-9_.-]+", p["capture_id"]):
        raise ValidationError("Profiler capture ID invalid")
    if not isinstance(benchmark_argv, list) or not benchmark_argv or not all(
            isinstance(x, str) for x in benchmark_argv):
        raise ValidationError("Profiler benchmark command must be structured argv")
    for item in runtime_mounts:
        target = item["target"]
        if any(target == reserved or target.startswith(reserved + "/") or reserved.startswith(target + "/")
               for reserved in (INNER_BUNDLE, INNER_ATTEMPT)):
            raise ValidationError("Profiler mount collides with a declared artifact")
    context = {"attempt_id": attempt.name, "capture_id": p["capture_id"],
               "executable_id": p["executable_id"], "rank_id": "0", "process_role": "benchmark",
               "image_identity": image.identity, "working_dir": "/bpce/work", "python": p["python"],
               "allocated_cuda_uuids": [], "allocated_hip_uuids": [],
               "coordination_dir": INNER_ATTEMPT + "/coordination",
               "node_exclusive": False, "gpu_exclusive": False,
               "workload_identity": spec["condition_id"],
               "benchmark_parameters": spec.get("parameters", {}),
               "output_dir": INNER_ATTEMPT + "/profiler/" + p["capture_id"],
               "execution_plane": "container-inner"}
    job = {"api_version": 1, "kind": "benchpark-container-profiler-capture",
           "bundle_dir": INNER_BUNDLE, "bundle_manifest_sha256": sha256(bundle / "manifest.json"),
           "context": context, "command": {"kind": "argv", "argv": list(benchmark_argv)}}
    capture = p["capture_id"]
    prepared = Path(attempt) / "profiler-prepared" / capture
    prepared.mkdir(parents=True, exist_ok=False)
    job_path = prepared / "job.json"
    atomic_json(job_path, job)
    output = Path(attempt) / "profiler" / capture
    inner_worker = INNER_BUNDLE + "/runtime/worker.pyz"
    inner_job = INNER_ATTEMPT + "/profiler-prepared/" + capture + "/job.json"
    cmd = [p["python"], inner_worker, "--job", inner_job, "--job-sha256", identity(job)]
    decoration = Decoration(cmd, job, str(job_path), str(output), identity(job))
    mounts = [dict(resolved_source=str(bundle), target=INNER_BUNDLE, readonly=True),
              dict(resolved_source=str(Path(attempt).resolve()), target=INNER_ATTEMPT, readonly=False)]
    return decoration, mounts


def inspect_capture(decoration):
    """Verify finalized observation, job origin and ALL artifact bytes."""
    if identity(decoration.job) != decoration.job_sha256:
        raise ValidationError("Profiler job hash changed")
    root = Path(decoration.host_output)
    if root.is_symlink():
        raise ValidationError("Profiler capture root is a symlink")
    root = root.resolve()
    obs_path = _file(root, "observation.json")
    obs = _load(obs_path)
    unsigned = dict(obs)
    if (obs.get("kind") != "benchpark-profiler-observation" or obs.get("phase") != "finalized"
            or unsigned.pop("record_sha256", None) != identity(unsigned)
            or type(obs.get("accepted")) is not bool):
        raise ValidationError("Profiler observation identity/status mismatch")
    if obs.get("job_sha256") != decoration.job_sha256 or obs.get("context") != decoration.job["context"]:
        raise ValidationError("Profiler observation foreign capture/job")
    manifest = _file(root, "artifact_manifest.json")
    if sha256(manifest) != obs.get("artifact_manifest_sha256"):
        raise ValidationError("Profiler capture manifest changed")
    data = _load(manifest)
    if set(data) != {"schema_version", "artifacts"} or data["schema_version"] != 1:
        raise ValidationError("Profiler artifact manifest schema mismatch")
    artifacts = data["artifacts"]
    if not isinstance(artifacts, list) or artifacts != obs.get("artifacts"):
        raise ValidationError("Profiler observation and artifact manifest disagree")
    seen = set()
    for item in artifacts:
        if not isinstance(item, dict) or set(item) != {"path", "sha256", "bytes", "role"}:
            raise ValidationError("Invalid profiler artifact manifest item")
        if item["path"] in seen:
            raise ValidationError("Duplicate profiler artifact")
        seen.add(item["path"])
        file = _file(root, item["path"])
        if (type(item["bytes"]) is not int or item["bytes"] != file.stat().st_size
                or item["sha256"] != sha256(file)):
            raise ValidationError("Profiler artifact hash/size mismatch")
    actual = set()
    for path in root.rglob("*"):
        if path.is_symlink():
            raise ValidationError("Profiler capture symlink")
        if path.is_file():
            actual.add(path.relative_to(root).as_posix())
    if actual - {"observation.json", "artifact_manifest.json", "progress.json"} != seen:
        raise ValidationError("Profiler output contains unregistered/unexpected files")
    if "origin.json" in seen:
        origin = _load(_file(root, "origin.json"))
        expected = {"schema_version": 1, "attempt_id": decoration.job["context"]["attempt_id"],
                    "capture_id": decoration.job["context"]["capture_id"],
                    "job_sha256": decoration.job_sha256}
        if origin != expected:
            raise ValidationError("Profiler origin mismatches capture")
    return obs


def _ref_file(attempt, path):
    path = Path(path)
    relative = path.relative_to(attempt).as_posix()
    verified = _file(attempt, relative)
    return {"path": relative, "sha256": sha256(verified), "bytes": verified.stat().st_size}


def capture_reference(decoration):
    obs = inspect_capture(decoration)
    root = Path(decoration.host_output).resolve()
    attempt = root.parent.parent
    ctx = obs["context"]
    if root != attempt / "profiler" / ctx["capture_id"]:
        raise ValidationError("Profiler capture output in wrong attempt location")
    request = obs.get("requested", {})
    return {"schema_version": 2, "kind": "benchpark-profiler-capture-reference",
            "attempt_id": ctx["attempt_id"], "capture_id": ctx["capture_id"],
            "provider": request.get("profile", {}).get("provider"),
            "profile": request.get("selected"),
            "configuration": {"bundle_manifest_sha256": decoration.job["bundle_manifest_sha256"],
                              "request_sha256": obs.get("request_sha256"),
                              "job_sha256": obs["job_sha256"]},
            "status": obs["status"], "profiling_status": obs["profiling_status"],
            "accepted": obs["accepted"], "measurement_kind": obs["measurement_kind"],
            "observation": _ref_file(attempt, root / "observation.json"),
            "artifact_manifest": _ref_file(attempt, root / "artifact_manifest.json"),
            "cer_writer": "CONTAINER_OWNER_ONLY"}


def collect_capture(attempt, record, decoration):
    from .cer.evidence import EvidenceContribution, register_evidence
    reference = capture_reference(decoration)
    if reference["attempt_id"] != record["attempt_id"]:
        raise ValidationError("Profiler capture belongs to a different CER attempt")
    capture = reference["capture_id"]
    if capture != decoration.job["context"]["capture_id"]:
        raise ValidationError("Profiler capture_id mismatch")
    job = Path(decoration.host_job)
    output = Path(decoration.host_output)
    root = Path(attempt).resolve()
    if not job.is_file() or not output.is_dir():
        raise ValidationError("Profiler prepared job or capture is missing")
    files = {}
    paths = [job] + sorted(output.rglob("*"))
    if len(paths) > 10000:
        raise ValidationError("Profiler evidence file-count limit exceeded")
    for path in paths:
        if path.is_symlink():
            raise ValidationError("Profiler evidence contains a symlink")
        if not path.is_file():
            continue
        if not path.resolve().is_relative_to(root):
            raise ValidationError("Profiler evidence escapes its attempt")
        rel = path.relative_to(root).as_posix()
        files[rel] = {"sha256": sha256(path), "bytes": path.stat().st_size}
    ref_paths = (reference["observation"]["path"], reference["artifact_manifest"]["path"])
    if not all(key in files for key in ref_paths):
        raise ValidationError("Profiler capture reference is not registered as evidence")
    contribution = EvidenceContribution(
        owner="profiler", observed={"profiler": {"captures": {capture: reference}}},
        files=files, scope={"attempt_id": record["attempt_id"], "capture_id": capture,
                            "execution_plane": "container-inner"},
        captured_at=datetime.now(timezone.utc).isoformat())
    register_evidence(attempt, record, contribution)
    return reference


def verified_benchmark_nonzero(reference, observation):
    """Classify a confirmed benchmark failure, never a Worker exit by itself.

    `inspect_capture` must have verified the signed observation and artifacts
    before this predicate is used by the execution owner.
    """
    workload = observation.get("workload")
    if (not isinstance(reference, dict) or reference.get("profiling_status") != "WORKLOAD_FAILED"
            or not isinstance(workload, dict) or workload.get("status") != "FAILED"):
        return False
    codes = workload.get("exit_codes")
    return (type(codes) is list and bool(codes)
            and all(type(v) is int for v in codes)
            and any(v != 0 for v in codes))


def _validate_link_ownership(attempt, decoration, reference):
    from .cer.evidence import validate_files
    cer = Path(attempt) / "cer.json"
    if not cer.is_file() or cer.is_symlink():
        raise ValidationError("A sealed regular CER is required for Profiler linkage")
    sealed = _load(cer)
    unsigned = dict(sealed)
    if unsigned.pop("record_sha256", None) != identity(unsigned):
        raise ValidationError("Sealed CER record identity is invalid")
    if (sealed.get("attempt_id") != reference.get("attempt_id")
            or sealed.get("run_id") != reference.get("attempt_id")):
        raise ValidationError("Sealed CER identity differs from Profiler capture")
    capture = decoration.job["context"]["capture_id"]
    try:
        actual = sealed["observed"]["profiler"]["captures"][capture]
    except (KeyError, TypeError) as exc:
        raise ValidationError("Sealed CER has no matching Profiler capture reference") from exc
    if actual != reference:
        raise ValidationError("Sealed CER Profiler capture reference mismatch")
    owner_refs = [r for r in sealed.get("evidence_references", []) if isinstance(r, dict)
                  and r.get("owner") == "profiler" and isinstance(r.get("scope"), dict)
                  and r["scope"].get("capture_id") == capture
                  and r["scope"].get("attempt_id") == sealed["attempt_id"]]
    if len(owner_refs) != 1:
        raise ValidationError("Sealed CER does not uniquely register Profiler ownership")
    registered = owner_refs[0].get("files")
    if not isinstance(registered, list):
        raise ValidationError("Sealed CER has no Profiler file membership")
    required = {reference["observation"]["path"], reference["artifact_manifest"]["path"],
                f"profiler-prepared/{capture}/job.json"}
    if not required <= set(registered):
        raise ValidationError("Sealed CER omitted a Profiler referenced file")
    sealed_files = sealed.get("files", {})
    if not all(key in sealed_files for key in registered):
        raise ValidationError("Sealed CER Profiler evidence membership is incomplete")
    validate_files(attempt, {key: sealed_files[key] for key in registered})
    if capture_reference(decoration) != reference:
        raise ValidationError("Profiler compact reference differs from current capture")
    return cer


def link_capture(attempt, decoration, expected_reference, *, receipt_directory):
    root = Path(attempt).resolve()
    cer = _validate_link_ownership(root, decoration, expected_reference)
    capture = decoration.job["context"]["capture_id"]
    ref = {"schema_version": 1, "kind": "benchpark-profiler-cer-link",
           "cer_identity_pointer": "/run_id", "cer": _ref_file(root, cer),
           "capture": expected_reference, "prepared_job": _ref_file(root, decoration.host_job),
           "finalization": "OWNER_SUPPLIED_FINAL_CER_CHECKSUM",
           "trust_boundary": "TRUSTED_OWNER_NOT_CRYPTOGRAPHIC_ATTESTATION"}
    ref["receipt_sha256"] = identity(ref)
    receipt_directory = Path(receipt_directory)
    if not receipt_directory.is_absolute():
        raise ValidationError("Profiler receipt path must be absolute")
    current = Path(receipt_directory.anchor)
    for part in receipt_directory.parts[1:]:
        current = current / part
        if current.is_symlink():
            raise ValidationError("Profiler receipt path uses symlink")
    receipt = receipt_directory / (capture + ".json")
    atomic_json_verified(receipt, ref)
    return {"status": "LINKED", "receipt": str(receipt),
            "attempt_id": expected_reference["attempt_id"], "capture_id": capture,
            "profiling_status": expected_reference["profiling_status"],
            "accepted": expected_reference["accepted"]}
