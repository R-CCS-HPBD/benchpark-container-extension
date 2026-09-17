# SPDX-License-Identifier: Apache-2.0
"""Load the workspace's staged helper; never depend on module __file__."""
import importlib
from pathlib import Path
import sys
from ramble.modkit import *


class BpceExecution(BasicModifier):
    name = "bpce-execution"
    mode("standard", description="External Common Base + additional requirements + CER")
    default_mode("standard")
    executable_modifier("wrap_benchmark")

    def wrap_benchmark(self, executable_name, executable, app_inst=None):
        resources = Path(self._file_path).resolve().parents[2]
        bundle = resources / "runtime.pyz"
        if not bundle.is_file():
            raise RuntimeError("Staged runtime helper is missing: " + str(bundle))
        # A process may set up two workspaces; never reuse another workspace's
        # bpce_node modules silently. Ramble setup executes these hooks serially.
        old = sys.modules.get("bpce_node")
        if old is not None and not str(getattr(old, "__file__", "")).startswith(str(bundle) + "/"):
            for name in list(sys.modules):
                if name == "bpce_node" or name.startswith("bpce_node."):
                    del sys.modules[name]
        sys.path.insert(0, str(bundle))
        try:
            module = importlib.import_module("bpce_node.ramble_adapter")
            return module.wrap_executable(self, executable_name, executable, app_inst)
        finally:
            sys.path.remove(str(bundle))
