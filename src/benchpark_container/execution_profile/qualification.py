"""Evidence checks, not an assertion of runtime/library/transport universality."""
from __future__ import annotations

from pathlib import Path
from typing import Callable

from .common import atomic_new, identity, need, read_json, sealed, sha_file


def check_gpu_allocation(allocated: list[str], visible: list[str], used: list[str], *, exclusive=True) -> dict:
    """Input identities must already be normalized UUID/PCI IDs by a site adapter.

    Does not derive allocation from nvidia-smi, ordinals or an idle-device observation.
    """
    for label, values in (("allocated", allocated), ("visible", visible), ("used", used)):
        need(type(values) is list and values and all(type(v) is str and v and not v.isdigit()
             for v in values), label + ": normalized device identities required, not ordinals")
    need(len(set(allocated)) == len(allocated) and len(set(visible)) == len(visible), "Duplicate inventory identities")
    need(set(visible) <= set(allocated), "Visible devices exceed allocation")
    need(set(used) <= set(visible), "Worker used device outside allowed visibility")
    if exclusive:
        need(len(set(used)) == len(used), "Multiple workers used the same exclusive GPU")
    return {"status": "PASS", "basis": "caller-supplied-normalized-allocation",
            "allocated": allocated, "visible": visible, "used": used}


def qualify(group_path: Path, *, require_nodes: int, profile_sha256: str,
            upstream_check: Callable, output: Path) -> dict:
    """Require the existing Extension validator AND every child's profile observation.

    upstream_check must be tools.validate_multinode_result.check from the tested source.
    No production-qualification status is issued by this module.
    """
    group_path = Path(group_path).resolve()
    need(type(require_nodes) is int and require_nodes > 0, "Explicit positive physical-node requirement")
    prior_hash = sha_file(group_path)
    upstream = upstream_check(group_path, require_nodes=require_nodes)
    need(type(upstream) is dict and upstream.get("status") == "ACCEPTANCE_PASS", "Upstream v2 validator did not pass")
    group = read_json(group_path)
    need(group.get("status") == "COMPLETED" and group.get("exit_code") == 0, "Group incomplete")
    need(len(group.get("nodes", [])) == require_nodes, "Physical-node evidence count mismatch")
    children = []
    receipts = set()
    for member in group.get("members", []):
        ref = member["cer"]
        path = (group_path.parent / ref["path"]).resolve()
        need(group_path.parent in path.parents, "CER escapes Execution Group")
        need(sha_file(path) == ref["sha256"], "Child CER digest mismatch")
        cer = read_json(path)
        need(cer.get("status") == "COMPLETED", "Child incomplete")
        observation = cer.get("observed", {}).get("execution_profile_p0")
        need(type(observation) is dict and observation.get("profile_sha256") == profile_sha256,
             "Child Profile observation missing/mismatched")
        need(observation.get("dependency_verification") == "declared-files-verified", "Child dependencies not verified")
        need(observation.get("execution_class") == "validation-only", "Unexpected execution class")
        receipts.add(observation["receipt_sha256"])
        children.append({"path": str(path), "sha256": ref["sha256"]})
    need(children and len(receipts) == 1, "Missing/mixed prepared receipts")
    need(sha_file(group_path) == prior_hash, "Group changed during qualification")
    result = sealed({"schema_version": 1,
                     "status": "ONE_NODE_VALIDATED" if require_nodes == 1 else "MULTINODE_VALIDATED",
                     "scope": "existing-v2-validator-and-declared-host-dependencies",
                     "profile_sha256": profile_sha256, "receipt_sha256": next(iter(receipts)),
                     "group": {"path": str(group_path), "sha256": prior_hash}, "children": children,
                     "physical_nodes": require_nodes, "upstream_validation": upstream,
                     "evidence_origin": "supplied-execution-records; no new run by this validator",
                     "operationally_qualified": False,
                     "full_benchpark_ramble_e2e": "not_assessed",
                     "overlay_read_inside_workload": "not_assessed",
                     "allocation_authority": "not_observed", "transport": "not_observed",
                     "runtime_library_loading": "not_observed", "remote_cleanup": "not_observed"})
    atomic_new(output, result)
    return result
