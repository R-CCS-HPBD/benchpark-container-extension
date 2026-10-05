# SPDX-License-Identifier: Apache-2.0
"""Host/NVIDIA/AMD hardware evidence for CER schema v2 (additive extension v1)."""
from __future__ import annotations
import csv
import datetime
import io
import json
import os
from pathlib import Path
import platform
import re

from .base import CollectorContext, CollectorResult, CommandRunner, json_maybe, record_command

_VISIBLE = ('CUDA_VISIBLE_DEVICES', 'ROCR_VISIBLE_DEVICES', 'HIP_VISIBLE_DEVICES')


def _meminfo():
    p = Path('/proc/meminfo')
    if not p.is_file():
        return {}
    data = {}
    for line in p.read_text(errors='replace').splitlines():
        if ':' in line:
            k, v = line.split(':', 1); data[k] = v.strip()
    return {k: data[k] for k in ('MemTotal','HugePages_Total','Hugepagesize') if k in data}


def _generic_amd_devices(value):
    """Extract identity-ish fields without binding CER to one AMD SMI JSON revision."""
    out=[]; seen=set()
    interesting=('uuid','bdf','market','name','device_id','serial','oam','partition','gfx','vendor')
    def walk(x):
        if isinstance(x, dict):
            selected={}
            for k,v in x.items():
                lk=str(k).lower()
                if any(t in lk for t in interesting) and isinstance(v,(str,int,float,bool,type(None))):
                    selected[str(k)] = v
            if selected and any('uuid' in k.lower() or 'bdf' in k.lower() or 'market' in k.lower() for k in selected):
                sig=json.dumps(selected,sort_keys=True,default=str)
                if sig not in seen:
                    seen.add(sig); out.append(selected)
            for v in x.values(): walk(v)
        elif isinstance(x,list):
            for v in x: walk(v)
    walk(value)
    return out


def _rocminfo_devices(text):
    devices=[]; cur={}
    for line in text.splitlines():
        m=re.match(r'\s*(Name|Uuid|Marketing Name|Vendor Name):\s*(.+?)\s*$', line)
        if not m: continue
        k=m.group(1).lower().replace(' ','_'); v=m.group(2).strip()
        if k=='name' and cur and ('uuid' in cur or 'marketing_name' in cur):
            devices.append(cur); cur={}
        cur[k]=v
    if cur and ('uuid' in cur or 'marketing_name' in cur): devices.append(cur)
    return devices


def _collect_nvidia(ctx, runner):
    r=CollectorResult('nvidia')
    if not runner.which('nvidia-smi'):
        r.status='unavailable'; return r
    query=('index','uuid','pci.bus_id','name','driver_version','memory.total')
    cp=record_command(ctx,r,runner,'hardware/nvidia/identity',
        ['nvidia-smi','--query-gpu='+','.join(query),'--format=csv,noheader,nounits'])
    devices=[]
    if cp.ok:
        for row in csv.reader(io.StringIO(cp.stdout), skipinitialspace=True):
            if row:
                devices.append({k.replace('.','_'):v.strip() for k,v in zip(query,row)})
    # State/config query is separate so an unsupported field cannot hide identity.
    state=('index','pstate','power.limit','clocks.current.graphics','clocks.current.memory')
    state_cp=record_command(ctx,r,runner,'hardware/nvidia/state',
        ['nvidia-smi','--query-gpu='+','.join(state),'--format=csv,noheader,nounits'])
    state_devices=[]
    if state_cp.ok:
        for row in csv.reader(io.StringIO(state_cp.stdout), skipinitialspace=True):
            if not row: continue
            state_devices.append({k.replace('.','_'):v.strip() for k,v in zip(state,row)})
    record_command(ctx,r,runner,'hardware/nvidia/list',['nvidia-smi','-L'])
    r.observed={'accelerator':{
        'vendor':'nvidia','collector':'nvidia-smi','devices':devices,
        'post_execution_state':{
            'capture_phase':'post-execution-finalization',
            'captured_at':datetime.datetime.now(datetime.timezone.utc).isoformat(),
            'devices':state_devices,
            'evidence':['cer-evidence/hardware/nvidia/state.stdout.log'],
        },
    }}
    return r


def _collect_amd(ctx, runner):
    r=CollectorResult('amd')
    has_smi=bool(runner.which('amd-smi')); has_rocminfo=bool(runner.which('rocminfo'))
    if not has_smi and not has_rocminfo:
        r.status='unavailable'; return r
    devices=[]; version=None
    if has_smi:
        version_cp=record_command(ctx,r,runner,'hardware/amd/version',['amd-smi','version','--json'])
        version=json_maybe(version_cp.stdout) if version_cp.ok else None
        list_cp=record_command(ctx,r,runner,'hardware/amd/list',['amd-smi','list','--json'])
        if list_cp.ok:
            parsed=json_maybe(list_cp.stdout)
            if parsed is not None: devices.extend(_generic_amd_devices(parsed))
        # Full static/configuration and metric evidence. These are deliberately
        # retained as evidence rather than hard-coding one AMD SMI JSON schema.
        record_command(ctx,r,runner,'hardware/amd/static',['amd-smi','static','--json'])
        record_command(ctx,r,runner,'hardware/amd/metric',['amd-smi','metric','--json'])
    if has_rocminfo and not devices:
        cp=record_command(ctx,r,runner,'hardware/amd/rocminfo',['rocminfo'],timeout=30.0)
        if cp.ok: devices=_rocminfo_devices(cp.stdout)
    accelerator={'vendor':'amd','collector':'amd-smi' if has_smi else 'rocminfo',
                 'version':version,'devices':devices}
    if has_smi:
        accelerator['static_configuration_evidence']=['cer-evidence/hardware/amd/static.stdout.log']
        accelerator['post_execution_state']={
            'capture_phase':'post-execution-finalization',
            'captured_at':datetime.datetime.now(datetime.timezone.utc).isoformat(),
            'evidence':['cer-evidence/hardware/amd/metric.stdout.log'],
        }
    r.observed={'accelerator':accelerator}
    return r


def collect_hardware(ctx, runner=None):
    runner=runner or CommandRunner(); r=CollectorResult('hardware')
    u=platform.uname()
    hardware={'schema_version':1,
              'host':{'hostname':u.node,'os':u.system,'kernel':u.release,'machine':u.machine,
                      'cpu_count_logical':os.cpu_count(),'memory':_meminfo()},
              'device_visibility':{k:ctx.env[k] for k in _VISIBLE if k in ctx.env},
              'accelerators':[]}
    if runner.which('lscpu'):
        cp=record_command(ctx,r,runner,'hardware/host/lscpu',['lscpu','-J'])
        parsed=json_maybe(cp.stdout) if cp.ok else None
        if parsed is not None: hardware['host']['lscpu']=parsed
    if runner.which('numactl'):
        record_command(ctx,r,runner,'hardware/host/numactl-H',['numactl','-H'])
    for fn in (_collect_nvidia,_collect_amd):
        child=fn(ctx,runner)
        r.files.update(child.files)
        if child.status!='unavailable':
            hardware['accelerators'].append(child.observed['accelerator'])
        if child.status=='partial':
            r.status='partial'; r.errors.extend(child.errors)
    r.observed={'hardware':hardware}
    return r
