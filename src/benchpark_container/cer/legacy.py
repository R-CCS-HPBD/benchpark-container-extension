# Copyright 2023 Lawrence Livermore National Security, LLC and other
# Benchpark Project Developers. See the top-level COPYRIGHT file for details.
#
# SPDX-License-Identifier: Apache-2.0

"""Container Execution Record (CER) data model.

CER は以下の3つの目的で各コンテナ実行の実行証跡を記録する:
  1. 性能劣化の原因追跡 — FoM 劣化時に何が変わったかを CER 間 diff で特定
  2. 環境変化の検出 — GPU ドライバ/HW 等の host 環境変化を構造化して検出
  3. 再実行の証跡 — メトリクス再取得時に同一条件での再実行条件を復元

スコープ外: セキュリティ監査、リソースポリシー enforcement、determinism 管理。

Implements the provenance schema defined in:
  docs/design/3_DETAIL_DESIGN/3.2_CONTAINER_DESIGN.md §7-0-1
  docs/design/3_DETAIL_DESIGN/3.2.1_CONTAINER_PROVENANCE_POLICY.md §6

Key types:
  CerStatus           — 4-stage finalization lifecycle (C-21)
  RunState            — execution lifecycle states (2.6_WORKFLOW_STATE_MACHINE)
  ArtifactSafetyState — safety classification (distinct from RunState)
  ContainerExecutionRecord — full provenance record for one container execution
  HostEnvironmentRecord    — Tier 1/2/3 host fields (3.2.9)
  DatasetManifest          — immutable dataset identity (3.2.5)
  DistributedExecutionRecord — aggregate record for N-rank execution (3.2.6 §7)

Functions:
  compute_rsd(values)      — relative standard deviation (REPLAY-02 criterion)
  validate_container_uri(uri) — enforce @sha256: digest (SEC-01 criterion)
"""

from __future__ import annotations

import enum
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class CerStatus(enum.Enum):
    """CER finalization lifecycle stages (3.2.1 §6, C-21).

    Transitions must be monotonically forward:
        draft → host_appended → finalized → frozen
    """

    DRAFT = "draft"
    HOST_APPENDED = "host_appended"
    FINALIZED = "finalized"
    FROZEN = "frozen"

    # Ordering table used by advance_status()
    _order = None  # populated after class definition


_CER_STATUS_ORDER = [
    CerStatus.DRAFT,
    CerStatus.HOST_APPENDED,
    CerStatus.FINALIZED,
    CerStatus.FROZEN,
]


class RunState(enum.Enum):
    """RunState enum (2.6_WORKFLOW_STATE_MACHINE.md)."""

    REQUESTED = "REQUESTED"
    SCHEDULED = "SCHEDULED"
    SUBMITTED = "SUBMITTED"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    RETRY_PENDING = "RETRY_PENDING"
    COLLECTING = "COLLECTING"
    COLLECTED = "COLLECTED"
    PARTIAL_SUCCESS = "PARTIAL_SUCCESS"
    STORED = "STORED"
    ANALYZED = "ANALYZED"
    ARCHIVED = "ARCHIVED"


class ArtifactSafetyState(enum.Enum):
    """Safety state for raw artifacts (3.2_CONTAINER_DESIGN.md §7-0, C-04).

    Distinct from RunState — an artifact can be QUARANTINED while
    RunState is FAILED.
    """

    NORMAL = "NORMAL"
    QUARANTINED = "QUARANTINED"
    PURGED = "PURGED"


# RuntimeFailure and BenchmarkFailure are string constants rather than an
# enum so that callers can write  ``failure_category="RuntimeFailure::Timeout"``
# matching the YAML schema exactly.

class _FailureCategory:
    """Namespace for failure category string constants (§7-0)."""

    # RuntimeFailure
    TRANSIENT = "RuntimeFailure::Transient"
    TERMINAL = "RuntimeFailure::Terminal"
    CORRUPTION = "RuntimeFailure::Corruption"
    TIMEOUT = "RuntimeFailure::Timeout"

    # BenchmarkFailure
    APPLICATION = "BenchmarkFailure::Application"
    RESOURCE = "BenchmarkFailure::Resource"
    PARTIAL = "BenchmarkFailure::Partial"

    _all = None  # populated below


_FailureCategory._all = {
    v
    for k, v in vars(_FailureCategory).items()
    if not k.startswith("_") and isinstance(v, str)
}

FailureCategory = _FailureCategory()


# ---------------------------------------------------------------------------
# Data records
# ---------------------------------------------------------------------------


@dataclass
class HostEnvironmentRecord:
    """Tier 1 mandatory host fields collected by Environment Collector (3.2.9).

    Tier 1 = required for L-RE2 replay equivalence (REPLAY-05 criterion).
    Additional Tier 2/3 fields may be stored in ``extra``.
    """

    # REPLAY-05 Tier 1 — required for L-RE2 equivalence judgment
    gpu_driver: Optional[str] = None
    cuda_runtime: Optional[str] = None
    gpu_model: Optional[str] = None

    # Host OS / kernel (explicit fields — provenance, NOT used in L-RE2)
    host_os: Optional[str] = None
    host_kernel: Optional[str] = None

    # Container OS (collected inside container from /etc/os-release)
    container_os: Optional[str] = None

    # Tier 2/3 — optional (cpu_model, memory_size, ib_model, etc.)
    extra: Dict[str, object] = field(default_factory=dict)

    TIER1_FIELDS = ("gpu_model", "gpu_driver", "cuda_runtime")
    """Fields required for REPLAY-05 acceptance criterion."""

    def tier1_complete(self) -> bool:
        """Return True if all Tier 1 fields defined in REPLAY-05 are present."""
        return all(getattr(self, f) is not None for f in self.TIER1_FIELDS)


@dataclass
class DatasetManifest:
    """Immutable dataset identity record (3.2.5_DATASET_IMMUTABILITY_POLICY.md).

    DS-01 criterion: manifest_hash must match the re-computed value.
    """

    schema_version: str = "1.0"
    name: str = ""
    version: str = ""
    manifest_hash: str = ""  # sha256:... of this manifest file
    root_hash: str = ""  # sha256 merkle tree of all files


@dataclass
class CerImageSection:
    uri: str = ""
    digest: str = ""
    source_type: Optional[str] = None  # "vendor" | "base" | "custom"
    dockerfile_hash: Optional[str] = None
    build_recipe_hash: Optional[str] = None
    sw_env_collection: Optional[str] = None  # "OK" | "FAILED" | None (not collected)


@dataclass
class CerScriptSection:
    git_remote: Optional[str] = None
    git_commit: Optional[str] = None
    mounted_path: str = "/bench"
    content_hash: Optional[str] = None


@dataclass
class CerDatasetSection:
    name: str = "none"
    version: Optional[str] = None
    mounted_path: Optional[str] = None
    manifest_hash: Optional[str] = None


@dataclass
class CerRuntimeSection:
    container_runtime: str = "docker"
    runtime_version: Optional[str] = None
    gpu_passthrough: bool = False
    mounts: List[Dict] = field(default_factory=list)
    env_vars_injected: List[str] = field(default_factory=list)
    resolved_command: Optional[str] = None
    scheduler_backend: Optional[str] = None  # "slurm" | "pjm" | "flux" (HPC replay)
    scheduler_job_id: Optional[str] = None   # scheduler job ID for provenance
    mpi_compatibility: Optional[Dict] = None


@dataclass
class CerResultSection:
    exit_code: Optional[int] = None
    failure_category: Optional[str] = None
    wall_time_sec: Optional[float] = None
    replay_equivalence_level: Optional[str] = None


@dataclass
class ContainerExecutionRecord:
    """Full provenance record for one container execution (§7-0-1, C-21).

    Lifecycle (CerStatus):
        Runner         → DRAFT         (image/script/dataset/runtime filled, host=None)
        Env Collector  → HOST_APPENDED (host section populated)
        Orchestrator   → FINALIZED     (run_state confirmed)
        Storage        → FROZEN        (immutable; no field modification allowed)
    """

    schema_version: str = "1.0"
    status: CerStatus = CerStatus.DRAFT
    run_state: Optional[RunState] = None
    artifact_safety_state: ArtifactSafetyState = ArtifactSafetyState.NORMAL
    created_at: Optional[str] = None  # ISO 8601 timestamp for temporal ordering
    finalized_at: Optional[str] = None  # ISO 8601 timestamp when FROZEN

    image: CerImageSection = field(default_factory=CerImageSection)
    script: CerScriptSection = field(default_factory=CerScriptSection)
    dataset: CerDatasetSection = field(default_factory=CerDatasetSection)
    runtime: CerRuntimeSection = field(default_factory=CerRuntimeSection)
    host: Optional[HostEnvironmentRecord] = None
    result: CerResultSection = field(default_factory=CerResultSection)

    def advance_status(self, new_status: CerStatus) -> None:
        """Advance lifecycle status forward only.

        Raises:
            ValueError  — if the transition is backward or if the record is
                          already FROZEN.
            PermissionError — attempted to mutate a FROZEN CER.
        """
        if self.status == CerStatus.FROZEN:
            raise PermissionError(
                "CER is FROZEN — no field modifications are allowed (3.2.1 §6 Stage 4)."
            )
        current_idx = _CER_STATUS_ORDER.index(self.status)
        new_idx = _CER_STATUS_ORDER.index(new_status)
        if new_idx != current_idx + 1:
            direction = "backward" if new_idx <= current_idx else "stage skip"
            raise ValueError(
                f"Invalid CER status transition ({direction}): "
                f"{self.status.value} → {new_status.value}. "
                "Each stage must advance exactly one step at a time "
                "(draft→host_appended→finalized→frozen)."
            )
        self.status = new_status
        if new_status == CerStatus.FROZEN and self.finalized_at is None:
            from datetime import datetime, timezone
            self.finalized_at = datetime.now(timezone.utc).isoformat()

    def is_frozen(self) -> bool:
        return self.status == CerStatus.FROZEN

    # ------------------------------------------------------------------
    # Serialization
    # ------------------------------------------------------------------

    def to_dict(self) -> dict:
        """Return JSON/YAML-compatible plain dict (enum values as strings).

        None-valued optional fields are omitted from the output.
        """
        import dataclasses as _dc

        def _convert(obj):
            if isinstance(obj, enum.Enum):
                return obj.value
            if _dc.is_dataclass(obj) and not isinstance(obj, type):
                return {
                    f.name: _convert(getattr(obj, f.name))
                    for f in _dc.fields(obj)
                    if getattr(obj, f.name) is not None
                }
            if isinstance(obj, list):
                return [_convert(x) for x in obj]
            if isinstance(obj, dict):
                return {k: _convert(v) for k, v in obj.items()}
            return obj

        return _convert(self)

    def to_yaml(self) -> str:
        """Serialize to YAML string (requires PyYAML)."""
        import yaml

        return yaml.dump(
            self.to_dict(),
            default_flow_style=False,
            allow_unicode=True,
            sort_keys=False,
        )

    @classmethod
    def from_dict(cls, data: dict) -> "ContainerExecutionRecord":
        """Reconstruct from a plain dict (inverse of to_dict())."""
        d = dict(data)

        # Enum fields
        if "status" in d:
            d["status"] = CerStatus(d["status"])
        if "run_state" in d and d["run_state"] is not None:
            d["run_state"] = RunState(d["run_state"])
        if "artifact_safety_state" in d:
            d["artifact_safety_state"] = ArtifactSafetyState(d["artifact_safety_state"])

        # Nested dataclass fields
        if "image" in d:
            d["image"] = CerImageSection(**d["image"])
        if "script" in d:
            d["script"] = CerScriptSection(**d["script"])
        if "dataset" in d:
            d["dataset"] = CerDatasetSection(**d["dataset"])
        if "runtime" in d:
            d["runtime"] = CerRuntimeSection(**d["runtime"])
        if "host" in d and d["host"] is not None:
            d["host"] = HostEnvironmentRecord(**d["host"])
        if "result" in d:
            d["result"] = CerResultSection(**d["result"])

        return cls(**d)

    @classmethod
    def from_yaml(cls, text: str) -> "ContainerExecutionRecord":
        """Reconstruct from YAML string (inverse of to_yaml())."""
        import yaml

        return cls.from_dict(yaml.safe_load(text))


@dataclass
class DerRankResult:
    rank: int = 0
    node: str = ""
    cer_ref: str = ""
    local_state: RunState = RunState.COMPLETED
    failure_category: Optional[str] = None
    exit_code: Optional[int] = None


@dataclass
class DistributedExecutionRecord:
    """Aggregate record for a distributed N-rank execution (3.2.6 §7, C-12).

    global_state determination rules (§7-3):
        All ranks COMPLETED          → COMPLETED
        Some FAILED, FoM retrievable → PARTIAL_SUCCESS
        Some FAILED, FoM not obtainable → FAILED
        All FAILED                   → FAILED
        Corruption detected          → FAILED + QUARANTINED
    """

    schema_version: str = "1.0"
    execution_id: str = ""
    scheduler_job_id: Optional[str] = None  # Slurm/PJM job ID for HPC provenance
    n_nodes: int = 1
    n_ranks: int = 1
    runtime_model: str = "scheduler_external"
    global_state: Optional[RunState] = None
    artifact_safety_state: ArtifactSafetyState = ArtifactSafetyState.NORMAL
    wall_time_sec: Optional[float] = None
    failure_reason: Optional[str] = None
    ranks: List[DerRankResult] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Utility functions
# ---------------------------------------------------------------------------

# SHA-256 digest pattern: at least 8 hex chars (full digest = 64; test fixtures may be shorter)
_DIGEST_RE = re.compile(r"@sha256:[0-9a-f]{8,}$")

# Local SIF file path — absolute path ending with .sif (F-CON-009)
_SIF_PATH_RE = re.compile(r"\.sif$", re.IGNORECASE)

# Mutable tag patterns (subset from existing container_design.py test)
_MUTABLE_TAG_RE = re.compile(
    r":latest\b|:main\b|:master\b|:dev\b|:nightly\b|:edge\b|:canary\b"
)


def validate_container_uri(uri: str) -> str:
    """Validate that *uri* uses an immutable reference (SEC-01, F-CON-009).

    Accepts two forms:

    1. **OCI registry URIs** (Docker, ORAS, Apptainer ``docker://`` / ``oras://``):
       must end with ``@sha256:<hex64>``.
       Examples::

           ghcr.io/org/bench@sha256:<hex64>
           docker://ghcr.io/org/bench@sha256:<hex64>
           oras://registry.example.com/bench@sha256:<hex64>

    2. **Local SIF file paths** (Apptainer/Singularity, F-CON-009):
       URI ending with ``.sif`` is accepted; the content hash is recorded
       separately via the ``sif_content_hash`` variant.
       Examples::

           /path/to/stream-benchmark.sif
           /data/images/hpl.sif

    Returns the URI unchanged if valid.

    Raises:
        ValueError  — with the text "digest required" if the URI matches
                      neither pattern, satisfying SEC-01:
                      ``error log に "digest required"``
    """
    # Local SIF files are valid — content hash stored in sif_content_hash variant
    if _SIF_PATH_RE.search(uri):
        return uri
    # OCI registry URIs (including docker:// and oras:// prefixed forms)
    if _DIGEST_RE.search(uri):
        return uri
    raise ValueError(
        f"digest required: container URI must end with @sha256:<hex64> "
        f"(or be a .sif file path for Apptainer/Singularity). "
        f"Got: {uri!r}"
    )


# F-CON-003/004/005 digest extraction pattern (reused by build_cer_draft_from_variants)
_DIGEST_EXTRACT_RE = re.compile(r"(sha256:[0-9a-f]{8,})$")


def build_cer_draft_from_variants(variants: Dict[str, List]) -> "ContainerExecutionRecord":
    """Build a DRAFT ContainerExecutionRecord from a spec variants dict.

    This is the Runner-side responsibility defined in §7-0-1:
      - image:   uri/digest from ``container_uri`` variant
      - script:  git_commit/remote from ``script_git_commit``/``script_git_remote``
      - dataset: name/version from ``dataset``/``dataset_version``
      - runtime: container_runtime and gpu_passthrough

    Called by Container.Helper.generate_cer_draft() and directly testable
    without requiring a ramble installation.

    Args:
        variants: dict mapping variant name → list-of-values (benchpark spec format)

    Returns:
        ContainerExecutionRecord in DRAFT status.

    Raises:
        RuntimeError — if package_manager != container.
    """
    if variants.get("package_manager", [""])[0] != "container":
        raise RuntimeError(
            "build_cer_draft_from_variants() may only be called when "
            "package_manager=container."
        )

    uri = variants.get("container_uri", [""])[0]
    if _SIF_PATH_RE.search(uri):
        # Local SIF file: digest is the sha256 of the file content (F-CON-009).
        # Recorded via the separate sif_content_hash variant rather than the URI.
        sif_hash = variants.get("sif_content_hash", ["none"])[0]
        digest = f"sha256:{sif_hash}" if sif_hash and sif_hash != "none" else ""
    else:
        _m = _DIGEST_EXTRACT_RE.search(uri)
        digest = _m.group(1) if _m else ""

    git_commit_raw = variants.get("script_git_commit", ["none"])[0]
    git_remote_raw = variants.get("script_git_remote", ["none"])[0]

    dataset_name = variants.get("dataset", ["none"])[0]
    dataset_version_raw = variants.get("dataset_version", ["none"])[0]

    runtime = variants.get("container_runtime", ["apptainer"])[0]
    gpu_passthrough_raw = variants.get("gpu_passthrough", ["True"])[0]
    if isinstance(gpu_passthrough_raw, str):
        gpu_passthrough = gpu_passthrough_raw.lower() not in ("false", "0", "no")
    else:
        gpu_passthrough = bool(gpu_passthrough_raw)

    from datetime import datetime, timezone
    now = datetime.now(timezone.utc).isoformat()

    return ContainerExecutionRecord(
        created_at=now,
        image=CerImageSection(uri=uri, digest=digest),
        script=CerScriptSection(
            git_commit=git_commit_raw if git_commit_raw != "none" else None,
            git_remote=git_remote_raw if git_remote_raw != "none" else None,
            mounted_path="/bench",
        ),
        dataset=CerDatasetSection(
            name=dataset_name,
            version=dataset_version_raw if dataset_version_raw != "none" else None,
        ),
        runtime=CerRuntimeSection(
            container_runtime=runtime,
            gpu_passthrough=gpu_passthrough,
        ),
    )


class HostEnvCollector:
    """Collect Tier 1 host environment fields (§3.2.9, F-CON-011).

    Uses nvidia-smi, nvcc, and /proc/version to populate
    HostEnvironmentRecord without requiring any external Python packages.

    Usage::

        her = HostEnvCollector.collect()
        cer.host = her
    """

    @staticmethod
    def collect() -> "HostEnvironmentRecord":
        """Return a HostEnvironmentRecord populated from the current host.

        Fields that cannot be determined are left as None.
        """
        import subprocess  # stdlib only — no optional deps needed

        def _run(*cmd) -> str:
            try:
                result = subprocess.run(
                    list(cmd),
                    capture_output=True,
                    text=True,
                    timeout=10,
                )
                return result.stdout.strip() if result.returncode == 0 else ""
            except Exception:
                return ""

        # GPU model and driver via nvidia-smi
        smi_out = _run(
            "nvidia-smi",
            "--query-gpu=name,driver_version",
            "--format=csv,noheader",
        )
        gpu_model: Optional[str] = None
        gpu_driver: Optional[str] = None
        if smi_out:
            first_line = smi_out.splitlines()[0]
            parts = [p.strip() for p in first_line.split(",", 1)]
            if len(parts) == 2:
                gpu_model, gpu_driver = parts[0] or None, parts[1] or None

        # CUDA runtime version via nvcc
        nvcc_out = _run("nvcc", "--version")
        cuda_runtime: Optional[str] = None
        if nvcc_out:
            import re as _re
            m = _re.search(r"release\s+([\d.]+)", nvcc_out)
            if m:
                cuda_runtime = m.group(1)

        # OS and kernel from /proc/version
        try:
            with open("/proc/version") as fh:
                proc_ver = fh.read().strip()
        except OSError:
            proc_ver = ""

        kernel: Optional[str] = None
        os_str: Optional[str] = None
        if proc_ver:
            import re as _re
            km = _re.search(r"version\s+(\S+)", proc_ver, _re.IGNORECASE)
            if km:
                kernel = km.group(1)
            om = _re.search(r"\(([^)]+@[^)]+)\)", proc_ver)
            if om:
                os_str = om.group(1)

        return HostEnvironmentRecord(
            gpu_model=gpu_model,
            gpu_driver=gpu_driver,
            cuda_runtime=cuda_runtime,
            host_os=os_str,
            host_kernel=kernel,
        )


def write_cer(cer: "ContainerExecutionRecord", path: "os.PathLike") -> None:
    """Write *cer* as YAML to *path* (§7-0-1 CER file format).

    Creates parent directories as needed.

    Args:
        cer:  ContainerExecutionRecord to serialize.
        path: Destination file path (e.g. ``experiment_dir/cer.yaml``).
    """
    import os

    path = str(path)
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    with open(path, "w") as fh:
        fh.write(cer.to_yaml())


def compute_rsd(values: List[float]) -> float:
    """Compute Relative Standard Deviation (RSD) for REPLAY-02.

    RSD = std_dev / mean  (returned as a fraction, not a percentage).
    Caller checks:  compute_rsd(fom_values) < 0.05  (i.e. < 5%)

    Raises:
        ValueError — if fewer than 2 values are supplied.
    """
    if len(values) < 2:
        raise ValueError("compute_rsd requires at least 2 values.")
    mean = sum(values) / len(values)
    if mean == 0:
        raise ValueError("compute_rsd: mean is zero, RSD is undefined.")
    variance = sum((v - mean) ** 2 for v in values) / len(values)
    std_dev = variance ** 0.5
    return std_dev / mean
