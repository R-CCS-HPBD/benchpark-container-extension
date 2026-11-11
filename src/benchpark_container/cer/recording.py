# SPDX-License-Identifier: Apache-2.0
import datetime
import os
from pathlib import Path
import platform
import socket
import uuid
from ..util import atomic_json, identity, sha256
from .collectors import collect_v1
from .collectors.base import merge_additive, merge_file_metadata


def now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def start_run(root, spec):
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    run_id = uuid.uuid4().hex
    directory = root / run_id
    directory.mkdir(mode=0o700)
    # run_id is a globally unique attempt, not the condition or repeat identity.
    record = {"schema_version": 2, "kind": "benchpark-container-execution-record",
        "run_id": run_id, "attempt_id": run_id, "condition_id": spec["condition_id"],
        "repeat_index": spec.get("repeat_index", "unknown"),
        "ramble_experiment": spec.get("ramble_experiment", "unknown"),
        "started_at": now(), "status": "RUNNING", "resolved": spec,
        "observed": {"host": {"hostname": socket.gethostname(), "os": platform.system(),
            "kernel": platform.release(), "machine": platform.machine()},
            "allocation": {k: os.environ[k] for k in
                ("SLURM_JOB_ID", "SLURM_JOB_NODELIST", "SLURM_NNODES", "CUDA_VISIBLE_DEVICES",
                 "ROCR_VISIBLE_DEVICES", "HIP_VISIBLE_DEVICES") if k in os.environ}},
        "result": {"benchmark_validity": "not-assessed", "quality": "not-assessed"},
        "privacy": {"environment": "explicit allowlist only", "credentials": "not-collected"}}
    atomic_json(directory / "started.json", record)
    return directory, record


def finish_run(directory, record):
    import copy
    from .evidence import validate_files
    directory = Path(directory)
    if (directory / "cer.json").exists():
        raise FileExistsError(str(directory / "cer.json"))
    record["finished_at"] = now()
    files = copy.deepcopy(record.get("files", {}))
    # A registered artifact changing is integrity failure, not an optional
    # collector failure. Keep started.json and raise instead of resealing it.
    _validate_before_seal(directory, record, files)
    paths = [p for p in directory.iterdir() if p.is_file() and p.name not in ("cer.json", "started.json", "evidence-integrity-failure.json")]
    for folder in ("outputs", "source-evidence"):
        paths += [p for p in (directory / folder).rglob("*") if p.is_file()]
    for path in sorted(paths):
        name = path.relative_to(directory).as_posix()
        meta = {"sha256": sha256(path), "bytes": path.stat().st_size}
        if name.startswith("source-evidence/"):
            meta["executable"] = bool(path.stat().st_mode & 0o111)
        validate_files(directory, {name: meta})
        if name in files:
            # Preserve extra attribution metadata, but never a stale digest.
            if any(files[name].get(k) != v for k, v in meta.items()):
                raise ValueError("CER evidence content collision: " + name)
        else:
            files[name] = meta
    record["files"] = files
    try:
        collected = collect_v1(directory, record)
        observed = copy.deepcopy(record.get("observed", {}))
        new_files = copy.deepcopy(files)
        validate_files(directory, collected["files"])
        merge_additive(observed, collected["observed"])
        merge_file_metadata(new_files, collected["files"])
        record["observed"], record["files"] = observed, new_files
        record["collection"] = collected["collection"]
    except Exception as exc:
        record["collection"] = {"schema_version": 1, "status": "partial", "non_fatal": True,
            "capture_phase": "post-execution-finalization", "collectors": [],
            "finalization_errors": [type(exc).__name__ + ": " + str(exc)]}
    _validate_before_seal(directory, record, record["files"])
    record["record_sha256"] = identity(record)
    atomic_json(directory / "cer.json", record)


def _validate_before_seal(directory, record, files):
    """Never seal stale evidence. Preserve app verdict in a separate failure record.

    Keep existing strict behavior (raise, no final cer.json), but do not lose
    the already observed application result when integrity validation fails.
    """
    from .evidence import validate_files
    from .collectors.publication import FAILURE_RECORD
    try:
        validate_files(directory, files)
    except Exception as exc:
        failure = {"schema_version": 1, "kind": "benchpark-evidence-integrity-failure",
            "attempt_id": record["attempt_id"], "condition_id": record["condition_id"],
            "application_status": record.get("status"), "application_result": record.get("result", {}),
            "evidence_integrity": "FAILED", "cer_sealed": False,
            "detected_at": now(), "error_type": type(exc).__name__, "error": str(exc)}
        failure["record_sha256"] = identity(failure)
        path = Path(directory) / FAILURE_RECORD
        if not path.exists() and not path.is_symlink():
            atomic_json(path, failure)
        raise
