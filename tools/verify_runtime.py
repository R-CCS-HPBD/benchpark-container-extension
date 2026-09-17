#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Real runtime acceptance probe using the packaged node worker.

This is NOT a Benchpark/Spack E2E certificate. The operator supplies an immutable
image with its own Python/pip/Bash, and a pinned requirements file. No fake or
native backend is used. Creates a private, Git-tracked validation fixture only.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'src'))
from benchpark_integration.api import ResolutionContext
from benchpark_container.resolver import resolve
from benchpark_container.runtime import concrete_plan
from benchpark_container.util import atomic_json, sha256
from benchpark_container.cli import load_record

WORKLOAD = r'''
import errno,json,os,subprocess
from pathlib import Path
input_file=Path('/validation/input.txt')
assert input_file.read_text()=='bpce-readonly-input\n'
try:
    with input_file.open('a') as stream:stream.write('unexpected modification')
except OSError as error:
    assert error.errno in (errno.EROFS,errno.EACCES,errno.EPERM),error
else:
    raise RuntimeError('Read-only input was writable')
assert Path.cwd()==Path('/bpce/work')
tool=subprocess.check_output([os.environ['BPCE_BASE_SHELL'],'/bpce/tools/bin/bpce-validation-tool'],text=True).strip()
assert tool=='bpce-validation-tool-ok'
data={'readonly_input':True,'setup_tool':tool,'workdir':str(Path.cwd()),'uid':os.getuid(),'gid':os.getgid()}
gpu=os.environ['VALIDATION_GPU']
if gpu!='none':
    import torch
    assert torch.cuda.is_available(),'No GPU visible to the image PyTorch'
    assert bool(torch.version.hip)==(gpu=='amd'),'Framework GPU backend mismatch'
    assert bool(torch.version.cuda)==(gpu=='nvidia'),'Framework GPU backend mismatch'
    expected=int(os.environ['VALIDATION_GPU_COUNT'])
    if expected:assert torch.cuda.device_count()==expected,'Unexpected number of GPUs visible'
    a=torch.ones((64,64),device='cuda',dtype=torch.float32)
    b=a@a;torch.cuda.synchronize()
    assert b.is_cuda and b.sum().item()==262144.0
    data['gpu']={'backend':gpu,'count':torch.cuda.device_count(),'name':torch.cuda.get_device_name(0),'computed_on_gpu':True}
else:
    data['gpu']={'backend':'none','checked':False}
Path('/results/smoke.json').write_text(json.dumps(data,sort_keys=True)+'\n')
print('BPCE_RUNTIME_SMOKE=PASS',flush=True)
'''
SETUP = r'''
set -euo pipefail
: "${BPCE_PREFIX:?}" "${BPCE_BASE_PYTHON:?}"
"$BPCE_BASE_PYTHON" - <<'PYTHON'
import os
from pathlib import Path
p=Path(os.environ['BPCE_PREFIX'])/'bin'/'bpce-validation-tool'
p.parent.mkdir(parents=True,exist_ok=True)
p.write_text("printf '%s\\n' 'bpce-validation-tool-ok'\n")
p.chmod(0o755)
PYTHON
'''


def make_fixture(args, out):
    source=out/'fixture';source.mkdir()
    (source/'requirements.txt').write_bytes(args.requirements.read_bytes())
    (source/'setup.sh').write_text(SETUP)
    (source/'smoke.py').write_text(WORKLOAD)
    (source/'input.txt').write_text('bpce-readonly-input\n')
    # Setup provenance is explicitly a generated validation fixture, not a
    # claim that this temporary commit is the user's experiment repository.
    for command in (['git','init','-q',str(source)],['git','-C',str(source),'add','.'],
        ['git','-C',str(source),'-c','user.name=BPCE validation fixture','-c','user.email=fixture@invalid','commit','-qm','Generated runtime acceptance fixture']):
        subprocess.run(command,check=True,capture_output=True)
    execution={'runtime':args.runtime,'executable':args.runtime_executable,'worker_python':sys.executable,
               'image_cache':str(out/'image-cache'),'gpu':args.gpu}
    if args.backend_options:
        execution['backend_options']=json.loads(args.backend_options.read_text())
    import platform
    native={'aarch64':'linux/arm64','x86_64':'linux/amd64'}[platform.machine()]
    backend={'executable':execution['executable']}
    if 'backend_options' in execution: backend['backend_options']=execution['backend_options']
    system={'schema_version':3,'execution':{k:execution[k] for k in ('worker_python','image_cache','gpu')},
            'default_runtime':args.runtime,'runtimes':{args.runtime:backend},'platform':native,'artifact_roots':{}}
    requirements={'schema_version':2,'default_image':{'uri':args.image,'managed':False,
            'tools':{'python':args.base_python,'shell':args.base_shell}},'default_release':'fixed','executables':['smoke'],
        'requirements':['requirements.txt'],'setup_scripts':['setup.sh'],
        'artifacts':[{'name':'script','kind':'script','source':'smoke.py','target':'/validation/smoke.py'},
                     {'name':'input','kind':'config','source':'input.txt','target':'/validation/input.txt'},
                     {'name':'result','kind':'result','target':'/results'}],
        'environment':{'VALIDATION_GPU':args.gpu,'VALIDATION_GPU_COUNT':str(args.expected_gpus)},
        'timeout_seconds':args.timeout_seconds,
        'protected_packages':{'torch':'torch'} if args.gpu!='none' else {}}
    if getattr(args, 'catalog_image', None):
        requirements['default_image']=args.catalog_image
        requirements['default_release']=args.catalog_release
    context=ResolutionContext(name='runtime-acceptance-fixture',explicit={},variants={},system=system,
                              requirements=requirements,source_root=str(source))
    contribution=resolve(context)
    resources=out/'resources';resources.mkdir()
    for resource in contribution.resources:
        destination=resources/resource.target;destination.parent.mkdir(parents=True,exist_ok=True)
        shutil.copy2(resource.source,destination)
        if sha256(destination)!=resource.sha256:raise RuntimeError('Staged fixture hash differs')
    plan=concrete_plan(contribution.payload,{},[shlex.quote(args.base_python)+' /validation/smoke.py'],experiment='runtime-acceptance-fixture',repeat='1')
    atomic_json(out/'spec.json',plan)
    return resources,plan


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--runtime',required=True,help='Registry backend name (apptainer/singularity/docker)')
    p.add_argument('--runtime-executable',required=True)
    image=p.add_mutually_exclusive_group(required=True)
    image.add_argument('--image',help='Direct immutable URI: runtime probe only, not a managed-store test')
    image.add_argument('--catalog-image',help='Registered catalog:name, source bytes retained in managed store')
    p.add_argument('--catalog-release',help='Required with --catalog-image')
    p.add_argument('--requirements',type=Path,required=True,help='Pinned, self-contained requirements text suitable for this Base')
    p.add_argument('--base-python',required=True)
    p.add_argument('--base-shell',required=True)
    p.add_argument('--backend-options',type=Path)
    p.add_argument('--gpu',choices=['none','nvidia','amd'],default='none')
    p.add_argument('--expected-gpus',type=int,default=0,help='0 means do not check an exact count')
    p.add_argument('--require-additions',action='store_true',help='Fail unless pip actually installs a nonempty additional layer')
    p.add_argument('--timeout-seconds',type=int,default=600)
    p.add_argument('--attempts',type=int,default=2)
    p.add_argument('--output',type=Path,required=True)
    a=p.parse_args()
    if bool(a.catalog_image) != bool(a.catalog_release):p.error('--catalog-image and --catalog-release are used together')
    if a.attempts<1 or a.expected_gpus<0:p.error('Invalid attempts or expected-gpus')
    out=a.output.resolve();out.mkdir(parents=True,exist_ok=False)
    report={'status':'RUNNING','runtime':a.runtime,'gpu':a.gpu,'attempts':[],
            'scope':'real container runtime / packaged node worker; NOT Benchpark or Spack E2E',
            'source_version':__import__('benchpark_container').__version__}
    code=1
    try:
        resources,plan=make_fixture(a,out)
        report['source_provenance']=plan['source_provenance']
        report['image_selection']=plan['image_selection']
        report['runtime_selection']=plan['runtime_selection']
        old={}
        for index in range(a.attempts):
            command=[sys.executable,str(resources/'runtime.pyz'),'--spec',str(out/'spec.json'),
                     '--spec-sha256',sha256(out/'spec.json'),'--resources',str(resources),'--run-root',str(out/'runs')]
            with (out/('worker-%02d.log'%index)).open('wb') as log:
                result=subprocess.run(command,stdout=log,stderr=subprocess.STDOUT)
            new=[path for path in (out/'runs').glob('*/cer.json') if str(path) not in old]
            if len(new)!=1:raise RuntimeError('Expected exactly one new final CER per attempt')
            record=load_record(new[0])
            if result.returncode or record['status']!='COMPLETED':
                raise RuntimeError('Runtime attempt failed: '+str(new[0]))
            if any(Path(path).read_bytes()!=data for path,data in old.items()):
                raise RuntimeError('Prior attempt was modified')
            result_path=new[0].parent/'outputs/result/smoke.json'
            smoke=json.loads(result_path.read_text())
            additions=record['observed']['software_environment'].get('resolved_additions',[])
            if a.require_additions and not additions:
                raise RuntimeError('No additional package layer was exercised; this is not a dependency-installation PASS')
            report['attempts'].append({'cer':str(new[0].relative_to(out)),'cer_sha256':sha256(new[0]),
                'runtime':record['observed']['runtime'],'image':record['observed']['image'],'smoke':smoke,
                'additional_layer_nonempty':bool(additions)})
            old[str(new[0])]=new[0].read_bytes()
        report.update(status='PASS_RUNTIME_SMOKE',retry_records_preserved=True,
                      additional_dependency_installation='PASS' if all(x['additional_layer_nonempty'] for x in report['attempts']) else 'NOT_EXERCISED')
        code=0
    except Exception as error:
        report.update(status='FAILED',error_type=type(error).__name__,error=str(error))
        print(str(error),file=sys.stderr)
    finally:
        atomic_json(out/'report.json',report)
    print(out/'report.json')
    return code


if __name__=='__main__':
    raise SystemExit(main())
