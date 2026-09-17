# SPDX-License-Identifier: Apache-2.0
from .sif import SIFRuntimeBackend


class ApptainerBackend(SIFRuntimeBackend):
    name = "apptainer"
    env_prefix = "APPTAINER_"
