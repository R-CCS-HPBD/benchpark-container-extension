# SPDX-License-Identifier: Apache-2.0
"""Small stdlib-only runtime contract; no Benchpark, package or workload logic."""
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Mapping
import os
import re
import shutil
import subprocess

from ..util import ValidationError

API_VERSION = 1
GPU_ENV = ("CUDA_VISIBLE_DEVICES", "ROCR_VISIBLE_DEVICES", "HIP_VISIBLE_DEVICES")


@dataclass(frozen=True)
class ImageRef:
    kind: str
    reference: str
    identity: str
    details: Mapping = field(default_factory=dict)

    def __post_init__(self):
        object.__setattr__(self, "details", MappingProxyType(dict(self.details)))

    def __str__(self):
        return self.reference

    def __fspath__(self):
        if self.kind != "sif":
            raise TypeError("An OCI image is not a local file")
        return self.reference


@dataclass(frozen=True)
class Mount:
    source: str
    target: str
    readonly: bool

    def __post_init__(self):
        for name, value in (("source", self.source), ("target", self.target)):
            if (not isinstance(value, str) or not PurePosixPath(value).is_absolute()
                    or any(c in value for c in ':,\n\r\x00')
                    or '..' in PurePosixPath(value).parts):
                raise ValidationError("Invalid mount " + name + ": " + repr(value))
        if not isinstance(self.readonly, bool):
            raise ValidationError("Mount readonly must be boolean")


@dataclass(frozen=True)
class ExecutionRequest:
    image: ImageRef
    mounts: tuple
    environment: Mapping
    workdir: str
    command: tuple
    accelerator: str = "none"

    def __post_init__(self):
        object.__setattr__(self, "mounts", tuple(self.mounts))
        object.__setattr__(self, "command", tuple(self.command))
        object.__setattr__(self, "environment", MappingProxyType(dict(self.environment)))
        if (not self.command or any(not isinstance(v, str) or '\x00' in v for v in self.command)
                or not self.command[0] or self.command[0].startswith('-')):
            raise ValidationError("A nonempty executable argv is required")
        if (not isinstance(self.workdir, str) or not self.workdir.startswith('/')
                or '..' in PurePosixPath(self.workdir).parts
                or any(c in self.workdir for c in '\n\r\x00')):
            raise ValidationError("workdir must be an absolute container path")
        for key, value in self.environment.items():
            if not isinstance(key, str) or not re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', key):
                raise ValidationError("Invalid environment name: " + repr(key))
            if not isinstance(value, str) or any(c in value for c in '\n\r\x00'):
                raise ValidationError("Invalid environment value: " + key)
        if not all(isinstance(m, Mount) for m in self.mounts):
            raise ValidationError("ExecutionRequest.mounts must contain Mount values")


@dataclass(frozen=True)
class RuntimeCapabilities:
    image_kinds: frozenset = frozenset({"sif", "oci"})
    accelerators: frozenset = frozenset({"none", "nvidia", "amd"})
    readonly_mounts: bool = True
    working_directory: bool = True
    immutable_image: bool = True
    readonly_overlay_formats: frozenset = frozenset()
    image_build_formats: frozenset = frozenset()


def oci_reference(base):
    if base.get("kind") != "oci":
        raise ValidationError("This backend requires a pinned OCI image; SIF is unsupported")
    uri = base.get("uri", "")
    if uri.startswith("docker://"):
        uri = uri[len("docker://"):]
    if (not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9._:/-]*@sha256:[0-9a-f]{64}', uri)
            or '://' in uri):
        raise ValidationError("OCI image must be repository@sha256:<64 lowercase hex>; mutable tags are not allowed")
    return uri


class RuntimeBackend:
    name = ""
    api_version = API_VERSION
    capabilities = RuntimeCapabilities()
    preferred_image_kinds = ("sif", "oci")

    @classmethod
    def validate_config(cls, settings):
        if settings.get("backend_options", {}):
            raise ValidationError(cls.name + " has no backend_options")

    def __init__(self, settings):
        # Import lazily: configuration validation consults the backend registry.
        from ..contracts import runtime_settings
        self.settings = runtime_settings(settings)
        if self.settings["runtime"] != self.name:
            raise ValidationError("Backend class does not match the declared runtime")
        self.executable = shutil.which(self.settings["executable"])
        if not self.executable:
            raise ValidationError("Declared runtime executable was not found on the execution node: " + self.settings["executable"])
        self.executable = os.path.abspath(self.executable)
        self.cache = Path(self.settings["image_cache"]).resolve()
        self.events = []
        self.attempt = None

    def attach_attempt(self, attempt):
        self.attempt = Path(attempt)

    def host_env(self):
        env = dict(os.environ)
        for key in list(env):
            if key.startswith(("APPTAINER_", "APPTAINERENV_", "SINGULARITY_", "SINGULARITYENV_")):
                env.pop(key)
        return env

    def version(self):
        result = subprocess.check_output([self.executable, "--version"], text=True,
                                         env=self.host_env(), timeout=30).strip()
        if self.name not in result.lower():
            raise ValidationError("Runtime version output does not identify " + self.name + ": " + result)
        return result

    def observe_runtime(self):
        return {"name": self.name, "version": self.version(), "executable": self.executable,
                "backend_api_version": self.api_version}

    def resolve_image(self, base, timeout):
        raise NotImplementedError

    def resolve_local_image(self, base, timeout):
        raise ValidationError("This backend has not declared offline image acquisition")

    def load_prepared_image(self, base, prepared, timeout):
        raise ValidationError("This backend has not declared prepared-image execution")

    def image_build_command(self, output, definition, *, fakeroot):
        raise ValidationError("Selected backend does not support image construction")

    def check_built_image(self, path, platform, log, timeout):
        raise ValidationError("Selected backend does not support built-image validation")

    def observe_image(self, image):
        return {"kind": image.kind, "identity": image.identity, **dict(image.details)}

    def validate_request(self, request):
        if isinstance(request.image, ImageRef) and request.image.details.get("overlay") and request.image.details["overlay"].get("format") not in self.capabilities.readonly_overlay_formats:
            raise ValidationError("Backend does not support the requested read-only Overlay")
        if request.accelerator not in self.capabilities.accelerators:
            raise ValidationError("Backend does not support the requested accelerator")
        if not self.capabilities.immutable_image:
            raise ValidationError("A runtime must support immutable image identity")
        if any(m.readonly for m in request.mounts) and not self.capabilities.readonly_mounts:
            raise ValidationError("Backend does not support read-only mounts")
        if not self.capabilities.working_directory:
            raise ValidationError("Backend does not support the requested working directory")
        if len({m.target for m in request.mounts}) != len(request.mounts):
            raise ValidationError("Duplicate mount targets are not allowed")

    def execution_binding(self, request):
        self.applied_binding = {
            "schema_version": 1, "runtime": self.name,
            "accelerator": request.accelerator,
            "device_visibility": {k: request.environment[k] for k in GPU_ENV if k in request.environment},
            "mount_count": len(request.mounts),
            "basis": "backend-encoded-request", "device_use_verified": False,
        }
        return dict(self.applied_binding)

    def build_command(self, request):
        raise NotImplementedError

    def argv(self, image, scratch, inputs, mounts, command, environment=None, pwd="/bpce/work"):
        """Compatibility adapter used by the unchanged preparation algorithm.

        The host HOME is intentionally not exposed to contained runtimes.
        Give the workload a private writable home/cache/tmp under the
        per-attempt scratch bind instead. Explicit experiment environment
        values still win.
        """
        scratch = Path(scratch).resolve()
        private_cache = scratch / "cache"
        private_tmp = scratch / "tmp"
        for path in (private_cache, private_tmp):
            path.mkdir(parents=True, exist_ok=True)

        all_mounts = [Mount(str(scratch), "/bpce", False)]
        if Path(inputs).is_dir():
            all_mounts.append(Mount(str(Path(inputs).resolve()), "/bpce/inputs", True))
        all_mounts.extend(Mount(m["resolved_source"], m["target"], m["readonly"]) for m in mounts)
        site_mounts = self.settings.get('backend_options', {}).get('site_mounts', [])
        if site_mounts:
            from ..contracts import validate_targets
            validate_targets(list(mounts) + site_mounts, concrete=True)
        for entry in site_mounts:
            source = Path(entry['source'])
            if not source.exists(): raise ValidationError('Required site mount is missing: '+str(source))
            all_mounts.append(Mount(str(source.resolve()), entry['target'], entry['readonly']))
        prefixes = getattr(self, "prepared_prefixes", ())
        for mount in prefixes:
            if (not isinstance(mount, Mount) or not mount.readonly
                    or mount.target not in ("/bpce/tools", "/bpce/python")):
                raise ValidationError("Invalid prepared internal prefix mount")
        all_mounts.extend(prefixes)
        env = {k: str(v) for k, v in (environment or {}).items()}
        env.setdefault("XDG_CACHE_HOME", "/bpce/cache")
        env.setdefault("TMPDIR", "/bpce/tmp")
        env.update({k: os.environ[k] for k in GPU_ENV if k in os.environ})
        return self.build_command(ExecutionRequest(image, tuple(all_mounts), env, pwd,
                    tuple(command), self.settings.get("gpu", "none")))

    @contextmanager
    def command_scope(self, command):
        yield

    def close(self):
        """Release runtime-owned resources, never results, caches or user files."""


@contextmanager
def command_scope(runtime, command):
    """Legacy third-party test adapters without a lifecycle remain usable."""
    scope = getattr(runtime, "command_scope", None)
    if scope is None:
        yield
    else:
        with scope(command):
            yield


@dataclass(frozen=True)
class PreparedCommand:
    """An argv valid only inside its context; cleanup belongs to that context."""
    argv: tuple
    host_environment: Mapping


@contextmanager
def prepared_command(runtime, command):
    """Adapt legacy backends without exposing in-place argv mutation to callers."""
    private = list(command)
    with command_scope(runtime, private):
        yield PreparedCommand(tuple(private), MappingProxyType(dict(runtime.host_env())))
