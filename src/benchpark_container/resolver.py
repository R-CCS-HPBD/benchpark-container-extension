# SPDX-License-Identifier: Apache-2.0
"""Resolve saved System declarations and benchmark requirements into plain data."""
from pathlib import Path
import re

from benchpark_integration.api import (ConfigurationContribution, ResourceSpec, SoftwareProvider, plain)
from benchpark_integration.source_repository import capture_for_experiment
from . import __version__
from .requirements import scan_requirements
from .image_selection import select_image, select_runtime
from .contracts import (base_tools, validate_targets, PREPARATION_POLICY,
                        preparation_needs, validate_environment)
from .artifacts import pin_external_artifacts
from .provenance import source_provenance
from .reproducibility import validate_inputs
from .util import ValidationError, check_no_secrets, inside, safe_name, sha256, strict




def resource(path, target):
    return ResourceSpec(target=target, source=str(path), sha256=sha256(path))



def requirements_resources(root, requested):
    """Snapshot exactly the input closure validated by the common reader."""
    scanned = scan_requirements(root, requested)
    return [ResourceSpec(target="inputs/" + rel, source=str(Path(root) / rel), sha256=h)
            for rel, h in scanned["files"].items()]


ACCELERATOR_BACKENDS = {"cuda": "nvidia", "rocm": "amd"}


def select_accelerator_backend(variants, explicit, runtime):
    """Resolve an explicitly selected Benchpark GPU model into a container accelerator.

    Generic CPU-only container experiments have neither variant and are left
    untouched.  GPU-capable experiments must explicitly select exactly one of
    +cuda/+rocm; a native default is never used as an implicit container backend.
    """
    explicit = plain(explicit)
    supported = [name for name in ACCELERATOR_BACKENDS if name in variants]
    requested = [name for name in ACCELERATOR_BACKENDS if name in explicit]
    if not supported and not requested:
        return None

    def flag(value):
        # Do not accept 1 as True or strings as bools at the native spec boundary.
        if not isinstance(value, (list, tuple)) or len(value) != 1 or type(value[0]) is not bool:
            raise ValidationError("Accelerator variant must contain one boolean")
        return value[0]

    concrete_enabled = [name for name in supported if flag(variants[name])]
    selected = [name for name in requested if flag(explicit[name])]
    if any(name not in supported for name in requested):
        raise ValidationError("Explicit accelerator backend is absent from native variants")
    if len(selected) != 1:
        raise ValidationError(
            "Container GPU experiment requires an explicit accelerator backend: select exactly one of +cuda or +rocm"
        )
    backend = selected[0]
    if concrete_enabled != [backend]:
        raise ValidationError(
            "Explicit accelerator backend was not preserved by Benchpark concretization: "
            "exactly one concrete backend must match +" + backend
        )
    accelerator = ACCELERATOR_BACKENDS[backend]
    if runtime.get("gpu", "none") != accelerator:
        raise ValidationError(
            "Experiment +%s requires System gpu=%s; initialized System declares gpu=%s"
            % (backend, accelerator, runtime.get("gpu", "none"))
        )
    return {"backend": backend, "accelerator": accelerator,
            "selection_origin": "experiment-explicit", "system_gpu": runtime["gpu"]}


def resolve(context):
    s, q, variants = plain(context.system), plain(context.requirements), plain(context.variants)
    check_no_secrets(s); check_no_secrets(q)
    if q.get("schema_version") != 2:
        raise ValidationError("Experiment schema_version=2 is required; migrate experiment.py and re-initialize")
    strict(q, ("schema_version", "default_image", "default_release", "requirements", "artifacts", "executables",
               "variables", "environment", "protected_packages", "smoke_imports", "timeout_seconds", "setup_scripts"),
           "Experiment", ("schema_version", "default_image", "default_release", "executables"))
    if q["schema_version"] != 2:
        raise ValidationError("Experiment schema_version=2 is required; migrate experiment.py and re-initialize")
    runtime, runtime_selection = select_runtime(s, variants)
    accelerator_selection = select_accelerator_backend(variants, context.explicit, runtime)
    requested_pm = plain(context.explicit).get("package_manager")
    if requested_pm and requested_pm != ["user-managed"]:
        raise ValidationError("Explicit package_manager conflicts with this user-managed application provider")
    for key in ("n_nodes", "n_ranks", "num_nodes"):
        if key in variants and variants[key] != ["1"]:
            raise ValidationError("Initial slice is one node/process: " + key)
    tune = variants.get("tune", ["none"])
    if tune != ["none"] and not plain(context.provenance).get("preparations", {}).get("tune"):
        raise ValidationError("Selected Tune was not prepared; declare extension_request_settings['tune']['allowed'] (v0.3), not legacy tuning_policy")
    root = Path(context.source_root).resolve()
    image, image_selection = select_image(s, q, variants, runtime)
    needs = preparation_needs(q)
    if needs["python"]:
        base_tools(image["tools"])
    if accelerator_selection is not None:
        selected_accelerator = image_selection.get("accelerator")
        if selected_accelerator != accelerator_selection["accelerator"]:
            raise ValidationError(
                "Selected image accelerator=%s does not match experiment backend +%s (%s)"
                % (selected_accelerator or "undeclared", accelerator_selection["backend"],
                   accelerator_selection["accelerator"])
            )
    req = q.get("requirements", [])
    if not isinstance(req, list) or not all(isinstance(x, str) for x in req):
        raise ValidationError("requirements must be a list of source-relative files")
    source_snapshot = capture_for_experiment(root, context.name)
    resources = list(source_snapshot.resources) if source_snapshot else []
    artifacts, targets, names = [], set(), set()
    roots = s.get("artifact_roots", {})
    for name, path in roots.items():
        safe_name(name, "artifact root")
        if not Path(path).is_absolute():
            raise ValidationError("System artifact root must be absolute")
    for a in q.get("artifacts", []):
        strict(a, ("name", "kind", "root", "path", "source", "target", "readonly", "sha256", "revision", "manifest", "manifest_sha256"),
               "artifact", ("name", "kind", "target"))
        name = safe_name(a["name"], "artifact name")
        target = a["target"]
        if (not isinstance(target, str) or not target.startswith("/") or ".." in Path(target).parts
                or any(c in target for c in ":,\n\r\x00") or target == "/"):
            raise ValidationError("Invalid mount target: " + repr(target))
        if target == "/bpce" or target.startswith("/bpce/") or name in names:
            raise ValidationError("Reserved/duplicate artifact: " + name)
        if any(target == t or target.startswith(t+"/") or t.startswith(target+"/") for t in targets):
            raise ValidationError("Overlapping mount targets")
        targets.add(target); names.add(name)
        b = dict(a)
        if a["kind"] in ("result", "log", "temporary", "cache"):
            if any(key in a for key in ("root", "path", "source")):
                raise ValidationError("Writable artifacts use private per-attempt directories")
            if a.get("readonly", False):
                raise ValidationError("Output cannot be read-only")
            b.update(readonly=False, location="attempt")
        elif "source" in a:
            if "root" in a or "path" in a or a.get("readonly", True) is not True:
                raise ValidationError("Fixed source artifact must be read-only")
            p = inside(root, a["source"], must_exist=True)
            if not p.is_file():
                raise ValidationError("Fixed source artifacts must be files; use a System root for datasets")
            res = resource(p, "inputs/" + str(p.relative_to(root)))
            if a.get("sha256") and a["sha256"] != res.sha256:
                raise ValidationError("Source artifact checksum mismatch")
            resources.append(res)
            b.update(readonly=True, location="resource", resource=res.target, sha256=res.sha256)
        else:
            if a.get("root") not in roots or "path" not in a or a.get("readonly", True) is not True:
                raise ValidationError("Input requires a known System root, relative path and read-only mode")
            if Path(a["path"]).is_absolute() or ".." in Path(a["path"]).parts:
                raise ValidationError("Input path escapes its System root")
            b.update(readonly=True, location="external", host_root=roots[a["root"]])
        artifacts.append(b)
    validate_targets(artifacts, image["tools"].values())
    artifacts = pin_external_artifacts(artifacts, variants)
    validation = validate_inputs(root, req, artifacts, variants)
    requirement_pins = validation["requirements"]["pins"]
    resources += [ResourceSpec(target="inputs/" + rel, source=str(root / rel), sha256=h)
                  for rel, h in validation["requirements"]["files"].items()]

    setup_scripts = q.get("setup_scripts", [])
    if not isinstance(setup_scripts, list) or not all(isinstance(x, str) for x in setup_scripts):
        raise ValidationError("setup_scripts must be a list of source-relative shell scripts")
    staged_setup_scripts = []
    for rel in setup_scripts:
        script = inside(root, rel, must_exist=True)
        if not script.is_file():
            raise ValidationError("setup script must be a regular file: " + rel)
        if script.suffix not in (".sh", ".bash"):
            raise ValidationError("setup script must be a .sh/.bash file: " + rel)
        check_no_secrets(script.read_text(encoding="utf-8"))
        target = "inputs/" + str(script.relative_to(root))
        resources.append(resource(script, target))
        staged_setup_scripts.append(target)

    executables = q["executables"]
    if not isinstance(executables, list) or len(executables) != 1:
        raise ValidationError("Initial adapter requires exactly one benchmark executable (which may contain multiple shell commands)")
    safe_name(executables[0], "executable")
    protected = q.get("protected_packages", {})
    if not isinstance(protected, dict):
        raise ValidationError("protected_packages maps distribution name to import module")
    for pkg, module in protected.items():
        safe_name(pkg)
        if not re.fullmatch(r"[A-Za-z_]\w*(?:\.[A-Za-z_]\w*)*", module):
            raise ValidationError("Invalid import module")
    package_resources = Path(__file__).parent / "resources"
    for p in sorted(package_resources.rglob("*")):
        if p.is_file() and "__pycache__" not in p.parts and not p.name.endswith(".pyc"):
            resources.append(resource(p, str(p.relative_to(package_resources))))
    # Resource collisions are permitted only for identical inputs, never last-wins.
    unique = {}
    for res in resources:
        if res.target in unique and res.sha256 != unique[res.target].sha256:
            raise ValidationError("Resource collision: " + res.target)
        unique[res.target] = res
    env = q.get("environment", {})
    validate_environment(env, needs["python"])
    # Report only checks that this declared preparation actually requests.
    validation["pending"] = ["mounted-artifact-verification"]
    if needs["pip"]:
        validation["pending"] += ["dependency-resolution-in-selected-base", "base-version-conflict-check"]
    if needs["python"]:
        validation["pending"].append("requested-python-validation")
    plan = {"schema_version": 1, "extension_version": __version__, "benchmark": context.name,
        "base": image, "runtime": runtime, "runtime_selection": runtime_selection,
        "accelerator_selection": accelerator_selection,
        "image_selection": image_selection, "dependency_policy": PREPARATION_POLICY, "requirements": ["inputs/" + r for r in req],
        "setup_scripts": staged_setup_scripts,
        "source_provenance": source_provenance(root, setup_scripts),
        "requirement_pins": requirement_pins, "validation": validation,
        "resources": {name: r.sha256 for name, r in unique.items()},
        "artifacts": artifacts, "protected_packages": protected,
        "smoke_imports": q.get("smoke_imports", []), "environment": env,
        "parameters": {k: v[0] if len(v) == 1 else v for k, v in variants.items()},
        "explicit": plain(context.explicit), "provenance": plain(context.provenance),
        "export_variables": q.get("variables", []), "executables": executables,
        "timeout_seconds": q.get("timeout_seconds", 3600)}
    if source_snapshot:
        plan["benchmark_source"] = source_snapshot.manifest
        plan["benchmark_content_sha256"] = source_snapshot.manifest["content_sha256"]
    check_no_secrets(plan)
    return ConfigurationContribution(owner="container", payload=plan,
        variables={"bpce_python": '"$BPCE_BASE_PYTHON"'} if needs["python"] else {},
        modifiers=({"name": "bpce-execution", "mode": "standard"},),
        software_provider=SoftwareProvider(section={"packages": {}, "environments": {}}),
        resources=tuple(unique.values()), modifier_repositories=("modifiers",),
        application_repositories=source_snapshot.application_repositories if source_snapshot else ())
