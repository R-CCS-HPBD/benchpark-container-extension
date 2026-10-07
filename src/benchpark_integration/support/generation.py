# SPDX-License-Identifier: Apache-2.0
"""External declarative configuration boundary; no live Benchpark object access."""
import hashlib
from pathlib import Path
from benchpark_integration.api import (API_VERSION, ConfigurationContribution, ExtensionError,
    ExtensionDescriptor, ResolutionContext, SoftwareProvider, plain)
from benchpark_integration.discovery import load_unique, NAME
from .io import file_hash, relative_path, read_system_snapshot, SNAPSHOT


def contributions(values):
    return tuple(values)


def validate_contributions(values):
    owners, resources, variables, envs, modifiers = set(), set(), set(), set(), set()
    provider = None
    for c in values:
        if not isinstance(c, ConfigurationContribution) or not NAME.fullmatch(c.owner) or c.owner in owners:
            raise ExtensionError("Duplicate or invalid contribution owner")
        owners.add(c.owner)
        for mapping in (c.payload, c.variables, c.environment):
            if not isinstance(plain(mapping), dict):
                raise ExtensionError("Contribution section must be a mapping")
        if c.overwrite_policy != "fail":
            raise ExtensionError("Only overwrite_policy=fail is supported")
        if c.software_provider is not None:
            if not isinstance(c.software_provider, SoftwareProvider) or provider is not None:
                raise ExtensionError("Conflicting or invalid software-section providers")
            if not isinstance(plain(c.software_provider.section), dict):
                raise ExtensionError("Software section must be a mapping")
            provider = c.software_provider
        for values_, occupied, label in ((c.variables,variables,"variable"),(c.environment,envs,"environment")):
            for name in values_:
                if name in occupied:
                    raise ExtensionError("Extension " + label + " collision: " + name)
                occupied.add(name)
        for m in c.modifiers:
            if m["name"] in modifiers:
                raise ExtensionError("Duplicate modifier: " + m["name"])
            modifiers.add(m["name"])
        for r in c.resources:
            name = str(relative_path(r.target))
            if name in resources:
                raise ExtensionError("Duplicate resource: " + name)
            resources.add(name)
            actual = file_hash(r.source) if r.source else hashlib.sha256(r.text.encode()).hexdigest()
            if actual != r.sha256:
                raise ExtensionError("Resource changed before publication: " + name)
        for repo in (*c.modifier_repositories, *c.application_repositories):
            relative_path(repo)
    return provider


def resolve_experiment(data):
    """Accept plain declared/resolved data; the Core owns native class access."""
    data = plain(data)
    state = data.get("state", {})
    selected = state.get("extension_selection", ())
    if not selected:
        return ()
    system_dir = data["system_dir"]
    system = read_system_snapshot(system_dir)
    settings = data["settings"]
    values = []
    for name in selected:
        desc = load_unique("benchpark.extensions", name)
        if not isinstance(desc, ExtensionDescriptor):
            raise ExtensionError("Invalid extension descriptor: " + name)
        if desc.requires_system and name not in system:
            raise ExtensionError("Missing saved System settings for %s; initialize a new System" % name)
        if desc.requires_declaration and name not in settings:
            raise ExtensionError("Experiment has no requirements/adapter declaration for " + name)
        snapshot = Path(system_dir) / SNAPSHOT
        context = ResolutionContext(name=data["name"],
            explicit=state.get("extension_explicit", {}), variants=data["variants"],
            system=system.get(name, {}), requirements=settings.get(name, {}),
            source_root=data["source_root"],
            provenance={"system_snapshot_sha256": file_hash(snapshot) if snapshot.is_file() else None,
                        "preparations": state.get("preparation_records", {})})
        value = desc.resolver(context)
        if not isinstance(value, ConfigurationContribution) or value.owner != name:
            raise ExtensionError("Wrong contribution owner/type: " + name)
        values.append(value)
    validate_contributions(values)
    return tuple(values)


def _has_requirements(section):
    if not section:
        return False
    if section.get("packages"):
        return True
    # Empty named environments are emitted even by disabled native helpers.
    if any(bool(value.get("packages")) or bool(set(value) - {"packages"})
           for value in section.get("environments", {}).values()):
        return True
    return any(value for key, value in section.items() if key not in ("packages", "environments"))


class RambleGeneration:
    """Single generation boundary: software selection and declarative additions.

    All normal native package_manager accesses remain untouched. Additional
    variables are added to the generated templates, before they are serialized.
    This adapter lives outside Core and sees only contributions and section callbacks.
    """
    def __init__(self, values):
        self.values = contributions(values)
        self.provider = validate_contributions(self.values)

    def software(self, native, helpers=()):
        """Narrow read-only operations, not a mutable native Experiment handle."""
        if self.provider is None:
            return native()
        for helper_section in helpers:
            if _has_requirements(helper_section()):
                raise ExtensionError("Selected provider cannot satisfy host/helper software")
        return plain(self.provider.section)

    def finish(self, data):
        if not self.values:
            return data
        ramble = data["ramble"]
        names = {m["name"] for m in ramble.get("modifiers", [])}
        for c in self.values:
            for m in c.modifiers:
                if m["name"] in names:
                    raise ExtensionError("Duplicate modifier: " + m["name"])
                names.add(m["name"])
                ramble.setdefault("modifiers", []).append(plain(m))
        for app in ramble.get("applications", {}).values():
            for workload in app.get("workloads", {}).values():
                for experiment in workload.get("experiments", {}).values():
                    occupied = set(ramble.get("variables", {})) | set(app.get("variables", {})) | set(workload.get("variables", {})) | set(experiment.get("variables", {}))
                    var = experiment.setdefault("variables", {})
                    env = experiment.setdefault("env_vars", {}).setdefault("set", {})
                    for c in self.values:
                        for name, value in c.variables.items():
                            if name in occupied:
                                raise ExtensionError("Extension variable collision: " + name)
                            occupied.add(name); var[name] = plain(value)
                        for name, value in c.environment.items():
                            if name in env:
                                raise ExtensionError("Extension environment collision: " + name)
                            env[name] = plain(value)
        return data
