# SPDX-License-Identifier: Apache-2.0
import argparse
import json
from pathlib import Path
import sys
import subprocess
import pytest
from benchpark_container import container_cli as cli
from benchpark_container.catalog.manager import Catalog


def invoke(args,capsys):
    p=argparse.ArgumentParser();cli.setup_parser(p)
    status=cli.command(p.parse_args(args));captured=capsys.readouterr()
    return status,captured.out,captured.err


def test_full_management_cli_without_hidden_configuration(tmp_path,capsys):
    config=tmp_path/'visible/catalogs.yaml';catalog=tmp_path/'shared';source=tmp_path/'base.sif';source.write_bytes(b'fixture')
    prefix=['--catalog-config',str(config)]
    assert invoke(prefix+['list'],capsys)[0]==0 and not config.exists()
    assert invoke(prefix+['catalog','init',str(catalog),'--name','personal'],capsys)[0]==0
    assert invoke(prefix+['catalog','add',str(catalog),'--name','personal'],capsys)[0]==0
    args=prefix+['register','--catalog','personal','--name','base','--release','r1','--kind','sif',
         '--source',str(source),'--platform','linux/amd64','--accelerator','none']
    status,out,err=invoke(args,capsys);assert status==0,err
    assert json.loads(out)['status']=='REGISTERED'
    assert len(json.loads(invoke(prefix+['list'],capsys)[1]))==1
    assert json.loads(invoke(prefix+['show','base','--release','r1'],capsys)[1])['name']=='base'
    assert json.loads(invoke(prefix+['validate','base','--release','r1'],capsys)[1])['status']=='VERIFIED_STORED'
    assert invoke(prefix+['catalog','remove','personal'],capsys)[0]==0
    assert Catalog(catalog).validate('base','r1')['status']=='VERIFIED_STORED'


def test_external_generic_cli_bridge_does_not_require_core_changes(tmp_path,capsys):
    desc=cli.cli_command();assert desc.api_version==1
    p=argparse.ArgumentParser();sub=p.add_subparsers(dest='command')
    desc.setup_parser(sub.add_parser('container'))
    args=p.parse_args(['container','--catalog-config',str(tmp_path/'config.yaml'),'list'])
    assert desc.handler(args)==0
    assert capsys.readouterr().out.strip()=='[]'


def test_bad_inputs_are_errors_not_success(tmp_path,capsys):
    prefix=['--catalog-config',str(tmp_path/'config.yaml')]
    status,_,err=invoke(prefix+['register','--catalog','unregistered'],capsys)
    assert status==1 and 'Register the catalog alias first' in err
    status,_,_=invoke(prefix+['show','missing','--release','r1'],capsys)
    assert status==1
