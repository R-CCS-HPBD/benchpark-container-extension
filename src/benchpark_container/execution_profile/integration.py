"""Opt-in host-side wrapper for the observed Benchpark concrete_plan API.

No preparation runs here; a prepared binding is required. No existing plan is edited.
"""
from __future__ import annotations

import functools
import importlib
import os
from pathlib import Path

from .binding import bind_plan, load_binding


def wrap_concrete_plan(original):
    @functools.wraps(original)
    def profiled(*args, **kwargs):
        binding_path = os.environ.get("BPCE_EXECUTION_BINDING")
        if not binding_path:
            if os.environ.get("BPCE_MPI_QUALIFIED_ROUTE"):
                raise ValueError("MPI qualified route requires BPCE_EXECUTION_BINDING")
            return original(*args, **kwargs)
        profile, policy, receipt = load_binding(Path(binding_path))
        raw = original(*args, **kwargs)
        # Imported only in the actual integration, not replaced with a test implementation.
        # Use the actual caller package. Portable Runtime/Ramble uses bpce_node;
        # the normal source import uses benchpark_container. The standalone kit
        # uses the explicitly provided installed extension, never its own fake API.
        parent = __package__.rsplit(".", 1)[0] if "." in __package__ else "benchpark_container"
        identity = importlib.import_module(parent + ".util").identity
        validate_resolved = importlib.import_module(parent + ".parallel.contract").validate_resolved
        validate_descriptors = importlib.import_module(parent + ".providers").validate_descriptors
        result = bind_plan(raw, profile, policy, receipt, identity_fn=identity)
# BEGIN BPCE QUALIFIED MPI ROUTE P1 (opt-in)
        route_path = os.environ.get("BPCE_MPI_QUALIFIED_ROUTE")
        if route_path:
            from .mpi_resolution import attach_to_bound_plan, load_selected_route
            route = load_selected_route(Path(route_path))
            result = attach_to_bound_plan(result, raw, profile, policy, receipt, route,
                                          identity_fn=identity)
        # END BPCE QUALIFIED MPI ROUTE P1
        validate_resolved(result["parallel"])
        validate_descriptors(result["runtime"]["providers"])
        return result
    return profiled
