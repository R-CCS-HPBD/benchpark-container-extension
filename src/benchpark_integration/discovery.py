# SPDX-License-Identifier: Apache-2.0
"""Trusted extension discovery. No Tune, workspace, image or pip policy here."""
import importlib.metadata
import re
from benchpark_integration.api import API_VERSION, ExtensionError

NAME = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*$")


def entries(group):
    found = importlib.metadata.entry_points()
    found = found.select(group=group) if hasattr(found, "select") else found.get(group, ())
    result = {}
    for ep in found:
        if NAME.fullmatch(ep.name):
            result.setdefault(ep.name, []).append(ep)
    return result


def load_unique(group, name):
    matches = entries(group).get(name, [])
    if len(matches) != 1:
        raise ExtensionError("Expected exactly one installed %s:%s, found %d" %
                             (group, name, len(matches)))
    try:
        value = matches[0].load()()
    except Exception as e:
        raise ExtensionError("Cannot load %s:%s: %s" % (group, name, e)) from e
    if getattr(value, "api_version", None) != API_VERSION or getattr(value, "name", None) != name:
        raise ExtensionError("Incompatible extension API/name: " + name)
    return value
