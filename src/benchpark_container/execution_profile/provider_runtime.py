"""Self-contained Provider API v1 source. Embedded into the pinned Runtime Spec.

No imports from this distribution: the existing portable runtime loads this source.
"""
import hashlib
import json
import os
import platform
import re
import shlex
import stat
from pathlib import Path

# PROFILE_PAYLOAD_INSERTION
_DATA = None
_ERROR = ValueError


def _need(value, message):
    if not value:
        raise _ERROR(message)


def _identity(value):
    raw = json.dumps(value, sort_keys=True, separators=(",", ":"),
                     ensure_ascii=False, allow_nan=False).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


def _host():
    release = {}
    try:
        for line in Path("/etc/os-release").read_text().splitlines():
            key, sep, value = line.partition("=")
            if sep and key in ("ID", "VERSION_ID"):
                release[key] = value.strip('"')
    except OSError:
        pass
    libc, version = platform.libc_ver()
    return {"system": platform.system(), "machine": platform.machine(),
            "os_id": release.get("ID", "unknown"),
            "os_version": release.get("VERSION_ID", "unknown"),
            "libc": libc, "libc_version": version}


def _verify_files():
    receipt = _DATA["receipt"]
    body = {k: v for k, v in receipt.items() if k != "record_sha256"}
    _need(_identity(body) == receipt["record_sha256"], "Host receipt hash changed")
    _need(_host() == receipt["host"], "Task host differs from prepared platform")
    for item in receipt["inputs"] + receipt["outputs"]:
        path = Path(item["path"])
        _need(str(path.resolve()) == item["realpath"], "Host dependency link target differs: " + item["id"])
        with path.open("rb") as stream:
            before = os.fstat(stream.fileno())
            _need(stat.S_ISREG(before.st_mode) and not before.st_mode & 0o022,
                  "Unsafe dependency permissions/type: " + item["id"])
            h = hashlib.sha256()
            for block in iter(lambda: stream.read(1024 * 1024), b""):
                h.update(block)
            after = os.fstat(stream.fileno())
        fields = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns)
        _need(fields(before) == fields(after) == fields(path.stat()), "Dependency changed during verification")
        _need(h.hexdigest() == item["sha256"], "Host dependency digest differs: " + item["id"])
        if item["executable"]:
            _need(os.access(path, os.X_OK), "Host dependency not executable")


def _verify_plan(plan):
    _need(plan == _DATA["parallel"], "Execution Profile parallel contract differs")
    _need(_DATA["profile"]["runtime"]["container_owner"] == "backend", "Site-owned adapter is unsupported")
    _need(_DATA["profile"]["qualification"]["mode"] == "validation", "P0 is validation-only")
    _verify_files()


def factory(api):
    """Session provider; delegates exact upstream prefix once, never reconstructs it."""
    global _ERROR
    _ERROR = api.ValidationError

    class ProfileSession:
        api_version = 1
        name = "execution-profile-v1"

        @staticmethod
        def command(plan, worker_argv):
            _verify_plan(plan)
            for key in ("LD_PRELOAD", "LD_AUDIT", "BASH_ENV", "ENV"):
                _need(not os.environ.get(key), "Unsafe inherited host execution hook: " + key)
            _need(isinstance(worker_argv, list) and worker_argv and
                  all(isinstance(s, str) and "\x00" not in s for s in worker_argv), "Invalid worker argv")
            exports = ["export " + k + "=" + shlex.quote(v)
                       for k, v in sorted(_DATA["profile"]["host_environment"].items())]
            if _DATA["profile"]["launch"]["kind"] == "local":
                launch = shlex.join(worker_argv)
            else:
                launch = plan["launcher"] + " " + shlex.join(worker_argv)
            # The MPI/Slurm prefix itself is neither reparsed nor re-created.
            return ["/bin/bash", "-c", " && ".join(exports + [launch])]

        @staticmethod
        def binding(plan, env):
            _verify_plan(plan)
            if _DATA["profile"]["launch"]["kind"] == "local":
                _need(plan["nodes"] == plan["tasks"] == 1, "Local profile cannot expand nodes/tasks")
                return {"rank": 0, "size": 1, "local_rank": 0}
            fields = {}
            for key, name in plan["rank_environment"].items():
                value = env.get(name)
                _need(isinstance(value, str) and re.fullmatch(r"[0-9]+", value), "Missing/invalid task binding: " + name)
                fields[key] = int(value)
            _need(fields["size"] == plan["tasks"] and 0 <= fields["rank"] < fields["size"]
                  and 0 <= fields["local_rank"] < fields["size"], "Task world differs from profile")
            return fields

    return ProfileSession


def collector_factory(api):
    """Additive CollectorResult; only selected public data, never an environment dump."""
    global _ERROR
    _ERROR = api.ValidationError

    def collect(ctx, record, runner):
        errors = []
        try:
            _verify_files()
            verified = "declared-files-verified"
        except Exception as exc:
            verified = "failed"
            errors.append("Host dependency verification failed: " + type(exc).__name__)
        env = dict(getattr(ctx, "env", {}))
        masks = {key: env[key][:4096] for key in (
            "CUDA_VISIBLE_DEVICES", "ROCR_VISIBLE_DEVICES", "HIP_VISIBLE_DEVICES", "ZE_AFFINITY_MASK"
        ) if isinstance(env.get(key), str)}
        value = {
            "schema_version": 1,
            "profile_sha256": _DATA["receipt"]["profile_sha256"],
            "policy_sha256": _DATA["receipt"]["policy_sha256"],
            "receipt_sha256": _DATA["receipt"]["record_sha256"],
            "profile_id": _DATA["profile"]["id"],
            "host": _DATA["receipt"]["host"],
            "dependencies": _DATA["receipt"]["outputs"],
            "dependency_verification": verified,
            "loaded_libraries": "not_observed",
            "gpu_visibility_environment": masks,
            "allocation_authority": "not_observed",
            "transport": "not_observed",
            "remote_cleanup": "not_observed",
            "execution_class": "validation-only",
        }
        return api.CollectorResult(
            name="execution-profile", status="partial" if errors else "complete",
            observed={"execution_profile_p0": value}, files={}, errors=errors)

    return collect
