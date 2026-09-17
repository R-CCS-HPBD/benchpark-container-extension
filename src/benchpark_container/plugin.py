# SPDX-License-Identifier: Apache-2.0
"""Lightweight descriptors; no pip, runtime or repository activity on import."""
def describe():
    from benchpark_integration.api import ExtensionDescriptor, OptionSpec
    return ExtensionDescriptor(
        name="container", api_version=2,
        options=(
            OptionSpec("container_runtime", "default", "Runtime from the initialized System capabilities"),
            OptionSpec("container_image", "default", "Catalog image name override (requires container_release)"),
            OptionSpec("container_release", "default", "Fixed release of the selected image"),
        ), resolver=resolve, required_variants={"package_manager": "user-managed"},
    )


def resolve(context):
    from .resolver import resolve
    return resolve(context)
