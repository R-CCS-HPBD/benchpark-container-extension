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
from .artifact_mapping import expand_variant_template, load_artifact_mapping, resolve_artifact_reference
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
    if type(q.get("schema_version")) is not int or q["schema_version"] not in (2, 3):
        raise ValidationError("Experiment schema_version=2 or 3 is required; migrate experiment.py and re-initialize")
    setup_lifecycle = q["schema_version"] == 3
    strict(q, ("schema_version", "default_image", "default_release", "requirements", "artifacts", "executables",
               "variables", "environment", "protected_packages", "smoke_imports", "timeout_seconds", "setup_scripts", "image_tools", "image_accelerator", "parallel", "profiler",
               "setup", "base_candidates", "image_build", "image_publish"),
           "Experiment", ("schema_version", "default_image", "default_release", "executables"))
    if not setup_lifecycle and any(k in q for k in ("setup", "base_candidates", "image_build", "image_publish")):
        raise ValidationError("Setup declarations require Experiment schema_version=3")
    if setup_lifecycle:
        from .contracts import SETUP_LIFECYCLE as LIFECYCLE
        setup_options = dict(offline=False, timeout_seconds=3600)
        if "setup" in q:
            strict(q["setup"], ("offline", "timeout_seconds"), "setup")
            setup_options.update(q["setup"])
        if "base_candidates" in q:
            from .image_selection import select_base_candidate
            q = select_base_candidate(s, q, variants)
    from .image_selection import choice
    if choice(variants, "container_image") != "default":
        # An explicit image override means an execution image, never silently
        # rebuild it using the profile's recipe or publish under its identity.
        q.pop("image_build", None)
        q.pop("image_publish", None)
    runtime, runtime_selection = select_runtime(s, variants)
    accelerator_selection = select_accelerator_backend(variants, context.explicit, runtime)
    requested_pm = plain(context.explicit).get("package_manager")
    if requested_pm and requested_pm != ["user-managed"]:
        raise ValidationError("Explicit package_manager conflicts with this user-managed application provider")
    parallel = None
    if "parallel" in q:
        from .parallel.contract import declaration
        parallel = declaration(q['parallel'], s.get('parallel', {}))
        from .parallel.session import session_provider
        session_provider(parallel, runtime.get('providers', []))
    else:
        for key in ("n_nodes", "n_ranks", "num_nodes"):
            if key in variants and variants[key] != ["1"]:
                raise ValidationError("Initial slice is one node/process: " + key)
    tune = variants.get("tune", ["none"])
    if tune != ["none"] and not plain(context.provenance).get("preparations", {}).get("tune"):
        raise ValidationError("Selected Tune was not prepared; declare extension_request_settings['tune']['allowed'] (v0.3), not legacy tuning_policy")
    root = Path(context.source_root).resolve()
    image, image_selection = select_image(s, q, variants, runtime)
    image_build = None
    build_resources = []
    if "image_build" in q:
        from .image_build import freeze_build
        image_build, build_files = freeze_build(q["image_build"], root, runtime,
                                               s.get("build_environment", {}))
        build_resources = [resource(path, target) for target, path in build_files.items()]
    image_publish = None
    if "image_publish" in q:
        from .catalog_publication import freeze_publication
        image_publish = freeze_publication(q["image_publish"])
    if image_build is not None and image.get("environment_snapshot") is not None:
        raise ValidationError("Image construction from composite Base+Overlay is not supported; select a plain Base")
    tools = image_build["tools"] if image_build else image["tools"]
    needs = preparation_needs(q)
    if needs["python"]:
        base_tools(tools)
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
    resources = (list(source_snapshot.resources) if source_snapshot else []) + build_resources
    artifacts, targets, names = [], set(), set()

    # New primary path: logical artifact IDs are resolved independently of
    # Benchpark System through BPCE_ARTIFACT_CONFIG.
    artifact_mapping, artifact_mapping_provenance = load_artifact_mapping()

    # Legacy compatibility only. Existing Systems that still provide
    # artifact_roots continue to work, but new Systems do not place user/site
    # model or dataset locations in System.
    roots = s.get("artifact_roots", {})
    for name, path in roots.items():
        safe_name(name, "artifact root")
        if not Path(path).is_absolute():
            raise ValidationError("System artifact root must be absolute")
    for a in q.get("artifacts", []):
        strict(a, ("name", "kind", "artifact", "root", "path", "source", "target", "readonly", "sha256", "revision", "manifest", "manifest_sha256", "acquire"),
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
            if any(key in a for key in ("artifact", "root", "path", "source", "acquire")):
                raise ValidationError("Writable artifacts use private per-attempt directories")
            if a.get("readonly", False):
                raise ValidationError("Output cannot be read-only")
            b.update(readonly=False, location="attempt")
        elif "acquire" in a:
            if not setup_lifecycle:
                raise ValidationError("Public input acquisition requires Experiment schema_version=3")
            if any(k in a for k in ("source", "root", "path", "artifact")) or a.get("readonly", True) is not True:
                raise ValidationError("Acquired inputs are read-only and cannot also use local mappings")
            from .resource_acquisition import validate_source
            source = validate_source(a["acquire"])
            if source["type"] == "https":
                if a.get("sha256", source["sha256"]) != source["sha256"]:
                    raise ValidationError("Acquired input hash differs from source declaration")
                b["sha256"] = source["sha256"]
            else:
                declared_revision = expand_variant_template(a.get("revision", source["revision"]), variants,
                                                            field="Artifact revision")
                if declared_revision != source["revision"]:
                    raise ValidationError("Acquired input revision differs from source declaration")
                b["revision"] = source["revision"]
            b.update(readonly=True, location="download")
        elif "source" in a:
            if (
                "artifact" in a
                or "root" in a
                or "path" in a
                or a.get("readonly", True) is not True
            ):
                raise ValidationError("Fixed source artifact must be read-only")
            p = inside(root, a["source"], must_exist=True)
            if not p.is_file():
                raise ValidationError("Fixed source artifacts must be files; use a System root for datasets")
            res = resource(p, "inputs/" + str(p.relative_to(root)))
            if a.get("sha256") and a["sha256"] != res.sha256:
                raise ValidationError("Source artifact checksum mismatch")
            resources.append(res)
            b.update(readonly=True, location="resource", resource=res.target, sha256=res.sha256)
        elif "artifact" in a:
            if (
                "root" in a
                or "path" in a
                or "source" in a
                or a.get("readonly", True) is not True
            ):
                raise ValidationError(
                    "Logical artifact input cannot also declare root/path/source "
                    "and must be read-only"
                )

            logical_id, mapped = resolve_artifact_reference(
                a["artifact"],
                variants,
                artifact_mapping,
                a["kind"],
                allow_missing=setup_lifecycle and bool(a.get("sha256")),
            )

            mapped_revision = mapped.get("revision")
            requested_revision = a.get("revision")

            if requested_revision is not None:
                requested_revision = expand_variant_template(
                    requested_revision,
                    variants,
                    field="Artifact revision",
                )
                b["revision"] = requested_revision

            if (
                mapped_revision is not None
                and requested_revision is not None
                and mapped_revision != requested_revision
            ):
                raise ValidationError(
                    "Artifact revision mismatch for %s" % logical_id
                )

            mapped_path = Path(mapped["path"])

            b["artifact"] = logical_id

            if requested_revision is None and mapped_revision is not None:
                b["revision"] = mapped_revision

            b.update(
                readonly=True,
                location="external",
                host_root=str(mapped_path.parent),
                path=mapped_path.name,
            )

            if artifact_mapping_provenance is not None:
                b["artifact_mapping_source"] = (
                    artifact_mapping_provenance["source"]
                )
                b["artifact_mapping_sha256"] = (
                    artifact_mapping_provenance["sha256"]
                )

        else:
            # Legacy compatibility for existing root/path declarations.
            if (
                a.get("root") not in roots
                or "path" not in a
                or a.get("readonly", True) is not True
            ):
                raise ValidationError(
                    "Input requires either a mapped logical artifact or a "
                    "legacy known System root, relative path and read-only mode"
                )

            if (
                Path(a["path"]).is_absolute()
                or ".." in Path(a["path"]).parts
            ):
                raise ValidationError("Input path escapes its System root")

            b.update(
                readonly=True,
                location="external",
                host_root=roots[a["root"]],
            )

        artifacts.append(b)
    validate_targets(artifacts, tools.values())
    if setup_lifecycle:
        # A private path can be absent at init only with an independently pinned
        # content identity. A revision alone does not identify local bytes.
        from .resource_acquisition import valid_hash
        for artifact in artifacts:
            if artifact.get("location") == "external" and artifact.get("sha256"):
                valid_hash(artifact["sha256"])
                artifact["verify_at_setup"] = True
        immediate = [a for a in artifacts if not a.get("verify_at_setup")]
        pinned = {a["name"]: a for a in pin_external_artifacts(immediate, variants)}
        artifacts = [a if a.get("verify_at_setup") else pinned[a["name"]] for a in artifacts]
    else:
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
    profiler_binding = None
    if "profiler" in q:
        from .profiler_frontend import stage_frozen_bundle
        profiler_binding, profiler_resources = stage_frozen_bundle(
            root, q["profiler"], executables[0], tools, runtime)
        if profiler_binding is not None and parallel is not None:
            raise ValidationError("Profiler P0 does not support parallel execution")
        resources.extend(profiler_resources)
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
    plan = {"schema_version": 2 if "environment_snapshot" in image else 1, "extension_version": __version__, "benchmark": context.name,
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
    if image_build is not None:
        plan["image_build"] = image_build
    if image_publish is not None:
        plan["image_publish"] = image_publish
    if setup_lifecycle:
        plan.update(schema_version=3, preparation_lifecycle=LIFECYCLE, setup=setup_options)
        from .setup_environment import validate_setup_contract
        validate_setup_contract(plan)
        plan["validation"]["pending"].append("ramble-setup-ready-environment")
        plan["validation"]["requirements"]["transitive_resolution"] = "common-base-pip-at-setup"
    if profiler_binding is not None:
        plan["profiler"] = profiler_binding
    if parallel is not None:
        writable = {a['name'] for a in artifacts if a.get('location') == 'attempt'}
        if not set(parallel['shared_outputs']) <= writable:
            raise ValidationError('Shared outputs must name declared writable artifacts')
        plan['parallel'] = parallel
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
