# SPDX-License-Identifier: Apache-2.0
"""Static rules, automatic resolver integration, and CI/manual equivalence."""
import json
from pathlib import Path
import subprocess
import sys
import pytest
from benchpark_integration.api import plain
from benchpark_container.requirements import scan_requirements
from benchpark_container.reproducibility import (pin_image, validate_inputs, validate_request,
                                               validate_saved_plan, main)
from benchpark_container.reproducibility_rules import (validate_model_artifacts,
    validate_additions, ReproducibilityError)
from benchpark_container.resolver import resolve
from benchpark_container.util import ValidationError, sha256
from benchpark_container.runtime import concrete_plan
from test_storage_runtime import publish
from benchpark_integration.support.storage import STATE_DIR
from conftest import changed

COMMIT = '1234567890abcdef1234567890abcdef12345678'
DIGEST = '1234567890abcdef' * 4
URI = 'registry.example/base@sha256:' + DIGEST


@pytest.mark.parametrize('requirement', ['vllm', 'vllm>=0.11', 'vllm~=0.11', 'vllm==0.11.*',
    'vllm==latest', 'vllm===1.0', 'demo @ https://example.org/demo.whl', '-e .',
    '--upgrade', '--ignore-installed', '--no-deps', '--no-binary :all:'])
def test_unfixed_or_unsafe_requirements_fail_automatic_and_manual(context, requirement):
    root = Path(context.source_root)
    (root / 'requirements.txt').write_text(requirement + '\n')
    with pytest.raises(ValueError):
        validate_inputs(root, ['requirements.txt'])
    with pytest.raises(ValueError):
        resolve(context)


def test_fixed_extras_markers_nested_and_hashes(context):
    root = Path(context.source_root); (root/'req').mkdir()
    (root/'requirements.txt').write_text('-r req/a.lock\n-c req/c.txt\n')
    (root/'req/a.lock').write_text('demo[extra]==1.2.3; python_version < "3.11"\n'
                                'demo==2.0; python_version >= "3.11"\n')
    (root/'req/c.txt').write_text('dep==1.0 \\\n --hash=sha256:' + DIGEST + '\n')
    result = validate_inputs(root, ['requirements.txt'])
    assert result['requirements']['pins'] == {'demo': ['1.2.3', '2.0'], 'dep': ['1.0']}
    assert set(result['requirements']['files']) == {'requirements.txt', 'req/a.lock', 'req/c.txt'}
    assert 'dependency-resolution-in-selected-base' in result['pending']
    plan = resolve(context).payload
    assert plan['validation']['requirements'] == result['requirements']
    assert set('inputs/' + p for p in result['requirements']['files']).issubset(plan['resources'])


@pytest.mark.parametrize('body', ['-r b.txt\n', '--requirement=b.txt\n', '-rb.txt\n'])
def test_recursive_unpinned_dependency_cannot_bypass(body, tmp_path):
    # pytest supplies tmp_path; no package/network operations are performed.
    (tmp_path/'a.txt').write_text(body)
    (tmp_path/'b.txt').write_text('demo>=1\n')
    with pytest.raises(ValueError, match='Pin each'):
        scan_requirements(tmp_path, ['a.txt'])


def test_cycle_and_unconditional_conflict(tmp_path):
    (tmp_path/'a.txt').write_text('-r b.txt\n'); (tmp_path/'b.txt').write_text('-r a.txt\n')
    with pytest.raises(ValueError, match='Cyclic'):
        scan_requirements(tmp_path, ['a.txt'])
    (tmp_path/'a.txt').write_text('demo==1.0\ndemo==2.0\n')
    with pytest.raises(ValueError, match='Conflicting'):
        scan_requirements(tmp_path, ['a.txt'])


@pytest.mark.parametrize('uri', ['registry.example/base:latest', 'registry.example/base:1.0',
    'registry.example/base:latest@sha256:' + DIGEST, 'registry.example/base@sha256:'+'0'*64])
def test_mutable_or_fake_image_rejected(uri):
    with pytest.raises(ValueError): pin_image({'uri': uri})


def test_fixed_image_and_local_hash(tmp_path):
    assert pin_image({'uri': URI})['digest'] == 'sha256:' + DIGEST
    p=tmp_path/'test-only.sif'; p.write_bytes(b'test file, not an executable SIF')
    assert pin_image({'uri': p.as_uri()})['sif_sha256'] == sha256(p)
    with pytest.raises(ValueError, match='does not match'):
        pin_image({'uri': p.as_uri(), 'sha256': DIGEST})


@pytest.mark.parametrize('revision', [None, '', 'main', 'master', 'latest', 'v1.0', 'abc1234'])
def test_model_revision_unfixed_fails(context, revision):
    q=plain(context.requirements)
    a=q['artifacts'][1]
    a.pop('revision', None)
    if revision is not None: a['revision']=revision
    with pytest.raises(ReproducibilityError): resolve(changed(context,requirements=q))


def test_all_matrix_revisions_checked_at_init(context):
    q=plain(context.requirements); q['artifacts'][1]['revision']='{model_revision}'
    v=plain(context.variants); v['model_revision']=[COMMIT, 'main']
    with pytest.raises(ReproducibilityError): resolve(changed(context,requirements=q,variants=v))
    v['model_revision']=[COMMIT, COMMIT[::-1]]
    p=resolve(changed(context,requirements=q,variants=v)).payload
    assert p['validation']['models'][0]['mounted_content_verified'] is False
    c=concrete_plan(p,{'model':'model-a','size':'16','model_revision':COMMIT},['true'])
    assert c['artifacts'][1]['revision'] == COMMIT
    with pytest.raises(ReproducibilityError):
        concrete_plan(p,{'model':'model-a','size':'16','model_revision':'main'},['true'])


def test_synthetic_models_need_no_fictional_revision(context):
    q=plain(context.requirements); q['artifacts']=[a for a in q['artifacts'] if a['kind']!='model']
    result=resolve(changed(context,requirements=q))
    assert result.payload['validation']['models']==[]


def test_local_model_snapshot_or_manifest(context):
    root=Path(context.source_root)
    (root/'weights.bin').write_bytes(b'local test model')
    q=plain(context.requirements)
    q['artifacts'][1]={'name':'model','kind':'model','source':'weights.bin','target':'/models/weights.bin'}
    p=resolve(changed(context,requirements=q)).payload
    assert p['artifacts'][1]['sha256'] == sha256(root/'weights.bin')
    a={'name':'model','kind':'model','manifest':'manifest.json','manifest_sha256':DIGEST}
    assert validate_model_artifacts([a])[0]['identity_evidence']==['manifest-sha256-declared']


def test_tokenizer_is_checked_independently():
    with pytest.raises(ReproducibilityError):
        validate_model_artifacts([{'name':'model','kind':'model','revision':COMMIT},
            {'name':'tokenizer','kind':'tokenizer','revision':'main'}])


def test_resolver_has_no_implicit_torch_policy(context):
    q=plain(context.requirements); q.pop('protected_packages')
    assert resolve(changed(context,requirements=q)).payload['protected_packages']=={}


def test_cli_uses_same_static_rules_and_reports_limitations(context, repository, capsys):
    root=Path(context.source_root)
    request={'schema_version':1,'source_root':str(root),'image':{'uri':URI},
             'requirements':['requirements.txt'], 'artifacts':plain(context.requirements)['artifacts']}
    path=root/'validation.json';path.write_text(json.dumps(request))
    # CLI direct source files do not include resolved artifact SHA; validator fills local files.
    assert main(['--request',str(path)])==0
    data=json.loads(capsys.readouterr().out)
    auto=resolve(context).payload['validation']
    assert data['requirements']==auto['requirements'] and data['models']==auto['models']
    assert data['status']=='passed-static-validation' and data['pending']
    request['artifacts'][1]['revision']='latest';path.write_text(json.dumps(request))
    assert main(['--request',str(path)])==2
    assert json.loads(capsys.readouterr().out)['code']=='MODEL_REVISION_NOT_FIXED'
    proc=subprocess.run([sys.executable,str(repository/'tools/validate_reproducibility.py'),
                         '--request',str(path)],capture_output=True,text=True)
    assert proc.returncode==2 and json.loads(proc.stdout)['code']=='MODEL_REVISION_NOT_FIXED'


def test_saved_plan_revalidates_hashes_without_rewrite(context,tmp_path):
    _,source,_=publish(context,tmp_path)
    plan=source/STATE_DIR/'plans/container.json';resources=source/STATE_DIR/'resources'
    old=plan.read_bytes()
    assert validate_saved_plan(plan,resources)['status']=='passed-static-validation'
    assert plan.read_bytes()==old
    target=resources/'inputs/requirements.txt';target.chmod(0o644);target.write_text('demo==1.0\n')
    with pytest.raises(ValueError,match='resource changed'):
        validate_saved_plan(plan,resources)


def test_pip_report_obvious_base_conflict_is_structured():
    report={'version':'1','install':[{'metadata':{'name':'Any_Package','version':'2.0'}}]}
    with pytest.raises(ReproducibilityError) as err:
        validate_additions(report,{'any-package':['2.0']},{'any-package':'1.0'})
    assert err.value.code=='BASE_VERSION_CONFLICT'
    assert err.value.details['conflicts']==[{'name':'any-package','base_version':'1.0','requested_version':'2.0'}]


def test_runtime_helper_shares_same_validation_source(repository):
    import zipfile
    source=repository/'src/benchpark_container/reproducibility_rules.py'
    resources=repository/'src/benchpark_container/resources'
    with zipfile.ZipFile(resources/'runtime.pyz') as z:
        assert z.read('bpce_node/reproducibility_rules.py')==source.read_bytes()
    # Validation code is bundled in the host-side runtime only; it is not
    # separately mounted into Common Base.
    assert not (resources/'reproducibility_rules.py').exists()


def test_cli_saved_pip_report_calls_runtime_rules(tmp_path,capsys):
    base=tmp_path/'base.json';base.write_text(json.dumps({'packages':{'demo':'1.0'}}))
    pins=tmp_path/'pins.json';pins.write_text(json.dumps({'demo':['2.0']}))
    report=tmp_path/'pip-plan.json';report.write_text(json.dumps({'version':'1','install':[{'metadata':{'name':'demo','version':'2.0'}}]}))
    assert main(['--pip-report',str(report),'--base-inventory',str(base),'--pins',str(pins)])==2
    result=json.loads(capsys.readouterr().out)
    assert result['code']=='BASE_VERSION_CONFLICT'
    assert result['details']['conflicts'][0]['base_version']=='1.0'


@pytest.mark.parametrize('bad', [{'demo':'1.0'}, {'demo':[]}, {'demo':[1]}, ['demo']])
def test_malformed_pin_map_fails_closed(bad):
    with pytest.raises(ReproducibilityError,match='Pins must'):
        validate_additions({'version':'1','install':[]},bad)


def test_missing_source_root_is_not_static_success(tmp_path):
    with pytest.raises(ValueError,match='existing directory'):
        validate_inputs(tmp_path/'missing',[])


def test_report_schema_is_not_assumed():
    with pytest.raises(ReproducibilityError,match='version 1'):
        validate_additions({'version':'99','install':[]},{})

@pytest.mark.parametrize('line', [
    './wheelhouse/demo-1.0-py3-none-any.whl',
    '../demo.whl',
    '/tmp/demo.whl',
    'file:///tmp/demo.whl',
    'git+https://example.org/repo.git@0123456789abcdef0123456789abcdef01234567',
    'demo @ https://example.org/demo-1.0-py3-none-any.whl',
])
def test_python_package_payloads_do_not_cross_requirement_boundary(tmp_path, line):
    (tmp_path/'requirements.txt').write_text(line+'\n')
    with pytest.raises(ValueError):
        scan_requirements(tmp_path, ['requirements.txt'])


def test_requirement_snapshot_contains_text_files_only(tmp_path):
    (tmp_path/'nested.txt').write_text('dep==2.0\n')
    (tmp_path/'requirements.txt').write_text('-r nested.txt\ndemo==1.0\n')
    result=scan_requirements(tmp_path,['requirements.txt'])
    assert set(result['files'])=={'requirements.txt','nested.txt'}
    assert result['boundary']=='requirements-text-only'
