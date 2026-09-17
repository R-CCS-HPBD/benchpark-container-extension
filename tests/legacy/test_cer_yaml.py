# Copyright 2023 Lawrence Livermore National Security, LLC and other
# Benchpark Project Developers. See the top-level COPYRIGHT file for details.
#
# SPDX-License-Identifier: Apache-2.0

"""Unit tests for ContainerExecutionRecord YAML serialization (P1-1).

Traceability:
  - 3.2_CONTAINER_DESIGN.md §7-0-1 (CER schema)
  - 3.2.10_SCHEMA_EVOLUTION_POLICY.md §3 (schema_version)
  - 3.2.1_CONTAINER_PROVENANCE_POLICY.md §6 (lifecycle stages)

Test classes:
  TestCerToDict         — to_dict() plain dict serialization
  TestCerFromDict       — from_dict() reconstruction
  TestCerYamlRoundtrip  — to_yaml() / from_yaml() roundtrip
"""

import pytest

from benchpark_container.cer.legacy import (
    ArtifactSafetyState,
    CerDatasetSection,
    CerImageSection,
    CerResultSection,
    CerRuntimeSection,
    CerScriptSection,
    CerStatus,
    ContainerExecutionRecord,
    FailureCategory,
    HostEnvironmentRecord,
    RunState,
)

# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

_VALID_URI = "ghcr.io/org/bench@sha256:" + "a" * 64
_DIGEST = "sha256:" + "a" * 64


def _make_full_frozen_cer() -> ContainerExecutionRecord:
    """Return a fully-populated FROZEN CER for roundtrip tests."""
    cer = ContainerExecutionRecord(
        image=CerImageSection(uri=_VALID_URI, digest=_DIGEST),
        script=CerScriptSection(git_commit="abc0123", mounted_path="/bench"),
        dataset=CerDatasetSection(name="imagenet", version="2012"),
        runtime=CerRuntimeSection(container_runtime="apptainer", gpu_passthrough=True),
    )
    cer.host = HostEnvironmentRecord(
        gpu_model="A100-SXM4-80GB",
        gpu_driver="535.104.05",
        cuda_runtime="12.2",
    )
    cer.advance_status(CerStatus.HOST_APPENDED)
    cer.run_state = RunState.COMPLETED
    cer.result = CerResultSection(
        exit_code=0,
        wall_time_sec=42.5,
        replay_equivalence_level="L-RE2",
    )
    cer.advance_status(CerStatus.FINALIZED)
    cer.advance_status(CerStatus.FROZEN)
    return cer


# ===========================================================================
# TestCerToDict — to_dict()
# ===========================================================================


class TestCerToDict:
    """to_dict() must produce a JSON/YAML-compatible plain dict."""

    def test_schema_version_in_dict(self):
        d = ContainerExecutionRecord().to_dict()
        assert d["schema_version"] == "1.0"

    def test_status_is_string_not_enum(self):
        d = ContainerExecutionRecord().to_dict()
        assert isinstance(d["status"], str)
        assert d["status"] == "draft"

    def test_frozen_status_serialized_correctly(self):
        cer = _make_full_frozen_cer()
        d = cer.to_dict()
        assert d["status"] == "frozen"

    def test_none_host_is_omitted(self):
        """Optional None fields must be omitted from the output dict."""
        d = ContainerExecutionRecord().to_dict()
        assert "host" not in d

    def test_none_run_state_is_omitted(self):
        d = ContainerExecutionRecord().to_dict()
        assert "run_state" not in d

    def test_host_section_included_when_set(self):
        cer = ContainerExecutionRecord()
        cer.host = HostEnvironmentRecord(
            gpu_model="A100", gpu_driver="535", cuda_runtime="12.2"
        )
        d = cer.to_dict()
        assert "host" in d
        assert d["host"]["gpu_model"] == "A100"

    def test_run_state_serialized_as_uppercase_string(self):
        cer = ContainerExecutionRecord(run_state=RunState.COMPLETED)
        d = cer.to_dict()
        assert d["run_state"] == "COMPLETED"

    def test_artifact_safety_state_serialized(self):
        cer = ContainerExecutionRecord(
            artifact_safety_state=ArtifactSafetyState.QUARANTINED
        )
        d = cer.to_dict()
        assert d["artifact_safety_state"] == "QUARANTINED"

    def test_image_uri_preserved(self):
        cer = ContainerExecutionRecord(
            image=CerImageSection(uri=_VALID_URI, digest=_DIGEST)
        )
        d = cer.to_dict()
        assert d["image"]["uri"] == _VALID_URI
        assert d["image"]["digest"] == _DIGEST

    def test_nested_mounts_list_preserved(self):
        """CerRuntimeSection.mounts list serializes correctly."""
        mount = {"src": "/data", "dst": "/mnt/data", "read_only": True}
        cer = ContainerExecutionRecord(runtime=CerRuntimeSection(mounts=[mount]))
        d = cer.to_dict()
        assert d["runtime"]["mounts"] == [mount]

    def test_full_frozen_cer_contains_all_expected_keys(self):
        d = _make_full_frozen_cer().to_dict()
        assert "schema_version" in d
        assert "status" in d
        assert "image" in d
        assert "host" in d
        assert "run_state" in d
        assert "result" in d

    def test_result_exit_code_preserved(self):
        cer = ContainerExecutionRecord(result=CerResultSection(exit_code=42))
        d = cer.to_dict()
        assert d["result"]["exit_code"] == 42


# ===========================================================================
# TestCerFromDict — from_dict()
# ===========================================================================


class TestCerFromDict:
    """from_dict() must reconstruct a valid CER from a plain dict."""

    def test_status_string_to_enum(self):
        cer = ContainerExecutionRecord.from_dict({"status": "frozen"})
        assert cer.status == CerStatus.FROZEN

    def test_draft_status_reconstructed(self):
        cer = ContainerExecutionRecord.from_dict({"status": "draft"})
        assert cer.status == CerStatus.DRAFT

    def test_host_appended_status_reconstructed(self):
        cer = ContainerExecutionRecord.from_dict({"status": "host_appended"})
        assert cer.status == CerStatus.HOST_APPENDED

    def test_finalized_status_reconstructed(self):
        cer = ContainerExecutionRecord.from_dict({"status": "finalized"})
        assert cer.status == CerStatus.FINALIZED

    def test_run_state_string_to_enum(self):
        cer = ContainerExecutionRecord.from_dict({"run_state": "COMPLETED"})
        assert cer.run_state == RunState.COMPLETED

    def test_failed_run_state_reconstructed(self):
        cer = ContainerExecutionRecord.from_dict({"run_state": "FAILED"})
        assert cer.run_state == RunState.FAILED

    def test_artifact_safety_state_to_enum(self):
        cer = ContainerExecutionRecord.from_dict(
            {"artifact_safety_state": "QUARANTINED"}
        )
        assert cer.artifact_safety_state == ArtifactSafetyState.QUARANTINED

    def test_missing_optional_fields_use_defaults(self):
        """Missing keys in input dict must fall back to dataclass defaults."""
        cer = ContainerExecutionRecord.from_dict({"schema_version": "1.0"})
        assert cer.status == CerStatus.DRAFT
        assert cer.host is None
        assert cer.run_state is None

    def test_nested_image_section_reconstructed(self):
        cer = ContainerExecutionRecord.from_dict(
            {"image": {"uri": _VALID_URI, "digest": _DIGEST}}
        )
        assert cer.image.uri == _VALID_URI
        assert cer.image.digest == _DIGEST

    def test_nested_host_section_reconstructed(self):
        cer = ContainerExecutionRecord.from_dict(
            {
                "host": {
                    "gpu_model": "A100",
                    "gpu_driver": "535",
                    "cuda_runtime": "12.2",
                }
            }
        )
        assert cer.host is not None
        assert cer.host.gpu_model == "A100"
        assert cer.host.tier1_complete() is True

    def test_nested_result_section_reconstructed(self):
        cer = ContainerExecutionRecord.from_dict(
            {"result": {"exit_code": 0, "wall_time_sec": 99.9}}
        )
        assert cer.result.exit_code == 0
        assert cer.result.wall_time_sec == pytest.approx(99.9)

    def test_invalid_status_raises_value_error(self):
        """Unknown status string must raise ValueError."""
        with pytest.raises(ValueError):
            ContainerExecutionRecord.from_dict({"status": "bananas"})

    def test_invalid_run_state_raises_value_error(self):
        with pytest.raises(ValueError):
            ContainerExecutionRecord.from_dict({"run_state": "FLYING"})


# ===========================================================================
# TestCerYamlRoundtrip — to_yaml() + from_yaml()
# ===========================================================================


class TestCerYamlRoundtrip:
    """Full YAML serialization roundtrip tests."""

    def test_minimal_cer_roundtrip(self):
        """All-default CER survives to_yaml() / from_yaml()."""
        cer = ContainerExecutionRecord()
        restored = ContainerExecutionRecord.from_yaml(cer.to_yaml())
        assert restored.schema_version == "1.0"
        assert restored.status == CerStatus.DRAFT

    def test_draft_cer_with_image_roundtrip(self):
        cer = ContainerExecutionRecord(
            image=CerImageSection(uri=_VALID_URI, digest=_DIGEST)
        )
        restored = ContainerExecutionRecord.from_yaml(cer.to_yaml())
        assert restored.status == CerStatus.DRAFT
        assert restored.image.uri == _VALID_URI

    def test_frozen_cer_roundtrip_preserves_status(self):
        cer = _make_full_frozen_cer()
        restored = ContainerExecutionRecord.from_yaml(cer.to_yaml())
        assert restored.status == CerStatus.FROZEN
        assert restored.is_frozen() is True

    def test_frozen_cer_roundtrip_preserves_all_fields(self):
        cer = _make_full_frozen_cer()
        restored = ContainerExecutionRecord.from_yaml(cer.to_yaml())
        assert restored.image.uri == _VALID_URI
        assert restored.host.gpu_model == "A100-SXM4-80GB"
        assert restored.host.tier1_complete() is True
        assert restored.run_state == RunState.COMPLETED
        assert restored.result.exit_code == 0
        assert restored.result.wall_time_sec == pytest.approx(42.5)
        assert restored.result.replay_equivalence_level == "L-RE2"

    def test_frozen_cer_remains_frozen_after_restore(self):
        """Restored FROZEN CER must still reject advance_status."""
        restored = ContainerExecutionRecord.from_yaml(_make_full_frozen_cer().to_yaml())
        with pytest.raises(PermissionError, match="FROZEN"):
            restored.advance_status(CerStatus.FROZEN)

    def test_yaml_contains_schema_version(self):
        text = ContainerExecutionRecord().to_yaml()
        assert "schema_version" in text
        assert "1.0" in text

    def test_yaml_status_is_lowercase_string(self):
        """YAML output must use 'draft', not 'CerStatus.DRAFT' or 'DRAFT'."""
        text = ContainerExecutionRecord().to_yaml()
        assert "draft" in text
        assert "CerStatus" not in text

    def test_yaml_run_state_is_uppercase_string(self):
        """RunState enum values are uppercase in canonical form."""
        cer = ContainerExecutionRecord(run_state=RunState.COMPLETED)
        text = cer.to_yaml()
        assert "COMPLETED" in text
        assert "RunState" not in text

    def test_yaml_is_valid_parseable_yaml(self):
        """to_yaml() output must be syntactically valid YAML."""
        import yaml

        text = _make_full_frozen_cer().to_yaml()
        parsed = yaml.safe_load(text)
        assert isinstance(parsed, dict)
        assert parsed["schema_version"] == "1.0"

    def test_failed_cer_preserves_failure_category(self):
        cer = ContainerExecutionRecord(
            run_state=RunState.FAILED,
            result=CerResultSection(
                exit_code=1,
                failure_category=FailureCategory.TIMEOUT,
            ),
        )
        restored = ContainerExecutionRecord.from_yaml(cer.to_yaml())
        assert restored.result.failure_category == FailureCategory.TIMEOUT
        assert restored.result.exit_code == 1

    def test_quarantined_artifact_safety_state_roundtrip(self):
        cer = ContainerExecutionRecord(
            artifact_safety_state=ArtifactSafetyState.QUARANTINED
        )
        restored = ContainerExecutionRecord.from_yaml(cer.to_yaml())
        assert restored.artifact_safety_state == ArtifactSafetyState.QUARANTINED

    def test_dataset_section_roundtrip(self):
        cer = ContainerExecutionRecord(
            dataset=CerDatasetSection(
                name="imagenet",
                version="2012",
                manifest_hash="sha256:" + "d" * 64,
            )
        )
        restored = ContainerExecutionRecord.from_yaml(cer.to_yaml())
        assert restored.dataset.name == "imagenet"
        assert restored.dataset.version == "2012"
        assert restored.dataset.manifest_hash == "sha256:" + "d" * 64

    def test_partial_success_run_state_roundtrip(self):
        cer = ContainerExecutionRecord(run_state=RunState.PARTIAL_SUCCESS)
        restored = ContainerExecutionRecord.from_yaml(cer.to_yaml())
        assert restored.run_state == RunState.PARTIAL_SUCCESS

    def test_created_at_roundtrip(self):
        """created_at timestamp must survive YAML roundtrip."""
        cer = ContainerExecutionRecord(created_at="2026-05-28T10:00:00+00:00")
        restored = ContainerExecutionRecord.from_yaml(cer.to_yaml())
        assert restored.created_at == "2026-05-28T10:00:00+00:00"

    def test_finalized_at_roundtrip(self):
        """finalized_at timestamp must survive YAML roundtrip."""
        cer = ContainerExecutionRecord(finalized_at="2026-05-28T12:00:00+00:00")
        restored = ContainerExecutionRecord.from_yaml(cer.to_yaml())
        assert restored.finalized_at == "2026-05-28T12:00:00+00:00"

    def test_source_type_roundtrip(self):
        """image.source_type must survive YAML roundtrip."""
        cer = ContainerExecutionRecord(
            image=CerImageSection(uri=_VALID_URI, digest=_DIGEST, source_type="vendor")
        )
        restored = ContainerExecutionRecord.from_yaml(cer.to_yaml())
        assert restored.image.source_type == "vendor"

    def test_scheduler_fields_roundtrip(self):
        """runtime.scheduler_backend and scheduler_job_id must survive roundtrip."""
        cer = ContainerExecutionRecord(
            runtime=CerRuntimeSection(
                container_runtime="apptainer",
                scheduler_backend="slurm",
                scheduler_job_id="12345",
            )
        )
        restored = ContainerExecutionRecord.from_yaml(cer.to_yaml())
        assert restored.runtime.scheduler_backend == "slurm"
        assert restored.runtime.scheduler_job_id == "12345"
