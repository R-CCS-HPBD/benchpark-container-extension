# SPDX-License-Identifier: Apache-2.0
"""Portable runtime-independent container job helper.

Design boundary:
- Benchpark/Extension provides declarations and immutable input artifacts.
- The Common Base Container provides Python, pip and every package-management
  executable used to construct the run-local software layer.
- The extension NEVER injects a Python interpreter, pip, package manager or
  installer binary into the container.
- Optional command/tool installation is expressed as source-controlled shell
  scripts from the experiment and executed inside the Common Base.
"""
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
from urllib.parse import urlparse

from .util import (ValidationError, atomic_json, check_no_secrets, identity,
                   inside, sha256, expand)
from .artifacts import materialize
from .reproducibility_rules import validate_model_artifacts, canonical
from .cer.recording import start_run, finish_run
from .contracts import (runtime_settings, validate_targets, ROOT, INPUTS, WORK, BENCHMARK_SCRIPT)
from .preparation import prepare_environment, EnvironmentBuildError, validate_layer_inventory, dependency_check_delta


# Compatibility import for callers of the earlier Apptainer helper.
from .backends.apptainer import ApptainerBackend as Apptainer
from .backends import create_backend
from .backends.base import command_scope


class _ExecutionSignal(KeyboardInterrupt):
    def __init__(self, signum):
        self.signum = signum
        super().__init__("Execution interrupted by signal " + str(signum))


def concrete_plan(plan, variables, command, experiment="unknown", repeat="unknown"):
    # Values only; runtime helper never reopens System or experiment definitions.
    data = json.loads(json.dumps(plan))
    parameters = data["parameters"]
    for key, value in list(parameters.items()):
        if key in variables:
            parameters[key] = variables[key]
        elif isinstance(value, list):
            raise ValidationError("Matrix value is not concrete: " + key)
    for key in plan.get("export_variables", []):
        if key not in variables:
            raise ValidationError("Missing exported measurement variable: " + key)
        parameters[key] = variables[key]
    data["parameters"] = parameters
    data["artifacts"] = expand(data["artifacts"], parameters)
    data["environment"] = expand(data["environment"], parameters)
    validate_targets(data["artifacts"], data["base"].get("tools", {}).values(), concrete=True)
    models = validate_model_artifacts(data["artifacts"])
    data.setdefault("validation", {})["concrete_models"] = models
    check_no_secrets(command)
    data["command"] = command
    data["plan_sha256"] = identity(plan)
    # Exclude record-only location/repeat values from condition identity.
    scientific = {k: data[k] for k in ("benchmark", "base", "runtime", "requirements",
        "setup_scripts", "artifacts", "parameters", "resources", "environment", "command",
        "dependency_policy", "provenance", "protected_packages", "smoke_imports") if k in data}
    for key in ("n_repeats", "repeat_index", "experiment_run_dir"):
        scientific["parameters"] = {k: v for k, v in scientific["parameters"].items() if k != key}
    data["condition_id"] = identity(scientific)
    data["ramble_experiment"] = experiment
    data["repeat_index"] = repeat
    return data


def run_logged(command, log_path, timeout, env):
    """Tee benchmark stdout for Ramble FoM and also retain each attempt's log."""
    process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               env=env, start_new_session=True)
    interrupted = []
    old_handlers = {}
    def handle(sig, frame):
        interrupted.append(sig)
        try:
            os.killpg(process.pid, sig)
        except ProcessLookupError:
            pass
    for sig in (signal.SIGTERM, signal.SIGINT):
        old_handlers[sig] = signal.signal(sig, handle)
    def tee():
        with open(log_path, "wb") as log:
            while True:
                chunk = process.stdout.read(8192)
                if not chunk:
                    break
                log.write(chunk); log.flush()
                try:
                    sys.stdout.buffer.write(chunk); sys.stdout.buffer.flush()
                except (AttributeError, BrokenPipeError):
                    pass
    thread = threading.Thread(target=tee, daemon=True)
    thread.start()
    state = "FAILED"
    try:
        try:
            code = process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGTERM)
            try:
                process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL); process.wait()
            code, state = 124, "TIMEOUT"
        if interrupted:
            code, state = 128 + interrupted[-1], "INTERRUPTED"
        elif state != "TIMEOUT":
            state = "COMPLETED" if code == 0 else "FAILED"
        return code, state
    finally:
        thread.join(timeout=10)
        process.stdout.close()
        for sig, old in old_handlers.items():
            signal.signal(sig, old)



def execute(spec, resources, run_root):
    resources = Path(resources).resolve()
    inputs = resources / "inputs"
    attempt, record = start_run(run_root, spec)
    scratch = attempt / "scratch"
    scratch.mkdir(); (scratch / "work").mkdir()
    code = 1
    preparation_start = time.monotonic()
    phase = "preparation"
    rt = None
    old_handlers = {}
    if threading.current_thread() is threading.main_thread():
        def interrupted(signum, frame):
            raise _ExecutionSignal(signum)
        for signum in (signal.SIGTERM, signal.SIGINT):
            old_handlers[signum] = signal.signal(signum, interrupted)
    try:
        timeout = float(spec["timeout_seconds"])
        if not 0 < timeout <= 7 * 86400:
            raise ValidationError("Invalid timeout_seconds")
        for relative, expected in spec["resources"].items():
            p = inside(resources, relative, must_exist=True)
            if sha256(p) != expected:
                raise ValidationError("Fixed resource has changed: " + relative)
        expected = spec["base"].get("platform", "unverified")
        architecture = record["observed"]["host"]["machine"]
        native = {"x86_64": "linux/amd64", "aarch64": "linux/arm64"}.get(architecture)
        if expected != "unverified" and expected != native:
            raise ValidationError("Declared image platform does not match execution host")
        rt = create_backend(spec["runtime"])
        rt.attach_attempt(attempt)
        runtime_observation = rt.observe_runtime()
        record["observed"]["runtime"] = runtime_observation
        record["observed"]["runtime_version"] = runtime_observation["version"]
        image = rt.resolve_image(spec["base"], timeout)
        record["observed"]["image"] = rt.observe_image(image)
        mounts = materialize(spec["artifacts"], resources, attempt)
        record["observed"]["mounts"] = mounts

        env_record = prepare_environment(rt, image, scratch, inputs, mounts, spec, attempt, timeout)
        record["observed"]["software_environment"] = env_record
        record["result"]["preparation_seconds"] = time.monotonic() - preparation_start

        script = "set -euo pipefail\n" + "\n".join(spec["command"]) + "\n"
        (scratch / "benchmark.sh").write_text(script, encoding="utf-8")
        environment = dict(spec["environment"])
        activation = env_record.get("runtime_environment")
        if env_record.get("state") != "ready" or not isinstance(activation, dict):
            raise ValidationError("Dependency construction did not produce a ready activation contract")
        environment.update(activation)
        command = rt.argv(image, scratch, inputs, mounts,
                          [env_record["tools"]["shell"], BENCHMARK_SCRIPT], environment)
        record["observed"]["command"] = command
        record["observed"]["execution_environment"] = environment
        phase = "benchmark"
        with command_scope(rt, command):
            measurement_start = time.monotonic()
            code, state = run_logged(command, attempt / "benchmark.log", timeout, rt.host_env())
            record["result"].update(exit_code=code,
                benchmark_process_seconds=time.monotonic() - measurement_start,
                measurement_note="Process time is not a replacement for application FoM; application controls warmup/measurement.")
            if code != 0:
                record["result"]["failure_phase"] = phase
                record["result"]["error_code"] = "BENCHMARK_NONZERO_EXIT"
            record["status"] = state
    except _ExecutionSignal as error:
        code, record["status"] = 128 + error.signum, "INTERRUPTED"
        record["result"].update(exit_code=code, failure_phase=phase, signal=error.signum)
    except KeyboardInterrupt:
        code, record["status"] = 130, "INTERRUPTED"
        record["result"].update(exit_code=130, failure_phase=phase)
    except Exception as e:
        previous_code = record["result"].get("exit_code", 1)
        code = previous_code if previous_code else 1
        if record["status"] not in ("FAILED", "TIMEOUT", "INTERRUPTED"):
            record["status"] = "PREPARATION_FAILED" if phase == "preparation" else "EXECUTION_ERROR"
        record["result"].update(exit_code=code, failure_phase=phase, error_type=type(e).__name__, error=str(e),
            preparation_seconds=time.monotonic() - preparation_start)
        print("BPCE " + phase + " failed: " + str(e), file=sys.stderr)
    finally:
        # A second termination request must not interrupt owned-resource cleanup
        # or prevent the final failure CER from being written.
        for signum in old_handlers:
            signal.signal(signum, signal.SIG_IGN)
        if rt is not None:
            try:
                rt.close()
            except Exception as error:
                record["result"]["runtime_close_error"] = str(error)
                if code == 0:
                    code, record["status"] = 1, "EXECUTION_ERROR"
                    record["result"].update(exit_code=1, failure_phase="cleanup")
            record["observed"]["runtime_invocations"] = rt.events
            cleanup_errors = [e for e in rt.events if e.get("cleanup") == "failed"]
            if cleanup_errors:
                record["result"]["runtime_cleanup_failed"] = True
                if code == 0:
                    code, record["status"] = 1, "EXECUTION_ERROR"
                    record["result"].update(exit_code=1, failure_phase="cleanup")
        partial = attempt / "environment.json"
        if partial.is_file() and "software_environment" not in record["observed"]:
            try:
                record["observed"]["software_environment"] = json.loads(partial.read_text())
            except (OSError, ValueError):
                pass
        try:
            finish_run(attempt, record)
            print("BPCE_CER=" + str(attempt / "cer.json"), file=sys.stderr)
        finally:
            for signum, handler in old_handlers.items():
                signal.signal(signum, handler)
    return code


def main():
    import argparse
    p = argparse.ArgumentParser()
    p.add_argument("--spec", required=True, type=Path)
    p.add_argument("--spec-sha256", required=True)
    p.add_argument("--resources", required=True, type=Path)
    p.add_argument("--run-root", required=True, type=Path)
    args = p.parse_args()
    if sha256(args.spec) != args.spec_sha256:
        p.error("Concrete spec hash mismatch")
    spec = json.loads(args.spec.read_text(encoding="utf-8"))
    return execute(spec, args.resources, args.run_root)


if __name__ == "__main__":
    sys.exit(main())
