# SPDX-License-Identifier: Apache-2.0
import copy
import json
from pathlib import Path
import subprocess
import sys
import pytest
from benchpark_container.catalog.manager import init_catalog, Catalog
from benchpark_container.catalog.config import update_registration
from benchpark_container.image_selection import select_runtime
from benchpark_container.image_store import verify_managed
from benchpark_container.resolver import resolve
from benchpark_container.ramble_adapter import concrete_plan
from benchpark_container.util import ValidationError, identity
from benchpark_integration.api import plain
from conftest import changed
from catalog_fixtures import sif_declaration, oci_layout


@pytest.fixture
def multi(context,tmp_path,monkeypatch):
    config=tmp_path/'registrations.yaml';monkeypatch.setenv('BPCE_CONFIG',str(config))
    root=tmp_path/'catalog';init_catalog(root,'team')
    sif=tmp_path/'base.sif';sif.write_bytes(b'store/selection fixture - not executed')
    oci=oci_layout(tmp_path/'oci')
    data=sif_declaration(sif,release='r1',accelerator='nvidia')
    data['artifacts'].append({'kind':'oci','oci_layout':str(oci['root']),'platform':'linux/amd64',
        'accelerator':'nvidia','tools':{'python':'python3','shell':'bash'}})
    cat=Catalog(root);cat.register(data);update_registration('team',root)
    s=plain(context.system);s['default_runtime']='singularity';s['execution']['gpu']='nvidia'
    s['runtimes']={r:{'executable':r} for r in ('singularity','apptainer','docker')}
    q=plain(context.requirements);q['default_image']='team:torch-base';q['default_release']='r1'
    return changed(context,system=s,requirements=q),cat,config


def test_one_system_default_and_instance_switch_select_matching_store(multi):
    ctx,cat,_=multi
    results={}
    for runtime in ('singularity','apptainer','docker'):
        variants=dict(plain(ctx.variants),container_runtime=[runtime])
        plan=resolve(changed(ctx,variants=variants)).payload
        results[runtime]=plan
        assert plan['runtime']['runtime']==runtime
        assert plan['runtime_selection']['selected']==runtime
        assert plan['image_selection']['managed'] is True
        assert plan['image_selection']['entry_sha256']==cat.read('torch-base','r1')['entry_sha256']
        verify_managed(plain(plan['base']))
    default=resolve(ctx).payload
    assert default['runtime']['runtime']=='singularity'
    assert default['runtime_selection']['selection_origin']=='system-default'
    assert results['apptainer']['base']['kind']=='sif'
    assert results['docker']['base']['kind']=='oci'
    assert plain(ctx.system)['default_runtime']=='singularity'  # input not mutated
    assert results['apptainer']['base']['sif_sha256']==results['singularity']['base']['sif_sha256']


def test_runtime_selection_does_not_probe_any_executable(multi,monkeypatch):
    ctx,_,_=multi
    monkeypatch.setattr('shutil.which',lambda _:pytest.fail('Do not probe compute-node runtime at init'))
    rt,selection=select_runtime(plain(ctx.system),{'container_runtime':['docker']})
    assert rt['runtime']=='docker'


@pytest.mark.parametrize('fault',['unregistered','multiple','no-default','bad-default','legacy','image-in-system','empty-runtimes'])
def test_invalid_runtime_selection_fails_without_fallback(multi,fault):
    ctx,_,_=multi;s=plain(ctx.system);v=plain(ctx.variants)
    if fault=='unregistered':v['container_runtime']=['missing']
    elif fault=='multiple':v['container_runtime']=['docker','apptainer']
    elif fault=='no-default':s.pop('default_runtime')
    elif fault=='bad-default':s['default_runtime']='absent'
    elif fault=='legacy':s['schema_version']=2
    elif fault=='image-in-system':s['base_images']={}
    else:s['runtimes']={}
    with pytest.raises(ValidationError):resolve(changed(ctx,system=s,variants=v))


def test_single_runtime_default_is_not_globally_singularity(multi):
    ctx,_,_=multi;s=plain(ctx.system);s.pop('default_runtime');s['runtimes']={'docker':{'executable':'docker'}}
    plan=resolve(changed(ctx,system=s)).payload
    assert plan['runtime']['runtime']=='docker'
    assert plan['runtime_selection']['selection_origin']=='only-registered-runtime'


@pytest.mark.parametrize('fault',['unknown-image','unregistered-catalog','missing-release','latest','wrong-platform','wrong-gpu','old-experiment'])
def test_image_selection_negative_cases(multi,fault):
    ctx,_,_=multi;s=plain(ctx.system);q=plain(ctx.requirements);v=plain(ctx.variants)
    if fault=='unknown-image':v.update(container_image=['absent'],container_release=['r1'])
    elif fault=='unregistered-catalog':q['default_image']='absent:torch-base'
    elif fault=='missing-release':v.update(container_image=['team:torch-base'])
    elif fault=='latest':v['container_release']=['latest']
    elif fault=='wrong-platform':s['platform']='linux/arm64'
    elif fault=='wrong-gpu':s['execution']['gpu']='amd'
    else:q['schema_version']=1
    with pytest.raises((ValidationError,OSError)):resolve(changed(ctx,system=s,requirements=q,variants=v))


def test_resolved_snapshot_does_not_follow_catalog_or_config_changes(multi,tmp_path):
    ctx,cat,config=multi
    plan=plain(resolve(ctx).payload);old_hash=identity(plan)
    # Detach catalog, delete metadata, retain authoritative managed bytes.
    update_registration('team',remove=True)
    (cat.root/'entries/torch-base/r1.json').unlink()
    assert verify_managed(plan['base'])['kind']=='sif'
    assert identity(plan)==old_hash
    assert plan['image_selection']['entry_snapshot']['release']=='r1'
    with pytest.raises(ValidationError):resolve(ctx)


def test_runtime_override_changes_condition_without_changing_workload(multi):
    ctx,_,_=multi;plans=[]
    for rt in ('apptainer','singularity'):
        v=dict(plain(ctx.variants),container_runtime=[rt])
        plans.append(concrete_plan(resolve(changed(ctx,variants=v)).payload, {'model':'model-a','size':'16'}, ['true']))
    assert plans[0]['condition_id']!=plans[1]['condition_id']
    assert plans[0]['command']==plans[1]['command']
    assert plans[0]['base']['sif_sha256']==plans[1]['base']['sif_sha256']


def test_image_override_uses_fixed_release_and_shared_schema(multi,tmp_path):
    ctx,cat,_=multi
    p=tmp_path/'user.sif';p.write_bytes(b'other base')
    cat.register(sif_declaration(p,name='mine',release='r2',accelerator='nvidia'))
    v=dict(plain(ctx.variants),container_image=['team:mine'],container_release=['r2'])
    plan=resolve(changed(ctx,variants=v)).payload
    assert plan['base']['logical_name']=='mine' and plan['base']['release']=='r2'
    assert plan['image_selection']['selection_origin']=='explicit'


def test_retained_cer_resolved_contains_actual_selected_image(multi,tmp_path):
    from benchpark_container.cer.recording import start_run, finish_run
    from benchpark_container.cli import load_record
    ctx,_,_=multi
    plan=concrete_plan(resolve(ctx).payload,{'model':'model-a','size':'16'},['true'])
    # This test verifies the record boundary only, NOT a runtime execution.
    attempt,record=start_run(tmp_path/'runs',plan)
    record['status']='PREPARATION_FAILED';record['result']={'exit_code':1,'failure_phase':'fixture-not-executed'}
    finish_run(attempt,record)
    loaded=load_record(attempt/'cer.json')
    assert loaded['resolved']['image_selection']['catalog_alias']=='team'
    assert loaded['resolved']['runtime_selection']['selected']=='singularity'


def test_catalog_through_real_generic_resolution_port(multi,tmp_path,installed):
    from benchpark import plugins
    from benchpark_integration.support.io import save_system_snapshot
    ctx,_,_=multi
    system=tmp_path/'saved-system';system.mkdir()
    save_system_snapshot({'container':plain(ctx.system)},system)
    for runtime in ('apptainer','docker'):
        args=dict(name=ctx.name,variants=dict(plain(ctx.variants),container_runtime=[runtime]),
             state={'extension_selection':['container'],'extension_explicit':{'container':[True]},'preparation_records':{}},
             system_dir=str(system),settings={'container':plain(ctx.requirements)},source_root=ctx.source_root)
        before=json.dumps(args,sort_keys=True)
        contributions=plugins.invoke('experiment_resolve',['container'],**args)
        assert len(contributions)==1
        plan=contributions[0].payload
        assert plan['runtime_selection']['selected']==runtime and plan['image_selection']['managed'] is True
        assert json.dumps(args,sort_keys=True)==before


def test_real_runtime_probe_can_freeze_catalog_input_without_running_engine(multi,tmp_path):
    from types import SimpleNamespace
    from verify_runtime import make_fixture
    ctx,_,_=multi
    req=tmp_path/'req.txt';req.write_text('# selection boundary probe only\n')
    out=tmp_path/'acceptance';out.mkdir()
    args=SimpleNamespace(requirements=req,runtime='apptainer',runtime_executable='apptainer',
         backend_options=None,gpu='none',expected_gpus=0,image=None,base_python='python3',base_shell='bash',
         catalog_image='team:torch-base',catalog_release='r1',timeout_seconds=60)
    resources,plan=make_fixture(args,out)
    assert plan['image_selection']['managed'] is True
    assert plan['base']['kind']=='sif'
    assert (resources/'runtime.pyz').is_file() and (out/'spec.json').is_file()
