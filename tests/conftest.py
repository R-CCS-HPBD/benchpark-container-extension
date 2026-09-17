# SPDX-License-Identifier: Apache-2.0
import importlib.util
import json
from pathlib import Path
import sys
from types import SimpleNamespace
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT/'src'), str(ROOT/'core/files/lib'), str(ROOT/'tools')]

@pytest.fixture
def repository():
    return ROOT

@pytest.fixture
def context(tmp_path):
    from benchpark_integration.api import ResolutionContext
    source=tmp_path/'experiment'; source.mkdir()
    (source/'requirements.txt').write_text('# no additional dependency for this unit fixture\n')
    (source/'script.py').write_text('print("metric=1")\n')
    models=tmp_path/'models'; (models/'model-a').mkdir(parents=True)
    (models/'model-a'/'weights.txt').write_text('unit fixture, not a real model')
    return ResolutionContext(
        name='common-base-smoke', explicit={'container':[True]},
        variants={'container':[True], 'package_manager':['user-managed'], 'model':['model-a'],
                  'revision':['0123456789abcdef0123456789abcdef01234567'], 'n_repeats':['1']},
        system={'schema_version':3, 'execution':{'worker_python':sys.executable,'image_cache':str(tmp_path/'images')},
                'platform':'linux/amd64', 'default_runtime':'apptainer',
                'runtimes':{'apptainer':{'executable':'apptainer'}},
                'artifact_roots':{'models':str(models)}},
        requirements={'schema_version':2,'default_image':{
                         'uri':'registry.example/base@sha256:'+('0123456789abcdef'*4),
                         'platform':'linux/amd64','tools':{'python':sys.executable,'shell':'bash'},'managed':False},
                      'default_release':'release-a','executables':['benchmark'],
                      'requirements':['requirements.txt'],'protected_packages':{'packaging':'packaging'},
                      'variables':['model','size'],
                      'artifacts':[
                          {'name':'script','kind':'script','source':'script.py','target':'/bench/run.py'},
                          {'name':'model','kind':'model','root':'models','path':'{model}','target':'/models','revision':'0123456789abcdef0123456789abcdef01234567'},
                          {'name':'result','kind':'result','target':'/results'}]},
        source_root=str(source))

def changed(context, **kwargs):
    from benchpark_integration.api import ResolutionContext, plain
    d={field:plain(getattr(context,field)) for field in ('name','explicit','variants','system','requirements','source_root','provenance')}
    d.update(kwargs)
    return ResolutionContext(**d)

class EP:
    def __init__(self,name,group,factory):
        self.name,self.group,self.factory=name,group,factory;self.loads=0
        self.value = factory.__module__ + ":" + factory.__qualname__
    def load(self):
        self.loads+=1
        return self.factory

class EPs(list):
    def select(self, **filters):
        return EPs(e for e in self if all(getattr(e,k)==v for k,v in filters.items()))

@pytest.fixture
def installed(monkeypatch):
    from benchpark_container.plugin import describe
    from benchpark_container.cli import command_descriptor
    from benchpark_tuning.plugin import describe as tune_descriptor
    from benchpark_integration import discovery as extensions
    eps=EPs([EP('container','benchpark.extensions',describe),EP('cer','benchpark.commands',command_descriptor),EP('tune','benchpark.preparers',tune_descriptor)])
    from benchpark_integration.hooks import provider
    eps.extend([EP('container','benchpark.lifecycle.v1',provider), EP('tune','benchpark.lifecycle.v1',provider)])
    monkeypatch.setattr(extensions.importlib.metadata, 'entry_points', lambda:eps)
    return eps
