# SPDX-License-Identifier: Apache-2.0
import json
from pathlib import Path
import pytest
from verify_upstream import planned_attempts, validate_attempt_evidence, validate_ramble_analysis


def make_record(root, name='exp.1', model='linear', size=16):
    root.mkdir(parents=True,exist_ok=True)
    (root/'outputs/result').mkdir(parents=True)
    d={'status':'COMPLETED','ramble_experiment':name,'repeat_index':'1','condition_id':model+str(size),
       'resolved':{'parameters':{'model':model,'size':str(size)}},
       'observed':{'software_environment':{'state':'ready','package_manager':{'source':'common-base'}}}}
    p=root/'cer.json';p.write_text(json.dumps(d))
    (root/'outputs/result/result.json').write_text(json.dumps({'model':model,'size':size,'rate':1.2,'setup_tool':'bpce-demo-tool-ok'}))
    (root/'benchmark.log').write_text('BPCE_SMOKE_OK\n')
    return p,(name,'1',model+str(size))


def test_attempt_gate_checks_identity_not_minimum_count(tmp_path):
    p,key=make_record(tmp_path/'a')
    assert validate_attempt_evidence([p],[key])['identities']=='matched'
    with pytest.raises(RuntimeError): validate_attempt_evidence([p],[key,key])
    with pytest.raises(RuntimeError): validate_attempt_evidence([p,p],[key])
    with pytest.raises(RuntimeError): validate_attempt_evidence([p],[('other','1','linear16')])


@pytest.mark.parametrize('fault',['missing-output','wrong-size','nan-rate','missing-marker','wrong-installer'])
def test_exit_zero_is_not_sufficient_evidence(tmp_path,fault):
    p,key=make_record(tmp_path/'a')
    result=p.parent/'outputs/result/result.json'
    if fault=='missing-output':result.unlink()
    if fault in ('wrong-size','nan-rate'):
        d=json.loads(result.read_text());d['size' if fault=='wrong-size' else 'rate']=32 if fault=='wrong-size' else 'NaN';result.write_text(json.dumps(d))
    if fault=='missing-marker':(p.parent/'benchmark.log').write_text('')
    if fault=='wrong-installer':
        d=json.loads(p.read_text());d['observed']['software_environment']['package_manager']['source']='injected';p.write_text(json.dumps(d))
    with pytest.raises(RuntimeError):validate_attempt_evidence([p],[key])


def test_analysis_gate_requires_success_fom_and_full_name(tmp_path):
    path=tmp_path/'results.latest.json'
    good={'name':'app.work.exp.1','EXPERIMENT_STATUS':'SUCCESS','CONTEXTS':[{'foms':[{'name':'toy_rate','value':1.0}]}]}
    path.write_text(json.dumps({'experiments':[good]}))
    expected=[('exp.1','1','identity')]
    assert validate_ramble_analysis(tmp_path,expected)['status']=='passed'
    good['CONTEXTS']=[];path.write_text(json.dumps({'experiments':[good]}))
    with pytest.raises(RuntimeError):validate_ramble_analysis(tmp_path,expected)
    good['EXPERIMENT_STATUS']='FAILED';path.write_text(json.dumps({'experiments':[good]}))
    with pytest.raises(RuntimeError):validate_ramble_analysis(tmp_path,expected)


def test_expected_plans_cover_selected_matrix(tmp_path):
    root=tmp_path/'.benchpark-extensions/concrete';root.mkdir(parents=True)
    for model in ('linear','mlp'):
        for size in ('16','32'):
            d={'parameters':{'model':model,'size':size},'ramble_experiment':model+size,'repeat_index':'1','condition_id':model+size}
            (root/(model+size+'.json')).write_text(json.dumps(d))
    assert len(planned_attempts(tmp_path))==4
    (root/'mlp32.json').unlink()
    with pytest.raises(RuntimeError):planned_attempts(tmp_path)




def test_planned_attempts_excludes_ramble_repeat_base(tmp_path):
    root=tmp_path/'.benchpark-extensions/concrete';root.mkdir(parents=True)
    for model in ('linear','mlp'):
        for size in ('16','32'):
            condition=model+size
            base={'parameters':{'model':model,'size':size,'n_repeats':'1'},
                  'ramble_experiment':condition,'repeat_index':'unknown','condition_id':condition}
            child={'parameters':{'model':model,'size':size,'n_repeats':'1'},
                   'ramble_experiment':condition+'.1','repeat_index':'1','condition_id':condition}
            (root/(condition+'-base.json')).write_text(json.dumps(base))
            (root/(condition+'-child.json')).write_text(json.dumps(child))
    keys=planned_attempts(tmp_path)
    assert len(keys)==4
    assert {k[1] for k in keys}=={'1'}


def test_planned_attempts_keeps_singleton_with_unknown_repeat_index(tmp_path):
    root=tmp_path/'.benchpark-extensions/concrete';root.mkdir(parents=True)
    for model in ('linear','mlp'):
        for size in ('16','32'):
            condition=model+size
            d={'parameters':{'model':model,'size':size,'n_repeats':'0'},
               'ramble_experiment':condition,'repeat_index':'unknown','condition_id':condition}
            (root/(condition+'.json')).write_text(json.dumps(d))
    assert len(planned_attempts(tmp_path))==4

def test_analysis_summary_spread_is_not_a_throughput_sample(tmp_path):
    path=tmp_path/'results.latest.json'
    foms=[{'name':'toy_rate','origin_type':'summary::mean','value':1.0},
          {'name':'toy_rate','origin_type':'summary::stdev','value':0.0},
          {'name':'toy_rate','origin_type':'summary::variance','value':'NA'}]
    row={'name':'app.work.exp','RAMBLE_STATUS':'SUCCESS','CONTEXTS':[{'foms':foms}]}
    path.write_text(json.dumps({'experiments':[row]}))
    expected=[('exp','0','identity')]
    assert validate_ramble_analysis(tmp_path,expected)['status']=='passed'
    # A spread/count without a rate does not prove a benchmark was measured.
    foms.pop(0)
    path.write_text(json.dumps({'experiments':[row]}))
    with pytest.raises(RuntimeError):validate_ramble_analysis(tmp_path,expected)
