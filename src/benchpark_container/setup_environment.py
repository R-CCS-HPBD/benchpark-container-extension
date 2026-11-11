# SPDX-License-Identifier: Apache-2.0
"""Ramble setup -> immutable prepared environment -> runtime handoff.

This is an orchestrator of existing backend/preparation contracts, not a new
package manager. All software construction still uses tools INSIDE the Base.
Prepared prefixes are ordinary immutable artifacts mounted read-only; they are
not filesystem overlays, and do not change the Catalog SquashFS contract.
"""
import contextlib
import copy
import json
import os
from pathlib import Path
import platform
import shutil
import signal
import threading
import tempfile
import time
import uuid

from .artifacts import materialize, tree_identity
from .backends import create_backend
from .backends.base import Mount
from .contracts import validate_preparation, validate_accelerator_contract, validate_environment
from .preparation import prepare_environment
from .resource_acquisition import prepare_artifacts, resource_lock
from .util import (ValidationError, atomic_json, check_no_secrets, identity,
                   inside, sha256, strict)

from .contracts import SETUP_LIFECYCLE as LIFECYCLE, validate_setup_contract


def uses_setup(spec):
    return spec.get("schema_version") == 3



def environment_root(spec, resources):
    return Path(resources).resolve().parent / "prepared" / identity(spec)


def _verify_resources(spec, resources):
    for relative, expected in spec["resources"].items():
        path = inside(resources, relative, must_exist=True)
        if not path.is_file() or sha256(path) != expected:
            raise ValidationError("Fixed resource has changed: " + relative)


def _verify_platform(spec):
    native = {"x86_64": "linux/amd64", "aarch64": "linux/arm64"}.get(platform.machine())
    if native != spec["base"]["platform"]:
        raise ValidationError("Run setup on a target-architecture build node; implicit cross-build is unsupported")


def _seal_tree(root):
    # chmod is accidental-write protection, not an authentication boundary.
    # Container-side read-only binds provide the execution write boundary.
    root = Path(root)
    for p in sorted(root.rglob("*"), reverse=True):
        if not p.is_symlink():
            p.chmod(p.stat().st_mode & ~0o222)
    root.chmod(root.stat().st_mode & ~0o222)


def _files(root, excluded=()):
    result = {}
    for p in sorted(Path(root).rglob("*")):
        rel = p.relative_to(root).as_posix()
        if any(rel == x or rel.startswith(x + "/") for x in excluded):
            continue
        if p.is_symlink():
            raise ValidationError("Symlink in setup evidence: " + rel)
        if p.is_file():
            result[rel] = {"sha256": sha256(p), "bytes": p.stat().st_size}
    return result


def _image_data(image):
    return {"kind": image.kind, "reference": image.reference,
            "identity": image.identity, "details": dict(image.details)}


def _recipe(spec, resources):
    names = set(spec.get("setup_scripts", []))
    # Includes constraints and nested requirement files captured by the resolver.
    if spec.get("requirements"):
        names.update(spec["requirements"])
        names.update("inputs/" + name for name in
                     spec.get("validation", {}).get("requirements", {}).get("files", {}))
    entries = []
    for name in sorted(names):
        path = inside(resources, name, must_exist=True)
        text = path.read_text(encoding="utf-8")
        check_no_secrets(text)
        entries.append({"path": name, "sha256": sha256(path), "text": text,
                        "executable": bool(path.stat().st_mode & 0o111)})
    return entries


def execution_base(spec, record):
    """Validate setup's image handoff; never acquire/rebuild at execution."""
    changed = "image_build" in spec or "image_publish" in spec
    base = record["lock"].get("execution_base")
    if not changed:
        if base is not None:
            raise ValidationError("Unexpected replacement execution image")
        return spec["base"]
    if not isinstance(base, dict):
        raise ValidationError("Prepared image handoff is missing")
    from .contracts import execution_tools
    if (base.get("kind") != "sif" or base.get("platform") != spec["base"]["platform"]
            or base.get("accelerator") != spec["runtime"]["gpu"]
            or base.get("tools") != execution_tools(spec)
            or base.get("source") != record["image"]["reference"]
            or base.get("sif_sha256") != record["image"]["details"].get("sif_sha256")
            or record["image"]["identity"] != "sha256:" + base["sif_sha256"]):
        raise ValidationError("Prepared image handoff does not match the execution contract")
    if "image_build" in spec:
        build = record["builder"].get("image_build")
        if (not isinstance(build, dict) or build.get("request", {}).get("build") != spec["image_build"]
                or build["request"].get("base_request") != spec["base"]
                or build.get("output_sha256") != base["sif_sha256"]):
            raise ValidationError("Prepared image is not bound to its fixed recipe")
        body = dict(build); expected = body.pop("record_sha256", None)
        if expected != identity(body):
            raise ValidationError("Prepared image build record mismatch")
    return base


def _validate_record(record, spec):
    strict(record, ("schema_version", "kind", "state", "plan_sha256", "plan", "image",
        "artifacts", "software_environment", "layers", "lock", "lock_sha256", "recipe",
        "files", "builder", "record_sha256"), "prepared record",
        ("schema_version", "kind", "state", "plan_sha256", "plan", "image", "artifacts",
         "software_environment", "layers", "lock", "lock_sha256", "recipe", "files", "builder", "record_sha256"))
    data = dict(record); digest = data.pop("record_sha256")
    if (record["schema_version"] != 1 or record["kind"] != "benchpark-prepared-environment"
            or record["state"] != "READY" or digest != identity(data)):
        raise ValidationError("Invalid/non-READY prepared environment record")
    if record["plan_sha256"] != identity(spec) or record["plan"] != spec:
        raise ValidationError("Prepared environment belongs to a different concrete plan")
    if record["lock_sha256"] != identity(record["lock"]):
        raise ValidationError("Prepared lock identity changed")
    if record["software_environment"].get("state") != "ready":
        raise ValidationError("Prepared activation is not ready")
    if record["software_environment"].get("construction") != spec["dependency_policy"]:
        raise ValidationError("Prepared construction policy mismatch")
    if not isinstance(record["layers"], list):
        raise ValidationError("Prepared layers must be a list")
    names = set()
    for layer in record["layers"]:
        strict(layer, ("name", "path", "target", "identity"), "prepared layer",
               ("name", "path", "target", "identity"))
        name = layer["name"]
        if (name not in ("python", "tools") or name in names
                or layer["path"] != "layers/" + name or layer["target"] != "/bpce/" + name):
            raise ValidationError("Invalid/duplicate prepared prefix")
        names.add(name)
    if "tools" not in names:
        raise ValidationError("Prepared tools prefix is missing")
    # Input/target identities cannot be changed by the setup result. Downloaded
    # inputs may gain only the resolved local location and content identity.
    declared = spec["artifacts"]
    if len(declared) != len(record["artifacts"]):
        raise ValidationError("Prepared artifact count mismatch")
    for before, after in zip(declared, record["artifacts"]):
        allowed = ({"location", "host_root", "path", "pinned_inputs"}
                   if before.get("location") == "download" else {"pinned_inputs"})
        if any(after.get(k) != v for k, v in before.items() if k not in allowed):
            raise ValidationError("Prepared artifact declaration mismatch")
        if set(after) - set(before) - allowed:
            raise ValidationError("Unknown prepared artifact field")
        if before.get("location") not in ("download", "external") and before != after:
            raise ValidationError("Setup modified an output/source artifact")
    execution_base(spec, record)
    check_no_secrets(record)
    return record


def load_prepared(spec, resources, *, verify_files=True):
    """No runtime initialization, download, package installation or repair."""
    validate_setup_contract(spec)
    root = environment_root(spec, resources)
    if root.is_symlink() or not root.is_dir():
        raise ValidationError("ENVIRONMENT_NOT_PREPARED: run ramble workspace setup")
    ready_path, record_path = root / "ready.json", root / "record.json"
    if ready_path.is_symlink() or record_path.is_symlink():
        raise ValidationError("Symlink in prepared environment references")
    try:
        ready = json.loads(ready_path.read_text())
        record = json.loads(record_path.read_text())
    except (OSError, ValueError) as exc:
        raise ValidationError("ENVIRONMENT_NOT_PREPARED: missing/invalid sealed record") from exc
    strict(ready, ("schema_version", "plan_sha256", "record_sha256", "record_file_sha256"),
           "ready reference", ("schema_version", "plan_sha256", "record_sha256", "record_file_sha256"))
    if (ready["schema_version"] != 1 or ready["plan_sha256"] != identity(spec)
            or ready["record_file_sha256"] != sha256(record_path)
            or ready["record_sha256"] != record.get("record_sha256")):
        raise ValidationError("Prepared environment reference/checksum mismatch")
    _validate_record(record, spec)
    if verify_files:
        _verify_resources(spec, resources)
        for rel, meta in record["files"].items():
            p = inside(root, rel, must_exist=True)
            if p.is_symlink() or not p.is_file() or sha256(p) != meta["sha256"] or p.stat().st_size != meta["bytes"]:
                raise ValidationError("Prepared build evidence changed: " + rel)
        for layer in record["layers"]:
            p = inside(root, layer["path"], must_exist=True)
            if p.is_symlink() or not p.is_dir() or tree_identity(p) != layer["identity"]:
                raise ValidationError("Prepared software prefix changed: " + layer["name"])
    return root, record


class SetupInterrupted(KeyboardInterrupt):
    """Raise into the current acquisition/preparation scope for child cleanup."""
    def __init__(self, signum):
        self.signum = signum
        super().__init__("Setup interrupted by signal " + str(signum))


@contextlib.contextmanager
def _setup_signals():
    old = {}
    if threading.current_thread() is threading.main_thread():
        def stop(signum, frame):
            raise SetupInterrupted(signum)
        for sig in (signal.SIGTERM, signal.SIGINT):
            old[sig] = signal.signal(sig, stop)
    try:
        yield
    finally:
        for sig, handler in old.items():
            signal.signal(sig, handler)


def prepare(spec, resources):
    with _setup_signals():
        return _prepare(spec, resources)


def _prepare(spec, resources):
    """Setup entrypoint: idempotent create-or-verify; never overwrite READY."""
    from .planning import validate_plan_version
    validate_plan_version(spec); validate_setup_contract(spec)
    validate_preparation(spec); validate_accelerator_contract(spec)
    resources = Path(resources).resolve()
    _verify_resources(spec, resources); _verify_platform(spec)
    root = environment_root(spec, resources)
    timeout = spec["setup"]["timeout_seconds"]
    root.parent.mkdir(parents=True, exist_ok=True)
    with resource_lock(root.parent / ".locks" / (root.name + ".lock"), timeout):
        if root.exists() or root.is_symlink():
            _, record = load_prepared(spec, resources)
            # READY bytes alone are insufficient: a shared Base/input may have
            # disappeared since the previous setup. Verify without reacquisition.
            local = copy.deepcopy(spec["runtime"])
            local["gpu"] = "none"
            local.setdefault("backend_options", {}).pop("gpu_devices", None)
            verifier = create_backend(local)
            try:
                verifier.load_prepared_image(execution_base(spec, record), record["image"], timeout)
                materialize([a for a in record["artifacts"] if a["location"] != "attempt"],
                            resources, root)
            finally:
                verifier.close()
            return record
        work = Path(tempfile.mkdtemp(prefix=".building-", dir=root.parent))
        scratch = work / "scratch"; scratch.mkdir(); (scratch / "work").mkdir()
        rt = None
        closed = False
        started = time.monotonic()
        try:
            # The build node must match CPU/OS; GPU use is validated at On, not
            # falsely certified by an import on a GPU-less setup node.
            settings = copy.deepcopy(spec["runtime"])
            settings["gpu"] = "none"
            settings.setdefault("backend_options", {}).pop("gpu_devices", None)
            rt = create_backend(settings); rt.attach_attempt(work)
            runtime_observed = rt.observe_runtime()
            if spec["setup"]["offline"]:
                image = rt.resolve_local_image(spec["base"], timeout)
            else:
                image = rt.resolve_image(spec["base"], timeout)
            image_base = spec["base"]
            image_build_record = None
            if "image_build" in spec:
                from .image_build import ensure_image
                image_base, image, image_build_record = ensure_image(
                    spec, resources, rt, image, work / "image-build", timeout)
            # Publication occurs only after build validation. Later preparation
            # failures do not turn an independently valid image into a READY run.
            if "image_publish" in spec:
                from .catalog_publication import publish
                image_base, publication = publish(spec, resources, image, work, timeout)
                image = rt.resolve_local_image(image_base, timeout)
            artifacts = prepare_artifacts(spec["artifacts"],
                Path(spec["runtime"]["image_cache"]) / "bpce-inputs-v1",
                offline=spec["setup"]["offline"], timeout=timeout)
            inputs = resources / "inputs"
            mounts = materialize([a for a in artifacts if a["location"] != "attempt"], resources, work)
            # No writable measurement outputs exist during software construction.
            build_spec = dict(spec, artifacts=artifacts)
            if spec["setup"]["offline"] and (spec["requirements"] or spec["setup_scripts"]):
                # Arbitrary scripts and Base pip may network. Do not claim
                # hermetic offline mode without a declared offline builder.
                raise ValidationError("OFFLINE_BUILD_UNSUPPORTED: use a prebuilt Base without network-capable preparation")
            environment = prepare_environment(rt, image, scratch, inputs, mounts, build_spec, work, timeout)
            # Re-verify inputs and image after arbitrary declared recipes. The
            # backend binds these read-only; this also detects external changes.
            _verify_resources(spec, resources)
            materialize([a for a in artifacts if a["location"] != "attempt"], resources, work)
            rt.load_prepared_image(image_base, _image_data(image), timeout)
            # Close transport resources before publishing success.
            rt.close(); closed = True
            if any(e.get("cleanup") == "failed" for e in rt.events):
                raise ValidationError("Setup runtime cleanup failed")
            layers = []
            (work / "layers").mkdir()
            for name in ("tools", "python"):
                src = scratch / name
                if src.is_symlink():
                    raise ValidationError("Prepared prefix root cannot be a symlink")
                if src.is_dir():
                    dest = work / "layers" / name
                    os.rename(src, dest)
                    layers.append({"name": name, "path": "layers/" + name,
                                   "target": "/bpce/" + name, "identity": tree_identity(dest)})
                    _seal_tree(dest)
            lock = {"schema_version": 1, "platform": spec["base"]["platform"],
                    "base_request": spec["base"], "image": _image_data(image),
                    "requirement_pins": spec.get("requirement_pins", {}),
                    "resource_hashes": spec["resources"],
                    "artifacts": artifacts,
                    "software_inventory": environment.get("constructed", {}).get("packages", {}),
                    "scope": "observed-environment; dependency-distribution hashes must be in Apps requirement/recipe inputs"}
            if "image_build" in spec or "image_publish" in spec:
                lock["execution_base"] = image_base
            atomic_json(work / "lock.json", lock)
            record = {"schema_version": 1, "kind": "benchpark-prepared-environment", "state": "READY",
                      "plan_sha256": identity(spec), "plan": spec, "image": _image_data(image),
                      "artifacts": artifacts, "software_environment": environment, "layers": layers,
                      "lock": lock, "lock_sha256": identity(lock), "recipe": _recipe(spec, resources),
                      "files": _files(work, ("layers", "scratch")),
                      "builder": {"runtime": runtime_observed, "runtime_settings": settings,
                                  "runtime_invocations": rt.events,
                                  "duration_seconds": time.monotonic() - started,
                                  "gpu_runtime_validation": "not-performed",
                                  "format": "readonly-prefixes-v1"}}
            if image_build_record is not None:
                record["builder"]["image_build"] = image_build_record
            record["record_sha256"] = identity(record)
            _validate_record(record, spec)
            atomic_json(work / "record.json", record)
            atomic_json(work / "ready.json", {"schema_version": 1, "plan_sha256": identity(spec),
                        "record_sha256": record["record_sha256"], "record_file_sha256": sha256(work / "record.json")})
            # Preserve scratch construction evidence, but it is never mounted at
            # execution. Only declared prefix artifacts are published for use.
            _seal_tree(work)
            os.rename(work, root)
            load_prepared(spec, resources)
            return record
        except BaseException as exc:
            if work.exists():
                # Leave incomplete attempts out of the ready namespace.
                work.chmod(0o700)
                failure = {"schema_version": 1, "state": "FAILED", "plan_sha256": identity(spec),
                           "error_type": type(exc).__name__, "error": str(exc)}
                try:
                    atomic_json(work / "failure.json", failure)
                except FileExistsError:
                    pass
                os.rename(work, root.parent / ("failed-" + root.name[:12] + "-" + uuid.uuid4().hex))
            raise
        finally:
            if rt is not None and not closed:
                rt.close()


def bind_prepared(rt, scratch, root, record):
    """Only internal, verified prefixes can occupy the reserved /bpce paths."""
    mounts = []
    for layer in record["layers"]:
        source = inside(root, layer["path"], must_exist=True)
        (Path(scratch) / layer["name"]).mkdir(exist_ok=True)
        mounts.append(Mount(str(source), layer["target"], True))
    rt.prepared_prefixes = tuple(mounts)


def retain_prepared(attempt, root, record):
    """Self-contained CER evidence; later readers need no Catalog/ready store."""
    for name in ("record.json", "ready.json", "lock.json"):
        target = Path(attempt) / ("prepared-" + name)
        if target.exists() or target.is_symlink():
            raise ValidationError("Prepared evidence filename collision")
        with target.open("xb") as out:
            out.write((Path(root) / name).read_bytes())
    # Retain raw build logs/inventories as existing CER source-evidence files.
    evidence_root = Path(attempt) / "source-evidence" / "prepared-environment"
    for rel in record["files"]:
        source = inside(root, rel, must_exist=True)
        dest = inside(evidence_root, rel)
        dest.parent.mkdir(parents=True, exist_ok=True)
        with dest.open("xb") as output:
            output.write(source.read_bytes())
    return {"schema_version": 1, "state": "READY", "plan_sha256": record["plan_sha256"],
            "evidence_root": "source-evidence/prepared-environment",
            "record": "prepared-record.json", "record_file_sha256": sha256(Path(attempt) / "prepared-record.json"),
            "record_sha256": record["record_sha256"], "lock_sha256": record["lock_sha256"],
            "verification_scope": "hash consistency; not signature/authenticity"}


def verify_cer_prepared(record):
    """Semantic binding in addition to CER files[] byte verification."""
    spec = record.get("resolved", {})
    ref = record.get("observed", {}).get("prepared_environment")
    if not uses_setup(spec):
        if ref is not None:
            raise ValidationError("Legacy CER cannot claim a setup environment")
        return
    if record.get("status") == "COMPLETED" and not ref:
        raise ValidationError("Completed schema-3 run has no prepared environment evidence")
    if ref:
        if ref.get("plan_sha256") != identity(spec):
            raise ValidationError("CER prepared environment plan mismatch")
        meta = record.get("files", {}).get(ref.get("record"))
        if not meta or meta.get("sha256") != ref.get("record_file_sha256"):
            raise ValidationError("CER build record is not bound to retained evidence")
