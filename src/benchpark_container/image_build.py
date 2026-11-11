# SPDX-License-Identifier: Apache-2.0
"""Optional, setup-only construction of a derived execution image.

Recipes and package choices belong to Apps. This module never installs host
packages, selects GPU products, or adds a second execution/READY protocol.
It stages hash-pinned inputs, calls the selected backend's build port, and
hands the resulting image to the existing setup lifecycle.
"""
import copy
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import signal
import subprocess
import tempfile
import time
import uuid

from .contracts import container_tools
from .resource_acquisition import validate_source, acquire, resource_lock, valid_hash, _relative
from .util import ValidationError, atomic_json, check_no_secrets, identity, inside, sha256, strict

FORMAT = "sif-definition-v1"


def validate_build_environment(value):
    if not isinstance(value, dict):
        raise ValidationError("Build environment must be a mapping")
    check_no_secrets(value)
    for key, item in value.items():
        if (not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key)
                or key.startswith(("BPCE_", "APPTAINER", "SINGULARITY"))
                or not isinstance(item, str) or any(c in item for c in "\x00\r\n")):
            raise ValidationError("Invalid/reserved build environment: " + str(key))
    return dict(value)


def _check_commands(commands):
    if not isinstance(commands, list) or not commands:
        raise ValidationError("image_build.checks must contain at least one container argv")
    for argv in commands:
        if (not isinstance(argv, list) or not argv
                or any(not isinstance(s, str) or any(c in s for c in "\x00\r\n") for s in argv)
                or not argv[0].startswith("/")):
            raise ValidationError("Build checks require absolute container executable argv")
    return commands


def freeze_build(declaration, source_root, runtime, system_environment=None):
    """Pure init operation: freeze Apps files; never invoke a builder/network."""
    strict(declaration, ("schema_version", "format", "scripts", "files", "inputs", "tools",
                        "environment", "checks", "fakeroot"), "image_build",
                       ("schema_version", "format", "scripts", "tools", "checks", "fakeroot"))
    if type(declaration["schema_version"]) is not int or declaration["schema_version"] != 1:
        raise ValidationError("Unsupported image_build schema")
    scripts = declaration["scripts"]
    if not isinstance(scripts, list) or not scripts or len(set(scripts)) != len(scripts):
        raise ValidationError("image_build.scripts must be nonempty and unique")
    files = declaration.get("files", [])
    if not isinstance(files, list) or len(set(files)) != len(files):
        raise ValidationError("image_build.files must be a unique list")
    for script in scripts:
        if not isinstance(script, str) or Path(script).suffix not in (".sh", ".bash"):
            raise ValidationError("Image build scripts must be .sh/.bash files")
    resources, frozen_files = {}, {}
    for rel in sorted(set(files) | set(scripts)):
        _relative(rel)
        source = inside(source_root, rel, must_exist=True)
        if source.is_symlink() or not source.is_file():
            raise ValidationError("Build context must contain regular files only: " + rel)
        if source.stat().st_size > 2 * 1024 * 1024:
            raise ValidationError("Large build inputs must use hash-pinned acquire declarations")
        # All context files are reviewable text. Binary wheels/sources are
        # acquired using the same verified input store as model/data resources.
        try:
            check_no_secrets(source.read_text(encoding="utf-8"))
        except UnicodeError as exc:
            raise ValidationError("Build context must be UTF-8 text") from exc
        target = "inputs/" + rel
        resources[target] = source
        frozen_files[rel] = {"resource": target, "sha256": sha256(source)}
    env = validate_build_environment(declaration.get("environment", {}))
    system_env = validate_build_environment(system_environment or {})
    conflicts = {k for k in env.keys() & system_env.keys() if env[k] != system_env[k]}
    if conflicts:
        raise ValidationError("System/App build environment conflict: " + ",".join(sorted(conflicts)))
    result = {"schema_version": 1, "format": declaration["format"],
              "scripts": list(scripts), "files": frozen_files,
              "inputs": copy.deepcopy(declaration.get("inputs", {})),
              "tools": container_tools(declaration["tools"]),
              "environment": dict(env, **system_env),
              "checks": copy.deepcopy(_check_commands(declaration["checks"])),
              "fakeroot": declaration["fakeroot"]}
    validate_frozen_build(result, {k: sha256(v) for k, v in resources.items()}, runtime)
    return result, resources


def validate_frozen_build(build, resources, runtime):
    strict(build, ("schema_version", "format", "scripts", "files", "inputs", "tools",
                   "environment", "checks", "fakeroot"), "frozen image_build",
                  ("schema_version", "format", "scripts", "files", "inputs", "tools",
                   "environment", "checks", "fakeroot"))
    if type(build["schema_version"]) is not int or build["schema_version"] != 1 or build["format"] != FORMAT:
        raise ValidationError("Unsupported frozen image_build")
    if type(build["fakeroot"]) is not bool:
        raise ValidationError("image_build.fakeroot must be boolean")
    from .backends.registry import backend_class
    cls = backend_class(runtime["runtime"], runtime.get("providers", []))
    if FORMAT not in cls.capabilities.image_build_formats:
        raise ValidationError("Selected runtime does not support declared image build format")
    container_tools(build["tools"])
    validate_build_environment(build["environment"])
    _check_commands(build["checks"])
    if not isinstance(build["files"], dict) or not build["files"]:
        raise ValidationError("Frozen build context is empty")
    for rel, item in build["files"].items():
        _relative(rel)
        strict(item, ("resource", "sha256"), "build file", ("resource", "sha256"))
        if item["resource"] != "inputs/" + rel or resources.get(item["resource"]) != valid_hash(item["sha256"]):
            raise ValidationError("Build file is not bound to the staged source closure")
    if (not isinstance(build["scripts"], list) or not build["scripts"]
            or len(set(build["scripts"])) != len(build["scripts"])
            or not set(build["scripts"]) <= set(build["files"])):
        raise ValidationError("Build scripts are not in the fixed context")
    if not isinstance(build["inputs"], dict):
        raise ValidationError("Build inputs must be named acquire declarations")
    names = sorted(build["inputs"])
    for name, source in build["inputs"].items():
        _relative(name)
        if any(other != name and other.startswith(name + "/") for other in names):
            raise ValidationError("Overlapping build input names")
        validate_source(source)
    check_no_secrets(build)
    return build


def run_logged(argv, log, timeout, env):
    """Bounded setup subprocess, with whole process-group cleanup on interruption."""
    log = Path(log)
    log.parent.mkdir(parents=True, exist_ok=True)
    with log.open("xb") as out:
        process = subprocess.Popen(argv, stdout=out, stderr=subprocess.STDOUT,
                                   env=env, start_new_session=True)
        try:
            rc = process.wait(timeout=timeout)
        except BaseException:
            try:
                os.killpg(process.pid, signal.SIGTERM)
                process.wait(timeout=5)
            except (ProcessLookupError, subprocess.TimeoutExpired):
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait()
            raise
    if rc:
        raise ValidationError("Setup subprocess failed (exit %s); log: %s" % (rc, log))


def render_definition(base_path, build, context, inputs, shell):
    """The only host paths allowed in the definition are verified inputs.

    Never accept %setup (host execution), dynamic Bootstrap, or unpinned From
    text from Apps. Apps scripts execute under %post inside the build rootfs.
    """
    paths = [str(base_path), *(str(p) for p in context.values()), *(str(p) for p in inputs.values())]
    if any(any(c in path for c in "\x00\r\n") for path in paths):
        raise ValidationError("Invalid definition input path")
    # From is a header, not shell syntax. Quoting it would alter the path.
    text = ["Bootstrap: localimage", "From: " + str(base_path), "", "%files"]
    for rel, path in sorted(context.items()):
        text.append("    " + shlex.quote(str(path)) + " " + shlex.quote("/bpce-build/context/" + rel))
    for name, path in sorted(inputs.items()):
        text.append("    " + shlex.quote(str(path)) + " " + shlex.quote("/bpce-build/inputs/" + name))
    text += ["", "%post", "    set -eu", "    export BPCE_BUILD_CONTEXT=/bpce-build/context",
             "    export BPCE_BUILD_INPUTS=/bpce-build/inputs"]
    for key, value in sorted(build["environment"].items()):
        text.append("    export " + key + "=" + shlex.quote(value))
    for rel in build["scripts"]:
        text.append("    " + shlex.join([shell, "/bpce-build/context/" + rel]))
    text += ["", "%test", "    set -eu"]
    # Require the declared final tools, not any tool from the host.
    for value in build["tools"].values():
        text.append("    command -v " + shlex.quote(value) + " >/dev/null")
    for argv in build["checks"]:
        text.append("    " + shlex.join(argv))
    return "\n".join(text) + "\n"


def _validate_cache(directory, request):
    directory = Path(directory)
    if directory.is_symlink() or not directory.is_dir():
        raise ValidationError("Invalid build cache directory")
    path = directory / "record.json"
    if path.is_symlink() or not path.is_file():
        raise ValidationError("Build cache has no record")
    record = json.loads(path.read_text(encoding="utf-8"))
    body = dict(record)
    digest = body.pop("record_sha256", None)
    if (record.get("schema_version") != 1 or record.get("state") != "BUILT"
            or record.get("request") != request or record.get("key") != identity(request)
            or digest != identity(body)):
        raise ValidationError("Build cache record identity mismatch")
    for rel, expected in record["files"].items():
        file = inside(directory, rel, must_exist=True)
        if file.is_symlink() or not file.is_file() or sha256(file) != expected:
            raise ValidationError("Build cache content changed: " + rel)
    if record["files"].get("image.sif") != record["output_sha256"]:
        raise ValidationError("Build cache output identity mismatch")
    return record


def _retain(directory, record, evidence):
    evidence.mkdir(parents=True, exist_ok=False)
    # Input blobs and SIFs remain in their content stores. Recipes, commands and
    # logs are retained in READY/CER through the existing evidence mechanism.
    for rel in record["files"]:
        if rel == "image.sif":
            continue
        source = inside(directory, rel, must_exist=True)
        target = inside(evidence, rel)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, target)
    shutil.copyfile(Path(directory) / "record.json", evidence / "record.json")


def ensure_image(spec, resources, rt, base_image, evidence, timeout):
    """Setup-only create-or-verify cache, independent of measurement parameters."""
    build = validate_frozen_build(spec["image_build"], spec["resources"], spec["runtime"])
    if base_image.kind != "sif" or base_image.details.get("overlay"):
        raise ValidationError("SIF definition build requires a plain resolved SIF base")
    observed = rt.observe_runtime()
    request = {"schema_version": 1, "base_request": spec["base"],
               "base_image_identity": base_image.identity, "build": build,
               "platform": spec["base"]["platform"], "accelerator": spec["runtime"]["gpu"],
               "runtime": {k: observed[k] for k in ("name", "version")}}
    key = identity(request)
    store = Path(rt.cache) / "bpce-image-builds-v1"
    final = store / "objects" / key
    with resource_lock(store / "locks" / (key + ".lock"), timeout):
        if final.exists() or final.is_symlink():
            record = _validate_cache(final, request)
        else:
            if spec["setup"]["offline"]:
                raise ValidationError("OFFLINE_IMAGE_BUILD_MISSING: no verified derived image; arbitrary recipe networking is not inferred")
            final.parent.mkdir(parents=True, exist_ok=True)
            work = Path(tempfile.mkdtemp(prefix=".building-", dir=final.parent))
            try:
                context = {}
                for rel, item in build["files"].items():
                    src = inside(resources, item["resource"], must_exist=True)
                    if sha256(src) != item["sha256"]:
                        raise ValidationError("Build input changed: " + rel)
                    dest = inside(work / "context", rel)
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(src, dest)
                    context[rel] = dest
                inputs = {name: acquire(source, Path(rt.cache) / "bpce-inputs-v1", timeout=timeout)
                          for name, source in build["inputs"].items()}
                definition = work / "image.def"
                definition.write_text(render_definition(base_image.reference, build, context, inputs,
                                                       spec["base"]["tools"]["shell"]), encoding="utf-8")
                argv = rt.image_build_command(work / "image.sif", definition, fakeroot=build["fakeroot"])
                start = time.monotonic()
                run_logged(argv, work / "build.log", timeout, rt.host_env())
                result = work / "image.sif"
                if result.is_symlink() or not result.is_file():
                    raise ValidationError("Image builder produced no regular output SIF")
                rt.check_built_image(result, spec["base"]["platform"], work / "image-info.log", timeout)
                # Check the base and declared external inputs again after build.
                rt.load_prepared_image(spec["base"], {"kind": base_image.kind, "reference": base_image.reference,
                    "identity": base_image.identity, "details": dict(base_image.details)}, timeout)
                for source in build["inputs"].values():
                    acquire(source, Path(rt.cache) / "bpce-inputs-v1", offline=True, timeout=timeout)
                files = {p.relative_to(work).as_posix(): sha256(p) for p in sorted(work.rglob("*")) if p.is_file()}
                record = {"schema_version": 1, "state": "BUILT", "key": key, "request": request,
                          "command": argv, "duration_seconds": time.monotonic() - start,
                          "output_sha256": files["image.sif"], "files": files,
                          "validation": "container-build-tests; GPU/workload validation not performed"}
                record["record_sha256"] = identity(record)
                atomic_json(work / "record.json", record)
                for path in sorted(work.rglob("*"), reverse=True):
                    if path.is_symlink():
                        raise ValidationError("Symlink in build cache")
                    path.chmod(0o555 if path.is_dir() else 0o444)
                work.chmod(0o555)
                os.rename(work, final)
            except BaseException as exc:
                if work.exists():
                    work.chmod(0o700)
                    atomic_json(work / "failure.json", {"state": "FAILED", "key": key,
                                "error_type": type(exc).__name__, "error": str(exc)})
                    os.rename(work, store / ("failed-" + key[:12] + "-" + uuid.uuid4().hex))
                raise
            record = _validate_cache(final, request)
    _retain(final, record, Path(evidence))
    execution_base = {"kind": "sif", "source": str(final / "image.sif"),
                      "uri": (final / "image.sif").as_uri(), "sif_sha256": record["output_sha256"],
                      "platform": spec["base"]["platform"], "accelerator": spec["runtime"]["gpu"],
                      "tools": build["tools"]}
    image = rt.resolve_local_image(execution_base, timeout)
    return execution_base, image, record
