# SPDX-License-Identifier: Apache-2.0
"""Pure versioned execution-plan validation; no runtime/collector imports."""
import json
from .util import ValidationError, identity, expand, check_no_secrets
from .contracts import (validate_accelerator_contract, validate_preparation,
                        validate_environment, validate_targets)
from .reproducibility_rules import validate_model_artifacts


def validate_plan_version(plan):
    if not isinstance(plan, dict) or type(plan.get("schema_version")) is not int or plan["schema_version"] not in (1, 2, 3):
        raise ValidationError("Unsupported execution plan schema_version; expected 1, 2 or 3")
    from .environment_snapshot import validate_environment_binding
    base = plan.get('base', {})
    snapshot = validate_environment_binding(base)
    if plan['schema_version'] == 3:
        from .contracts import validate_setup_contract
        validate_setup_contract(plan)
    else:
        if any(k in plan for k in ('preparation_lifecycle', 'setup', 'image_build', 'image_publish')):
            raise ValidationError('Legacy plan cannot carry a setup lifecycle')
        if (plan['schema_version'] == 2) != (snapshot is not None):
            raise ValidationError('Environment snapshot requires execution plan schema_version=2')
    return plan


def concrete_plan(plan, variables, command, experiment="unknown", repeat="unknown"):
    validate_plan_version(plan)
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
    if 'parallel' in data:
        from .parallel.contract import resolve
        data['parallel'] = resolve(data['parallel'], dict(parameters, **variables))
    validate_accelerator_contract(data)
    needs = validate_preparation(data)
    data["artifacts"] = expand(data["artifacts"], parameters)
    data["environment"] = expand(data["environment"], parameters)
    validate_environment(data["environment"], needs["python"])
    from .contracts import execution_tools
    validate_targets(data["artifacts"], execution_tools(data).values(), concrete=True)
    models = validate_model_artifacts(data["artifacts"])
    data.setdefault("validation", {})["concrete_models"] = models
    check_no_secrets(command)
    data["command"] = command
    data["plan_sha256"] = identity(plan)
    # Exclude record-only location/repeat values from condition identity.
    scientific = {k: data[k] for k in ("benchmark", "base", "runtime", "requirements",
        "setup_scripts", "artifacts", "parameters", "resources", "environment", "command",
        "dependency_policy", "provenance", "protected_packages", "smoke_imports", "benchmark_content_sha256") if k in data}
    if data.get('schema_version') == 3:
        scientific['preparation_lifecycle'] = data['preparation_lifecycle']
        scientific['setup'] = data['setup']
        for key in ('image_build', 'image_publish'):
            if key in data:
                scientific[key] = data[key]
    if 'parallel' in data:
        scientific['parallel'] = data['parallel']
    if 'profiler' in data:
        scientific['profiler'] = data['profiler']
    for key in ("n_repeats", "repeat_index", "experiment_run_dir"):
        scientific["parameters"] = {k: v for k, v in scientific["parameters"].items() if k != key}
    data["condition_id"] = identity(scientific)
    data["ramble_experiment"] = experiment
    data["repeat_index"] = repeat
    return data
