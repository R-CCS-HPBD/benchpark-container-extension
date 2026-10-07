#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Create/check/apply a *diff* against a local Benchpark checkout.

No whole-file overlay. Known structural anchors fail closed on unknown layouts.
List presentation hooks use AST validation so formatting is not a dependency.
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


REPOSITORY_CONFIG_TARGET = "lib/benchpark/config.py"
LEGACY_LOADER_SHA256 = 'da85bc922a9dcc7a8164127528528c7e95b44fedc07771df2fc09f71626dd598'
V12_LOADER_SHA256 = '78f4d18ee6e7e64a6cbafcd6a2497583654d95eeb451fff85b212f9f71724ef2'
PRE_LIST_PROVENANCE_LOADER_SHA256 = '0f66a519fa627acac9363fcbc1bc856639cc5f5b54b520602e7754e571eec1a6'
REPOSITORY_CLASS_BEFORE = 'class Repos(ConfigSection):\n    filename = "repos.yaml"\n    name = "repos"\n'
REPOSITORY_CLASS_AFTER = 'class Repos(ConfigSection):\n    filename = "repos.yaml"\n    name = "repos"\n\n    @classmethod\n    def try_load(cls, cfg_dir):\n        result = super().try_load(cfg_dir)\n        from benchpark import plugins\n        result.data = plugins.repository_config(result.data, str(result.path.parent))\n        return result\n'

# Only these known list-function semantics may be replaced. Formatting is not
# part of the contract; arbitrary upstream behavior is never overwritten.
LIST_FUNCTION_REPLACEMENTS = (
    ('''def list_benchmarks(args):
    _print_helper("Benchmarks:" if not args.no_title else None, benchpark_benchmarks())
''', '''def list_benchmarks(args):
    collection = benchpark_benchmarks()
    if args.no_title:
        _print_helper(None, collection)
        return
    native_benchmarks = benchpark_benchmarks(native_only=True)
    native, groups = plugins.repository_list_groups("benchmarks", collection, native_benchmarks)
    _print_helper("Benchmarks:", native)
    for label, items in groups:
        _print_helper(label + " Benchmarks:", items)
'''),
    ('''def list_experiments(args):
    _print_helper(
        (
            ["Experiments - ", "BENCHMARK@.+", "PROGRAMMING_MODEL@.+", "SCALING"]
            if not args.no_title
            else None
        ),
        benchpark_experiments(),
        filter=args.experiment,
    )
''', '''def list_experiments(args):
    collection = benchpark_experiments()
    if args.no_title:
        _print_helper(None, collection, filter=args.experiment)
        return
    native_benchmarks = benchpark_benchmarks(native_only=True)
    native, groups = plugins.repository_list_groups("experiments", collection, native_benchmarks)
    _print_helper(
        ["Experiments - ", "BENCHMARK@.+", "PROGRAMMING_MODEL@.+", "SCALING"],
        native,
        filter=args.experiment,
    )
    for label, items in groups:
        _print_helper(label + " Experiments:", items, filter=args.experiment)
'''),
)


def replace(text, old, new, count=1):
    if text.count(old) != count:
        raise ValueError("Unsupported upstream layout: expected %d occurrence(s) of %r, got %d" %
                         (count, old[:110], text.count(old)))
    return text.replace(old, new)


def read_text_verbatim(path):
    """Preserve source line endings for hashes, review diffs, and round trips."""
    with Path(path).open(encoding="utf-8", newline="") as stream:
        return stream.read()


def transform_list_module(text):
    """Patch only known list functions, without depending on layout whitespace.

    Use AST for validation/location, not unparse for rewriting the module.
    Unrelated functions, comments and whitespace retain their original bytes.
    """
    name = "lib/benchpark/cmd/list.py"

    def unsupported(detail):
        raise ValueError("Unsupported upstream layout: " + name + ": " + detail)

    try:
        tree = ast.parse(text, filename=name)
    except SyntaxError as exc:
        unsupported("invalid Python: " + str(exc))

    imports = [n for n in tree.body if isinstance(n, ast.ImportFrom)
               and n.module == "benchpark.accounting" and n.level == 0]
    if len(imports) != 1:
        unsupported("expected one top-level benchpark.accounting import")
    imported = {a.name for a in imports[0].names if a.asname in (None, a.name)}
    if not {"benchpark_benchmarks", "benchpark_experiments"} <= imported:
        unsupported("missing or aliased accounting functions")
    # Do not insert a second binding or silently clobber a site's plugins name.
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            bound = {a.asname or (a.name if isinstance(node, ast.ImportFrom)
                                 else a.name.split(".")[0]) for a in node.names}
            if "plugins" in bound:
                unsupported("plugins is already imported; inspect existing integration")
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            if any(isinstance(n, ast.Name) and n.id == "plugins"
                   for target in targets for n in ast.walk(target)):
                unsupported("plugins already has a top-level binding")
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.name == "plugins":
            unsupported("plugins already has a top-level definition")
    helpers = [n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == "_print_helper"]
    if len(helpers) != 1:
        unsupported("expected one top-level _print_helper function")

    lines = text.splitlines(keepends=True)
    newline = "\r\n" if "\r\n" in text and "\n" not in text.replace("\r\n", "") else "\n"
    edits = []
    for expected_text, replacement in LIST_FUNCTION_REPLACEMENTS:
        expected = ast.parse(expected_text).body[0]
        matches = [n for n in tree.body if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))
                   and n.name == expected.name]
        if len(matches) != 1:
            unsupported("expected one top-level " + expected.name + " function")
        actual = matches[0]
        if ast.dump(actual, include_attributes=False) != ast.dump(expected, include_attributes=False):
            unsupported("unrecognized function structure: " + expected.name + "; refusing to replace changed behavior")
        edits.append((actual.lineno - 1, actual.end_lineno, replacement.replace("\n", newline)))

    # Insert after the actual accounting import statement, not after a textual
    # closing-parenthesis/blank-line pattern adjacent to _print_helper.
    end = imports[0].end_lineno
    edits.append((end, end, "from benchpark import plugins" + newline))
    for start, end, replacement in sorted(edits, reverse=True):
        lines[start:end] = [replacement]
    result = "".join(lines)
    compile(result, name, "exec")
    return result


def transform(name, text):
    if "bpce:" in text or "benchpark.plugins" in text:
        raise ValueError("Core already patched; use a clean checkout: " + name)
    if name == REPOSITORY_CONFIG_TARGET:
        text = replace(text, REPOSITORY_CLASS_BEFORE, REPOSITORY_CLASS_AFTER)
    elif name == "lib/benchpark/accounting.py":
        text = replace(text, "from benchpark.base_paths import base_paths\n",
                       "from benchpark.base_paths import base_paths\nfrom benchpark import plugins\n")
        text = replace(text, "for x in sorted(os.listdir(experiments_dir)):",
                       'for x, experiments_dir in plugins.repository_entries("experiments", experiments_dir):', count=2)
        text = replace(text, "for exp in sorted(os.listdir(experiments_dir)):",
                       'for exp, experiments_dir in plugins.repository_entries("experiments", source_dir / "experiments"):')
        text = replace(text, 'for x in sorted(os.listdir(source_dir / "systems")):',
                       'for x, systems_dir in plugins.repository_entries("systems", source_dir / "systems"):')
        text = replace(text, 'os.listdir(source_dir / "systems" / x)', 'os.listdir(systems_dir / x)')
        text = replace(text, "def benchpark_benchmarks():\n", "def benchpark_benchmarks(native_only=False):\n")
        marker = 'def benchpark_benchmarks(native_only=False):\n'
        head, tail = text.split(marker, 1)
        tail = replace(
            tail,
            '    for x, experiments_dir in plugins.repository_entries("experiments", experiments_dir):\n',
            '    for x, experiments_dir in plugins.repository_entries(\n'
            '        "experiments", experiments_dir, include_external=not native_only\n'
            '    ):\n',
        )
        text = head + marker + tail
    elif name == "lib/benchpark/cmd/list.py":
        text = transform_list_module(text)
    elif name == "lib/benchpark/spec.py":
        text = replace(text, "        self._variants = other.variants\n",
            "        self._variants = other.variants\n"
            "        # Generic plugin metadata is data, not an external class.\n"
            "        if hasattr(other, 'plugin_state'):\n"
            "            import copy\n"
            "            self.plugin_state = copy.deepcopy(other.plugin_state)\n"
            "        if hasattr(other, 'repository_selection'):\n"
            "            import copy\n"
            "            self.repository_selection = copy.deepcopy(\n"
            "                other.repository_selection\n"
            "            )\n")
        text = replace(text, """class ExperimentSpec(Spec):
    @property
    def experiment_class(self):
        return repo_path.get_obj_class(self.name)

""", """class ExperimentSpec(Spec):
    @property
    def experiment_class(self):
        selection = getattr(self, "repository_selection", None)
        if selection is not None:
            import benchpark.repo
            from benchpark.repo import ObjectTypes
            with benchpark.repo.override_ramble_hardcoded_globals():
                with benchpark.repo.use_repositories(
                        selection["repository"],
                        object_type=ObjectTypes.experiments) as selected_repo:
                    return selected_repo.get_obj_class(self.name)
        return repo_path.get_obj_class(self.name)

    @property
    def experiment_source_root(self):
        selection = getattr(self, "repository_selection", None)
        if selection is not None:
            return selection["object_dir"]
        return str(
            pathlib.Path(
                repo_path.filename_for_object_name(self.name)
            ).resolve().parent
        )

""")
        text = replace(text, "        return ConcreteExperimentSpec(self)\n", '''        from benchpark import plugins
        # Preserve the original anonymous-spec diagnostic before repository lookup.
        if not self.name:
            return ConcreteExperimentSpec(self)
        # Enabled extension features may select another implementation
        # of the same logical benchmark before its class is loaded.
        values = dict(self.variants.items())
        repository_features = sorted(
            name for name, value in values.items()
            if name in plugins.available() and value == (True,)
        )
        selection = plugins.invoke(
            "repository_select",
            repository_features,
            default=None,
            kind="experiments",
            name=self.name,
            features=repository_features,
        )
        if selection is not None:
            self.repository_selection = selection

        cls = self.object_class
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
            source_root=self.experiment_source_root)
        request = ExperimentSpec(self)
        request.variants = VariantMap()
        for n, v in prepared["native"].items():
            request.variants[n] = v
        result = ConcreteExperimentSpec(request)
        combined = VariantMap(result.variants)
        for n, v in dict(disabled, **prepared["extension_variants"]).items():
            combined[n] = v
        result._variants = ConcreteVariantMap(combined)
        state = dict(prepared["state"])
        if selection is not None:
            state["repository_selection"] = selection
        result.plugin_state = state
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
            source_root=experiment_spec.experiment_source_root)
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
           "lib/benchpark/cmd/setup.py", "lib/benchpark/cmd/list.py",
           REPOSITORY_CONFIG_TARGET, "lib/benchpark/accounting.py"]


def make_patch(checkout):
    checkout = Path(checkout).resolve()
    records, parts = [], []
    for rel in TARGETS:
        p = checkout / rel
        if p.is_symlink():
            raise ValueError("Refusing symlinked Core file: " + rel)
        old = read_text_verbatim(p)
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


def make_repository_upgrade_patch(checkout):
    """Extend a known old generic seam without resetting native/site files."""
    checkout = Path(checkout).resolve()
    loader = checkout / "lib/benchpark/plugins.py"
    config = checkout / REPOSITORY_CONFIG_TARGET
    accounting = checkout / "lib/benchpark/accounting.py"
    list_module = checkout / "lib/benchpark/cmd/list.py"
    expected_loader = (ROOT / "core/files/lib/benchpark/plugins.py").read_text(encoding="utf-8")
    for path in (loader, config, accounting, list_module):
        if path.is_symlink():
            raise ValueError("Refusing symlinked Core integration target: " + str(path))
    old_loader = loader.read_text(encoding="utf-8")
    old_config = config.read_text(encoding="utf-8")
    old_accounting = accounting.read_text(encoding="utf-8")
    old_list = read_text_verbatim(list_module)
    config_done = REPOSITORY_CLASS_AFTER in old_config
    accounting_done = 'plugins.repository_entries(' in old_accounting
    list_done = 'plugins.repository_list_groups(' in old_list
    provenance_done = (
        'def benchpark_benchmarks(native_only=False):' in old_accounting
        and 'benchpark_benchmarks(native_only=True)' in old_list
    )
    if old_loader == expected_loader and config_done and accounting_done and list_done and provenance_done:
        return "", []

    old_hash = hashlib.sha256(old_loader.encode()).hexdigest()
    known_loader = old_hash in (
        V12_LOADER_SHA256,
        LEGACY_LOADER_SHA256,
        PRE_LIST_PROVENANCE_LOADER_SHA256,
    ) or old_loader == expected_loader
    if not known_loader:
        raise ValueError("Unknown existing Core loader; do not overwrite local/upstream changes")

    anchors = {"lib/main.py": "register_commands", "lib/benchpark/spec.py": 'plugins.invoke("request"',
               "lib/benchpark/cmd/setup.py": "extension_workspace.stage",
               "lib/benchpark/cmd/experiment.py": "experiment_resolve",
               "lib/benchpark/cmd/system.py": "system_saved"}
    for rel, anchor in anchors.items():
        if anchor not in (checkout / rel).read_text(encoding="utf-8"):
            raise ValueError("Incomplete existing Core integration: " + rel)

    def upgrade_listing_provenance(accounting_text, list_text):
        if 'def benchpark_benchmarks(native_only=False):' not in accounting_text:
            accounting_text = replace(
                accounting_text,
                "def benchpark_benchmarks():\n",
                "def benchpark_benchmarks(native_only=False):\n",
            )
            marker = 'def benchpark_benchmarks(native_only=False):\n'
            head, tail = accounting_text.split(marker, 1)
            tail = replace(
                tail,
                '    for x, experiments_dir in plugins.repository_entries("experiments", experiments_dir):\n',
                '    for x, experiments_dir in plugins.repository_entries(\n'
                '        "experiments", experiments_dir, include_external=not native_only\n'
                '    ):\n',
            )
            accounting_text = head + marker + tail
        if 'benchpark_benchmarks(native_only=True)' not in list_text:
            list_text = replace(
                list_text,
                '    native, groups = plugins.repository_list_groups("benchmarks", collection)\n',
                '    native_benchmarks = benchpark_benchmarks(native_only=True)\n'
                '    native, groups = plugins.repository_list_groups("benchmarks", collection, native_benchmarks)\n',
            )
            list_text = replace(
                list_text,
                '    native, groups = plugins.repository_list_groups("experiments", collection)\n',
                '    native_benchmarks = benchpark_benchmarks(native_only=True)\n'
                '    native, groups = plugins.repository_list_groups("experiments", collection, native_benchmarks)\n',
            )
        return accounting_text, list_text

    changes = []
    if old_hash == V12_LOADER_SHA256:
        if not config_done or not accounting_done:
            raise ValueError("Incomplete v1.2 repository seam; inspect before upgrade")
        changes.append(("lib/benchpark/plugins.py", old_loader, expected_loader))
        if not list_done:
            changes.append(("lib/benchpark/cmd/list.py", old_list, transform("lib/benchpark/cmd/list.py", old_list)))
    elif old_hash == PRE_LIST_PROVENANCE_LOADER_SHA256:
        if not config_done or not accounting_done or not list_done:
            raise ValueError("Incomplete provider-selection repository seam; inspect before upgrade")
        new_accounting, new_list = upgrade_listing_provenance(old_accounting, old_list)
        changes.extend((
            ("lib/benchpark/plugins.py", old_loader, expected_loader),
            ("lib/benchpark/accounting.py", old_accounting, new_accounting),
            ("lib/benchpark/cmd/list.py", old_list, new_list),
        ))
    elif old_hash == LEGACY_LOADER_SHA256:
        if config_done or accounting_done or list_done:
            raise ValueError("Partial or unknown repository seam; inspect before upgrade")
        changes.extend((
            ("lib/benchpark/plugins.py", old_loader, expected_loader),
            (REPOSITORY_CONFIG_TARGET, old_config, transform(REPOSITORY_CONFIG_TARGET, old_config)),
            ("lib/benchpark/accounting.py", old_accounting, transform("lib/benchpark/accounting.py", old_accounting)),
            ("lib/benchpark/cmd/list.py", old_list, transform("lib/benchpark/cmd/list.py", old_list)),
        ))
    elif old_loader == expected_loader and config_done and accounting_done and not list_done:
        changes.append(("lib/benchpark/cmd/list.py", old_list, transform("lib/benchpark/cmd/list.py", old_list)))
    elif old_loader == expected_loader and config_done and accounting_done and list_done and not provenance_done:
        new_accounting, new_list = upgrade_listing_provenance(old_accounting, old_list)
        changes.extend((
            ("lib/benchpark/accounting.py", old_accounting, new_accounting),
            ("lib/benchpark/cmd/list.py", old_list, new_list),
        ))
    else:
        raise ValueError("Unknown existing Core loader; do not overwrite local/upstream changes")

    parts, records = [], []
    for rel, old, new in changes:
        if old == new:
            continue
        parts.extend(difflib.unified_diff(old.splitlines(True), new.splitlines(True),
                     fromfile="a/" + rel, tofile="b/" + rel))
        records.append({"path": rel, "old_sha256": hashlib.sha256(old.encode()).hexdigest(),
                        "new_sha256": hashlib.sha256(new.encode()).hexdigest()})
    return "".join(parts), records


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("checkout", type=Path)
    p.add_argument("--output", type=Path, default=Path("benchpark-extension-api.patch"))
    p.add_argument("--apply", action="store_true")
    p.add_argument("--upgrade-repositories", action="store_true", help="Upgrade a known existing generic repository seam (including v1.2)")
    p.add_argument("--reverse", action="store_true", help="Reverse a previously generated patch, after git apply --check")
    args = p.parse_args()
    try:
        root = args.checkout.resolve()
        if args.reverse:
            patch = read_text_verbatim(args.output)
            flags = ["--reverse"]
        else:
            modified = subprocess.run(["git", "-C", str(root), "diff", "--name-only", "HEAD"],
                                      capture_output=True, text=True, check=True).stdout.strip()
            if not args.upgrade_repositories and set(modified.splitlines()) & set(TARGETS):
                raise ValueError("Modified Core targets are present; preserve/reconcile those edits before patching")
            output = args.output.resolve()
            metadata = output.with_suffix(".json")
            if metadata == output:
                raise ValueError("Patch output must not use .json (reserved for metadata)")
            if output == root or root in output.parents:
                raise ValueError("Write the review patch outside the Benchpark checkout (--output /path/elsewhere.patch)")
            if output.exists() or metadata.exists() or output.is_symlink() or metadata.is_symlink():
                raise ValueError("Patch/metadata output already exists; choose a new output path")
            patch, records = (make_repository_upgrade_patch(root) if args.upgrade_repositories else make_patch(root))
            if not patch:
                print("Known repository discovery seam already present; no changes")
                return 0
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
