# SPDX-License-Identifier: Apache-2.0
"""Register external commands after the *actual* builtin argparse registry.

    No hard-coded command-name list. Unselected implementations are not loaded.
    Like builtin commands, dispatch follows the normal Benchpark bootstrap.
"""
import sys
from benchpark_integration.api import CommandDescriptor, ExtensionError
from benchpark_integration.discovery import entries, load_unique


def _requested(argv):
    i = 0
    while i < len(argv):
        a = argv[i]
        if a in ("-C", "--config"):
            i += 2
        elif a.startswith("--config=") or (a.startswith("-C") and len(a) > 2):
            i += 1
        elif a.startswith("-"):
            return None
        else:
            return a
    return None


def register_commands(subparsers, actions, argv):
    requested = _requested(argv)
    builtin_names = set(subparsers.choices)
    for name in sorted(entries("benchpark.commands")):
        if name in builtin_names or name in actions:
            continue  # Native command always wins, including future additions.
        selected = requested == name
        desc = load_unique("benchpark.commands", name) if selected else None
        if selected and not isinstance(desc, CommandDescriptor):
            raise ExtensionError("Invalid command descriptor: " + name)
        parser = subparsers.add_parser(name, help=desc.help if desc else "Installed external command")
        if selected:
            desc.setup_parser(parser)
            actions[name] = desc.handler


def print_extension_help():
    names = sorted(entries("benchpark.commands"))
    options = sorted(entries("benchpark.extensions"))
    if names:
        print("\nInstalled external commands: " + ", ".join(names))
    if options:
        print("Installed experiment features: " + ", ".join("+" + n for n in options))
