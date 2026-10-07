# SPDX-License-Identifier: Apache-2.0
"""Frozen resources and workspace preservation; independent of plugin policy."""
import json
import os
from pathlib import Path
import shutil
from benchpark_integration.api import API_VERSION, ExtensionError, digest, plain
from .io import STATE_DIR, SNAPSHOT, file_hash, relative_path, contained, write_new_json
from .generation import contributions, validate_contributions

def publish_experiment(values, destdir, system_dir, upstream_root=None):
    values = contributions(values)
    if not values:
        return
    validate_contributions(values)
    dest = Path(destdir)
    state = dest / STATE_DIR
    state.mkdir()
    files = []
    for r in (r for c in values for r in c.resources):
        path = contained(state / "resources", r.target)
        path.parent.mkdir(parents=True, exist_ok=True)
        if r.source:
            shutil.copyfile(r.source, path)
        else:
            path.write_text(r.text, encoding="utf-8")
        path.chmod(0o555 if r.executable else 0o444)
        if file_hash(path) != r.sha256:
            raise ExtensionError("Input changed while copying: " + r.target)
        files.append({"path": str(path.relative_to(state)), "sha256": file_hash(path)})
    for c in values:
        plan = state / "plans" / (c.owner + ".json")
        write_new_json(plan, c.payload)
        files.append({"path": str(plan.relative_to(state)), "sha256": file_hash(plan)})
    # Capture System-generated configuration at experiment creation.
    # Only immediate config files; never other experiments under the System.
    system_files = []
    system_root = Path(system_dir)
    candidates = [p for p in system_root.iterdir() if p.is_file()
                  and (p.suffix == ".yaml" or p.name == "execute_experiment.tpl")]
    candidates += sorted((system_root / "auxiliary_software_files").rglob("*.yaml"))
    for p in candidates:
        relative = p.relative_to(system_root)
        target = state / "system-config" / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(p, target)
        files.append({"path": str(target.relative_to(state)), "sha256": file_hash(target)})
        system_files.append(str(relative))
    # Freeze generated experiment YAML; setup must not discover newly added
    # files or re-read definitions after init. Files themselves are verified.
    for parent, dirs, names in os.walk(dest):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for name in names:
            if name.startswith(".") or not name.endswith(".yaml"):
                continue
            p = Path(parent) / name
            target = state / "experiment-config" / p.relative_to(dest)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(p, target)
            files.append({"path": str(target.relative_to(state)), "sha256": file_hash(target)})
    candidates = [system_root / "execute_experiment.tpl", dest / "execute_experiment.tpl"]
    if upstream_root is not None:
        candidates.append(Path(upstream_root) / "common-resources/execute_experiment.tpl")
        common = Path(upstream_root) / "systems/common"
        if common.is_dir():
            for p in sorted(common.rglob("*.yaml")):
                target = state / "common-config" / p.relative_to(common)
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(p, target)
                files.append({"path": str(target.relative_to(state)), "sha256": file_hash(target)})
    for p in candidates:
        if p.is_file():
            target = state / "execution-template"
            shutil.copyfile(p, target)
            files.append({"path": str(target.relative_to(state)), "sha256": file_hash(target)})
            break
    manifest = {"schema_version": 3, "owners": [c.owner for c in values], "api_version": API_VERSION,
        "overwrite_policy": "fail", "files": files, "system_files": system_files,
        "modifier_repositories": list(dict.fromkeys(r for c in values for r in c.modifier_repositories)),
        "config_sha256": file_hash(dest / "ramble.yaml"),
        "payload_sha256": {c.owner: digest(c.payload) for c in values}}
    application_repositories = list(dict.fromkeys(r for c in values for r in c.application_repositories))
    if application_repositories:
        manifest["application_repositories"] = application_repositories
    write_new_json(state / "manifest.json", manifest)

def verify_manifest(source):
    path = Path(source) / STATE_DIR / "manifest.json"
    if not path.is_file():
        return None
    data = json.loads(path.read_text(encoding="utf-8"))
    if data.get("schema_version") not in (1, 2, 3) or data.get("overwrite_policy") != "fail":
        raise ExtensionError("Unknown extension manifest schema/policy")
    for item in data["files"]:
        resource = contained(path.parent, item["path"])
        if resource.is_symlink() or not resource.is_file() or file_hash(resource) != item["sha256"]:
            raise ExtensionError("Fixed input changed or missing: " + str(resource))
    if file_hash(Path(source) / "ramble.yaml") != data["config_sha256"]:
        raise ExtensionError("Initialized ramble.yaml changed; create a new experiment")
    return data

def guard_workspace(source, output):
    """Must run before upstream setup deletes an existing workspace."""
    m = verify_manifest(source)
    out = Path(output)
    existing = out / "workspace" / STATE_DIR / "manifest.json"
    if (m or existing.exists()) and (out.exists() or out.is_symlink()):
        raise ExtensionError("Refusing to replace protected workspace: " + str(out))
    if m:
        src = Path(source).resolve()
        dst = out.resolve()
        if src == dst or src in dst.parents or dst in src.parents:
            raise ExtensionError("Source/workspace paths must not contain each other")
    return m

def _fixed_config_tree(source, target, include_fn):
    source, target = Path(source), Path(target)
    for parent, dirs, files in os.walk(source):
        dirs[:] = [d for d in dirs if not d.startswith(".")]
        for name in files:
            if not include_fn(name):
                continue
            src = Path(parent) / name
            dst = target / src.relative_to(source)
            dst.parent.mkdir(parents=True, exist_ok=True)
            if dst.exists() or dst.is_symlink():
                if file_hash(src) == file_hash(dst):
                    continue
                raise ExtensionError("Workspace config collision: " + str(dst))
            shutil.copyfile(src, dst)

def stage_workspace(source, configs, system_source, copier, include_fn):
    """Public setup connection. Return False for the unchanged upstream path."""
    data = verify_manifest(source)
    if not data:
        return False
    workspace = Path(configs).parent
    shutil.copytree(Path(source) / STATE_DIR, workspace / STATE_DIR)
    _fixed_config_tree(Path(source) / STATE_DIR / "system-config", configs, include_fn)
    if data["schema_version"] < 3:
        raise ExtensionError("Old snapshot does not freeze all configuration inputs; initialize a new experiment")
    _fixed_config_tree(Path(source) / STATE_DIR / "experiment-config", configs, include_fn)
    # Paths in this config are relative to the workspace, not installation sites.
    if data["modifier_repositories"]:
        import yaml
        p = Path(configs) / "modifier_repos.yaml"
        if p.exists():
            raise ExtensionError("Reserved workspace config filename: " + str(p))
        repos = [str(workspace / STATE_DIR / "resources" / relative_path(r))
                 for r in data["modifier_repositories"]]
        p.write_text(yaml.safe_dump({"modifier_repos": repos}), encoding="utf-8")
    # Workspace-scoped application definitions take precedence over the live
    # registered repository. Existing experiments never reread updated app code.
    if data.get("application_repositories"):
        import yaml
        p = Path(configs) / "repos.yaml"
        config = yaml.safe_load(p.read_text()) if p.exists() else {}
        config = config or {}
        current = config.get("repos", [])
        if not isinstance(current, list):
            raise ExtensionError("Workspace application repos must be a list")
        frozen = [str(workspace / STATE_DIR / "resources" / relative_path(r))
                  for r in data["application_repositories"]]
        config["repos"] = list(dict.fromkeys(frozen + current))
        p.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    return True

def copy_template(source, target, experiment_source):
    if not (Path(experiment_source) / STATE_DIR / "manifest.json").exists():
        os.symlink(source, target)
        return
    frozen = Path(experiment_source) / STATE_DIR / "execution-template"
    if not frozen.is_file():
        raise ExtensionError("No execution template was frozen at init; cannot use a live site template")
    shutil.copyfile(frozen, target)

class WorkspaceSession:
    """Preflight before deletion; stage later, when paths exist.

    Three calls are intentional. Replacing them by one late callback would lose
    the guard against deleting a previous result before the callback executes.
    """
    def __init__(self, source, output):
        self.source = Path(source)
        self.manifest = guard_workspace(source, output)

    def stage(self, configs, system_source, native_copy, include):
        if not stage_workspace(self.source, configs, system_source, None, include):
            native_copy(system_source, configs, include)
            native_copy(self.source, configs, include)

    def common(self, source, target, native_copy, include):
        if not self.manifest:
            native_copy(source, target, include)
        else:
            _fixed_config_tree(self.source / STATE_DIR / "common-config", target, include)

    def template(self, source, target):
        copy_template(source, target, self.source)
