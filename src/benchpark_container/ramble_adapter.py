# SPDX-License-Identifier: Apache-2.0
"""Ramble public executable-modifier adapter, also shipped in the runtime zip."""
import json
from pathlib import Path
import re
import shlex
from .runtime import concrete_plan
from .util import ValidationError, atomic_json_verified, identity, sha256


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
    if executable.mpi or executable.run_in_background:
        raise ValidationError("Initial adapter requires a foreground, non-MPI benchmark executable; it does not erase the scheduler/launcher")
    for name_ in ("n_nodes", "n_ranks"):
        value = str(expander.expand_var_name(name_))
        if value not in ("1", "1.0", "{" + name_ + "}"):
            raise ValidationError("Initial adapter requires one node/process: " + name_)
    values = {}
    required = set(plan["export_variables"])
    for key, value in plan["parameters"].items():
        if isinstance(value, list):
            required.add(key)
    for a in plan["artifacts"]:
        for field in ("path", "revision", "target"):
            required.update(re.findall(r"\{([A-Za-z_]\w*)\}", str(a.get(field, ""))))
    for value in plan["environment"].values():
        required.update(re.findall(r"\{([A-Za-z_]\w*)\}", str(value)))
    for key in sorted(required):
        values[key] = expander.expand_var_name(key)
        if isinstance(values[key], str) and re.search(r"\{[A-Za-z_]\w*\}", values[key]):
            raise ValidationError("Ramble did not expand " + key)
    reserved = {"bpce_python": '"$BPCE_BASE_PYTHON"'}
    extra = dict(executable.variables)
    if any(key in extra for key in reserved):
        raise ValidationError("Application executable overrides a reserved execution tool variable")
    extra.update(reserved)
    command = [expander.expand_var(x, extra_vars=extra) for x in executable.template]
    run_dir = Path(expander.expand_var_name("experiment_run_dir")).resolve()
    experiment = expander.expand_var_name("experiment_name")
    repeat = expander.expand_var_name("repeat_index")
    if repeat == "{repeat_index}":
        repeat = "unknown"
    concrete = concrete_plan(plan, values, command, experiment, repeat)
    # This concrete-spec file is immutable; a new attempt is created at launch.
    # Keep concrete plans outside allocation-cleaned experiment directories.
    spec_dir = resources.parent / "concrete"
    spec_dir.mkdir(parents=True, exist_ok=True)
    spec_path = spec_dir / (identity(concrete) + ".json")
    # Content-addressed concrete plans may be published concurrently by setup
    # workers. Identical publication is safe; different content at the same
    # identity is a hard error.
    atomic_json_verified(spec_path, concrete)
    helper = resources / "runtime.pyz"
    if not helper.is_file():
        raise ValidationError("Runtime helper was not staged into this workspace")
    executable.template = [shlex.join([
        plan["runtime"]["worker_python"], str(helper),
        "--spec", str(spec_path), "--spec-sha256", sha256(spec_path),
        "--resources", str(resources), "--run-root", str(resources.parent / "runs" / concrete["condition_id"]),
    ])]
    executable.bpce_wrapped = True
    return [], []
