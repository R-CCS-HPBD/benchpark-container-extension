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


def backend_class(name, providers=()):
    from ..providers import validate_descriptors, load_provider
    validate_descriptors(list(providers))
    selected = [d for d in providers if d['kind'] == 'runtime' and d['name'] == name]
    if selected and name in BACKENDS:
        raise ValidationError('External provider shadows built-in runtime: ' + name)
    try:
        cls = load_provider(selected[0]) if selected else BACKENDS[name]
    except (KeyError, TypeError) as error:
        raise ValidationError("Unknown container runtime: " + repr(name)) from error
    if (not isinstance(cls, type) or not issubclass(cls, RuntimeBackend)
            or cls.name != name or cls.api_version != API_VERSION):
        raise ValidationError("Incompatible runtime backend registration: " + str(name))
    return cls


def create_backend(settings):
    return backend_class(settings["runtime"], settings.get("providers", []))(settings)
