# SPDX-License-Identifier: Apache-2.0
from benchpark_container.preparation import validate_layer_inventory, EnvironmentBuildError
import pytest


def test_same_named_base_package_is_rejected_even_same_version():
    with pytest.raises(EnvironmentBuildError) as exc:
        validate_layer_inventory({'packaging':'25.0'},
            [{'name':'packaging','version':'25.0','content_sha256':'a'*64}],
            {'packaging':['25.0']})
    assert exc.value.code=='BASE_VERSION_CONFLICT'


def test_different_fixed_additional_versions_are_distinct():
    a=validate_layer_inventory({},[{'name':'release-comparison','version':'1.0.0','content_sha256':'a'*64}],{'release-comparison':['1.0.0']})
    b=validate_layer_inventory({},[{'name':'release-comparison','version':'2.0.0','content_sha256':'b'*64}],{'release-comparison':['2.0.0']})
    assert a['validated_additions']==b['validated_additions']==1


def test_no_implicit_torch_policy():
    assert validate_layer_inventory({},[{'name':'demo','version':'1.0','content_sha256':'a'*64}],{'demo':['1.0']})['status']=='passed'
