# SPDX-License-Identifier: Apache-2.0
from .sif import SIFRuntimeBackend


class SingularityBackend(SIFRuntimeBackend):
    name = "singularity"
    env_prefix = "SINGULARITY_"
    cache_namespace = "singularity"
