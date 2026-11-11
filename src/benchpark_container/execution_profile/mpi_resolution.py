"""Fail-closed route qualification for a prepared Execution Profile P0.

No MPI package manager, version guessing, or altered system MPI.  The site
selects a concrete approved profile.  A real Parallel-v2 communication result
and fingerprints authorize *only* the tested topology, image and launcher.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
import re
import subprocess
import sys
from pathlib import Path

from .common import ProfileError, atomic_new, check_digest, check_seal, identity, need, read_json, sealed, sha_file, strict
from .binding import expected_launcher
from .model import validate_profile
from .prepare import verify_receipt

KINDS = {"site-direct", "container-follows-host", "experiment-shared", "host-follows-container"}
STATUS = "REAL_V2_COMMUNICATION_QUALIFIED_FOR_TOPOLOGY"
ROUTE_COLLECTOR = "execution-mpi-route-v1"


def _real(path):
    p = Path(path)
    need(p.is_absolute() and ".." not in p.parts, "An absolute evidence path without '..' is required")
    return p.resolve()


def _reference(path):
    path = _real(path)
    need(path.is_file(), "Evidence file missing: " + str(path))
    return {"path": str(path), "sha256": sha_file(path)}


def _check_reference(ref):
    strict(ref, {"path", "sha256"}, "Evidence reference")
    check_digest(ref["sha256"], "Evidence SHA256")
    p = _real(ref["path"])
    need(p.is_file() and sha_file(p) == ref["sha256"], "Evidence changed: " + str(p))
    return p


def _git(repo, *args):
    p = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True, check=False)
    need(p.returncode == 0, "Cannot inspect source revision")
    return p.stdout.strip()


def _verify_source(extension, sha):
    extension = _real(extension)
    need(extension.is_dir(), "Extension path missing")
    need(type(sha) is str and re.fullmatch(r"[0-9a-f]{40}", sha), "Expected full Git SHA")
    need(_git(extension, "rev-parse", "HEAD") == sha, "Extension commit mismatch")
    need(not _git(extension, "status", "--porcelain", "--untracked-files=all"), "Extension source is dirty")
    checker = extension / "tools" / "validate_multinode_result.py"
    need(checker.is_file(), "Authoritative v2 validator missing")
    return checker


def _validate_topology(p):
    strict(p, {"schema_version", "nodes", "tasks", "workers_per_task", "spawn_owner", "placement"}, "Topology")
    need(p["schema_version"] == 2, "Only Parallel Schema v2")
    need(all(type(p[k]) is int and p[k] > 0 for k in ("nodes", "tasks", "workers_per_task")), "Invalid node/task counts")
    need(p["tasks"] >= p["nodes"], "Tasks less than nodes")
    need((p["spawn_owner"], p["placement"]) in {("launcher", "task"), ("application", "one-task-per-node")},
         "Unsupported spawn/placement combination")
    if p["spawn_owner"] == "launcher":
        need(p["workers_per_task"] == 1, "MPI rank owner cannot also spawn workers")
    else:
        need(p["tasks"] == p["nodes"], "Exactly one node agent per node")
    return p


def topology_from_plan(plan):
    return _validate_topology({k: plan[k] for k in
                               ("schema_version", "nodes", "tasks", "workers_per_task", "spawn_owner", "placement")})


def validate_candidate(candidate, profile, policy, receipt, spec):
    """Check *declared* topology and prepared actual prefix; no MPI version inference."""
    validate_profile(profile, policy)
    verify_receipt(receipt, profile, policy)
    strict(candidate, {"schema_version", "route_id", "kind", "system_id", "profile_sha256",
                       "receipt_sha256", "container_sha256", "container_mpi_sha256",
                       "container_mpi_path", "launcher_sha256", "extension_git_sha", "topology"}, "MPI candidate")
    need(candidate["schema_version"] == 1 and candidate["kind"] in KINDS, "MPI route type")
    need(type(candidate["route_id"]) is str and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,95}", candidate["route_id"]), "Route ID")
    need(type(candidate["system_id"]) is str and candidate["system_id"] == policy["site_id"], "Site identity differs")
    for key in ("profile_sha256", "receipt_sha256", "container_sha256", "container_mpi_sha256", "launcher_sha256"):
        check_digest(candidate[key], key)
    need(type(candidate["extension_git_sha"]) is str and re.fullmatch(r"[0-9a-f]{40}", candidate["extension_git_sha"]), "Extension revision")
    need(candidate["profile_sha256"] == identity(profile) and candidate["receipt_sha256"] == receipt["record_sha256"], "Different prepared profile/receipt")
    need(candidate["container_sha256"] == profile["container"]["digest"], "Container artifact differs")
    need(type(candidate["container_mpi_path"]) is str and candidate["container_mpi_path"].startswith("/")
         and ".." not in Path(candidate["container_mpi_path"]).parts, "Invalid container MPI library path")
    need(candidate["kind"] == profile["runtime"]["mpi_policy"] or
         (candidate["kind"] == "site-direct" and profile["launch"]["kind"] == "site"), "MPI policy differs")
    need(profile["application"]["uses_mpi"] and profile["application"]["entrypoint"] == "rank", "MPI rank entrypoint required")
    need(profile["launch"]["kind"] in ("mpi", "site"), "MPI rank needs external launcher")
    launcher = next((entry for entry in receipt["outputs"] if entry["id"] == profile["launch"]["executable_id"]), None)
    need(launcher is not None and launcher["sha256"] == candidate["launcher_sha256"], "Prepared MPI launcher identity differs")
    parallel = spec["parallel"]
    topology = topology_from_plan(parallel)
    need(topology == candidate["topology"], "MPI topology differs")
    need(parallel["provider"] == "ramble-launcher-v1", "MPI validation requires upstream launcher")
    need(parallel["launcher"] == expected_launcher(profile, receipt, parallel), "Launcher prefix differs from prepared profile")
    need(spec["base"]["kind"] == "sif" and spec["base"]["sif_sha256"] == candidate["container_sha256"], "SIF digest differs")
    need(spec["runtime"]["runtime"] == profile["runtime"]["name"], "Container runtime differs")
    need(parallel.get("probe_required") is True, "MPI probe is required")
    return candidate


def container_mpi_fingerprint(sif, container_path, *, apptainer="/usr/bin/apptainer", timeout=90):
    """Read SHA256 of the actual *container file*; never guess from a version string."""
    sif = _real(sif)
    need(sif.is_file(), "SIF missing")
    need(type(container_path) is str and container_path.startswith("/") and ".." not in Path(container_path).parts,
         "Invalid MPI library path")
    app = Path(apptainer)
    need(app.is_absolute() and app.is_file() and os.access(app, os.X_OK), "Approved absolute Apptainer required")
    need(type(timeout) is int and 1 <= timeout <= 300, "Invalid timeout")
    proc = subprocess.run([str(app), "exec", "--cleanenv", str(sif), "/usr/bin/sha256sum", container_path],
                          capture_output=True, text=True, timeout=timeout, check=False)
    need(proc.returncode == 0, "Failed to fingerprint container MPI library")
    match = re.fullmatch(r"([0-9a-f]{64})\s+(.+)\n?", proc.stdout)
    need(match is not None and match.group(2) == container_path, "Unexpected container MPI fingerprint")
    return {"path": container_path, "sha256": match.group(1), "sif_sha256": sha_file(sif)}


def _group_evidence(case, spec, expected):
    # Existing v2 runner writes exactly one real Group. Reject synthetic top-level PASS.
    case = _real(case)
    need(case.is_dir() and (case / "spec.json").is_file(), "v2 case missing spec")
    groups = sorted((case / "runs").rglob("group.json"))
    need(len(groups) == 1, "Expected one new Execution Group")
    group_path = groups[0]
    group = read_json(group_path)
    need(group.get("status") == "COMPLETED" and group.get("exit_code") == 0, "MPI Group not completed")
    need(group.get("placement_validation") == "passed" and group.get("parallel_validation") == "application-probe-passed", "No real placement/communication probe")
    need(len(group.get("nodes", [])) == expected["nodes"], "Physical node count differs")
    recorded_spec = read_json(case / "spec.json")
    tested_parallel = recorded_spec.get("parallel", {})
    need(topology_from_plan(tested_parallel) == expected, "Test topology differs")
    need(tested_parallel.get("launcher") == spec["parallel"]["launcher"], "Test launcher differs from intended plan")
    need(recorded_spec.get("base", {}).get("sif_sha256") == spec["base"]["sif_sha256"], "Test SIF differs")
    need(recorded_spec.get("runtime", {}).get("runtime") == spec["runtime"]["runtime"], "Test runtime differs")
    members = group.get("members", [])
    need(len(members) == expected["tasks"], "Child task count differs")
    children, ranks, nodes = [], [], set()
    for member in members:
        ref = member.get("cer") or {}
        need(type(ref.get("path")) is str, "Child CER reference missing")
        p = (group_path.parent / ref["path"]).resolve()
        need(group_path.parent in p.parents and p.is_file(), "Child CER outside Group")
        file_sha = sha_file(p)
        if "sha256" in ref:
            need(file_sha == ref["sha256"], "Child CER hash differs")
        cer = read_json(p)
        need(cer.get("status") == "COMPLETED", "Child CER not completed")
        obs = cer.get("observed", {}).get("execution_mapping") or {}
        rank = obs.get("task_rank")
        need(type(rank) is int and obs.get("task_world_size") == expected["tasks"], "Child outer task mapping invalid")
        ranks.append(rank)
        nodes.add(obs.get("node_id"))
        need((cer.get("observed", {}).get("runtime") or {}).get("name") == spec["runtime"]["runtime"], "Child Container Runtime mismatch")
        children.append({"path": str(p), "sha256": file_sha, "rank": rank})
    need(sorted(ranks) == list(range(expected["tasks"])), "Duplicate or missing MPI task rank")
    need(len(nodes) == expected["nodes"] and None not in nodes, "Child node mapping differs")
    return group_path, children


def qualify_real_route(*, candidate, profile, policy, receipt, spec, case, extension,
                       sif, apptainer, result_path, validator=None):
    """Promote a candidate only after a *real v2* Group passes its authoritative validator.

    Validator is loaded from the exact fixed Extension source; no caller override.
    Newly produced record never claims full Ramble E2E or remote cleanup.
    """
    validate_candidate(candidate, profile, policy, receipt, spec)
    need(candidate["extension_git_sha"] == _git(extension, "rev-parse", "HEAD"), "Candidate source differs")
    checker = _verify_source(extension, candidate["extension_git_sha"])
    fingerprint = container_mpi_fingerprint(sif, candidate["container_mpi_path"], apptainer=apptainer)
    need(fingerprint["sif_sha256"] == candidate["container_sha256"], "Actual SIF digest differs")
    need(fingerprint["sha256"] == candidate["container_mpi_sha256"], "Actual libmpi digest differs")
    group_file, children = _group_evidence(case, spec, candidate["topology"])
    checker_before = sha_file(checker)
    import importlib.util
    previously_no_bytecode = sys.dont_write_bytecode
    old_sys_path = list(sys.path)
    try:
        sys.dont_write_bytecode = True
        sys.path.insert(0, str(checker.parent))
        module_spec = importlib.util.spec_from_file_location("_bpce_fixed_v2_checker", checker)
        need(module_spec is not None and module_spec.loader is not None, "Cannot load fixed v2 validator")
        module = importlib.util.module_from_spec(module_spec)
        module_spec.loader.exec_module(module)
        verdict = module.check(group_file, require_nodes=candidate["topology"]["nodes"])
    finally:
        sys.dont_write_bytecode = previously_no_bytecode
        sys.path[:] = old_sys_path
    need(type(verdict) is dict and verdict.get("status") == "ACCEPTANCE_PASS", "Authoritative v2 validator rejected MPI run")
    need(sha_file(checker) == checker_before, "Upstream MPI checker changed")
    _verify_source(extension, candidate["extension_git_sha"])
    result = sealed({"schema_version": 1, "status": STATUS,
                     "candidate": candidate, "candidate_sha256": identity(candidate),
                     "profile_sha256": identity(profile), "receipt_sha256": receipt["record_sha256"],
                     "tested_spec": _reference(Path(case) / "spec.json"),
                     "group": _reference(group_file), "children": children,
                     "validator_sha256": checker_before,
                     "container_libmpi": fingerprint,
                     "topology": copy.deepcopy(candidate["topology"]),
                     "upstream_status": verdict["status"],
                     "full_benchpark_ramble_e2e": False,
                     "operationally_qualified": False,
                     "remote_cleanup": "not_observed", "transport": "not_observed"})
    atomic_new(result_path, result)
    return result


def verify_route_record(record, profile, policy, receipt, spec, *, verify_files=True):
    """Fail closed after Setup and before v2 plan publication."""
    strict(check_seal(record), {"schema_version", "status", "candidate", "candidate_sha256",
                               "profile_sha256", "receipt_sha256", "tested_spec", "group", "children",
                               "validator_sha256", "container_libmpi", "topology", "upstream_status",
                               "full_benchpark_ramble_e2e", "operationally_qualified", "remote_cleanup", "transport"}, "MPI route record")
    need(record["status"] == STATUS and record["upstream_status"] == "ACCEPTANCE_PASS", "Unqualified MPI route")
    candidate = record["candidate"]
    validate_candidate(candidate, profile, policy, receipt, spec)
    need(record["candidate_sha256"] == identity(candidate) and record["topology"] == candidate["topology"], "Candidate was changed")
    need(record["profile_sha256"] == identity(profile) and record["receipt_sha256"] == receipt["record_sha256"], "Profile/receipt differs")
    need(record["container_libmpi"]["sha256"] == candidate["container_mpi_sha256"]
         and record["container_libmpi"]["sif_sha256"] == candidate["container_sha256"], "MPI library fingerprint differs")
    if verify_files:
        group_path = _check_reference(record["group"])
        tested_path = _check_reference(record["tested_spec"])
        need(group_path.is_file() and tested_path.is_file(), "Missing communication evidence")
        for child in record["children"]:
            _check_reference({"path": child["path"], "sha256": child["sha256"]})
    need(record["full_benchpark_ramble_e2e"] is False and record["operationally_qualified"] is False,
         "Misleading operation/Benchpark certification")
    return record


def _embedded_collector(record):
    """Separate additive provider: does not alter existing host/profile CER collector."""
    summary = {"schema_version": 1, "status": record["status"],
               "route_id": record["candidate"]["route_id"], "route_sha256": identity(record),
               "mpi_policy": record["candidate"]["kind"], "container_libmpi_sha256": record["candidate"]["container_mpi_sha256"],
               "launcher_sha256": record["candidate"]["launcher_sha256"],
               "qualification_group_sha256": record["group"]["sha256"],
               "qualified_topology": record["topology"], "scope": "validated-prior-v2-communication-not-current-attempt",
               "operationally_qualified": False}
    # Encoded input has no secrets/environments; static source safe for zip bundled Provider API v1.
    code = '''import json\n_INFO = json.loads(''' + repr(json.dumps(summary, sort_keys=True)) + ''')\ndef factory(api):\n    def collect(ctx, record, runner):\n        return api.CollectorResult(name="execution-mpi-route", status="complete", observed={"execution_mpi_route_p1": _INFO}, files={}, errors=[])\n    return collect\n'''
    compile(code, "<execution-mpi-route>", "exec")
    return {"schema_version": 1, "api_version": 1, "kind": "collector", "name": ROUTE_COLLECTOR,
            "code": code, "sha256": hashlib.sha256(code.encode()).hexdigest()}


def attach_to_bound_plan(bound, raw, profile, policy, receipt, record, *, identity_fn=identity):
    """Add pinned route evidence while preserving upstream v2 and original launcher."""
    # Raw spec keeps upstream ramble-launcher-v1; bound spec has execution-profile-v1.
    verify_route_record(record, profile, policy, receipt, raw)
    need(bound["parallel"]["launcher"] == raw["parallel"]["launcher"], "Launcher changed while binding route")
    out = copy.deepcopy(bound)
    providers = out["runtime"]["providers"]
    need(type(providers) is list and not any(p.get("name") == ROUTE_COLLECTOR for p in providers), "Route already embedded")
    d = _embedded_collector(record)
    providers.append(d)
    out["condition_id"] = identity_fn({"original_profiled_condition_id": bound["condition_id"],
                                    "mpi_route_sha256": identity(record), "mpi_route_provider_sha256": d["sha256"]})
    return out


def load_qualified_route(path):
    return read_json(path)


def select_qualified_route(paths, profile, policy, receipt, raw_spec):
    """Select one eligible, content-verified record; never guess from version labels."""
    need(type(paths) in (list, tuple) and paths, "At least one qualified candidate record is required")
    selected = []
    rejected = []
    for path in paths:
        try:
            record = load_qualified_route(path)
            verify_route_record(record, profile, policy, receipt, raw_spec)
            selected.append((path, record))
        except (ProfileError, KeyError, OSError, TypeError, ValueError) as exc:
            rejected.append({"file": str(path), "reason": str(exc)[:200]})
    need(len(selected) == 1, "No unique qualified MPI route (count=" + str(len(selected)) + "); rejected=" + str(rejected))
    return {"status": STATUS, "selected": _reference(selected[0][0]),
            "candidate": selected[0][1]["candidate"]["route_id"], "rejected": rejected}


def load_selected_route(path):
    """Load either an immutable qualified route record or a unique-route selection."""
    obj = read_json(path)
    if obj.get("status") == STATUS and "selected" in obj:
        ref = obj["selected"]
        route = read_json(_check_reference(ref))
        need(route.get("candidate", {}).get("route_id") == obj.get("candidate"), "Selected route changed")
        return route
    return obj
