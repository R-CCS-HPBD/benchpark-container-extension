"""Strict P0 contract. No vendor detection, policy guessing, or silent fallback."""
from __future__ import annotations

import platform
import re
import string
from pathlib import Path
from typing import Any

from .common import (NAME, REVISION, SECRET, ProfileError, absolute, allowed_path,
                     check_digest, identity, need, strict)

PROTECTED_ENV = {
    "CUDA_VISIBLE_DEVICES", "ROCR_VISIBLE_DEVICES", "HIP_VISIBLE_DEVICES", "ZE_AFFINITY_MASK",
    "LD_PRELOAD", "LD_AUDIT", "PYTHONPATH", "PYTHONHOME", "BASH_ENV", "ENV", "IFS",
}
PROTECTED_PREFIX = ("BPCE_", "OMPI_", "PMIX_", "PRTE_", "PMI_", "SLURM_", "PBS_", "PALS_", "SSH_")


def host_fingerprint() -> dict:
    os_release = {}
    try:
        for line in Path("/etc/os-release").read_text().splitlines():
            key, sep, value = line.partition("=")
            if sep and key in ("ID", "VERSION_ID"):
                os_release[key] = value.strip('"')
    except OSError:
        pass
    libc_name, libc_version = platform.libc_ver()
    return {"system": platform.system(), "machine": platform.machine(),
            "os_id": os_release.get("ID", "unknown"),
            "os_version": os_release.get("VERSION_ID", "unknown"),
            "libc": libc_name, "libc_version": libc_version}


def validate_policy(policy: dict) -> dict:
    strict(policy, {"schema_version", "site_id", "approved_profiles", "host",
                    "allowed_roots", "runtimes", "host_environment_names", "max_nodes"}, "site policy")
    need(type(policy["schema_version"]) is int and policy["schema_version"] == 1, "Policy schema")
    need(type(policy["site_id"]) is str and NAME.fullmatch(policy["site_id"]), "site_id")
    need(type(policy["approved_profiles"]) is list and policy["approved_profiles"], "Empty approval set")
    for d in policy["approved_profiles"]:
        check_digest(d, "approved profile")
    strict(policy["host"], {"system", "machine", "os_id", "os_version", "libc", "libc_version"}, "host")
    need(all(type(v) is str and v for v in policy["host"].values()), "Host must be explicit")
    need(policy["host"]["system"] == "Linux", "P0 execution requires Linux")
    roots = policy["allowed_roots"]
    need(type(roots) is list and roots, "Missing permitted roots")
    for p in roots:
        need(str(absolute(p)) != "/", "Approving filesystem root is not supported")
    need(type(policy["runtimes"]) is list and policy["runtimes"] and
         set(policy["runtimes"]) <= {"apptainer", "singularity", "docker"}, "Unknown runtime")
    need(type(policy["host_environment_names"]) is list, "host environment names")
    for name in policy["host_environment_names"]:
        validate_env_name(name)
    need(type(policy["max_nodes"]) is int and 1 <= policy["max_nodes"] <= 65536, "max_nodes")
    return policy


def validate_env_name(name: Any) -> None:
    need(type(name) is str and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name), "Invalid environment name")
    need(not SECRET.search(name) and name not in PROTECTED_ENV
         and not name.startswith(PROTECTED_PREFIX), "Protected environment variable: " + name)


def _files(entries: Any, label: str, policy: dict, optional_hash: bool = False) -> dict:
    need(type(entries) is list and len(entries) <= 4096, label + ": invalid list")
    result = {}
    seen_paths = set()
    for item in entries:
        strict(item, {"id", "path", "sha256", "executable"}, label)
        need(type(item["id"]) is str and NAME.fullmatch(item["id"]) and item["id"] not in result,
             label + ": duplicate/invalid file id")
        p = allowed_path(item["path"], policy["allowed_roots"])
        need(str(p) not in seen_paths, label + ": duplicate real path")
        seen_paths.add(str(p))
        need(type(item["executable"]) is bool, label + ": executable must be bool")
        if not (optional_hash and item["sha256"] is None):
            check_digest(item["sha256"], label)
        result[item["id"]] = item
    return result


def validate_profile(profile: dict, policy: dict, *, approve: bool = True, check_host: bool = True) -> dict:
    validate_policy(policy)
    strict(profile, {"schema_version", "id", "revision", "site_id", "application", "container",
                     "launch", "runtime", "preparation", "host_environment", "required_binds",
                     "qualification"}, "execution profile")
    need(type(profile["schema_version"]) is int and profile["schema_version"] == 1, "Profile schema")
    need(type(profile["id"]) is str and NAME.fullmatch(profile["id"]), "Profile id")
    need(type(profile["revision"]) is str and REVISION.fullmatch(profile["revision"]), "Profile recipe Git revision")
    need(profile["site_id"] == policy["site_id"], "Site mismatch")
    if approve:
        need(identity(profile) in policy["approved_profiles"], "Profile digest is not approved by site policy")
    if check_host:
        need(host_fingerprint() == policy["host"], "Host platform differs from policy; no automatic MPI selection")
    app = strict(profile["application"], {"uses_mpi", "entrypoint", "revision", "resources"}, "application")
    need(type(app["uses_mpi"]) is bool, "uses_mpi must be bool")
    need(app["entrypoint"] in ("rank", "node-agent"), "Unsupported application entrypoint")
    need(type(app["revision"]) is str and REVISION.fullmatch(app["revision"]), "Apps Git revision")
    resources = app["resources"]
    need(type(resources) is dict and resources, "Application source resource hashes are required")
    for name, digest in resources.items():
        need(type(name) is str and not Path(name).is_absolute() and ".." not in Path(name).parts,
             "Invalid application resource path")
        check_digest(digest, "application resource")
    container = strict(profile["container"], {"kind", "digest", "overlay_sha256", "snapshot_sha256"}, "container")
    need(container["kind"] in ("sif", "oci"), "Container kind")
    check_digest(container["digest"], "Container digest")
    for k in ("overlay_sha256", "snapshot_sha256"):
        if container[k] is not None:
            check_digest(container[k], k)
    need(not container["overlay_sha256"] or (container["kind"] == "sif" and container["snapshot_sha256"]),
         "Overlay requires SIF and fixed snapshot")
    runtime = strict(profile["runtime"], {"name", "container_owner", "library_supply", "mpi_policy"}, "runtime")
    need(runtime["name"] in policy["runtimes"], "Runtime not allowed")
    need(runtime["name"] in ("apptainer", "singularity") and container["kind"] == "sif",
         "P0 execution-profile binding supports SIF backends only; legacy OCI/Docker paths remain unchanged")
    need(runtime["container_owner"] == "backend", "Site-owned container requires an unimplemented site adapter")
    need(runtime["library_supply"] in ("container", "host-bind"), "Library supply unsupported")
    need(runtime["mpi_policy"] in ("none", "container-follows-host", "experiment-shared", "host-follows-container"),
         "MPI policy")
    if not app["uses_mpi"]:
        need(runtime["mpi_policy"] == "none", "libmpi policy must be none for non-MPI entrypoint")
    else:
        need(runtime["mpi_policy"] != "none", "MPI entrypoint needs explicit compatibility policy")
    if runtime["mpi_policy"] == "experiment-shared":
        need(runtime["library_supply"] == "host-bind", "Shared MPI requires host-bind")
    if runtime["mpi_policy"] == "host-follows-container":
        need(runtime["library_supply"] == "container", "Container-first policy must retain container libmpi")
    launch = strict(profile["launch"], {"kind", "executable_id", "prefix_template", "rank_environment"}, "launch")
    need(launch["kind"] in ("mpi", "site", "local"), "Launch kind")
    rankenv = strict(launch["rank_environment"], {"rank", "size", "local_rank"}, "rank bindings")
    need(all(type(v) is str and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", v) for v in rankenv.values())
         and len(set(rankenv.values())) == 3, "Distinct rank variable names required")
    template = launch["prefix_template"]
    need(type(template) is str and template and not any(c in template for c in "\x00\n\r"), "Launcher prefix")
    fields = []
    try:
        for _, field, fmt, conv in string.Formatter().parse(template):
            if field is not None:
                need(not fmt and not conv and field in ("launcher", "tasks", "nodes", "workers_per_task"),
                     "Unsupported launcher template field")
                fields.append(field)
    except ValueError as exc:
        raise ProfileError("Invalid launcher template") from exc
    need(fields.count("launcher") == 1 and template.startswith("{launcher}"), "Prefix must start with pinned launcher")
    prep = strict(profile["preparation"], {"kind", "inputs", "outputs", "commands", "timeout_seconds"}, "preparation")
    need(prep["kind"] in ("external", "command"), "Use existing external software or approved command recipe")
    inputs = _files(prep["inputs"], "inputs", policy)
    outputs = _files(prep["outputs"], "outputs", policy, optional_hash=prep["kind"] == "command")
    need(launch["executable_id"] in outputs and outputs[launch["executable_id"]]["executable"], "Launcher output missing")
    need(type(prep["timeout_seconds"]) is int and 1 <= prep["timeout_seconds"] <= 86400, "Preparation timeout")
    need(type(prep["commands"]) is list and len(prep["commands"]) <= 16, "commands")
    if prep["kind"] == "external":
        need(not prep["commands"], "External software must not install anything")
    else:
        need(bool(prep["commands"]), "Preparation commands missing")
    for argv in prep["commands"]:
        need(type(argv) is list and 1 <= len(argv) <= 128 and
             all(type(x) is str and x and not any(c in x for c in "\x00\n\r") for x in argv), "Invalid setup argv")
        need(any(i["path"] == argv[0] and i["executable"] for i in inputs.values()), "Setup executable must be a pinned input")
        need(Path(argv[0]).name not in ("sudo", "su", "doas"), "Privileged installer not supported")
    hostenv = profile["host_environment"]
    need(type(hostenv) is dict, "Host environment")
    for k, v in hostenv.items():
        validate_env_name(k)
        need(k in policy["host_environment_names"], "Host environment not allowed: " + k)
        need(type(v) is str and v and not any(c in v for c in "\x00\n\r"), "Environment value")
        if k in ("PATH", "LD_LIBRARY_PATH"):
            for part in v.split(":"):
                allowed_path(part, policy["allowed_roots"])
    need(type(profile["required_binds"]) is list, "required_binds")
    for bind in profile["required_binds"]:
        strict(bind, {"source", "target", "readonly"}, "host bind")
        source = allowed_path(bind["source"], policy["allowed_roots"])
        target = absolute(bind["target"])
        need(str(target) not in ("/", "/usr", "/usr/lib", "/lib", "/proc", "/sys", "/dev"), "Broad host bind forbidden")
        need(bind["readonly"] is True, "Host library bind must be readonly")
        need(any(Path(i["path"]).resolve() == source or source in Path(i["path"]).resolve().parents
                 for i in outputs.values()), "Host bind needs declared output file fingerprints")
    need(runtime["library_supply"] != "host-bind" or profile["required_binds"], "host-bind needs explicit binds")
    qual = strict(profile["qualification"], {"mode", "max_nodes"}, "qualification policy")
    need(qual["mode"] == "validation", "P0 does not authorize production/operational qualification")
    need(type(qual["max_nodes"]) is int and 1 <= qual["max_nodes"] <= policy["max_nodes"], "Topology limit")
    return profile
