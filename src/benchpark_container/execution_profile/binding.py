"""Bind prepared profiles to NEW v2 plans using the existing Provider API v1."""
from __future__ import annotations

import copy
import hashlib
import importlib.resources
import json
import shlex
from pathlib import Path

from .common import (atomic_new, check_seal, identity, need, read_json, sealed,
                     sha_file, strict)
from .model import validate_profile
from .prepare import verify_receipt

SESSION = "execution-profile-v1"
COLLECTOR = "execution-profile"


def reference(path: Path) -> dict:
    path = Path(path).absolute()
    return {"path": str(path), "sha256": sha_file(path)}


def new_binding(profile_path: Path, policy_path: Path, receipt_path: Path, output: Path) -> dict:
    profile, policy, receipt = map(read_json, (profile_path, policy_path, receipt_path))
    verify_receipt(receipt, profile, policy)
    data = sealed({"schema_version": 1, "mode": "validation",
                   "profile": reference(profile_path), "policy": reference(policy_path),
                   "receipt": reference(receipt_path)})
    atomic_new(output, data)
    return data


def load_binding(path: Path) -> tuple[dict, dict, dict]:
    binding = read_json(path)
    body = check_seal(binding)
    strict(body, {"schema_version", "mode", "profile", "policy", "receipt"}, "binding")
    need(body["schema_version"] == 1 and body["mode"] == "validation", "Unsupported binding mode")
    records = []
    for key in ("profile", "policy", "receipt"):
        ref = strict(body[key], {"path", "sha256"}, "binding reference")
        need(sha_file(ref["path"]) == ref["sha256"], "Binding reference changed: " + key)
        records.append(read_json(ref["path"]))
    profile, policy, receipt = records
    verify_receipt(receipt, profile, policy)
    return profile, policy, receipt


def expected_launcher(profile: dict, receipt: dict, parallel: dict) -> str:
    files = {f["id"]: f for f in receipt["outputs"]}
    launcher = files[profile["launch"]["executable_id"]]["path"]
    return profile["launch"]["prefix_template"].format(
        launcher=shlex.quote(launcher), nodes=parallel["nodes"], tasks=parallel["tasks"],
        workers_per_task=parallel["workers_per_task"])


def descriptors(profile: dict, policy: dict, receipt: dict, parallel: dict) -> list[dict]:
    # Ramble may load this package from runtime.pyz as bpce_node.execution_profile.
    # importlib.resources supports both a real source tree and a zip importer.
    template = importlib.resources.files(__package__).joinpath("provider_runtime.py").read_text(encoding="utf-8")
    payload = {"profile": profile, "policy": policy, "receipt": receipt, "parallel": parallel}
    insertion = "# PROFILE_PAYLOAD_INSERTION\n_DATA = None"
    need(template.count(insertion) == 1, "Provider template mismatch")
    code = template.replace(insertion, "_DATA = json.loads(" + repr(json.dumps(payload, sort_keys=True)) + ")")
    collector = code + "\nfactory = collector_factory\n"
    out = []
    for kind, name, source in (("session", SESSION, code), ("collector", COLLECTOR, collector)):
        need(len(source.encode()) <= 1024 * 1024, "Embedded profile/receipt exceeds Provider API source budget")
        compile(source, "<execution-profile-provider>", "exec")
        out.append({"schema_version": 1, "api_version": 1, "kind": kind,
                    "name": name, "code": source, "sha256": hashlib.sha256(source.encode()).hexdigest()})
    return out


def bind_plan(spec: dict, profile: dict, policy: dict, receipt: dict, *, identity_fn=None) -> dict:
    """Pure transform after prepare/before publication. Original Spec is untouched.

    The live integration supplies benchpark_container.util.identity, avoiding assumptions
    about its JSON identity convention. No library/mount is silently injected.
    """
    validate_profile(profile, policy)
    verify_receipt(receipt, profile, policy)
    out = copy.deepcopy(spec)
    parallel = out.get("parallel")
    need(type(parallel) is dict and type(parallel.get("schema_version")) is int
         and parallel["schema_version"] == 2, "Execution Profile P0 requires Parallel Schema v2")
    need(parallel.get("provider") in ("ramble-launcher-v1", "container-local-v1"), "Refuse to replace unknown/nested provider")
    need(parallel.get("rank_accounting", "launcher-task") == "launcher-task", "No v1 worker/rank reinterpretation")
    for name in ("nodes", "tasks", "workers_per_task", "expected_workers"):
        need(type(parallel.get(name)) is int and parallel[name] > 0, "Concrete positive task counts required")
    need(parallel["expected_workers"] == parallel["tasks"] * parallel["workers_per_task"], "Worker accounting differs")
    need(parallel["tasks"] >= parallel["nodes"], "Too few tasks for nodes")
    need(parallel["nodes"] <= profile["qualification"]["max_nodes"], "Profile topology bound exceeded")
    if profile["application"]["entrypoint"] == "node-agent":
        need(parallel.get("spawn_owner") == "application" and parallel.get("placement") == "one-task-per-node"
             and parallel["tasks"] == parallel["nodes"], "Node agent requires exactly one outer task per node")
    else:
        need(parallel.get("spawn_owner") == "launcher" and parallel["workers_per_task"] == 1
             and parallel.get("placement") == "task", "Rank profile cannot double-spawn workers")
    need(parallel.get("probe_required") is True, "Profile validation requires an application probe")
    need(parallel.get("rank_environment") == profile["launch"]["rank_environment"], "Rank environment contract differs")
    if profile["launch"]["kind"] == "local":
        need(parallel["provider"] == "container-local-v1" and parallel["nodes"] == parallel["tasks"] == 1
             and parallel["launcher"] == "container-local-direct", "Local execution must stay one node/task")
    else:
        need(parallel["provider"] == "ramble-launcher-v1", "External launch may not fall back to container-local")
        need(parallel.get("launcher") == expected_launcher(profile, receipt, parallel),
             "Upstream launcher differs from prepared profile; set System/Workflow mpi_command before concretization")
    runtime = out["runtime"]
    need(runtime["runtime"] == profile["runtime"]["name"], "Runtime differs from approved profile")
    base = out["base"]
    expected = profile["container"]
    need(base["kind"] == expected["kind"], "Container kind mismatch")
    if base["kind"] == "sif":
        actual = base.get("sif_sha256")
    else:
        # Existing OCI identity keys are not guessed. P0 enables a SIF adapter first.
        raise ValueError("OCI profile binding needs source-verified OCI identity adapter; P0 currently binds SIF")
    need(actual == expected["digest"], "Container digest mismatch")
    overlay = base.get("overlay")
    if expected["overlay_sha256"] is None:
        need(not overlay, "Unexpected Overlay")
    else:
        need(type(overlay) is dict and overlay.get("sha256") == expected["overlay_sha256"]
             and overlay.get("format") == "squashfs" and overlay.get("readonly") is True,
             "SquashFS binding mismatch")
    snapshot = base.get("environment_snapshot")
    need((snapshot or {}).get("snapshot_sha256") == expected["snapshot_sha256"], "Container snapshot mismatch")
    for wanted in profile["required_binds"]:
        matches = [a for a in out.get("artifacts", []) if a.get("target") == wanted["target"]]
        need(len(matches) == 1, "Declare the approved host bind through existing Experiment artifacts")
        actual_bind = matches[0]
        source = actual_bind.get("resolved_source", actual_bind.get("source"))
        need(source == wanted["source"] and actual_bind.get("readonly") is True,
             "Existing artifact does not implement approved host bind")
    for key, digest in profile["application"]["resources"].items():
        need(out.get("resources", {}).get(key) == digest,
             "Application source closure differs from approved profile: " + key)
    # Script/library imports remain the application's responsibility; do not change app argv.
    parallel["provider"] = SESSION
    providers = runtime.setdefault("providers", [])
    need(type(providers) is list, "Runtime providers type")
    need(not any(p.get("name") in (SESSION, COLLECTOR) for p in providers), "Execution Profile already attached")
    providers.extend(descriptors(profile, policy, receipt, parallel))
    make_identity = identity_fn or identity
    out["condition_id"] = make_identity({"original_condition_id": spec["condition_id"],
                                         "execution_profile_sha256": identity(profile),
                                         "preparation_receipt_sha256": receipt["record_sha256"],
                                         "parallel": parallel,
                                         "providers": [{"kind": p["kind"], "name": p["name"], "sha256": p["sha256"]}
                                                       for p in providers]})
    return out
