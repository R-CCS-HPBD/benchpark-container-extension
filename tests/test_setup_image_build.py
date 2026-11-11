# SPDX-License-Identifier: Apache-2.0
"""Real resolver/setup/READY/runtime/CER with external build/HTTP transport doubles.

These tests do NOT claim to execute Apptainer, GPU kernels or MLPerf.
"""
import copy
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
from types import SimpleNamespace as S

import pytest

from conftest import changed
from test_setup_lifecycle import BashTransport
from benchpark_integration.api import plain
from benchpark_container import setup_environment as setup, runtime
from benchpark_container import image_build as build
from benchpark_container.catalog_publication import freeze_publication
from benchpark_container.catalog.manager import Catalog
from benchpark_container.cli import load_record, verify_recorded_files
from benchpark_container.planning import concrete_plan, validate_plan_version
from benchpark_container.resolver import resolve
from benchpark_container.ramble_adapter import prepare_experiment
from benchpark_container.util import ValidationError, identity, sha256


def declaration():
    return {"schema_version": 1, "format": "sif-definition-v1", "scripts": ["image.sh"],
            "files": ["software.lock"], "tools": {"shell": "/bin/bash"}, "fakeroot": True,
            "environment": {"BUILD_VARIANT": "fixed"}, "checks": [["/bin/sh", "-c", "true"]], "inputs": {}}


class BuildTransport(BashTransport):
    """Real subprocess build handoff; output is deliberately NOT a real SIF."""
    def __init__(self, cache):
        super().__init__()
        self.cache = cache
        self.build_count = 0
        self.settings = {"runtime": "apptainer"}

    def image_build_command(self, output, definition, *, fakeroot):
        self.build_count += 1
        assert fakeroot
        self.definition = Path(definition).read_text()
        # Do not execute Apps scripts on the host: a real runtime would execute
        # them only inside the image. Exercise process and publication behavior.
        return [sys.executable, "-c", "from pathlib import Path; import sys; Path(sys.argv[1]).write_bytes(b'fixture-derived-image')", str(output)]

    def check_built_image(self, path, platform, log, timeout):
        Path(log).write_text("transport fixture, not a SIF validation\n")


@pytest.fixture
def built(context, tmp_path, monkeypatch):
    native = {"x86_64": "linux/amd64", "aarch64": "linux/arm64"}[platform.machine()]
    source = Path(context.source_root)
    (source / "image.sh").write_text('set -eu\nprintf "fixed tools supplied by app"\n')
    (source / "software.lock").write_text('{"python":"3.12.3","description":"test only"}\n')
    (source / "setup.sh").write_text('set -eu\nprintf prepared > "$BPCE_PREFIX/value"\n')
    base = tmp_path / "raw.sif"; base.write_bytes(b"not-a-real-image-no-python")
    q = plain(context.requirements)
    q.update(schema_version=3, default_image={"uri": base.as_uri(), "sha256": sha256(base),
        "managed": False, "tools": {"shell": "/bin/bash"}, "platform": native},
        image_build=declaration(), requirements=[], protected_packages={}, smoke_imports=[],
        setup_scripts=["setup.sh"], artifacts=[{"name": "result", "kind": "result", "target": "/results"}], variables=[])
    system = plain(context.system); system["platform"] = native
    ctx = changed(context, requirements=q, system=system)
    contribution = resolve(ctx)
    resources = tmp_path / "ws/.benchpark-extensions/resources"; resources.mkdir(parents=True)
    for resource in contribution.resources:
        dest = resources / resource.target; dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(resource.source, dest)
    spec = concrete_plan(plain(contribution.payload), {}, ['test "$(cat "$BPCE_PREFIX/value")" = prepared', 'echo RUN_ONLY_OK'], "first", "0")
    builder, worker = BuildTransport(tmp_path / "images"), BuildTransport(tmp_path / "images")
    monkeypatch.setattr(setup, "create_backend", lambda settings: builder)
    monkeypatch.setattr(runtime, "create_backend", lambda settings: worker)
    monkeypatch.setattr("benchpark_container.cer.recording.collect_v1", lambda *a: {"observed": {}, "files": {}, "collection": {"status": "not-requested"}})
    return S(spec=spec, context=ctx, resources=resources, builder=builder, worker=worker, root=tmp_path, source=source)


def test_raw_base_without_python_has_frozen_recipe_but_no_build_at_init(built):
    assert built.spec["base"]["tools"] == {"shell": "/bin/bash"}
    assert built.spec["image_build"]["files"]["software.lock"]["sha256"] == sha256(built.source / "software.lock")
    assert built.builder.build_count == 0
    assert not (built.root / "images").exists()


def test_python_can_be_an_explicit_build_output_not_input(built):
    q = plain(built.context.requirements)
    q["image_build"]["tools"]["python"] = "/opt/stack/bin/python"
    q["smoke_imports"] = ["json"]
    result = resolve(changed(built.context, requirements=q))
    assert plain(result.payload)["image_build"]["tools"]["python"] == "/opt/stack/bin/python"
    assert "python" not in plain(result.payload)["base"]["tools"]


def test_setup_build_then_runtime_uses_derived_image_and_retains_cer(built, monkeypatch):
    rec = setup.prepare(built.spec, built.resources)
    assert built.builder.build_count == 1
    assert rec["image"]["identity"] != "sha256:" + built.spec["base"]["sif_sha256"]
    assert rec["builder"]["image_build"]["request"]["build"] == built.spec["image_build"]
    assert "Bootstrap: localimage" in built.builder.definition
    assert "%setup" not in built.builder.definition
    assert "/bpce-build/context/image.sh" in built.builder.definition
    assert setup.prepare(built.spec, built.resources) == rec
    assert built.builder.build_count == 1
    monkeypatch.setattr(build, "ensure_image", lambda *a, **k: pytest.fail("On built an image"))
    monkeypatch.setattr(runtime, "prepare_environment", lambda *a, **k: pytest.fail("On installed packages"))
    built.worker.resolve_image = lambda *a, **k: pytest.fail("On acquired an image")
    assert runtime.execute(built.spec, built.resources, built.root / "runs") == 0
    path = built.worker.attempt / "cer.json"
    cer = load_record(path)
    assert cer["observed"]["prepared_environment_integrity_after"] == "verified"
    assert verify_recorded_files(path, cer) > 0
    assert (built.worker.attempt / "source-evidence/prepared-environment/image-build/record.json").is_file()


def test_different_experiments_reuse_same_fixed_image(built):
    setup.prepare(built.spec, built.resources)
    other = copy.deepcopy(built.spec); other["ramble_experiment"] = "second"; other["parameters"]["size"] = 2
    setup.prepare(other, built.resources)
    assert built.builder.build_count == 1


def test_build_environment_changes_cache_key(built):
    a = setup.prepare(built.spec, built.resources)
    other = copy.deepcopy(built.spec); other["image_build"]["environment"]["GPU_TARGET"] = "different-isa"
    b = setup.prepare(other, built.resources)
    assert built.builder.build_count == 2
    assert a["builder"]["image_build"]["key"] != b["builder"]["image_build"]["key"]


def test_model_parameters_do_not_change_image_key(built):
    a = setup.prepare(built.spec, built.resources)
    other = copy.deepcopy(built.spec); other["parameters"]["model"] = "another-model"
    b = setup.prepare(other, built.resources)
    assert built.builder.build_count == 1
    assert a["builder"]["image_build"]["key"] == b["builder"]["image_build"]["key"]


def test_missing_ready_on_never_builds(built):
    assert runtime.execute(built.spec, built.resources, built.root / "runs") != 0
    assert built.worker.build_count == 0
    assert "ENVIRONMENT_NOT_PREPARED" in load_record(built.worker.attempt / "cer.json")["result"]["error"]


def test_offline_missing_built_image_fails_before_builder(built):
    built.spec["setup"]["offline"] = True
    with pytest.raises(ValidationError, match="OFFLINE_IMAGE_BUILD_MISSING"):
        setup.prepare(built.spec, built.resources)
    assert built.builder.build_count == 0
    assert not setup.environment_root(built.spec, built.resources).exists()


def test_offline_cached_build_without_additional_preparation_succeeds(built):
    built.spec["setup_scripts"] = []
    setup.prepare(built.spec, built.resources)
    other = copy.deepcopy(built.spec); other["setup"]["offline"] = True
    setup.prepare(other, built.resources)
    assert built.builder.build_count == 1


def test_failed_build_does_not_publish_ready(built):
    built.builder.image_build_command = lambda *a, **k: [sys.executable, "-c", "raise SystemExit(19)"]
    with pytest.raises(ValidationError, match="exit 19"):
        setup.prepare(built.spec, built.resources)
    assert not setup.environment_root(built.spec, built.resources).exists()
    assert list((built.root / "images/bpce-image-builds-v1").glob("failed-*/failure.json"))


def test_build_success_without_sif_fails(built):
    built.builder.image_build_command = lambda *a, **k: [sys.executable, "-c", "pass"]
    with pytest.raises(ValidationError, match="no regular output"):
        setup.prepare(built.spec, built.resources)
    assert not setup.environment_root(built.spec, built.resources).exists()


def test_tampered_build_blocks_second_setup_without_repair(built):
    result = setup.prepare(built.spec, built.resources)
    p = Path(result["image"]["reference"]); p.chmod(0o600); p.write_bytes(b"changed")
    other = copy.deepcopy(built.spec); other["ramble_experiment"] = "second"
    with pytest.raises(ValidationError, match="changed"):
        setup.prepare(other, built.resources)
    assert built.builder.build_count == 1
    assert runtime.execute(built.spec, built.resources, built.root / "runs") != 0


def test_changed_context_rejected_before_build(built):
    p = built.resources / "inputs/software.lock"; p.write_text("changed")
    with pytest.raises(ValidationError, match="changed"):
        setup.prepare(built.spec, built.resources)
    assert built.builder.build_count == 0


def test_dry_run_has_no_external_side_effects(built, monkeypatch):
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: pytest.fail("dry run spawned process"))
    monkeypatch.setattr(subprocess, "Popen", lambda *a, **k: pytest.fail("dry run spawned process"))
    prepare_experiment(None, S(dry_run=True), None)
    assert not (built.root / "images").exists()
    assert not setup.environment_root(built.spec, built.resources).exists()


@pytest.mark.parametrize("field,value", [("format", "automatic"), ("fakeroot", 1), ("checks", []),
    ("scripts", ["../escape.sh"]), ("environment", {"BPCE_FAKE": "x"}), ("environment", {"TOKEN": "bad"})])
def test_bad_build_declarations_fail_closed(built, field, value):
    q = plain(built.context.requirements); q["image_build"][field] = value
    with pytest.raises((ValidationError, KeyError, TypeError, FileNotFoundError)):
        resolve(changed(built.context, requirements=q))


def test_system_build_flags_are_generic_and_conflicts_are_rejected(built):
    system = plain(built.context.system); system["build_environment"] = {"GPU_TARGETS": "arch-a;arch-b"}
    p = plain(resolve(changed(built.context, system=system)).payload)
    assert p["image_build"]["environment"]["GPU_TARGETS"] == "arch-a;arch-b"
    system["build_environment"] = {"BUILD_VARIANT": "conflict"}
    with pytest.raises(ValidationError, match="conflict"):
        resolve(changed(built.context, system=system))


@pytest.mark.parametrize("cpu", ["linux/amd64", "linux/arm64"])
@pytest.mark.parametrize("accelerator,backend", [("nvidia", "cuda"), ("amd", "rocm"), ("none", None)])
def test_build_selection_never_depends_on_product_name(built, cpu, accelerator, backend):
    q = plain(built.context.requirements)
    q.pop("image_build")
    q["base_candidates"] = [{"platform": p, "accelerator": gpu, "runtime": "apptainer", "release": "fixed",
         "image": {"uri": "registry.example/base@sha256:" + "0123456789abcdef" * 4,
                   "managed": False, "tools": {"shell": "/bin/bash"}}, "image_build": declaration()}
        for p in ("linux/arm64", "linux/amd64") for gpu in ("none", "nvidia", "amd")]
    system = plain(built.context.system); system["platform"] = cpu; system["execution"]["gpu"] = accelerator
    variants = plain(built.context.variants); explicit = plain(built.context.explicit)
    if backend: variants[backend] = [True]; explicit[backend] = [True]
    result = plain(resolve(changed(built.context, requirements=q, system=system, variants=variants, explicit=explicit)).payload)
    assert result["base"]["platform"] == cpu and result["base"]["accelerator"] == accelerator
    assert result["image_build"]["scripts"] == ["image.sh"]


def test_schema2_cannot_silently_use_setup_build(built):
    q = plain(built.context.requirements); q["schema_version"] = 2
    with pytest.raises(ValidationError, match="schema_version=3"):
        resolve(changed(built.context, requirements=q))
    for version in (1, 2):
        p = copy.deepcopy(built.spec); p["schema_version"] = version
        p.pop("setup"); p.pop("preparation_lifecycle")
        with pytest.raises(ValidationError):
            validate_plan_version(p)


def test_explicit_image_override_never_silently_builds(built):
    variants = plain(built.context.variants)
    variants.update(container_image=["registry.example/ready@sha256:" + "ab" * 32])
    p = plain(resolve(changed(built.context, variants=variants)).payload)
    assert "image_build" not in p


def test_image_publish_uses_existing_catalog_and_retains_binding(built, monkeypatch):
    config = built.root / "catalogs.json"; destination = built.root / "catalog"
    config.write_text(json.dumps({"schema_version": 1, "catalogs": {"test": str(destination)}}))
    monkeypatch.setenv("BPCE_CONFIG", str(config))
    built.spec["image_publish"] = freeze_publication({"catalog": "test", "name": "stack", "release": "r1"})
    assert not destination.exists()  # pure init/config freeze
    record = setup.prepare(built.spec, built.resources)
    cat = Catalog(destination); entry = cat.read("stack", "r1")
    assert cat.validate("stack", "r1")["status"] == "VERIFIED_STORED"
    assert record["lock"]["execution_base"]["managed"]["root"] == str(destination)
    assert record["image"]["details"]["sif_sha256"] == entry["artifacts"][0]["content"]["sif_sha256"]
    assert setup.prepare(built.spec, built.resources) == record
    assert runtime.execute(built.spec, built.resources, built.root / "runs") == 0
    assert verify_recorded_files(built.worker.attempt / "cer.json", load_record(built.worker.attempt / "cer.json")) > 0


def test_publication_never_overwrites_fixed_release(built, monkeypatch):
    config = built.root / "catalogs.json"; destination = built.root / "catalog"
    config.write_text(json.dumps({"schema_version": 1, "catalogs": {"test": str(destination)}}))
    monkeypatch.setenv("BPCE_CONFIG", str(config))
    built.spec["image_publish"] = freeze_publication({"catalog": "test", "name": "stack", "release": "r1"})
    setup.prepare(built.spec, built.resources)
    from benchpark_container.catalog_publication import host_main
    other = built.root / "different.sif"; other.write_bytes(b"different")
    request = json.loads((setup.environment_root(built.spec, built.resources) / "catalog-request.json").read_text())
    request.update(source=str(other), sha256=sha256(other))
    p = built.root / "publication.json"; p.write_text(json.dumps(request))
    with pytest.raises(ValidationError, match="Immutable"):
        host_main([str(p), str(built.root / "response.json")])


def test_frozen_catalog_helper_matches_source(repository):
    import importlib.util
    path = repository / 'tools/build_catalog_helper.py'
    spec = importlib.util.spec_from_file_location('catalog_helper_builder_test', path)
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    assert (repository / 'src/benchpark_container/resources/catalog_publish.pyz').read_bytes() == module.build(repository)


def test_prebuilt_publication_does_not_call_builder(built, monkeypatch):
    built.spec.pop('image_build')
    config = built.root / 'catalogs.json'; destination = built.root / 'catalog'
    config.write_text(json.dumps({'schema_version': 1, 'catalogs': {'test': str(destination)}}))
    monkeypatch.setenv('BPCE_CONFIG', str(config))
    built.spec['image_publish'] = freeze_publication({'catalog':'test', 'name':'prebuilt', 'release':'fixed'})
    record = setup.prepare(built.spec, built.resources)
    assert built.builder.build_count == 0
    assert Catalog(destination).validate('prebuilt','fixed')['status'] == 'VERIFIED_STORED'
    assert 'image_build' not in record['builder']
    assert runtime.execute(built.spec,built.resources,built.root/'runs') == 0


def test_other_setup_environment_reuses_built_image_without_fetching_inputs(built, monkeypatch):
    setup.prepare(built.spec, built.resources)
    other = copy.deepcopy(built.spec); other['ramble_experiment'] = 'another-instance'
    monkeypatch.setattr(build, 'acquire', lambda *a, **kw: pytest.fail('Cached build re-acquired input'))
    setup.prepare(other,built.resources)
    assert built.builder.build_count == 1


def test_frozen_helper_tamper_stops_before_catalog_entry(built, monkeypatch):
    config = built.root / 'catalogs.json'; destination = built.root / 'catalog'
    config.write_text(json.dumps({'schema_version':1,'catalogs':{'test':str(destination)}}))
    monkeypatch.setenv('BPCE_CONFIG', str(config))
    built.spec['image_publish'] = freeze_publication({'catalog':'test','name':'stack','release':'r1'})
    helper = built.resources/'catalog_publish.pyz'
    helper.write_bytes(b'not the frozen helper')
    with pytest.raises(ValidationError,match='changed'):
        setup.prepare(built.spec,built.resources)
    assert not destination.exists()


def test_build_command_timeout_is_not_published(built):
    built.spec['setup']['timeout_seconds'] = .2
    built.builder.image_build_command = lambda *a, **k: [sys.executable,'-c','import time; time.sleep(5)']
    with pytest.raises(subprocess.TimeoutExpired):
        setup.prepare(built.spec,built.resources)
    assert not setup.environment_root(built.spec,built.resources).exists()
    assert not list((built.root/'images/bpce-image-builds-v1/objects').glob('*/record.json'))
