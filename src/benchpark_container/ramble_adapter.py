# SPDX-License-Identifier: Apache-2.0
"""Ramble public executable-modifier adapter, also shipped in the runtime zip."""
import json
from pathlib import Path
import re
import shlex
from .planning import concrete_plan

# BEGIN BPCE EXECUTION PROFILE P0 (opt-in host shim)
_bpce_unprofiled_concrete_plan = concrete_plan

def concrete_plan(*args, **kwargs):
    # Without opt-in, preserve the original function, including all v1/native behavior.
    if not __import__("os").environ.get("BPCE_EXECUTION_BINDING"):
        return _bpce_unprofiled_concrete_plan(*args, **kwargs)
    from .execution_profile.integration import wrap_concrete_plan
    return wrap_concrete_plan(_bpce_unprofiled_concrete_plan)(*args, **kwargs)
# END BPCE EXECUTION PROFILE P0
from .contracts import preparation_needs
from .util import ValidationError, atomic_json_verified, identity, sha256


def status_preserving_template(argv, exit_log, executable_name):
    """Produce a POSIX-shell group so Ramble's output redirect still wraps all output.

    On success the native Ramble `Exit code for ...: $?` remains authoritative.
    On failure the shell would otherwise run the native logging echo last and
    exit 0. Append the same failure line explicitly, then terminate the script
    with the worker's code. Never touch a generated execute_experiment file.
    """
    if not argv or any(not isinstance(arg, str) or not arg or "\x00" in arg for arg in argv):
        raise ValidationError("Invalid worker command")
    log_path = Path(exit_log)
    if not log_path.is_absolute() or "\x00" in str(exit_log):
        raise ValidationError("Ramble exit-code log must be an absolute path")
    if not executable_name or any(c in executable_name for c in "\r\n\x00"):
        raise ValidationError("Invalid executable name for exit-code record")
    # shlex protects argv, log path and Ramble's executable name from shell input.
    fmt = shlex.quote("Exit code for " + executable_name + ": %s\\n")
    log = shlex.quote(str(log_path))
    # IMPORTANT: braces form one shell command; Ramble appends its own output
    # redirect to the *entire* group. A normal return leaves Ramble's native
    # success logger unchanged. The failure path logs and exits explicitly.
    failure_log = "printf " + fmt + ' "$bpce_runtime_rc" >> ' + log + ' 2>&1 || exit 125'
    return ('{ ' + shlex.join(argv) + ' ; bpce_runtime_rc=$?; '
            + 'if [ "$bpce_runtime_rc" -ne 0 ]; then ' + failure_log
            + '; exit "$bpce_runtime_rc"; fi; }')


def _validate_parallel_executable(parallel, executable):
    """Check launcher ownership without assuming all workers are MPI ranks."""
    if executable.run_in_background:
        raise ValidationError('Parallel profile requires one foreground upstream MPI-enabled executable')
    if (parallel.get('provider') == 'container-local-v1'
            and parallel.get('spawn_owner') == 'application'):
        return  # Container command owns MPI or torchrun; no host launcher.
    if not executable.mpi:
        raise ValidationError('Parallel profile requires one foreground upstream MPI-enabled executable')


def wrap_executable(modifier, name, executable, app_inst=None):
    # _file_path is supplied by the pinned Ramble repository loader. Contain
    # this compatibility dependency in the adapter, not Core or experiments.
    resources = Path(modifier._file_path).resolve().parents[2]
    expander = app_inst.expander if app_inst is not None else modifier.expander
    plan_path = resources.parent / "plans" / "container.json"
    plan = json.loads(plan_path.read_text(encoding="utf-8"))
    if name not in plan["executables"]:
        return [], []
    # Ramble renders a non-executed repeat-base experiment plus the concrete
    # repeat children. ExecutePipeline intentionally skips the repeat base.
    # Do not create a BPCE concrete execution plan for something that Ramble
    # will never execute.
    repeats = getattr(app_inst, "repeats", None) if app_inst is not None else None
    if repeats is not None and bool(getattr(repeats, "is_repeat_base", False)):
        return [], []
    if getattr(executable, "bpce_wrapped", False):
        return [], []
    parallel = plan.get('parallel')
    if parallel is None:
        if executable.mpi or executable.run_in_background:
            raise ValidationError("Initial adapter requires a foreground, non-MPI benchmark executable; it does not erase the scheduler/launcher")
        for name_ in ("n_nodes", "n_ranks"):
            value = str(expander.expand_var_name(name_))
            if value not in ("1", "1.0", "{" + name_ + "}"):
                raise ValidationError("Initial adapter requires one node/process: " + name_)
    else:
        _validate_parallel_executable(parallel, executable)
    extra = dict(executable.variables)
    values = {}
    required = set(plan["export_variables"])
    if parallel is not None:
        from .parallel.contract import variables as parallel_variables
        required.update(parallel_variables(parallel))
    for key, value in plan["parameters"].items():
        if isinstance(value, list):
            required.add(key)
    for a in plan["artifacts"]:
        for field in ("path", "revision", "target"):
            required.update(re.findall(r"\{([A-Za-z_]\w*)\}", str(a.get(field, ""))))
    for value in plan["environment"].values():
        required.update(re.findall(r"\{([A-Za-z_]\w*)\}", str(value)))
    for key in sorted(required):
        # Use the same executable variable scope as Ramble's command expansion.
        values[key] = expander.expand_var('{' + key + '}', extra_vars=extra)
        if isinstance(values[key], str) and re.search(r"(?<!\$)\{[A-Za-z_]\w*\}", values[key]):
            raise ValidationError("Ramble did not expand " + key)
    reserved = {"bpce_python": '"$BPCE_BASE_PYTHON"'}
    extra = dict(executable.variables)
    if any(key in extra for key in reserved):
        raise ValidationError("Application executable overrides a reserved execution tool variable")
    if not preparation_needs(plan)["python"] and any(
            "{bpce_python}" in text for text in executable.template):
        raise ValidationError("bpce_python requires declared Python preparation/validation; "
                              "otherwise use an explicit benchmark command")
    extra.update(reserved)
    command = [expander.expand_var(x, extra_vars=extra) for x in executable.template]
    run_dir = Path(expander.expand_var_name("experiment_run_dir")).resolve()
    experiment = expander.expand_var_name("experiment_name")
    repeat = expander.expand_var_name("repeat_index")
    repeat_resolved = repeat != "{repeat_index}"
    if not repeat_resolved:
        repeat = "unknown"
    concrete = concrete_plan(plan, values, command, experiment, repeat)
    # Ramble can invoke executable modifiers once while rendering setup with
    # repeat_index still unresolved, then again for the executable repeat.
    # Only repeat-resolved specs are execution-accountable concrete plans.
    # Keep the setup-time render artifact separate so accounting never expects
    # a CER for a command Ramble will not execute. Both remain immutable.
    spec_dir = resources.parent / ("concrete" if repeat_resolved else "provisional")
    spec_dir.mkdir(parents=True, exist_ok=True)
    spec_path = spec_dir / (identity(concrete) + ".json")
    # Content-addressed concrete plans may be published concurrently by setup
    # workers. Identical publication is safe; different content at the same
    # identity is a hard error.
    atomic_json_verified(spec_path, concrete)
    helper = resources / "runtime.pyz"
    if not helper.is_file():
        raise ValidationError("Runtime helper was not staged into this workspace")
    worker_command = [
        plan["runtime"]["worker_python"], str(helper),
        "--spec", str(spec_path), "--spec-sha256", sha256(spec_path),
        "--resources", str(resources), "--run-root", str(resources.parent / "runs" / concrete["condition_id"]),
    ]
    if parallel is not None:
        worker_command.append('--parallel-launch')
        # The coordinator invokes the exact expanded mpi_command ONCE. Keep
        # upstream variables unchanged, but prevent Ramble adding it again.
        executable.mpi = False
    # Respect a non-default Ramble exit-code path when its variable is expanded.
    # Older unit-test expanders and some Ramble revisions leave it unspecified:
    # the known upstream default is <experiment_run_dir>/exit_codes.out.
    exit_log = expander.expand_var_name("exit_code_log")
    if exit_log in (None, "", "{exit_code_log}"):
        exit_log = str(run_dir / "exit_codes.out")
    if (not isinstance(exit_log, str) or re.search(r"\{[A-Za-z_]\w*\}", exit_log)):
        raise ValidationError("Unresolved Ramble exit_code_log")
    executable.template = [status_preserving_template(worker_command, exit_log, name)]
    executable.bpce_wrapped = True
    return [], []


def prepare_experiment(modifier, workspace, app_inst=None):
    """After make_experiments: prepare only specs belonging to this experiment.

    The helper is a separate process to isolate signal handling and imports
    from Ramble. Never resolve a mutable installed Extension from the host.
    """
    if workspace.dry_run:
        return  # No image/resource lookups and no READY or build directory.
    if app_inst is None:
        raise ValidationError("Setup adapter requires the concrete Ramble application")
    repeats = getattr(app_inst, "repeats", None)
    if repeats is not None and bool(getattr(repeats, "is_repeat_base", False)):
        return
    resources = Path(modifier._file_path).resolve().parents[2]
    plan = json.loads((resources.parent / "plans" / "container.json").read_text())
    if plan.get("schema_version") != 3:
        return  # Previously frozen plans keep their original semantics.
    experiment = app_inst.expander.expand_var_name("experiment_name")
    # Ramble renders provisional plans before expanding repeat_index. Those
    # plans are never executed, so preparing them would create spurious READY
    # environments (and potentially download/build inputs twice per run).
    # Only the concrete plans used by execute_experiment are setup-accountable.
    found = []
    for path in sorted((resources.parent / "concrete").glob("*.json")):
        spec = json.loads(path.read_text())
        if spec.get("ramble_experiment") == experiment and spec.get("schema_version") == 3:
            if spec.get("repeat_index") in (None, "", "unknown", "{repeat_index}"):
                raise ValidationError("Concrete setup plan has unresolved repeat_index: " + str(path))
            found.append((path, spec))
    if not found:
        raise ValidationError("Setup found no generated container plan for " + str(experiment))
    import subprocess
    for path, spec in found:
        command = [spec["runtime"]["worker_python"], str(resources / "runtime.pyz"),
            "--spec", str(path), "--spec-sha256", sha256(path), "--resources", str(resources),
            "--run-root", str(resources.parent / "runs" / spec["condition_id"]), "--setup-environment"]
        subprocess.run(command, check=True)
