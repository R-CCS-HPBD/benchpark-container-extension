# SPDX-License-Identifier: Apache-2.0
"""One internal registration table; no Core entry points or runtime conditionals."""
from .base import API_VERSION, RuntimeBackend
from .apptainer import ApptainerBackend
from .singularity import SingularityBackend
from .docker import DockerBackend
from ..util import ValidationError

BACKENDS = {
    "apptainer": ApptainerBackend,
    "singularity": SingularityBackend,
    "docker": DockerBackend,
}


def backend_class(name):
    try:
        cls = BACKENDS[name]
    except (KeyError, TypeError) as error:
        raise ValidationError("Unknown container runtime: " + repr(name)) from error
    if (not isinstance(cls, type) or not issubclass(cls, RuntimeBackend)
            or cls.name != name or cls.api_version != API_VERSION):
        raise ValidationError("Incompatible runtime backend registration: " + str(name))
    return cls


def create_backend(settings):
    return backend_class(settings["runtime"])(settings)
