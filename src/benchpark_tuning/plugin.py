# SPDX-License-Identifier: Apache-2.0
"""Fixed Tune presets. No imports of Benchpark spec/repo/classes."""
from pathlib import Path
import hashlib
import yaml
from benchpark_integration.api import (PreparerDescriptor, PreparationResult, ExtensionError, plain)


def describe():
    return PreparerDescriptor(name="tune", api_version=2, prepare=prepare)


def prepare(context):
    root = Path(context.source_root).resolve()
    path = (root / "tuning" / (context.selector + ".yaml")).resolve()
    if root not in path.parents:
        raise ExtensionError("Tune file escapes the experiment")
    body = path.read_bytes()
    data = yaml.safe_load(body)
    if not isinstance(data, dict) or data.get("schema_version") != 1:
        raise ExtensionError("Invalid Tune schema")
    if set(data) - {"schema_version", "overrides", "applies_to", "objective"}:
        raise ExtensionError("Initial implementation supports fixed Tune presets only")
    policy = plain(context.settings).get("allowed", {})
    for name, value in data.get("applies_to", {}).items():
        values = list(context.variants.get(name, (context.defaults.get(name),)))
        if [str(x) for x in values] != [str(value)]:
            raise ExtensionError("Tune is not applicable: " + name)
    overrides = data.get("overrides", {})
    if not isinstance(overrides, dict) or not overrides:
        raise ExtensionError("Tune needs nonempty overrides")
    for name, value in overrides.items():
        if name not in policy:
            raise ExtensionError("Tune may not change " + name)
        if type(value) not in (str, bool, int, float):
            raise ExtensionError("Fixed Tune presets require scalar values")
        rule = policy[name]
        if "choices" in rule and value not in rule["choices"]:
            raise ExtensionError("Tune value outside choices: " + name)
        if "min" in rule and float(value) < float(rule["min"]):
            raise ExtensionError("Tune value below range: " + name)
        if "max" in rule and float(value) > float(rule["max"]):
            raise ExtensionError("Tune value above range: " + name)
    return PreparationResult(overrides=overrides, provenance={
        "name": context.selector, "sha256": hashlib.sha256(body).hexdigest(),
        "source": "tuning/" + path.name, "overrides": overrides,
        "objective": data.get("objective", {})})
