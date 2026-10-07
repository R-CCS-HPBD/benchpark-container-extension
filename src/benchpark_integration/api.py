# SPDX-License-Identifier: Apache-2.0
"""External feature contract v2 (retained from v0.5.2); NOT a Benchpark Core module."""
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Tuple
from types import MappingProxyType
import hashlib
import json

API_VERSION = 2


class ExtensionError(ValueError):
    """A selected extension or its declarative data is invalid."""


def plain(value):
    """Deep-copy JSON data, rejecting objects, NaN and non-string mapping keys."""
    if isinstance(value, Mapping):
        if not all(isinstance(k, str) for k in value):
            raise ExtensionError("Extension data must use string keys")
        return {k: plain(v) for k, v in value.items()}
    if isinstance(value, (tuple, list)):
        return [plain(v) for v in value]
    if value is None or type(value) in (str, bool, int, float):
        try:
            json.dumps(value, allow_nan=False)
        except ValueError as e:
            raise ExtensionError(str(e)) from e
        return value
    raise ExtensionError("Non-JSON extension data: " + type(value).__name__)


def readonly(value):
    if isinstance(value, Mapping):
        return MappingProxyType({k: readonly(v) for k, v in value.items()})
    if isinstance(value, (tuple, list)):
        return tuple(readonly(v) for v in value)
    return value


def digest(value):
    return hashlib.sha256(json.dumps(plain(value), sort_keys=True,
        separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()


@dataclass(frozen=True)
class OptionSpec:
    name: str
    default: Any
    description: str = ""
    kind: str = "string"
    choices: Tuple[Any, ...] = ()
    multi: bool = False


@dataclass(frozen=True)
class ExtensionDescriptor:
    name: str
    api_version: int
    options: Tuple[OptionSpec, ...]
    resolver: Callable
    # Required native options, negotiated ONCE before concretization.
    required_variants: Mapping = field(default_factory=dict)
    requires_system: bool = True
    requires_declaration: bool = True


@dataclass(frozen=True)
class PreparationContext:
    """Read-only request data; no live Benchpark spec or repository object."""
    name: str
    selector: str
    variants: Mapping
    defaults: Mapping
    settings: Mapping
    source_root: str

    def __post_init__(self):
        for key in ("variants", "defaults", "settings"):
            object.__setattr__(self, key, readonly(plain(getattr(self, key))))


@dataclass(frozen=True)
class PreparationResult:
    overrides: Mapping = field(default_factory=dict)
    provenance: Mapping = field(default_factory=dict)


@dataclass(frozen=True)
class PreparerDescriptor:
    name: str
    api_version: int
    prepare: Callable
    inactive_value: str = "none"


@dataclass(frozen=True)
class CommandDescriptor:
    name: str
    api_version: int
    help: str
    setup_parser: Callable
    handler: Callable


@dataclass(frozen=True)
class ResolutionContext:
    name: str
    explicit: Mapping
    variants: Mapping
    system: Mapping
    requirements: Mapping
    source_root: str
    provenance: Mapping = field(default_factory=dict)

    def __post_init__(self):
        for key in ("explicit", "variants", "system", "requirements", "provenance"):
            object.__setattr__(self, key, readonly(plain(getattr(self, key))))


@dataclass(frozen=True)
class ResourceSpec:
    """A fixed input copied to .benchpark-extensions/resources/<target>."""
    target: str
    sha256: str
    source: str = ""
    text: str = ""
    executable: bool = False


@dataclass(frozen=True)
class SoftwareProvider:
    """An exclusive software-section contribution, not an alternate PM getter.

    Native options are fixed by ExtensionDescriptor.required_variants before
    concretization. Active helper dependencies must not be silently discarded.
    """
    section: Mapping


@dataclass(frozen=True)
class ConfigurationContribution:
    owner: str
    payload: Mapping
    variables: Mapping = field(default_factory=dict)
    environment: Mapping = field(default_factory=dict)
    modifiers: Tuple[Mapping, ...] = ()
    # Optional provider; helper/host software must be reconciled explicitly.
    software_provider: SoftwareProvider | None = None
    resources: Tuple[ResourceSpec, ...] = ()
    # These repositories are workspace-local, never site-scope additions.
    modifier_repositories: Tuple[str, ...] = ()
    overwrite_policy: str = "fail"
    # Optional snapshot-relative Ramble application repositories.
    application_repositories: Tuple[str, ...] = ()
