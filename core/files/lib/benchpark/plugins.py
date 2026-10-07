# SPDX-License-Identifier: Apache-2.0
"""Optional, trusted lifecycle/CLI providers (protocol v1).

Only discovery, routing and dispatch live here. Providers own all policy and
storage. Requests contain data, not native Spec/System/Experiment objects.
A persisted owner list is a fail-closed routing marker, not a plugin import.
"""
import importlib.metadata
import json
from pathlib import Path
import re

API_VERSION = 1
NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*$")


class PluginError(ValueError):
    pass


def entries(group):
    metadata = importlib.metadata.entry_points()
    points = metadata.select(group=group) if hasattr(metadata, "select") else metadata.get(group, ())
    result = {}
    for ep in points:
        if NAME.fullmatch(ep.name):
            result.setdefault(ep.name, []).append(ep)
    return result


def available():
    return set(entries("benchpark.lifecycle.v1"))


def _load(ep):
    try:
        provider = ep.load()()
    except Exception as exc:
        raise PluginError("Cannot load selected plugin %s: %s" % (ep.name, exc)) from exc
    if getattr(provider, "api_version", None) != API_VERSION:
        raise PluginError("Unsupported plugin API: " + ep.name)
    return provider


def invoke(event, keys, default=None, **context):
    """Coalesce feature aliases to one explicit coordinator; never last-wins.

    A coordinator can compose multiple feature plugins. Independent coordinators
    claiming the same invocation are an error, not an implicit ordering policy.
    """
    names = sorted(set(keys))
    if not names:
        return default
    candidates = entries("benchpark.lifecycle.v1")
    chosen = []
    for name in names:
        matches = candidates.get(name, ())
        if len(matches) != 1:
            raise PluginError("Expected one lifecycle provider for %s, found %d" % (name, len(matches)))
        chosen.append(matches[0])
    if len({ep.value for ep in chosen}) != 1:
        raise PluginError("Conflicting lifecycle coordinators: " + ", ".join(names))
    provider = _load(chosen[0])
    return provider.handle(event, context)


def owners_at(*roots):
    """Read generic routing markers before a destructive workspace operation.

    Preserve v0.5.2's marker spelling for existing initialized experiments.
    Missing selected providers MUST NOT make these directories native again.
    """
    owners = set()
    for root in roots:
        path = Path(root) / ".benchpark-extensions" / "manifest.json"
        if not path.exists() and not path.is_symlink():
            continue
        try:
            value = json.loads(path.read_text(encoding="utf-8"))["owners"]
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise PluginError("Invalid plugin routing marker: " + str(path)) from exc
        if not isinstance(value, list) or not value or not all(isinstance(x, str) and NAME.fullmatch(x) for x in value):
            raise PluginError("Invalid plugin owner list: " + str(path))
        owners.update(value)
    return sorted(owners)


def register_commands(subparsers, actions, argv):
    # Consume only the existing global config argument. Native argparse remains
    # the authority; unknown global options are not reinterpreted here.
    args = list(argv)
    while args:
        if args[0] in ("-C", "--config"):
            args = args[2:]
        elif args[0].startswith("--config=") or (args[0].startswith("-C") and len(args[0]) > 2):
            args = args[1:]
        else:
            break
    requested = args[0] if args and not args[0].startswith("-") else None
    for name, matches in sorted(entries("benchpark.cli.v1").items()):
        if name in subparsers.choices or name in actions:
            continue
        if requested == name:
            if len(matches) != 1:
                raise PluginError("Conflicting command plugins: " + name)
            command = _load(matches[0])
            parser = subparsers.add_parser(name, help=command.help)
            command.setup_parser(parser)
            actions[name] = command.handler
        else:
            subparsers.add_parser(name, help="Installed external command")


def print_extension_help():
    names = sorted(entries("benchpark.cli.v1"))
    features = sorted(entries("benchpark.extensions"))
    if names:
        print("\nInstalled external commands: " + ", ".join(names))
    if features:
        print("Installed experiment features: " + ", ".join("+" + n for n in features))


def repository_config(repositories, config_dir):
    """Generic data-only repository contribution port; no Core config writes."""
    candidates = entries("benchpark.repositories.v1")
    if not candidates:
        return repositories
    # JSON copying prevents a failing provider from mutating native config.
    result = json.loads(json.dumps(repositories, allow_nan=False))
    for name, matches in sorted(candidates.items()):
        if len(matches) != 1:
            raise PluginError("Conflicting repository providers: " + name)
        provider = _load(matches[0])
        try:
            result = provider.handle("repositories", {
                "repositories": result, "config_dir": str(config_dir)})
            if not isinstance(result, dict):
                raise ValueError("Repository provider must return a mapping")
            result = json.loads(json.dumps(result, allow_nan=False))
        except Exception as exc:
            raise PluginError("Repository provider %s: %s" % (name, exc)) from exc
    return result

def repository_entries(kind, directory, include_external=True):
    """Optional external object directories for native accounting/list loops."""
    result = [[p.name, str(directory)] for p in sorted(Path(directory).iterdir())]
    if not include_external: return [(name, Path(parent)) for name, parent in result]
    for name, matches in sorted(entries("benchpark.repositories.v1").items()):
        if len(matches) != 1:
            raise PluginError("Conflicting repository providers: " + name)
        try:
            result = _load(matches[0]).handle("repository_entries", {
                "kind": kind, "entries": result})
            if not isinstance(result, list) or not all(
                    isinstance(x, (tuple, list)) and len(x) == 2 and
                    all(isinstance(v, str) for v in x) for x in result):
                raise ValueError("Repository entries must be name/path pairs")
        except Exception as exc:
            raise PluginError("Repository provider %s: %s" % (name, exc)) from exc
    return [(name, Path(parent)) for name, parent in result]
def repository_list_groups(kind, collection, native_benchmarks=()):
    """Partition accounting output for presentation; resolution stays unified."""
    if kind not in ("benchmarks", "experiments"):
        raise PluginError("Unsupported repository list kind: " + str(kind))
    original, claimed, groups = list(collection), set(), []
    for name, matches in sorted(entries("benchpark.repositories.v1").items()):
        if len(matches) != 1:
            raise PluginError("Conflicting repository providers: " + name)
        try:
            group = _load(matches[0]).handle("repository_list_group", {"kind": kind, "collection": list(original), "native_benchmarks": list(native_benchmarks)})
            if group is None: continue
            if not isinstance(group, dict) or set(group) != {"label", "items"}: raise ValueError("Repository list group must contain label/items")
            label, items = group["label"], group["items"]
            if not isinstance(label, str) or not label.strip() or not isinstance(items, list) or not all(isinstance(x, str) for x in items): raise ValueError("Invalid repository list group")
            if len(items) != len(set(items)) or any(x not in original for x in items) or claimed.intersection(items): raise ValueError("Repository list group contains duplicate/unknown/claimed items")
            claimed.update(items)
            if items: groups.append((label.strip(), items))
        except Exception as exc:
            raise PluginError("Repository provider %s: %s" % (name, exc)) from exc
    return [item for item in original if item not in claimed], groups
