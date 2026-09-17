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
