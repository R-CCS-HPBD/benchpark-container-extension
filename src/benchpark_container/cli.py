# SPDX-License-Identifier: Apache-2.0
"""Read/export per-attempt CER. Never guess a missing run's actual conditions."""
import json
from pathlib import Path
from .util import identity, atomic_json, ValidationError, sha256


def command_descriptor():
    from benchpark_integration.api import CommandDescriptor
    return CommandDescriptor(name="cer", api_version=2,
        help="Inspect recorded concrete container runs", setup_parser=setup_parser, handler=command)


def setup_parser(parser):
    sub = parser.add_subparsers(dest="cer_action", required=True)
    for verb in ("show", "validate"):
        p = sub.add_parser(verb); p.add_argument("record", type=Path)
    p = sub.add_parser("list"); p.add_argument("root", type=Path)
    p = sub.add_parser("diff"); p.add_argument("left", type=Path); p.add_argument("right", type=Path)
    for verb in ("export", "write"):
        p = sub.add_parser(verb, help="Export an existing record; does not reconstruct unobserved runs")
        p.add_argument("record", type=Path); p.add_argument("--output", type=Path, required=True)


def load_record(path):
    path = Path(path)
    if path.is_dir():
        path = path / ("cer.json" if (path / "cer.json").is_file() else "started.json")
    text = path.read_text(encoding="utf-8")
    if path.suffix in (".yaml", ".yml"):
        import yaml
        data = yaml.safe_load(text)
        # No silent promotion to the new schema or claim of equivalent provenance.
        from .cer.legacy import ContainerExecutionRecord
        ContainerExecutionRecord.from_dict(data)
        return {"schema": "legacy", "record": data}
    data = json.loads(text)
    if data.get("schema_version") != 2:
        raise ValidationError("Unknown CER schema")
    required = {"kind", "run_id", "attempt_id", "condition_id", "resolved", "observed", "result", "status"}
    if required - set(data) or data["kind"] != "benchpark-container-execution-record":
        raise ValidationError("Incomplete/unknown CER structure")
    if "record_sha256" in data:
        body = dict(data); h = body.pop("record_sha256")
        if identity(body) != h:
            raise ValidationError("CER record checksum mismatch")
    elif data.get("status") not in ("RUNNING",):
        raise ValidationError("Unsealed final CER")
    return data


def verify_recorded_files(record_path, data):
    """Verify bytes named by files[] inside a sealed schema-v2 CER attempt."""
    path=Path(record_path)
    if path.is_dir():
        path=path / ("cer.json" if (path / "cer.json").is_file() else "started.json")
    root=path.parent.resolve()
    checked=0
    for rel, meta in (data.get("files") or {}).items():
        rel_path=Path(rel)
        if rel_path.is_absolute() or ".." in rel_path.parts:
            raise ValidationError("Unsafe CER file path: " + str(rel))
        target=(root / rel_path).resolve()
        if root not in target.parents and target != root:
            raise ValidationError("CER file escapes attempt directory: " + str(rel))
        if not target.is_file() or target.is_symlink():
            raise ValidationError("CER recorded file missing/not regular: " + str(rel))
        if target.stat().st_size != int(meta.get("bytes", -1)):
            raise ValidationError("CER recorded file size mismatch: " + str(rel))
        if sha256(target) != meta.get("sha256"):
            raise ValidationError("CER recorded file checksum mismatch: " + str(rel))
        checked += 1
    return checked


def differences(left, right, path=""):
    if isinstance(left, dict) and isinstance(right, dict):
        result = []
        for key in sorted(set(left) | set(right)):
            if key not in left or key not in right:
                result.append({"path": path + "/" + key, "left": left.get(key), "right": right.get(key)})
            else:
                result += differences(left[key], right[key], path + "/" + key)
        return result
    return [] if left == right else [{"path": path, "left": left, "right": right}]


def command(args):
    try:
        action = args.cer_action
        if action == "list":
            paths = sorted(args.root.rglob("started.json"))
            result = []
            for path in paths:
                target = path.parent / "cer.json"
                record = load_record(target if target.exists() else path)
                result.append({"path": str(target if target.exists() else path),
                    "run_id": record["run_id"], "condition_id": record["condition_id"],
                    "status": record["status"] if target.exists() else "INCOMPLETE"})
        elif action == "diff":
            result = {"differences": differences(load_record(args.left), load_record(args.right)),
                      "equivalence": "not-classified"}
        else:
            result = load_record(args.record)
            if action in ("export", "write"):
                atomic_json(args.output, result)
                result = {"exported": str(args.output), "source_unchanged": True}
            elif action == "validate":
                legacy = result.get("schema") == "legacy"
                checked = None if legacy else verify_recorded_files(args.record, result)
                result = {"valid_record_structure": True, "scientific_equivalence": "not-assessed",
                          "legacy": legacy, "recorded_files_verified": checked}
        print(json.dumps(result, indent=2, ensure_ascii=False))
        return 0
    except (ValueError, OSError, KeyError) as e:
        import sys
        print("CER error: " + str(e), file=sys.stderr)
        return 2
