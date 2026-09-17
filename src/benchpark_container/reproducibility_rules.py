# SPDX-License-Identifier: Apache-2.0
"""Pure validation rules shared by init/CLI and the shipped execution helper.

No Benchpark, pip, registry, or filesystem mutation. A declared revision is not
proof of the mounted bytes; callers separately perform content verification.
"""
import re

RULESET_VERSION = 1
SHA256 = re.compile(r"[0-9a-f]{64}\Z")
COMMIT = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
VARIABLE = re.compile(r"\{([A-Za-z_][A-Za-z0-9_]*)\}")


class ReproducibilityError(ValueError):
    def __init__(self, code, message, **details):
        self.code, self.details = code, details
        super().__init__(message)


def canonical(name):
    return re.sub(r"[-_.]+", "-", name).lower()


def fixed_hash(value, label, commit=False):
    pattern = COMMIT if commit else SHA256
    if (not isinstance(value, str) or not pattern.fullmatch(value)
            or len(set(value)) == 1):
        raise ReproducibilityError(
            "MODEL_REVISION_NOT_FIXED" if commit else "INVALID_CONTENT_HASH",
            label + " must be a full immutable " + ("commit (40/64 hex)" if commit else "SHA-256 (64 hex)"),
            field=label, value=value)
    return value


def validate_model_artifacts(artifacts, variants=None):
    """Check model/tokenizer identities; synthetic models have no model artifact.

    A revision template is allowed only when EVERY candidate is a fixed commit.
    Runtime callers pass concrete artifacts and no variants, so unresolved
    placeholders are never accepted in a concrete plan. Local files may use a
    snapshot SHA-256, and directories may use a pinned manifest instead of Git.
    """
    variants = variants or {}
    results = []
    for artifact in artifacts:
        if artifact.get("kind") not in ("model", "tokenizer"):
            continue
        name = artifact.get("name", "unnamed")
        evidence = []
        revision = artifact.get("revision")
        if revision is not None:
            match = VARIABLE.fullmatch(str(revision))
            if match:
                candidates = variants.get(match.group(1))
                if not isinstance(candidates, (list, tuple)) or not candidates:
                    raise ReproducibilityError("MODEL_REVISION_UNRESOLVED",
                        "No concrete revision candidates for " + name, artifact=name)
            else:
                candidates = [revision]
            for candidate in candidates:
                fixed_hash(candidate, name + ".revision", commit=True)
            evidence.append("commit-declared")
        if artifact.get("sha256") is not None:
            fixed_hash(artifact["sha256"], name + ".sha256")
            evidence.append("content-sha256-declared")
        if artifact.get("manifest_sha256") is not None:
            if not artifact.get("manifest"):
                raise ReproducibilityError("MODEL_MANIFEST_MISSING",
                    "manifest_sha256 requires a manifest path: " + name, artifact=name)
            fixed_hash(artifact["manifest_sha256"], name + ".manifest_sha256")
            evidence.append("manifest-sha256-declared")
        if not evidence:
            raise ReproducibilityError("MODEL_IDENTITY_MISSING",
                "Model/tokenizer requires an immutable revision or fixed content/manifest hash: " + name,
                artifact=name)
        results.append({"artifact": name, "identity_evidence": evidence,
                        "mounted_content_verified": False})
    return results


def validate_additions(report, pins, base_packages=None, protected=(), version_equal=None):
    """Inspect pip's actual target-environment solve BEFORE installation.

    All new distributions must be pinned. A same-name, different-version Base
    replacement is rejected without a hard-coded torch/CUDA package policy.
    The caller can supply the packaging.version equality function from its
    fixed validation tool. No dependency solver is reimplemented here.
    """
    if report.get("version") != "1" or not isinstance(report.get("install"), list):
        raise ReproducibilityError("PIP_REPORT_SCHEMA", "Expected pip installation report version 1")
    if not isinstance(pins, dict) or any(
            not isinstance(k, str) or not isinstance(v, (list, tuple))
            or not v or any(not isinstance(item, str) for item in v)
            for k, v in pins.items()):
        raise ReproducibilityError("PIN_MAP_SCHEMA", "Pins must map names to nonempty arrays of exact version strings")
    if base_packages is not None and (not isinstance(base_packages, dict)
            or any(not isinstance(k, str) or not isinstance(v, str) for k, v in base_packages.items())):
        raise ReproducibilityError("BASE_INVENTORY_SCHEMA", "Base packages must map names to version strings")
    equal = version_equal or (lambda a, b: a == b)
    base = {canonical(k): str(v) for k, v in (base_packages or {}).items()}
    pins = {canonical(k): [str(x) for x in v] for k, v in pins.items()}
    protected = {canonical(n) for n in protected}
    changes = []
    for item in report["install"]:
        metadata = item.get("metadata", {})
        if not isinstance(metadata.get("name"), str) or not isinstance(metadata.get("version"), str):
            raise ReproducibilityError("PIP_REPORT_METADATA", "pip report item has no package name/version")
        name, version = canonical(metadata["name"]), metadata["version"]
        old = base.get(name)
        if old is not None and not equal(old, version):
            changes.append({"name": name, "base_version": old, "requested_version": version})
    if changes:
        description = "; ".join("{name}: Base={base_version}, requested={requested_version}".format(**c) for c in changes)
        raise ReproducibilityError("BASE_VERSION_CONFLICT",
            "Additional requirements would replace/shadow Base packages: " + description,
            conflicts=changes)
    for item in report["install"]:
        metadata = item["metadata"]
        name, version = canonical(metadata["name"]), metadata["version"]
        if name in protected:
            raise ReproducibilityError("EXPLICIT_PROTECTED_PACKAGE",
                "Installation would replace/shadow protected Base package: " + name, package=name)
        if not any(equal(version, pinned) for pinned in pins.get(name, [])):
            raise ReproducibilityError("UNPINNED_TRANSITIVE_DEPENDENCY",
                "Unpinned additional/transitive dependency %s==%s; add it to the fixed requirements/lock" % (name, version),
                package=name, version=version)
    return {"ruleset_version": RULESET_VERSION, "stage": "resolved-additions",
            "status": "passed", "base_version_conflicts": [],
            "validated_additions": len(report["install"])}
