# SPDX-License-Identifier: Apache-2.0
"""Real Unix process signals; backend and environment preparation are doubles."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

import pytest

SCRIPT = r'''
import json,os,sys
from pathlib import Path
sys.path.insert(0,sys.argv[1])
from benchpark_container import runtime as r
from benchpark_container.backends.base import RuntimeBackend,ImageRef
from benchpark_container.preparation import _container_run
root=Path(sys.argv[2]);phase=sys.argv[3]
class SignalAdapter(RuntimeBackend):
    name='signal-test-double'
    def __init__(self,settings):
        self.events=[];self.attempt=None;self.executable=sys.executable
    def observe_runtime(self):return {'name':self.name,'version':'fixture','executable':sys.executable}
    def resolve_image(self,*a):return ImageRef('sif','fixture','sha256:fixture')
    def argv(self,*a,**kw):
        script='import os,time; from pathlib import Path; Path(%r).write_text(str(os.getpid())); time.sleep(60)' % str(root/'child-ready')
        return [sys.executable,'-u','-c',script]
    def close(self):
        (root/'closed').write_text('yes')
def prepare(rt,image,scratch,inputs,mounts,spec,attempt,timeout):
    if phase=='preparation':_container_run(rt,image,scratch,inputs,mounts,[],timeout)
    return {'state':'ready','tools':{'shell':'fixture'},'runtime_environment':{}}
r.create_backend=lambda settings:SignalAdapter(settings)
r.prepare_environment=prepare
spec={'condition_id':'signal-fixture','resources':{},'runtime':{},'base':{},'artifacts':[],
      'environment':{},'command':['ignored'],'timeout_seconds':60}
sys.exit(r.execute(spec,root,root/'runs'))
'''


@pytest.mark.parametrize('phase',['preparation','benchmark'])
@pytest.mark.parametrize('signum',[signal.SIGTERM,signal.SIGINT])
def test_external_signal_cleans_child_and_writes_interrupted_cer(repository,tmp_path,phase,signum):
    log=tmp_path/'driver.log'
    with log.open('wb') as stream:
        child=subprocess.Popen([sys.executable,'-c',SCRIPT,str(repository/'src'),str(tmp_path),phase],stdout=stream,stderr=stream)
        try:
            deadline=time.monotonic()+10
            while not (tmp_path/'child-ready').is_file() and child.poll() is None and time.monotonic()<deadline:
                time.sleep(0.02)
            assert (tmp_path/'child-ready').is_file(),log.read_text()
            descendant=int((tmp_path/'child-ready').read_text())
            child.send_signal(signum)
            assert child.wait(timeout=15)==128+signum,log.read_text()
            with pytest.raises(ProcessLookupError):
                os.kill(descendant,0)
        finally:
            if child.poll() is None:
                child.kill();child.wait(timeout=5)
    record=json.loads(next((tmp_path/'runs').glob('*/cer.json')).read_text())
    assert record['status']=='INTERRUPTED'
    assert record['result']['failure_phase']==phase
    assert record['result']['exit_code']==128+signum
    assert (tmp_path/'closed').read_text()=='yes'
