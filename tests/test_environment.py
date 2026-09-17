# SPDX-License-Identifier: Apache-2.0
"""Dependency-layer policy tests; no real container required."""
from pathlib import Path
import json
import pytest

from benchpark_container.preparation import (
    EnvironmentBuildError, validate_layer_inventory, dependency_check_delta)
from benchpark_container.resolver import resolve
from conftest import changed


def test_extension_does_not_ship_package_manager_or_in_container_builder(repository):
    resources = repository / 'src/benchpark_container/resources'
    assert not (resources / 'installer').exists()
    assert not (resources / 'environment.py').exists()
    assert not (resources / 'activation').exists()
    assert not (repository / 'tools/build_installer.py').exists()
    assert not (repository / 'src/benchpark_container/environment.py').exists()


def test_layer_requires_exact_declared_pin():
    with pytest.raises(EnvironmentBuildError) as exc:
        validate_layer_inventory({}, [{'name':'extra','version':'2.0','content_sha256':'a'*64}], {'extra':['1.0']})
    assert exc.value.code == 'UNPINNED_TRANSITIVE_DEPENDENCY'
    assert validate_layer_inventory({}, [{'name':'extra','version':'2.0','content_sha256':'a'*64}], {'extra':['2.0']})['status']=='passed'


def test_added_layer_may_not_shadow_base_distribution():
    with pytest.raises(EnvironmentBuildError) as exc:
        validate_layer_inventory({'torch':'2.11.0+cu128'},
            [{'name':'torch','version':'2.11.0+cu128','content_sha256':'a'*64}],
            {'torch':['2.11.0+cu128']})
    assert exc.value.code == 'BASE_VERSION_CONFLICT'


def test_dependency_check_scope_is_delta_only():
    before={'exit_code':1,'issues':['base-existing-problem']}
    after={'exit_code':1,'issues':['base-existing-problem']}
    assert dependency_check_delta(before,after)==[]
    after={'exit_code':1,'issues':['base-existing-problem','new-layer-problem']}
    assert dependency_check_delta(before,after)==['new-layer-problem']


def test_setup_scripts_are_source_snapshotted(context):
    root=Path(context.source_root)
    script=root/'setup.sh';script.write_text('#!/bin/bash\nset -euo pipefail\nmkdir -p "$BPCE_PREFIX/bin"\n')
    req=dict(context.requirements);req['setup_scripts']=['setup.sh']
    plan=resolve(changed(context,requirements=req)).payload
    assert plan['setup_scripts']==['inputs/setup.sh']
    assert 'inputs/setup.sh' in plan['resources']


def test_setup_script_must_be_git_style_source_file(context):
    req=dict(context.requirements);req['setup_scripts']=['../escape.sh']
    with pytest.raises(Exception):
        resolve(changed(context,requirements=req))
