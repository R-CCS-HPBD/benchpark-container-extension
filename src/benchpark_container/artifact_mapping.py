# SPDX-License-Identifier: Apache-2.0

"""Resolve logical external artifact IDs independently of Benchpark System.

A System describes execution capability. It does not describe where a user's
models, datasets, or other external artifacts are physically stored.

Physical artifact locations are supplied separately through
BPCE_ARTIFACT_CONFIG.

Example:

schema_version: 1
artifacts:
  datasets:cifar10:
    path: /data/artifacts/cifar10
    kind: workload

  models:qwen3-0.6b:
    path: /shared/models/qwen3-0.6b
    kind: model
    revision: e6de...
"""

import os
from pathlib import Path
import re

import yaml

from .util import ValidationError, sha256


ENVIRONMENT_VARIABLE = "BPCE_ARTIFACT_CONFIG"

_ARTIFACT_ID = re.compile(
    r"^[A-Za-z0-9][A-Za-z0-9_.-]*"
    r"(?::[A-Za-z0-9][A-Za-z0-9_.-]*)+$"
)

_TEMPLATE_VARIABLE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")


def expand_variant_template(template, variants, field="value"):
    """Expand a scalar {variant} template to exactly one concrete value."""
    if not isinstance(template, str) or not template:
        raise ValidationError(
            "%s must be a non-empty string" % field
        )

    def replace(match):
        name = match.group(1)
        values = variants.get(name)

        if (
            not isinstance(values, (list, tuple))
            or len(values) != 1
        ):
            raise ValidationError(
                "%s template variable must resolve to exactly one "
                "Experiment variant: %s" % (field, name)
            )

        value = values[0]

        if not isinstance(value, (str, int)):
            raise ValidationError(
                "%s template variable must be scalar: %s"
                % (field, name)
            )

        return str(value)

    expanded = _TEMPLATE_VARIABLE.sub(replace, template)

    if "{" in expanded or "}" in expanded:
        raise ValidationError(
            "Unresolved %s template: %s" % (field, expanded)
        )

    return expanded

_ENTRY_KEYS = {
    "path",
    "kind",
    "revision",
}


def _expand_artifact_id(template, variants):
    if not isinstance(template, str) or not template:
        raise ValidationError("artifact must be a non-empty logical artifact ID")

    def replace(match):
        name = match.group(1)
        values = variants.get(name)

        if (
            not isinstance(values, (list, tuple))
            or len(values) != 1
        ):
            raise ValidationError(
                "Artifact ID template variable must resolve to exactly one "
                "Experiment variant: " + name
            )

        value = values[0]

        if not isinstance(value, (str, int)):
            raise ValidationError(
                "Artifact ID template variable must be scalar: " + name
            )

        return str(value)

    logical_id = _TEMPLATE_VARIABLE.sub(replace, template)

    if "{" in logical_id or "}" in logical_id:
        raise ValidationError(
            "Unresolved artifact ID template: " + logical_id
        )

    if not _ARTIFACT_ID.fullmatch(logical_id):
        raise ValidationError(
            "Invalid logical artifact ID: " + logical_id
        )

    return logical_id


def load_artifact_mapping():
    """Load the user/site artifact mapping.

    No configuration is required for experiments that do not request logical
    external artifacts.
    """

    configured = os.environ.get(ENVIRONMENT_VARIABLE)

    if not configured:
        return {}, None

    config_path = Path(configured).expanduser()

    if not config_path.is_absolute():
        raise ValidationError(
            ENVIRONMENT_VARIABLE + " must name an absolute path"
        )

    config_path = config_path.resolve()

    if not config_path.is_file():
        raise ValidationError(
            "Artifact mapping file does not exist: " + str(config_path)
        )

    try:
        data = yaml.safe_load(
            config_path.read_text(encoding="utf-8")
        )
    except Exception as exc:
        raise ValidationError(
            "Unable to read artifact mapping: " + str(exc)
        ) from exc

    if not isinstance(data, dict):
        raise ValidationError("Artifact mapping must be a mapping")

    unknown = set(data) - {"schema_version", "artifacts"}

    if unknown:
        raise ValidationError(
            "Unknown artifact mapping fields: "
            + ", ".join(sorted(unknown))
        )

    if data.get("schema_version") != 1:
        raise ValidationError(
            "Artifact mapping schema_version=1 is required"
        )

    entries = data.get("artifacts")

    if not isinstance(entries, dict):
        raise ValidationError(
            "Artifact mapping requires an artifacts mapping"
        )

    result = {}

    for logical_id, declaration in entries.items():
        if (
            not isinstance(logical_id, str)
            or not _ARTIFACT_ID.fullmatch(logical_id)
        ):
            raise ValidationError(
                "Invalid logical artifact ID: " + repr(logical_id)
            )

        if not isinstance(declaration, dict):
            raise ValidationError(
                "Artifact declaration must be a mapping: " + logical_id
            )

        unknown_entry = set(declaration) - _ENTRY_KEYS

        if unknown_entry:
            raise ValidationError(
                "Unknown fields for artifact %s: %s"
                % (
                    logical_id,
                    ", ".join(sorted(unknown_entry)),
                )
            )

        raw_path = declaration.get("path")

        if not isinstance(raw_path, str) or not raw_path:
            raise ValidationError(
                "Artifact requires an absolute path: " + logical_id
            )

        path = Path(raw_path)

        if not path.is_absolute():
            raise ValidationError(
                "Artifact path must be absolute: " + logical_id
            )

        item = dict(declaration)
        item["path"] = str(path)

        kind = item.get("kind")
        if kind is not None and (
            not isinstance(kind, str) or not kind
        ):
            raise ValidationError(
                "Artifact kind must be a non-empty string: " + logical_id
            )

        revision = item.get("revision")
        if revision is not None and (
            not isinstance(revision, str) or not revision
        ):
            raise ValidationError(
                "Artifact revision must be a non-empty string: "
                + logical_id
            )

        result[logical_id] = item

    provenance = {
        "schema_version": 1,
        "source": str(config_path),
        "sha256": sha256(config_path),
    }

    return result, provenance


def resolve_artifact_reference(
    template,
    variants,
    mapping,
    expected_kind,
):
    """Resolve one logical artifact requirement to a physical host path."""

    logical_id = _expand_artifact_id(template, variants)

    if logical_id not in mapping:
        raise ValidationError(
            "Logical artifact is not mapped: " + logical_id
        )

    declaration = dict(mapping[logical_id])

    declared_kind = declaration.get("kind")

    if (
        declared_kind is not None
        and declared_kind != expected_kind
    ):
        raise ValidationError(
            "Artifact kind mismatch for %s: expected %s, mapping declares %s"
            % (
                logical_id,
                expected_kind,
                declared_kind,
            )
        )

    path = Path(declaration["path"]).resolve()

    if not path.exists():
        raise ValidationError(
            "Mapped artifact does not exist: "
            + logical_id
            + " -> "
            + str(path)
        )

    if not path.name:
        raise ValidationError(
            "Artifact may not map to filesystem root: " + logical_id
        )

    declaration["path"] = str(path)

    return logical_id, declaration
