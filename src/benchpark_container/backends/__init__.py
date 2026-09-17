# SPDX-License-Identifier: Apache-2.0
"""Runtime extension point. New backends require implementation + registry + tests."""
from .base import ExecutionRequest, ImageRef, Mount, RuntimeBackend, RuntimeCapabilities
from .registry import create_backend, backend_class

__all__ = ["ExecutionRequest", "ImageRef", "Mount", "RuntimeBackend", "RuntimeCapabilities",
           "create_backend", "backend_class"]
