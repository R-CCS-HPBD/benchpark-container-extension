# SPDX-License-Identifier: Apache-2.0
"""Functional PyTorch check, not a validated performance methodology."""
import argparse
import json
from pathlib import Path
import time
import subprocess
import os
import shutil
import torch

p=argparse.ArgumentParser();p.add_argument('--model',choices=('linear','mlp'),required=True)
p.add_argument('--size',type=int,required=True);p.add_argument('--steps',type=int,required=True);p.add_argument('--warmup',type=int,required=True)
p.add_argument('--execution-mode',choices=('no-grad','inference'),required=True)
a=p.parse_args()
if min(a.size,a.steps)<1 or a.warmup<0:
    p.error('positive size/steps and non-negative warmup required')
settings=json.loads(Path('/bench/workload.json').read_text())
torch.manual_seed(settings['seed']);torch.set_num_threads(1)
net=torch.nn.Linear(a.size,a.size)
if a.model=='mlp':net=torch.nn.Sequential(net,torch.nn.ReLU(),torch.nn.Linear(a.size,a.size))
x=torch.ones((1,a.size));net.eval()
context=torch.inference_mode() if a.execution_mode=='inference' else torch.no_grad()
with context:
    for _ in range(a.warmup):net(x)
    start=time.perf_counter()
    for _ in range(a.steps):y=net(x)
    elapsed=time.perf_counter()-start
if tuple(y.shape)!=(1,a.size) or not torch.isfinite(y).all():
    raise RuntimeError('Toy model output invalid')
tool_path=shutil.which('bpce-demo-tool')
if tool_path is None: raise RuntimeError('setup did not provide bpce-demo-tool')
tool=subprocess.check_output([os.environ['BPCE_BASE_SHELL'],tool_path], text=True).strip()
if tool!='bpce-demo-tool-ok': raise RuntimeError('Installed tool did not work')
result={'execution_mode':a.execution_mode,'model':a.model,'size':a.size,'steps':a.steps,'seconds':elapsed,'rate':a.steps/elapsed,
        'torch_version':torch.__version__,'torch_origin':torch.__file__,'setup_tool':tool,
        'note':'CPU functional fixture, not an LLM/GPU performance claim'}
Path('/results/result.json').write_text(json.dumps(result,indent=2)+'\n')
print('BPCE_TOY_RATE=%.6f'%result['rate'])
print('BPCE_SMOKE_OK')
