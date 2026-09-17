# SPDX-License-Identifier: Apache-2.0
from dataclasses import replace
import shutil
import sys
import pytest
from benchpark_container.backends import ExecutionRequest,ImageRef,Mount
from benchpark_container.backends.base import RuntimeCapabilities
from benchpark_container.backends.apptainer import ApptainerBackend
from benchpark_container.backends.docker import DockerBackend
from benchpark_container.contracts import runtime_settings
from benchpark_container.util import ValidationError


def config(tmp_path,name='apptainer',options=None):
    value={'runtime':name,'executable':name,'worker_python':sys.executable,'image_cache':str(tmp_path/'cache')}
    if options is not None:value['backend_options']=options
    return value


@pytest.mark.parametrize('options',[{'gpu_devices':[{}]}, {'gpu_devices':[['0']]},
    {'ipc':{}}, {'network':['bridge']}, {'seccomp':[]}])
def test_malformed_structured_options_raise_controlled_validation(tmp_path,options):
    with pytest.raises(ValidationError):runtime_settings(config(tmp_path,'docker',options))


def test_wrong_backend_constructor_cannot_mislabel_runtime(tmp_path,monkeypatch):
    monkeypatch.setattr(shutil,'which',lambda name:'/declared/'+name)
    with pytest.raises(ValidationError,match='does not match'):
        ApptainerBackend(config(tmp_path,'singularity'))


@pytest.mark.parametrize('changes',[{'accelerators':frozenset()}, {'readonly_mounts':False},
    {'working_directory':False},{'immutable_image':False}])
def test_required_capabilities_rejected_before_command(tmp_path,monkeypatch,changes):
    monkeypatch.setattr(shutil,'which',lambda name:'/declared/'+name)
    rt=ApptainerBackend(config(tmp_path));rt.capabilities=replace(RuntimeCapabilities(),**changes)
    req=ExecutionRequest(ImageRef('sif','/base.sif','sha256:'+'a1'*32),
        (Mount('/input','/input',True),),{},'/work',('true',))
    with pytest.raises(ValidationError):rt.build_command(req)


def test_duplicate_mount_targets_rejected(tmp_path,monkeypatch):
    monkeypatch.setattr(shutil,'which',lambda name:'/declared/'+name)
    rt=ApptainerBackend(config(tmp_path))
    req=ExecutionRequest(ImageRef('sif','/base.sif','sha256:'+'a1'*32),
        (Mount('/a','/input',True),Mount('/b','/input',False)),{},'/work',('true',))
    with pytest.raises(ValidationError,match='Duplicate'):rt.build_command(req)


def test_closed_oci_backend_not_reused(tmp_path,monkeypatch):
    monkeypatch.setattr(shutil,'which',lambda name:'/declared/'+name)
    rt=DockerBackend(config(tmp_path,'docker'));rt.close()
    req=ExecutionRequest(ImageRef('oci','sha256:'+'a1'*32,'registry.example/base@sha256:'+'a1'*32),(),{},'/work',('true',))
    with pytest.raises(ValidationError,match='closed'):rt.build_command(req)
