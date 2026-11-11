#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Freeze the existing Catalog code for setup-only HOST publication.

Separate from runtime.pyz: this helper uses host PyYAML, an existing dependency.
No installed mutable Extension modules are imported and no payload is injected
into the benchmark container.
"""
import argparse
import io
from pathlib import Path
import zipfile

ROOT = Path(__file__).resolve().parents[1]
MODULES = ["__init__.py", "util.py", "contracts.py", "image_store.py", "environment_snapshot.py",
           "catalog_publication.py", "catalog/__init__.py", "catalog/config.py", "catalog/manager.py",
           "catalog/store.py", "catalog/environments.py"]


def build(root=ROOT):
    source = Path(root) / "src/benchpark_container"
    data = {"__main__.py": b"from bpce_catalog_frozen.catalog_publication import host_main\nraise SystemExit(host_main())\n"}
    data.update({"bpce_catalog_frozen/" + n: (source / n).read_bytes() for n in MODULES})
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name in sorted(data):
            info = zipfile.ZipInfo(name, (2020, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            archive.writestr(info, data[name])
    return out.getvalue()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    args = parser.parse_args()
    path = ROOT / "src/benchpark_container/resources/catalog_publish.pyz"
    expected = build()
    if args.check:
        if not path.is_file() or path.read_bytes() != expected:
            raise SystemExit("Catalog helper is stale; run tools/build_catalog_helper.py")
    else:
        path.write_bytes(expected)
    print("frozen Catalog host helper:", path)


if __name__ == "__main__":
    main()
