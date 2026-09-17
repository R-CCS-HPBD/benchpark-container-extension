# SPDX-License-Identifier: Apache-2.0
"""The operator probe itself is tested without claiming native runtime success."""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
from zipfile import ZipFile

import pytest
import verify_runtime


def arguments(tmp_path, runtime):
    requirement = tmp_path / 'extra.txt'
    requirement.write_text('# no packages for fixture generation test\n')
    return argparse.Namespace(runtime=runtime, runtime_executable='bpce-nonexistent-runtime-7dcf',
        requirements=requirement, backend_options=None, image='registry.example/base@sha256:'+'0123456789abcdef'*4,
        gpu='none', base_python='python3', base_shell='bash', expected_gpus=0, timeout_seconds=30)


@pytest.mark.parametrize('name',['apptainer','singularity','docker'])
def test_real_probe_fixture_resolves_and_stages_immutable_worker(tmp_path,name):
    args=arguments(tmp_path,name)
    out=tmp_path/'fixture-test';out.mkdir()
    resources, plan=verify_runtime.make_fixture(args,out)
    assert plan['runtime']['runtime']==name
    assert plan['base']['uri']==args.image
    assert (resources/'runtime.pyz').is_file()
    with ZipFile(resources/'runtime.pyz') as z:
        assert 'bpce_node/backends/'+name+'.py' in z.namelist()
    assert plan['source_provenance']
    assert (out/'fixture/.git').is_dir()


def test_generated_setup_script_builds_and_runs_declared_tool(tmp_path):
    script=tmp_path/'setup.sh';script.write_text(verify_runtime.SETUP)
    prefix=tmp_path/'prefix'
    env=dict(os.environ,BPCE_PREFIX=str(prefix),BPCE_BASE_PYTHON=sys.executable)
    result=subprocess.run(['bash',str(script)],capture_output=True,text=True,env=env)
    assert result.returncode==0,result.stderr
    result=subprocess.run(['bash',str(prefix/'bin/bpce-validation-tool')],capture_output=True,text=True)
    assert result.returncode==0
    assert result.stdout=='bpce-validation-tool-ok\n'


def test_real_probe_missing_runtime_is_failed_not_smoke_pass(repository,tmp_path):
    args=arguments(tmp_path,'docker');out=tmp_path/'missing-runtime'
    command=[sys.executable,str(repository/'tools/verify_runtime.py'),'--runtime',args.runtime,
        '--runtime-executable',args.runtime_executable,'--image',args.image,
        '--requirements',str(args.requirements),'--base-python',args.base_python,
        '--base-shell',args.base_shell,'--output',str(out)]
    result=subprocess.run(command,capture_output=True,text=True,timeout=30)
    assert result.returncode==1,result.stdout+result.stderr
    report=json.loads((out/'report.json').read_text())
    assert report['status']=='FAILED'
    assert report['attempts']==[]
    records=list((out/'runs').glob('*/cer.json'))
    assert len(records)==1,result.stdout+result.stderr
    record=json.loads(records[0].read_text())
    assert record['status']=='PREPARATION_FAILED'
    assert 'not found' in json.dumps(record)
