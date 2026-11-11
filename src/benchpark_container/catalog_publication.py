# SPDX-License-Identifier: Apache-2.0
"""Optional setup publication through the existing Catalog implementation.

The runtime bundle only launches a separately frozen HOST helper. It neither
imports mutable installed Extension code nor brings YAML/pip into a container.
"""
import json
from pathlib import Path
import sys

from .util import ValidationError, atomic_json, identity, safe_name, sha256, strict


def freeze_publication(declaration):
    strict(declaration, ("catalog", "name", "release"), "image_publish",
                       ("catalog", "name", "release"))
    from .catalog.config import read_config
    from .catalog.manager import fixed_release
    for key in ("catalog", "name"):
        safe_name(declaration[key], "image_publish." + key)
    fixed_release(declaration["release"])
    config = read_config()
    alias = declaration["catalog"]
    if alias not in config["catalogs"]:
        raise ValidationError("image_publish needs a declared Catalog location in BPCE_CONFIG: " + alias)
    # The declared directory may be absent: only setup initializes it.
    return validate_publication(dict(declaration, root=config["catalogs"][alias]))


def validate_publication(value):
    strict(value, ("catalog", "root", "name", "release"), "frozen image_publish",
                  ("catalog", "root", "name", "release"))
    for key in ("catalog", "name", "release"):
        safe_name(value[key], "image_publish." + key)
    if value["release"].lower() in ("latest", "default", "none"):
        raise ValidationError("Fixed publication release required")
    root = value["root"]
    if (not isinstance(root, str) or not Path(root).is_absolute()
            or ".." in Path(root).parts or any(c in root for c in "\x00\r\n")):
        raise ValidationError("Publication root must be an absolute site path")
    return value


def publish(spec, resources, image, work, timeout):
    from .contracts import execution_tools
    from .image_build import run_logged
    if image.kind != "sif" or image.details.get("overlay"):
        raise ValidationError("Setup publication currently requires a plain SIF result")
    declaration = spec["image_publish"]
    helper = Path(resources) / "catalog_publish.pyz"
    if not helper.is_file() or spec["resources"].get(helper.name) != sha256(helper):
        raise ValidationError("Frozen Catalog host helper is missing/changed")
    payload = {"schema_version": 1, "publication": declaration,
               "source": image.reference, "sha256": image.details["sif_sha256"],
               "platform": spec["base"]["platform"], "accelerator": spec["runtime"]["gpu"],
               "runtime": spec["runtime"]["runtime"], "tools": execution_tools(spec)}
    request = Path(work) / "catalog-request.json"
    response = Path(work) / "catalog-response.json"
    atomic_json(request, payload)
    # A frozen host helper uses the already-required host PyYAML dependency.
    # No installed benchpark_container module is imported by that helper.
    import os
    env = dict(os.environ)
    env.pop("PYTHONPATH", None)
    env["PYTHONNOUSERSITE"] = "1"
    run_logged([spec["runtime"]["worker_python"], str(helper), str(request), str(response)],
               Path(work) / "catalog-publication.log", timeout, env)
    result = json.loads(response.read_text())
    if result.get("request_sha256") != identity(payload):
        raise ValidationError("Catalog response request mismatch")
    base = result["base"]
    if (base.get("kind") != "sif" or base.get("sif_sha256") != image.details["sif_sha256"]
            or base.get("platform") != payload["platform"] or base.get("tools") != payload["tools"]):
        raise ValidationError("Catalog response image mismatch")
    return base, result


def host_main(argv=None):
    """Inside catalog_publish.pyz only; delegates all registration to Catalog."""
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 2:
        raise ValidationError("Expected publication request and response paths")
    request = json.loads(Path(argv[0]).read_text())
    strict(request, ("schema_version", "publication", "source", "sha256", "platform",
                     "accelerator", "runtime", "tools"), "publication request",
                    ("schema_version", "publication", "source", "sha256", "platform",
                     "accelerator", "runtime", "tools"))
    if request["schema_version"] != 1:
        raise ValidationError("Unsupported publication request")
    pub = request["publication"]
    strict(pub, ("catalog", "root", "name", "release"), "publication", ("catalog", "root", "name", "release"))
    root = Path(pub["root"])
    if not root.is_absolute() or root.is_symlink():
        raise ValidationError("Invalid publication root")
    from .catalog.config import locked
    from .catalog.manager import Catalog, init_catalog
    root.parent.mkdir(parents=True, exist_ok=True)
    with locked(root.parent / (".bpce-catalog-init-" + identity(str(root)) + ".lock")):
        if not root.exists():
            init_catalog(root, pub["catalog"])
    catalog = Catalog(root)
    source = Path(request["source"])
    if source.is_symlink() or not source.is_file() or sha256(source) != request["sha256"]:
        raise ValidationError("Publication input is missing/changed")
    manifest = {"schema_version": 1, "name": pub["name"], "release": pub["release"],
                "artifacts": [{"kind": "sif", "uri": source.as_uri(), "sha256": request["sha256"],
                               "platform": request["platform"], "accelerator": request["accelerator"],
                               "runtimes": [request["runtime"]], "tools": request["tools"]}]}
    # Existing manager supplies locking, content retention, immutable release
    # validation, and atomic entry publication. No parallel Catalog is created.
    result = catalog.register(manifest)
    entry = result["entry"]
    base = dict(catalog.image(entry["artifacts"][0]), accelerator=request["accelerator"],
                logical_name=entry["name"], release=entry["release"])
    atomic_json(argv[1], {"schema_version": 1, "request_sha256": identity(request),
                         "status": result["status"], "catalog_id": entry["catalog_id"],
                         "name": entry["name"], "release": entry["release"],
                         "entry_sha256": entry["entry_sha256"], "base": base})
    return 0
