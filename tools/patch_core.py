#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Create/check/apply a *diff* against a local Benchpark checkout.

No whole-file overlay. Exact structural anchors fail closed on unknown layouts.
The default writes a patch, never edits the checkout. --apply is explicit.
"""
import argparse
import ast
import difflib
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
API_FILES = tuple(str(p.relative_to(ROOT / "core/files/lib/benchpark"))
                  for p in sorted((ROOT / "core/files/lib/benchpark").rglob("*.py")))


def replace(text, old, new, count=1):
    if text.count(old) != count:
        raise ValueError("Unsupported upstream layout: expected %d occurrence(s) of %r, got %d" %
                         (count, old[:110], text.count(old)))
    return text.replace(old, new)


def transform(name, text):
    if "bpce:" in text or "benchpark.plugins" in text:
        raise ValueError("Core already patched; use a clean checkout: " + name)
    if name == "lib/benchpark/spec.py":
        text = replace(text, "        self._variants = other.variants\n",
            "        self._variants = other.variants\n"
            "        # Generic plugin metadata is data, not an external class.\n"
            "        if hasattr(other, 'plugin_state'):\n"
            "            import copy\n"
            "            self.plugin_state = copy.deepcopy(other.plugin_state)\n")
        text = replace(text, "        return ConcreteExperimentSpec(self)\n", '''        from benchpark import plugins
        # Preserve the original anonymous-spec diagnostic before repository lookup.
        if not self.name:
            return ConcreteExperimentSpec(self)
        # Only explicit request names can activate an installed provider.
        cls = self.object_class
        values = dict(self.variants.items())
        settings = getattr(cls, "extension_request_settings", {})
        declared = getattr(cls, "extension_defaults", {})
        names = {n for group in cls.variants.values() for n in group}
        keys = (set(values) & plugins.available()) - (names - set(settings))
        disabled = {n: v for n, v in values.items()
                    if n in declared and n not in names and v == (False,) and n not in keys}
        if not keys and not disabled:
            return ConcreteExperimentSpec(self)
        prepared = plugins.invoke("request", keys,
            default={"native": {n: v for n, v in values.items() if n not in disabled},
                     "extension_variants": {}, "state": {}},
            name=self.name, variants=values, native_names=sorted(names),
            defaults={n: v.default for when, group in cls.variants.items()
                      if not when.variants for n, v in group.items()},
            settings=settings, declared_features=list(declared),
            source_root=str(pathlib.Path(repo_path.filename_for_object_name(self.name)).resolve().parent))
        request = ExperimentSpec(self)
        request.variants = VariantMap()
        for n, v in prepared["native"].items():
            request.variants[n] = v
        result = ConcreteExperimentSpec(request)
        combined = VariantMap(result.variants)
        for n, v in dict(disabled, **prepared["extension_variants"]).items():
            combined[n] = v
        result._variants = ConcreteVariantMap(combined)
        result.plugin_state = prepared["state"]
        return result
''')
    elif name == "lib/benchpark/cmd/system.py":
        text = replace(text, "        system.write_system_dict(destdir)\n",
            "        system.write_system_dict(destdir)\n"
            "        from benchpark import plugins\n"
            "        settings = getattr(system, 'extension_settings', {})\n"
            "        plugins.invoke('system_saved', set(settings) & plugins.available(), settings=settings, dest=destdir)\n")
    elif name == "lib/benchpark/cmd/experiment.py":
        text = replace(text, "    experiment.system_spec = system_spec\n", '''    experiment.system_spec = system_spec
    from benchpark import plugins
    from pathlib import Path
    plugin_state = getattr(experiment_spec, "plugin_state", {})
    owners = plugin_state.get("extension_selection", ())
    if owners:
        settings = (experiment.get_extension_settings() if hasattr(experiment, "get_extension_settings")
                    else getattr(experiment, "extension_settings", {}))
        experiment.extension_contributions = plugins.invoke("experiment_resolve", owners,
            name=experiment_spec.name, variants=dict(experiment_spec.variants.items()), state=plugin_state,
            settings=settings, system_dir=args.system,
            source_root=str(Path(benchpark.spec.repo_path.filename_for_object_name(experiment_spec.name)).resolve().parent))
''')
        text = replace(text, '        experiment.write_ramble_dict(f"{destdir}/ramble.yaml")\n', '''        experiment.write_ramble_dict(f"{destdir}/ramble.yaml")
        from benchpark.paths import paths
        plugins.invoke("experiment_saved", owners, dest=destdir, system_dir=args.system,
            upstream_root=str(paths.benchpark_root), values=getattr(experiment, "extension_contributions", ()))
''')
    elif name == "lib/benchpark/experiment.py":
        if re.search(r'values=.*["\']container["\']', text) or '_ramble_pm' in text:
            raise ValueError("Legacy Container edits found; use a clean upstream worktree")
        text = replace(text, "    def compute_ramble_dict(self):\n", '''    def compute_ramble_dict(self):
        from benchpark import plugins
        values = getattr(self, "extension_contributions", ())
        generation = plugins.invoke("generation", [v.owner for v in values], values=values)
''')
        text = replace(text, '"software": self.compute_package_section_wrapper(),',
            '"software": (self.compute_package_section_wrapper() if generation is None else\n'
            '                             generation.software(self.compute_package_section_wrapper,\n'
            '                                 tuple(h.compute_package_section for h in self.helpers))),')
        text = replace(text, '        return ramble_dict\n',
            '        return ramble_dict if generation is None else generation.finish(ramble_dict)\n')
    elif name == "lib/benchpark/cmd/setup.py":
        # Some upstream branches emit {spack_env_cmd} in the generated initializer
        # unconditionally, while assigning it only in the Spack branch.  A non-Spack
        # provider must therefore see the neutral empty command.  This is a generic
        # package-manager compatibility fix, not Container-specific behavior.
        if "{spack_env_cmd}" in text and '    spack_env_cmd = ""\n    if "spack" in pkg_manager:\n' not in text:
            text = replace(text, '    if "spack" in pkg_manager:\n',
                '    spack_env_cmd = ""\n    if "spack" in pkg_manager:\n')
        text = replace(text, "    workspace_dir = pathlib.Path(experiments_root) / experiment_id\n", '''    workspace_dir = pathlib.Path(experiments_root) / experiment_id
    # Route before any deletion. Missing selected providers are not native mode.
    from benchpark import plugins
    extension_workspace = plugins.invoke("workspace",
        plugins.owners_at(experiment_src_dir, workspace_dir / "workspace"),
        source=str(experiment_src_dir), output=str(workspace_dir))
''')
        text = replace(text,
            "    symlink_tree(configs_src_dir, ramble_configs_dir, include_fn)\n"
            "    symlink_tree(experiment_src_dir, ramble_configs_dir, include_fn)\n", '''    if extension_workspace is None:
        symlink_tree(configs_src_dir, ramble_configs_dir, include_fn)
        symlink_tree(experiment_src_dir, ramble_configs_dir, include_fn)
    else:
        extension_workspace.stage(ramble_configs_dir, configs_src_dir, symlink_tree, include_fn)
''')
        text = replace(text,
            '    symlink_tree(\n        source_dir / "systems" / "common",\n        ramble_spack_experiment_configs_dir,\n        include_fn,\n    )', '''    if extension_workspace is None:
        symlink_tree(
            source_dir / "systems" / "common",
            ramble_spack_experiment_configs_dir,
            include_fn,
        )
    else:
        extension_workspace.common(source_dir / "systems" / "common",
            ramble_spack_experiment_configs_dir, symlink_tree, include_fn)''')
        text = replace(text,
            '    os.symlink(\n        choice_template,\n        ramble_configs_dir / "execute_experiment.tpl",\n    )', '''    if extension_workspace is None:
        os.symlink(
            choice_template,
            ramble_configs_dir / "execute_experiment.tpl",
        )
    else:
        extension_workspace.template(choice_template, ramble_configs_dir / "execute_experiment.tpl")''')
    elif name == "lib/main.py":
        if "import benchpark.cmd.cer" in text:
            raise ValueError("Existing CER CLI found; use a clean checkout")
        text = replace(text, '    print(helpstr)\n',
            '    print(helpstr)\n'
            '    from benchpark.plugins import print_extension_help\n'
            '    print_extension_help()\n')
        text = replace(text, "    init_commands(subparsers, actions)\n",
            "    init_commands(subparsers, actions)\n"
            "    from benchpark.plugins import register_commands\n"
            "    register_commands(subparsers, actions, sys.argv[1:])\n")
    else:
        raise ValueError("Unknown patch target: " + name)
    ast.parse(text, filename=name)
    return text

TARGETS = ["lib/main.py", "lib/benchpark/spec.py", "lib/benchpark/experiment.py",
           "lib/benchpark/cmd/system.py", "lib/benchpark/cmd/experiment.py",
           "lib/benchpark/cmd/setup.py"]


def make_patch(checkout):
    checkout = Path(checkout).resolve()
    records, parts = [], []
    for rel in TARGETS:
        p = checkout / rel
        if p.is_symlink():
            raise ValueError("Refusing symlinked Core file: " + rel)
        old = p.read_text(encoding="utf-8")
        new = transform(rel, old)
        records.append({"path": rel, "old_sha256": hashlib.sha256(old.encode()).hexdigest(),
                        "new_sha256": hashlib.sha256(new.encode()).hexdigest()})
        parts.extend(difflib.unified_diff(old.splitlines(True), new.splitlines(True),
                                         fromfile="a/" + rel, tofile="b/" + rel))
    for filename in API_FILES:
        rel = "lib/benchpark/" + filename
        if (checkout / rel).exists() or (checkout / rel).is_symlink():
            raise ValueError("New Core filename already exists: " + rel)
        new = (ROOT / "core/files" / rel).read_text(encoding="utf-8")
        parts.extend(difflib.unified_diff([], new.splitlines(True),
                                         fromfile="/dev/null", tofile="b/" + rel))
        records.append({"path": rel, "old_sha256": None,
                        "new_sha256": hashlib.sha256(new.encode()).hexdigest()})
    return "".join(parts), records


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("checkout", type=Path)
    p.add_argument("--output", type=Path, default=Path("benchpark-extension-api.patch"))
    p.add_argument("--apply", action="store_true")
    p.add_argument("--reverse", action="store_true", help="Reverse a previously generated patch, after git apply --check")
    args = p.parse_args()
    try:
        root = args.checkout.resolve()
        if args.reverse:
            patch = args.output.read_text(encoding="utf-8")
            flags = ["--reverse"]
        else:
            modified = subprocess.run(["git", "-C", str(root), "diff", "--name-only", "HEAD"],
                                      capture_output=True, text=True, check=True).stdout.strip()
            if set(modified.splitlines()) & set(TARGETS):
                raise ValueError("Modified Core targets are present; preserve/reconcile those edits before patching")
            output = args.output.resolve()
            metadata = output.with_suffix(".json")
            if metadata == output:
                raise ValueError("Patch output must not use .json (reserved for metadata)")
            if output == root or root in output.parents:
                raise ValueError("Write the review patch outside the Benchpark checkout (--output /path/elsewhere.patch)")
            if output.exists() or metadata.exists() or output.is_symlink() or metadata.is_symlink():
                raise ValueError("Patch/metadata output already exists; choose a new output path")
            patch, records = make_patch(root)
            with output.open("x", encoding="utf-8") as stream:
                stream.write(patch)
            commit = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"], text=True,
                                    capture_output=True).stdout.strip() or None
            with metadata.open("x", encoding="utf-8") as stream:
                json.dump({"api_version": 1, "head": commit, "files": records}, stream, indent=2)
                stream.write("\n")
            flags = []
        cmd = ["git", "-C", str(root), "apply", *flags]
        subprocess.run([*cmd, "--check", "-"], input=patch, text=True, check=True)
        if args.apply:
            subprocess.run([*cmd, "-"], input=patch, text=True, check=True)
            print("Applied checked diff to", root)
        else:
            print("Checked only; checkout unchanged. Patch:", args.output.resolve())
        return 0
    except (ValueError, OSError, subprocess.CalledProcessError) as e:
        print(str(e), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
