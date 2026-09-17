# SPDX-License-Identifier: Apache-2.0
"""Test adapter only: old doubles -> documented generation port.

No production Core or plugin imports this file. Assertions retain the old
functional semantics while the native Experiment object no longer crosses Core.
"""
from benchpark_integration.support.generation import RambleGeneration


def generation_port(experiment_double):
    generation = RambleGeneration(getattr(experiment_double, "extension_contributions", ()))
    helpers = tuple(h.compute_package_section for h in experiment_double.helpers)
    class Port:
        def software(self, native):
            return generation.software(native, helpers)
        def finish(self, data):
            return generation.finish(data)
    return Port()
