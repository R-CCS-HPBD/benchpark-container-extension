# SPDX-License-Identifier: Apache-2.0
import json
from benchpark_container.cer.collectors.base import CommandResult, CommandRunner, CollectorContext
from benchpark_container.cer.collectors.hardware import collect_hardware
from benchpark_container.cer.collectors.container import collect_container
from benchpark_container.cer.recording import start_run, finish_run
from benchpark_container.util import identity

class FakeRunner(CommandRunner):
    def __init__(self, available=(), outputs=None): self.available=set(available); self.outputs=outputs or {}
    def which(self,name): return '/fake/'+name if name in self.available else None
    def run(self,argv,timeout=15.0):
        rc,out,err=self.outputs.get(tuple(argv),(0,'',''))
        return CommandResult(tuple(argv),rc,out,err)


def test_nvidia_hardware_collection_csv(tmp_path):
    identity='0, GPU-1, 0000:02:00.0, NVIDIA Test GPU, 999.1, 81920\n'
    state='0, P0, 700, 1500, 2000\n'
    runner=FakeRunner(('nvidia-smi',),{
      ('nvidia-smi','--query-gpu=index,uuid,pci.bus_id,name,driver_version,memory.total','--format=csv,noheader,nounits'):(0,identity,''),
      ('nvidia-smi','--query-gpu=index,pstate,power.limit,clocks.current.graphics,clocks.current.memory','--format=csv,noheader,nounits'):(0,state,''),
      ('nvidia-smi','-L'):(0,'GPU 0: NVIDIA Test GPU (UUID: GPU-1)\n','')})
    r=collect_hardware(CollectorContext(tmp_path),runner)
    nv=next(x for x in r.observed['hardware']['accelerators'] if x['vendor']=='nvidia')
    assert nv['devices'][0]['uuid']=='GPU-1'
    assert nv['devices'][0]['driver_version']=='999.1'
    assert nv['devices'][0]['uuid']=='GPU-1'
    assert 'power_limit' not in nv['devices'][0]
    assert nv['post_execution_state']['devices'][0]['power_limit']=='700'
    assert nv['post_execution_state']['capture_phase']=='post-execution-finalization'

def test_amd_hardware_collection_json(tmp_path):
    listing=[{'gpu':0,'bdf':'0000:01:00.0','uuid':'GPU-abc','market_name':'AMD Instinct MI350X'}]
    runner=FakeRunner(('amd-smi','rocminfo'),{
      ('amd-smi','version','--json'):(0,'{"version":"test"}',''),
      ('amd-smi','list','--json'):(0,json.dumps(listing),''),
      ('amd-smi','static','--json'):(0,'[]',''),
      ('amd-smi','metric','--json'):(0,'[]',''),
      ('rocminfo',):(0,'','')})
    r=collect_hardware(CollectorContext(tmp_path),runner)
    amd=next(x for x in r.observed['hardware']['accelerators'] if x['vendor']=='amd')
    assert amd['collector']=='amd-smi'
    assert 'GPU-abc' in json.dumps(amd['devices'])
    assert 'cer-evidence/hardware/amd/static.stdout.log' in r.files

def test_amd_rocminfo_fallback(tmp_path):
    runner=FakeRunner(('rocminfo',),{('rocminfo',):(0,'  Name: gfx942\n  Uuid: GPU-x\n  Marketing Name: AMD Instinct MI300X\n','')})
    r=collect_hardware(CollectorContext(tmp_path),runner)
    amd=next(x for x in r.observed['hardware']['accelerators'] if x['vendor']=='amd')
    assert amd['collector']=='rocminfo' and amd['devices']

def test_container_summary_uses_existing_observation():
    record={'resolved':{'base':{'logical_name':'vllm-base','release':'r1','kind':'sif','platform':'linux/arm64','sif_sha256':'a'*64},
                        'image_selection':{'catalog_alias':'personal','entry_sha256':'b'*64,'managed':True}},
            'observed':{'runtime':{'name':'apptainer','version':'1.5.0','executable':'/missing'},
                        'image':{'kind':'sif','identity':'sha256:'+('a'*64),'sif_sha256':'a'*64},
                        'command':['apptainer','exec','--nv','image.sif','true'],
                        'mounts':[{}], 'execution_environment':{'X':'1'}}}
    c=collect_container(record).observed['container']
    assert c['image']['logical_name']=='vllm-base' and c['gpu_passthrough']=='nvidia'
    assert c['mount_count']==1 and c['environment_names']==['X']

def test_finish_run_adds_collection_without_changing_identity(tmp_path,monkeypatch):
    # Avoid depending on test-host GPU utilities.
    from benchpark_container.cer.collectors import registry
    monkeypatch.setattr(registry.CommandRunner,'which',lambda self,name: None)
    spec={'condition_id':'c'*64,'plan_sha256':'p'*64,'ramble_experiment':'x','repeat_index':'0'}
    d,r=start_run(tmp_path,spec); before=r['condition_id']; r['status']='COMPLETED'; r['result']['exit_code']=0
    finish_run(d,r)
    data=json.loads((d/'cer.json').read_text())
    assert data['condition_id']==before and data['resolved']['plan_sha256']=='p'*64
    assert data['observed']['hardware']['schema_version']==1
    assert data['observed']['container']['schema_version']==1
    body=dict(data); h=body.pop('record_sha256'); assert identity(body)==h
    assert data['collection']['non_fatal'] is True


def test_collector_exception_is_nonfatal_and_preserves_benchmark_result(tmp_path, monkeypatch):
    from benchpark_container.cer.collectors import registry
    def boom(*args, **kwargs):
        raise RuntimeError('probe failed')
    monkeypatch.setattr(registry, 'collect_hardware', boom)
    spec={'condition_id':'d'*64,'plan_sha256':'q'*64,'ramble_experiment':'x','repeat_index':'0'}
    d,r=start_run(tmp_path,spec)
    r['status']='COMPLETED'; r['result']['exit_code']=0
    finish_run(d,r)
    data=json.loads((d/'cer.json').read_text())
    assert data['status']=='COMPLETED' and data['result']['exit_code']==0
    assert data['collection']['status']=='partial'
    h=next(x for x in data['collection']['collectors'] if x['name']=='hardware')
    assert 'probe failed' in h['errors'][0]


def test_existing_observed_field_is_never_overwritten_by_collector(tmp_path, monkeypatch):
    from benchpark_container.cer.collectors import registry
    monkeypatch.setattr(registry.CommandRunner,'which',lambda self,name: None)
    spec={'condition_id':'e'*64,'plan_sha256':'r'*64,'ramble_experiment':'x','repeat_index':'0'}
    d,r=start_run(tmp_path,spec)
    r['observed']['hardware']={'schema_version':999,'owner':'preexisting'}
    r['status']='COMPLETED'; r['result']['exit_code']=0
    finish_run(d,r)
    data=json.loads((d/'cer.json').read_text())
    assert data['observed']['hardware']=={'schema_version':999,'owner':'preexisting'}
    assert data['collection']['status']=='partial'
    h=next(x for x in data['collection']['collectors'] if x['name']=='hardware')
    assert any('collision' in e.lower() for e in h['errors'])


def test_cer_validate_detects_tampered_recorded_file(tmp_path, monkeypatch):
    from benchpark_container.cer.collectors import registry
    from benchpark_container.cli import verify_recorded_files
    from benchpark_container.util import sha256, ValidationError
    import pytest
    monkeypatch.setattr(registry.CommandRunner,'which',lambda self,name: None)
    spec={'condition_id':'f'*64,'plan_sha256':'s'*64,'ramble_experiment':'x','repeat_index':'0'}
    d,r=start_run(tmp_path,spec)
    f=d/'benchmark.log'; f.write_text('original')
    r['status']='COMPLETED'; r['result']['exit_code']=0
    finish_run(d,r)
    data=json.loads((d/'cer.json').read_text())
    assert verify_recorded_files(d/'cer.json',data) >= 1
    f.write_text('tampered')
    with pytest.raises(ValidationError, match='mismatch'):
        verify_recorded_files(d/'cer.json',data)
