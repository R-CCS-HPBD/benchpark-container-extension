"""Small preparation/binding front end; MPI installation remains in existing tools."""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

from .binding import bind_plan, load_binding, new_binding
from .common import ProfileError, atomic_new, identity, read_json
from .model import host_fingerprint, validate_profile
from .prepare import prepare
from .qualification import qualify


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("host", help="Print observed host fingerprint; does not approve a Profile")
    d = sub.add_parser("digest", help="Canonical JSON digest for explicit operator approval")
    d.add_argument("file", type=Path)
    for cmd in ("validate", "prepare", "binding"):
        p = sub.add_parser(cmd)
        p.add_argument("--profile", required=True, type=Path)
        p.add_argument("--policy", required=True, type=Path)
        if cmd != "validate":
            p.add_argument("--receipt", required=True, type=Path)
        if cmd == "prepare":
            p.add_argument("--execute", action="store_true", help="Authorize approved local setup commands")
            p.add_argument("--dry-run", action="store_true")
        if cmd == "binding":
            p.add_argument("--output", required=True, type=Path)
    w = sub.add_parser("workspace-setup", help="Prepare approved host software, then invoke existing Ramble workspace setup")
    w.add_argument("--profile", required=True, type=Path)
    w.add_argument("--policy", required=True, type=Path)
    w.add_argument("--receipt", required=True, type=Path)
    w.add_argument("--binding", required=True, type=Path)
    w.add_argument("--execute", action="store_true")
    w.add_argument("--extension-root", required=True, type=Path)
    w.add_argument("ramble_command", nargs=argparse.REMAINDER)
    b = sub.add_parser("bind-plan", help="Create a new candidate Spec; never edits the original")
    b.add_argument("--binding", required=True, type=Path)
    b.add_argument("--spec", required=True, type=Path)
    b.add_argument("--output", required=True, type=Path)
    b.add_argument("--extension-root", required=True, type=Path)
    q = sub.add_parser("qualify")
    q.add_argument("--group", required=True, type=Path)
    q.add_argument("--nodes", required=True, type=int)
    q.add_argument("--profile-sha256", required=True)
    q.add_argument("--extension-root", required=True, type=Path)
    q.add_argument("--output", required=True, type=Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "host":
            result = host_fingerprint()
        elif args.command == "digest":
            print(identity(read_json(args.file)))
            return 0
        elif args.command == "validate":
            p, s = read_json(args.profile), read_json(args.policy)
            validate_profile(p, s)
            result = {"status": "VALID_DECLARATION", "profile_sha256": identity(p), "mpi_compatibility": "NOT_TESTED"}
        elif args.command == "prepare":
            result = prepare(read_json(args.profile), read_json(args.policy), args.receipt,
                             execute=args.execute, dry_run=args.dry_run)
        elif args.command == "binding":
            result = new_binding(args.profile, args.policy, args.receipt, args.output)
        elif args.command == "workspace-setup":
            from .common import need, sha_file
            cmd = args.ramble_command
            if cmd and cmd[0] == "--":
                cmd = cmd[1:]
            need(len(cmd) >= 3 and Path(cmd[0]).is_absolute() and Path(cmd[0]).name == "ramble"
                 and cmd[-2:] == ["workspace", "setup"],
                 "Only an explicit existing Ramble workspace setup command is accepted; never on/execute")
            need(cmd.count("--workspace-dir") == 1 and cmd.index("--workspace-dir") + 1 < len(cmd),
                 "Use the explicit --workspace-dir form")
            ws = Path(cmd[cmd.index("--workspace-dir") + 1]).resolve()
            meta = ws / ".benchpark-extensions"
            need(not list((meta / "runs").rglob("cer.json")) and not list((meta / "runs").rglob("group.json")),
                 "Refuse workspace setup over existing execution evidence")
            adapter = args.extension_root / "src/benchpark_container/ramble_adapter.py"
            need(adapter.is_file() and "# BEGIN BPCE EXECUTION PROFILE P0 (opt-in host shim)" in adapter.read_text(),
                 "Profile hook is not installed; refuse unprofiled workspace setup")
            profile, policy = read_json(args.profile), read_json(args.policy)
            prepare(profile, policy, args.receipt, execute=args.execute)
            if args.binding.exists():
                prior_p, prior_s, prior_r = load_binding(args.binding)
                need(prior_p == profile and prior_s == policy
                     and prior_r == read_json(args.receipt), "Existing binding differs")
            else:
                new_binding(args.profile, args.policy, args.receipt, args.binding)
            env = os.environ.copy()
            env["BPCE_EXECUTION_BINDING"] = str(args.binding.absolute())
            rc = subprocess.run(cmd, env=env, check=False).returncode
            if rc:
                return rc
            from .binding import descriptors
            plans = sorted((meta / "concrete").glob("*.json"))
            need(plans, "Ramble created no Concrete Plans; no implicit PASS")
            receipt = read_json(args.receipt)
            for path in plans:
                plan = read_json(path)
                need(plan.get("parallel", {}).get("provider") == "execution-profile-v1",
                     "Ramble ignored the execution-profile hook: " + str(path))
                providers = plan.get("runtime", {}).get("providers", [])
                need(all(d in providers for d in descriptors(profile, policy, receipt, plan["parallel"])),
                     "Concrete Plan does not contain the prepared Profile descriptors")
            result = {"status": "PROFILED_WORKSPACE_SETUP", "concrete_plans": len(plans),
                      "runtime_executed": False, "mpi_compatibility": "NOT_TESTED"}
        elif args.command == "bind-plan":
            sys.path.insert(0, str(args.extension_root / "src"))
            from benchpark_container.util import identity as native_identity
            from benchpark_container.parallel.contract import validate_resolved
            from benchpark_container.providers import validate_descriptors
            profile, policy, receipt = load_binding(args.binding)
            result = bind_plan(read_json(args.spec), profile, policy, receipt, identity_fn=native_identity)
            validate_resolved(result["parallel"])
            validate_descriptors(result["runtime"]["providers"])
            atomic_new(args.output, result)
            result = {"status": "NEW_SPEC_WRITTEN", "path": str(args.output), "runtime_executed": False}
        elif args.command == "qualify":
            sys.path.insert(0, str(args.extension_root / "src"))
            sys.path.insert(0, str(args.extension_root / "tools"))
            from validate_multinode_result import check
            result = qualify(args.group, require_nodes=args.nodes,
                             profile_sha256=args.profile_sha256, upstream_check=check, output=args.output)
        print(json.dumps(result, indent=2, ensure_ascii=False, allow_nan=False))
        return 0
    except (ProfileError, OSError, ValueError, KeyError, ImportError) as exc:
        print(f"BPCE_PROFILE_ERROR={type(exc).__name__}: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
