from ports import generation_port
# SPDX-License-Identifier: Apache-2.0
from dataclasses import replace
import copy
import json
from pathlib import Path
import pytest
from benchpark_integration.api import (ResolutionContext, ExtensionError, plain, digest, ConfigurationContribution)
from benchpark_integration import discovery as core
from benchpark_integration.support import cli
from benchpark_integration.support.generation import RambleGeneration
from benchpark_container.resolver import resolve, select_image, requirements_resources
from benchpark_container.util import ValidationError, inside, check_no_secrets, sha256
from conftest import changed


def test_context_deep_readonly(context):
    with pytest.raises(TypeError): context.system['execution']['runtime']='docker'
    assert context.variants['model']==('model-a',)

@pytest.mark.parametrize('value',[object(),{2:'x'},float('nan'),float('inf')])
def test_reject_non_json(value):
    with pytest.raises(ExtensionError):plain(value)


def test_resolver_uses_system_image_experiment_materials(context):
    c=resolve(context)
    assert c.payload['base']['logical_name']=='direct'
    assert 'runtime' not in c.payload['base']
    assert c.payload['runtime']['runtime']=='apptainer'
    assert c.software_provider.section=={'packages':{}, 'environments':{}}
    assert context.variants['package_manager']==('user-managed',) # no mutation
    assert 'runtime.pyz' in c.payload['resources']
    assert c.payload['requirements']==['inputs/requirements.txt']
    assert c.payload['artifacts'][0]['readonly'] is True
    assert c.payload['artifacts'][-1]['location']=='attempt'

@pytest.mark.parametrize('kind',['tag','placeholder','missing-base','release','unknown-runtime','path','extra-key','conflict-pm','ranks'])
def test_invalid_container_config(context,kind):
    s=plain(context.system);v=plain(context.variants);e=plain(context.explicit);q=plain(context.requirements)
    image=q['default_image']
    if kind=='tag':image['uri']='registry.example/base:latest'
    elif kind=='placeholder':image['uri']='registry.example/base@sha256:'+'0'*64
    elif kind=='missing-base':q['default_image']='unknown'
    elif kind=='release':q['default_image']='unknown';v['container_release']=['not-there']
    elif kind=='unknown-runtime':v['container_runtime']=['unregistered-runtime']
    elif kind=='path':s['execution']['image_cache']='relative/cache'
    elif kind=='extra-key':s['execution']['runtime_typo']='apptainer'
    elif kind=='conflict-pm':e['package_manager']=['spack']
    else:v['n_ranks']=['2']
    with pytest.raises(ValidationError):resolve(changed(context,system=s,variants=v,explicit=e,requirements=q))


def test_local_sif_pinned(context,tmp_path):
    q=plain(context.requirements);p=tmp_path/'fake.sif';p.write_bytes(b'not-real-sif-test-only')
    q['default_image']={'uri':p.as_uri(),'managed':False,'tools':{'python':__import__('sys').executable,'shell':'bash'}}
    plan=resolve(changed(context,requirements=q)).payload
    assert plan['base']['sif_sha256']==sha256(p)

@pytest.mark.parametrize('mutation',['overlap','readonly-output','root-unknown','traversal','rw-input','reserved','duplicate'])
def test_mount_validation(context,mutation):
    q=plain(context.requirements)
    if mutation=='overlap':q['artifacts'][1]['target']='/bench'
    elif mutation=='readonly-output':q['artifacts'][-1]['readonly']=True
    elif mutation=='root-unknown':q['artifacts'][1]['root']='missing'
    elif mutation=='traversal':q['artifacts'][1]['path']='../outside'
    elif mutation=='rw-input':q['artifacts'][1]['readonly']=False
    elif mutation=='reserved':q['artifacts'][0]['target']='/bpce/script.py'
    else:q['artifacts'][1]['name']='script'
    with pytest.raises(ValidationError):resolve(changed(context,requirements=q))


def test_nested_requirements_snapshot(context):
    p=Path(context.source_root);(p/'req').mkdir();(p/'req'/'extra.txt').write_text('# extra\n');(p/'requirements.txt').write_text('-r req/extra.txt\n')
    c=resolve(context)
    assert 'inputs/req/extra.txt' in c.payload['resources']
    before=dict(c.payload['resources']);(p/'req'/'extra.txt').write_text('# different\n')
    assert before!=resolve(context).payload['resources']

@pytest.mark.parametrize('content',['-r /etc/passwd','-e .','../bad.whl','https://user:password@example.org/a.whl','--index-url https://x?token=abc'])
def test_unsafe_requirement(context,content):
    (Path(context.source_root)/'requirements.txt').write_text(content+'\n')
    with pytest.raises((ValidationError,ValueError)):resolve(context)

@pytest.mark.parametrize('value',[{'HF_TOKEN':'secret'}, {'API_KEY':'secret'},'https://u:p@host/a','token=123'])
def test_no_secret_configuration(value):
    with pytest.raises(ValidationError):check_no_secrets(value)


def test_tokenizer_and_token_lengths_allowed():
    check_no_secrets({'tokenizer':'file','input_tokens':128,'max_output_tokens':512})


def test_symlink_escape(tmp_path):
    root=tmp_path/'r';root.mkdir();out=tmp_path/'o';out.write_text('x');(root/'link').symlink_to(out)
    with pytest.raises(ValidationError):inside(root,'link',True)


def test_builtin_no_plugin_loading(installed):
    import argparse
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest='command')
    sub.add_parser('setup'); actions={'setup':lambda a:0}
    cli.register_commands(sub,actions,['setup','a','b'])
    assert all(e.loads==0 for e in installed if e.group != "benchpark.lifecycle.v1")


def test_help_only_metadata(installed,capsys):
    cli.print_extension_help();out=capsys.readouterr().out
    assert '+container' in out and 'cer' in out
    assert all(e.loads==0 for e in installed if e.group != "benchpark.lifecycle.v1")


def test_duplicate_plugin_error(installed):
    installed.append(installed[0])
    with pytest.raises(ExtensionError):core.load_unique('benchpark.extensions','container')


def test_no_environment_contribution_overwrite(context):
    from types import SimpleNamespace as S
    c=replace(resolve(context),environment={'X':'2'})
    ex=S(extension_contributions=(c,), helpers=[])
    data={'ramble':{'applications':{'a':{'workloads':{'w':{'experiments':{'e':{
          'variables':{},'env_vars':{'set':{'X':'1'}}}}}}}}}}
    with pytest.raises(ExtensionError):generation_port(ex).finish(data)


def test_software_provider_preserves_native_and_rejects_helpers(context):
    from types import SimpleNamespace as S
    native=S(helpers=[])
    assert generation_port(native).software(lambda:'native')=='native'
    ex=S(extension_contributions=(resolve(context),),
         helpers=[S(compute_package_section=lambda:{'packages':{'caliper':{}}})])
    with pytest.raises(ExtensionError,match='host/helper'):
        generation_port(ex).software(lambda:'must not be called')
    ex.helpers=[]
    assert generation_port(ex).software(lambda:'native')=={'packages':{},'environments':{}}


@pytest.mark.parametrize('broken',[object(), {'api_version':999,'name':'container'}])
def test_incompatible_descriptor_is_explicit_error(installed,broken):
    installed[0].factory=lambda:broken
    with pytest.raises(ExtensionError,match='Incompatible extension API/name'):
        core.load_unique('benchpark.extensions','container')
