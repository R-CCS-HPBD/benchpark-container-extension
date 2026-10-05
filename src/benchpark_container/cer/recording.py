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
    directory = Path(directory)
    if (directory / "cer.json").exists():
        raise FileExistsError(str(directory / "cer.json"))
    record["finished_at"] = now()

    # Additive post-run reproducibility evidence. Collection is intentionally
    # outside condition/plan identity and is non-fatal to benchmark status.
    collected = collect_v1(directory, record)
    try:
        merge_additive(record.setdefault("observed", {}), collected["observed"])
    except ValueError as exc:
        collected["collection"]["status"] = "partial"
        collected["collection"].setdefault("finalization_errors", []).append(str(exc))
    record["collection"] = collected["collection"]

    files = {}
    try:
        merge_file_metadata(files, collected["files"])
    except ValueError as exc:
        record["collection"]["status"] = "partial"
        record["collection"].setdefault("finalization_errors", []).append(str(exc))
    for p in sorted(directory.iterdir()):
        if p.is_file() and p.name not in ("cer.json", "started.json"):
            meta = {"sha256": sha256(p), "bytes": p.stat().st_size}
            merge_file_metadata(files, {p.name: meta})
    # Result artifacts live on private mounts outside the mutable Python env.
    # Do not hash the whole venv, model cache, or unbounded external trees.
    for p in sorted((directory / "outputs").rglob("*")):
        if p.is_file() and not p.is_symlink():
            key = str(p.relative_to(directory))
            meta = {"sha256": sha256(p), "bytes": p.stat().st_size}
            merge_file_metadata(files, {key: meta})
    record["files"] = files
    record["record_sha256"] = identity(record)
    atomic_json(directory / "cer.json", record)
    # started.json is intentionally retained. No final record is ever overwritten.
