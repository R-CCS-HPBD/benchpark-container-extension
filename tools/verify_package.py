#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Install the real wheel into a temporary target and test actual entry points.

Uses a patched, explicitly labeled CLI fixture, not a real upstream checkout.
No source-tree extension import or editable install may satisfy this test.
"""
import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
from patch_core import transform

ROOT=Path(__file__).resolve().parents[1]


def require(value, message):
    if not value: raise RuntimeError(message)


def verify(wheel):
    with tempfile.TemporaryDirectory(prefix='bpce-package-') as tmp:
        tmp=Path(tmp);target=tmp/'installed';lib=tmp/'cli/lib';lib.mkdir(parents=True)
        subprocess.run([sys.executable,'-m','pip','install','--no-deps','--no-index','--target',str(target),str(Path(wheel).resolve())],check=True,capture_output=True)
        shutil.copytree(ROOT/'core/files/lib/benchpark',lib/'benchpark',ignore=shutil.ignore_patterns('__pycache__'))
        original=(ROOT/'tests/fixtures/upstream_like/lib/main.py').read_text()
        (lib/'main.py').write_text(transform('lib/main.py',original))
        env=dict(os.environ);env['PYTHONPATH']=os.pathsep.join((str(lib),str(target)));env['PYTHONNOUSERSITE']='1'
        info=subprocess.check_output([sys.executable,'-c',
            'import benchpark_container,importlib.metadata,json; '
            'print(json.dumps({"version":benchpark_container.__version__,"origin":benchpark_container.__file__, '
            '"entries":sorted(e.name for e in importlib.metadata.entry_points(group="benchpark.extensions"))}))'],
            env=env,cwd=tmp,text=True)
        data=json.loads(info)
        require(Path(data['origin']).is_relative_to(target) and 'container' in data['entries'], 'Not using the installed wheel')
        # The deployed wheel contains the host-side runtime adapter, but must
        # NOT ship a package manager/interpreter/installer into Common Base.
        resources=Path(data['origin']).parent/'resources'
        require((resources/'runtime.pyz').is_file(), 'Host runtime bundle missing')
        require(not (resources/'installer').exists(), 'Forbidden installer payload')
        require(not (resources/'activation').exists(), 'Forbidden activation payload')
        require(not (resources/'environment.py').exists(), 'Forbidden container-side builder payload')
        # Exercise the actual new registry against a tiny explicitly labelled
        # argparse host. Native Benchpark bootstrap is NOT mocked as successful.
        harness=lib/'registry_fixture.py'
        harness.write_text("""import argparse, sys
from benchpark.plugins import register_commands
parser=argparse.ArgumentParser()
parser.add_argument('-C','--config')
sub=parser.add_subparsers(dest='subcommand')
sub.add_parser('native')
actions={'native':lambda args:0}
register_commands(sub,actions,sys.argv[1:])
a=parser.parse_args()
sys.exit(actions[a.subcommand](a) or 0)
""")
        commands=[]
        for args in (['--version'],['--help'],['cer','--help'],['-C','/unused-test-config','cer','--help']):
            entry = harness if 'cer' in args else lib/'main.py'
            r=subprocess.run([sys.executable,str(entry),*args],env=env,cwd=tmp,capture_output=True,text=True)
            if r.returncode!=0:raise RuntimeError(r.stderr)
            commands.append({'arguments':args,'exit_code':r.returncode})
            if args==['--help']:require('+container' in r.stdout and 'cer' in r.stdout, 'CLI help missing extension surface')
            if 'cer' in args:require('validate' in r.stdout and 'export' in r.stdout, 'CER command not registered')
        container_commands=[]
        for args in (['container','--help'], ['container','catalog','list'],
                     ['-C','/unused-test-config','container','--catalog-config',str(tmp/'catalogs.yaml'),'list']):
            r=subprocess.run([sys.executable,str(harness),*args],env=env,cwd=tmp,capture_output=True,text=True)
            require(r.returncode==0, r.stderr)
            if args==['container','--help']: require('register' in r.stdout and 'validate' in r.stdout, 'Catalog CLI absent')
            container_commands.append({'arguments':args,'exit_code':r.returncode})
        # Relative source location is sufficient; don't publish temporary paths.
        return {'wheel':Path(wheel).name,'entry_points':'actual installed distribution metadata',
                'installed_origin_verified':True,'version':data['version'],'cli_fixture_commands':commands,
                'container_cli_commands':container_commands, 'real_upstream_checkout':False}


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('wheel',type=Path);p.add_argument('--report',type=Path)
    a=p.parse_args();data=verify(a.wheel);text=json.dumps(data,indent=2)+'\n'
    if a.report:a.report.write_text(text)
    print(text)

if __name__=='__main__':main()
