#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Build a reproducible stdlib-only host runtime zip; never bundle site-packages.

The zip runs on the execution node. It is NOT mounted into the Common Base.
"""
from pathlib import Path
import argparse
import io
import zipfile

ROOT = Path(__file__).resolve().parents[1]
MODULES = ["__init__.py", "util.py", "artifacts.py", "runtime.py", "ramble_adapter.py",
           "image_store.py", "cer/__init__.py", "cer/recording.py", "reproducibility_rules.py", "contracts.py", "preparation.py"]


def build():
    source = ROOT / "src/benchpark_container"
    data = {"__main__.py": b"from bpce_node.runtime import main\nraise SystemExit(main())\n"}
    modules = MODULES + [str(p.relative_to(source)) for p in sorted((source / "backends").rglob("*.py"))]
    data.update({"bpce_node/" + n: (source / n).read_bytes() for n in modules})
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED) as z:
        for name in sorted(data):
            info = zipfile.ZipInfo(name, (2020, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            z.writestr(info, data[name])
    return out.getvalue()


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--check", action="store_true")
    args = p.parse_args()
    target = ROOT / "src/benchpark_container/resources/runtime.pyz"
    expected = build()
    if args.check:
        if not target.is_file() or target.read_bytes() != expected:
            raise SystemExit("Runtime resources are stale; run tools/build_runtime.py")
    else:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(expected)
    print("runtime bundle:", target)


if __name__ == "__main__":
    main()
