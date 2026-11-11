# SPDX-License-Identifier: Apache-2.0
"""Portable runtime-independent container job helper.

Design boundary:
- Benchpark/Extension provides declarations and immutable input artifacts.
- The Base Container supplies Bash and the tools needed by declared preparation.
  Python/pip are required only by the corresponding Python preparation work.
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
from .cer.source_evidence import retain_source_evidence
from .contracts import (runtime_settings, validate_targets, validate_accelerator_contract,
                        validate_preparation, validate_environment, ROOT, INPUTS, WORK, BENCHMARK_SCRIPT)
from .preparation import prepare_environment, EnvironmentBuildError, validate_layer_inventory, dependency_check_delta


# Compatibility import for callers of the earlier Apptainer helper.
from .backends.apptainer import ApptainerBackend as Apptainer
from .backends import create_backend
from .backends.base import command_scope, prepared_command


class _ExecutionSignal(KeyboardInterrupt):
    def __init__(self, signum):
        self.signum = signum
        super().__init__("Execution interrupted by signal " + str(signum))


# Compatibility re-export for saved callers.
from .planning import concrete_plan, validate_plan_version


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



def execute(spec, resources, run_root, execution_context=None):
    resources = Path(resources).resolve()
    inputs = resources / "inputs"
    attempt, record = start_run(run_root, spec)
    scratch = attempt / "scratch"
    scratch.mkdir(); (scratch / "work").mkdir()
    code = 1
    preparation_start = time.monotonic()
    phase = "preparation"
    rt = None
    image_lease = None
    setup_record = None
    setup_root = None
    profiler_decoration = None
    profiler_reference = None
    old_handlers = {}
    if threading.current_thread() is threading.main_thread():
        def interrupted(signum, frame):
            raise _ExecutionSignal(signum)
        for signum in (signal.SIGTERM, signal.SIGINT):
            old_handlers[signum] = signal.signal(signum, interrupted)
    try:
        # In-process legacy callers may omit the version. File entrypoints may not.
        if "schema_version" in spec:
            validate_plan_version(spec)
        if execution_context is not None:
            execution_context.started(attempt, record)
        from .environment_snapshot import validate_environment_binding, verify_environment_files
        environment_snapshot = validate_environment_binding(spec["base"])
        if environment_snapshot is not None and spec.get("schema_version") not in (2, 3):
            raise ValidationError("Composite environment requires execution plan schema=2")
        validate_accelerator_contract(spec)
        needs = validate_preparation(spec)
        validate_environment(spec["environment"], needs["python"])
        timeout = float(spec["timeout_seconds"])
        if not 0 < timeout <= 7 * 86400:
            raise ValidationError("Invalid timeout_seconds")
        for relative, expected in spec["resources"].items():
            p = inside(resources, relative, must_exist=True)
            if sha256(p) != expected:
                raise ValidationError("Fixed resource has changed: " + relative)
        source = retain_source_evidence(resources, attempt, spec)
        if source is not None:
            record["observed"]["benchmark_source"] = source
        expected = spec["base"].get("platform", "unverified")
        architecture = record["observed"]["host"]["machine"]
        native = {"x86_64": "linux/amd64", "aarch64": "linux/arm64"}.get(architecture)
        if expected != "unverified" and expected != native:
            raise ValidationError("Declared image platform does not match execution host")
        if environment_snapshot is not None:
            record["observed"]["environment_integrity_before"] = verify_environment_files(spec["base"])
        rt = create_backend(spec["runtime"])
        rt.attach_attempt(attempt)
        from .acquisition import ImageLease
        if spec.get("schema_version") != 3 and hasattr(rt, "cache"):
            image_lease = ImageLease(rt, spec["runtime"].get("image_policy", "cache"), attempt)
            record["observed"]["image_acquisition"] = image_lease.record()
        runtime_observation = rt.observe_runtime()
        record["observed"]["runtime"] = runtime_observation
        record["observed"]["runtime_version"] = runtime_observation["version"]
        if spec.get("schema_version") == 3:
            from .setup_environment import load_prepared, bind_prepared, retain_prepared, execution_base
            setup_root, setup_record = load_prepared(spec, resources)
            image = rt.load_prepared_image(execution_base(spec, setup_record), setup_record["image"], timeout)
            bind_prepared(rt, scratch, setup_root, setup_record)
            record["observed"]["prepared_environment"] = retain_prepared(attempt, setup_root, setup_record)
            execution_artifacts = setup_record["artifacts"]
        else:
            image = rt.resolve_image(spec["base"], timeout)
            execution_artifacts = spec["artifacts"]
        record["observed"]["image"] = rt.observe_image(image)
        if execution_context is None:
            mounts = materialize(execution_artifacts, resources, attempt)
        else:
            mounts = materialize(execution_artifacts, resources, attempt,
                                 shared_root=execution_context.group / 'shared-outputs',
                                 shared_names=execution_context.plan['shared_outputs'])
        record["observed"]["mounts"] = mounts

        if setup_record is not None:
            env_record = setup_record["software_environment"]
        else:
            env_record = prepare_environment(rt, image, scratch, inputs, mounts, spec, attempt, timeout)
        record["observed"]["software_environment"] = env_record
        record["result"]["preparation_seconds"] = time.monotonic() - preparation_start

        script = "set -euo pipefail\n" + "\n".join(spec["command"]) + "\n"
        (scratch / "benchmark.sh").write_text(script, encoding="utf-8")
        environment = dict(spec["environment"])
        if execution_context is not None:
            passed = execution_context.environment()
            if set(environment) & set(passed):
                raise ValidationError('Application environment overrides parallel/site-owned bindings')
            environment.update(passed)
        activation = env_record.get("runtime_environment")
        if env_record.get("state") != "ready" or not isinstance(activation, dict):
            raise ValidationError("Dependency construction did not produce a ready activation contract")
        environment.update(activation)
        benchmark_argv = [env_record["tools"]["shell"], BENCHMARK_SCRIPT]
        if spec.get("profiler", {}).get("enabled"):
            from .profiler_interop import prepare_capture
            profiler_decoration, profiler_mounts = prepare_capture(
                spec, resources, attempt, image, benchmark_argv, mounts)
            # The existing backend handles both runtime families; mounts remain
            # source/target/read-only data, not guessed docker/apptainer flags.
            command = rt.argv(image, scratch, inputs, mounts + profiler_mounts,
                              profiler_decoration.inner_argv, environment)
            record["observed"]["profiler_launch"] = {
                "api_version": 2, "capture_id": spec["profiler"]["capture_id"],
                "job_sha256": profiler_decoration.job_sha256,
                "bundle_manifest_sha256": spec["profiler"]["manifest_sha256"]}
        else:
            command = rt.argv(image, scratch, inputs, mounts,
                              benchmark_argv, environment)
        record["observed"]["command"] = command
        if hasattr(rt, "applied_binding"):
            record["observed"]["container_binding"] = dict(rt.applied_binding)
        record["observed"]["execution_environment"] = environment
        phase = "profiler-worker" if profiler_decoration is not None else "benchmark"
        with prepared_command(rt, command) as prepared:
            record["observed"]["command"] = list(prepared.argv)
            measurement_start = time.monotonic()
            code, state = run_logged(prepared.argv, attempt / "benchmark.log", timeout, prepared.host_environment)
            record["result"].update(exit_code=code,
                benchmark_process_seconds=time.monotonic() - measurement_start,
                measurement_note="Process time is not a replacement for application FoM; application controls warmup/measurement.")
            if profiler_decoration is not None:
                record["result"]["profiler_worker_exit_code"] = code
            if code != 0:
                record["result"]["failure_phase"] = phase
                record["result"]["error_code"] = (
                    "PROFILER_WORKER_NONZERO_EXIT" if profiler_decoration is not None
                    else "BENCHMARK_NONZERO_EXIT")
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
        if image_lease is not None:
            try:
                image_lease.close()
                record["observed"]["image_acquisition"]["lease_cleanup"] = "complete"
            except Exception as error:
                record["result"]["image_cleanup_error"] = str(error)
                if code == 0:
                    code, record["status"] = 1, "EXECUTION_ERROR"
                    record["result"].update(exit_code=1, failure_phase="cleanup")
        if spec.get("base", {}).get("environment_snapshot") is not None:
            try:
                from .environment_snapshot import verify_environment_files
                record["observed"]["environment_integrity_after"] = verify_environment_files(spec["base"])
            except Exception as error:
                record["observed"]["environment_integrity_after"] = {"status": "failed", "error": str(error)}
                record["result"]["application_exit_code"] = record["result"].get("exit_code", code)
                if code == 0:
                    code, record["status"] = 1, "EXECUTION_ERROR"
                    record["result"].update(exit_code=1, failure_phase="environment-integrity")
        if setup_record is not None:
            try:
                from .setup_environment import load_prepared
                _, after_record = load_prepared(spec, resources)
                rt.load_prepared_image(execution_base(spec, setup_record), setup_record["image"], timeout)
                if after_record != setup_record:
                    raise ValidationError("Prepared record changed during execution")
                record["observed"]["prepared_environment_integrity_after"] = "verified"
            except Exception as error:
                record["observed"]["prepared_environment_integrity_after"] = "failed"
                record["result"]["application_exit_code"] = record["result"].get("exit_code", code)
                record["result"]["prepared_environment_error"] = str(error)
                if code == 0:
                    code, record["status"] = 1, "EXECUTION_ERROR"
                    record["result"].update(exit_code=1, failure_phase="environment-integrity")
        partial = attempt / "environment.json"
        if partial.is_file() and "software_environment" not in record["observed"]:
            try:
                record["observed"]["software_environment"] = json.loads(partial.read_text())
            except (OSError, ValueError):
                pass
        if execution_context is not None:
            try:
                probe_ok = execution_context.capture_probe(attempt, record)
            except Exception as error:
                probe_ok = False
                record['result']['parallel_probe'] = 'failed'
                record['result']['parallel_probe_error'] = str(error)
            if not probe_ok and code == 0:
                code, record['status'] = 1, 'FAILED'
                record['result'].update(exit_code=1, failure_phase='parallel-validation')
        if profiler_decoration is not None:
            # Never let an unverified or changed capture look like a successful
            # benchmark/CER simply because the workload happened to exit 0.
            try:
                from .profiler_interop import collect_capture
                profiler_reference = collect_capture(attempt, record, profiler_decoration)
                record["result"]["profiler_status"] = profiler_reference["profiling_status"]
                record["result"]["profiler_accepted"] = profiler_reference["accepted"]
                # A Profiler Worker is not the benchmark itself. Preserve the
                # benchmark's observed exit codes separately where available.
                from .profiler_interop import inspect_capture as read_profiler_observation
                details = read_profiler_observation(profiler_decoration)
                workload = details.get("workload", {})
                record["result"]["profiler_workload_status"] = workload.get("status")
                record["result"]["application_exit_codes"] = workload.get("exit_codes", [])
                # `failure_phase` remains the outer lifecycle stage (profiler-worker).
                # Set a separate cause only after verifying a genuine nonzero
                # application exit in the Profiler's finalized observation.
                from .profiler_interop import verified_benchmark_nonzero
                if code != 0 and verified_benchmark_nonzero(profiler_reference, details):
                    record["result"]["failure_origin"] = "benchmark"
                if not profiler_reference["accepted"] and code == 0:
                    code, record["status"] = 1, "FAILED"
                    record["result"].update(exit_code=1, failure_phase="profiler-validation")
            except Exception as error:
                record["result"]["profiler_registration"] = {
                    "status": "FAILED", "error_type": type(error).__name__, "error": str(error)}
                if code == 0:
                    code, record["status"] = 1, "FAILED"
                    record["result"].update(exit_code=1, failure_phase="profiler-evidence")
        try:
            finish_run(attempt, record)
            if execution_context is not None:
                execution_context.completed(attempt, record, code)
            print("BPCE_CER=" + str(attempt / "cer.json"), file=sys.stderr)
            if profiler_reference is not None:
                # No mutations are permitted after seal; an external receipt is
                # supplemental and can be retried with the same final CER hash.
                from .profiler_interop import link_capture
                receipt_directory = attempt.parent / "profiler-links" / attempt.name
                try:
                    link = link_capture(attempt, profiler_decoration, profiler_reference,
                                        receipt_directory=receipt_directory)
                    print("BPCE_PROFILER_LINK=" + link["receipt"], file=sys.stderr)
                except Exception as error:
                    print("BPCE_PROFILER_LINK_PENDING=" + type(error).__name__ + ": " + str(error),
                          file=sys.stderr)
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
    mode = p.add_mutually_exclusive_group()
    mode.add_argument('--setup-environment', action='store_true')
    mode.add_argument('--parallel-launch', action='store_true')
    mode.add_argument('--parallel-task', type=Path)
    args = p.parse_args()
    if sha256(args.spec) != args.spec_sha256:
        p.error("Concrete spec hash mismatch")
    spec = json.loads(args.spec.read_text(encoding="utf-8"))
    validate_plan_version(spec)
    if args.setup_environment:
        from .setup_environment import prepare
        ready = prepare(spec, args.resources)
        print("BPCE_SETUP_READY=" + ready["record_sha256"])
        return 0
    if args.parallel_launch:
        from .parallel.runner import launch
        return launch(spec, args.spec, args.resources, args.run_root, Path(sys.argv[0]).resolve())
    if args.parallel_task:
        from .parallel.runner import task
        return task(spec, args.resources, args.run_root, args.parallel_task)
    if 'parallel' in spec:
        p.error('A parallel plan must be entered through its coordinator')
    return execute(spec, args.resources, args.run_root)


if __name__ == "__main__":
    sys.exit(main())
