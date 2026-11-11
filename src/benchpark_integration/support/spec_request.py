# SPDX-License-Identifier: Apache-2.0
"""Spec selection/preparation outside Core; no import of Core or mutable handles.

The broker receives plain request data and two narrow Core-owned operations.
It never reimplements native concretization, parses the user's spec again, or
changes a native class's variant declarations. Runtime/Catalog policy remains
in the existing feature descriptors and prepare_request implementation.
"""
from copy import deepcopy

from ..api import ExtensionError, plain
from ..repository_provider import select_repository
from .options import prepare_request


def concretize_request(context):
    """Return a concrete native object via the Core port, or None if unhandled.

    Missing/malformed providers or invalid selected requests propagate errors.
    No exception path may fall through to an unintended native software build.
    The original abstract Spec is not mutated, including on a failed request.
    """
    name = context["name"]
    variants = plain(context["variants"])
    routes = set(context["routes"])
    binding = context.get("repository_binding")
    required = set(binding["features"]) if binding is not None else set()
    if not isinstance(name, str) or not name:
        raise ExtensionError("Named experiment required at Spec boundary")
    if not routes or not routes <= set(variants) | required:
        raise ExtensionError("Spec routes must refer to explicit options or bound features")
    for feature in required:
        if list(variants.get(feature, [])) != [True]:
            raise ExtensionError("Qualified repository requires explicit +" + feature)
    # The original explicit request is the execution authority.  Repository
    # provenance must never synthesize a feature, even on a qualified name.
    effective = dict(variants)
    definition, build = context["definition"], context["build"]
    if not callable(definition) or not callable(build):
        raise ExtensionError("Spec boundary requires definition/build ports")

    features = sorted(k for k in routes
                      if len(effective[k]) == 1 and effective[k][0] is True)
    # Qualified binding already attests exact namespace and owning checkout;
    # only the legacy explicit-feature route uses a bare-name selection.
    if binding is not None:
        selection = deepcopy(binding)
    elif features:
        selection = select_repository({"kind": "experiments", "name": name,
                                       "features": features})
    else:
        selection = None
    schema = plain(definition(deepcopy(selection)))
    native_names = set(schema["native_names"])
    settings = schema["settings"]
    declared = set(schema["declared_features"])
    keys = routes - (native_names - set(settings))
    disabled = {k: v for k, v in variants.items()
                if k in declared and k not in native_names and k not in keys
                and len(v) == 1 and v[0] is False}
    if not keys and not disabled:
        return None  # Native-owned option; leave the native constructor alone.

    if keys:
        prepared = prepare_request({
            "name": name, "variants": effective,
            "native_names": schema["native_names"], "defaults": schema["defaults"],
            "settings": settings, "declared_features": schema["declared_features"],
            "source_root": schema["source_root"], "native_owner_priority": True,
        })
    else:
        prepared = {"native": {k: v for k, v in variants.items() if k not in disabled},
                    "extension_variants": {}, "state": {}}
    extra = dict(disabled, **prepared["extension_variants"])
    state = deepcopy(prepared["state"])
    if selection is not None:
        state["repository_selection"] = deepcopy(selection)
    if binding is not None:
        # No repository feature is implicit; preserve the CLI request.
        state["repository_implicit_features"] = []
        state["extension_explicit"] = deepcopy(variants)
    # The callback invokes the unchanged ConcreteExperimentSpec exactly once.
    result = build(prepared["native"], extra, state, deepcopy(selection))
    if result is None:
        raise ExtensionError("Native build port returned no result for a handled request")
    return result
