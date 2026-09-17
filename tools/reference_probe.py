#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Isolated comparison probe. Native parser fixture, not full upstream E2E."""
import argparse
import hashlib
import json
from pathlib import Path
import sys
import tempfile
from types import SimpleNamespace as S


def main():
    parser=argparse.ArgumentParser();parser.add_argument('repository',type=Path)
    a=parser.parse_args();root=a.repository.resolve()
    sys.path[:0]=[str(root/'src'),str(root/'core/files/lib'),str(root/'tools'),str(root/'tests')]
    import pytest
    import conftest
    import test_core_patch
    old=(root/'core/files/lib/benchpark/extension_api.py').exists()
    if old:
        from benchpark.extension_api import ConfigurationContribution,ResourceSpec
        from benchpark.extension_support.io import save_system_snapshot
        from benchpark.extension_support.storage import publish_experiment, verify_manifest
    else:
        from benchpark_integration.api import ConfigurationContribution,ResourceSpec
        from benchpark_integration.support.io import save_system_snapshot
        from benchpark_integration.support.storage import publish_experiment, verify_manifest
    data={}
    import benchpark_container
    modern = benchpark_container.__version__ == '1.0.0.dev4'
    cases=['smoke','smoke size=16,32','smoke ~container','smoke +container',
           'smoke +container container_base=base-a container_release=release-b',
           'smoke package_manager=user-managed','smoke +container package_manager=user-managed',
           'smoke +container package_manager=spack','smoke +not_installed',
           'smoke +container container_base=a,b','smoke +container size=abc',
           'smoke tune=large','smoke +container tune=large','smoke tune=large size=32',
           'smoke tune=large size=16','smoke tune=missing','smoke ~container tune=large',
           'smoke +container tune=none','smoke +container container_typo=x',
           'smoke backend=other','smoke workload=all']
    with tempfile.TemporaryDirectory(prefix='bpce-reference-probe-') as tmp:
        for index,case in enumerate(cases):
            mp=pytest.MonkeyPatch();site=Path(tmp)/str(index);site.mkdir()
            try:
                f=test_core_patch.native_spec.__wrapped__(root,mp,site)
                conftest.installed.__wrapped__(mp)
                (f.root/'tuning/large.yaml').write_text('schema_version: 1\noverrides: {size: 32}\n')
                value=f.spec.ExperimentSpec(case.replace('container_base=', 'container_image=') if modern else case).concretize()
                state=(getattr(value,'plugin_state',{}) if not old else
                       {key:getattr(value,key,{}) for key in ('extension_explicit','extension_selection','preparation_records')})
                records={k:dict(v.get('overrides',{})) for k,v in state.get('preparation_records',{}).items()}
                variants=dict(value.variants.items())
                explicit=dict(state.get('extension_explicit',{}))
                if modern:
                    variants.pop('container_runtime', None)
                    if 'container_image' in variants:
                        variants['container_base']=variants.pop('container_image')
                    if 'container_image' in explicit:
                        explicit['container_base']=explicit.pop('container_image')
                data[case]={'accepted':True,'variants':variants,
                            'explicit':explicit,
                            'selection':state.get('extension_selection',[]) or [],'tune':records}
            except Exception as exc:
                data[case]={'accepted':False}
            finally:mp.undo()
        system=Path(tmp)/'system';system.mkdir()
        settings={'container':{'execution':{'runtime':'declared-test-runtime'},'nested':['fixed']}}
        save_system_snapshot(S(extension_settings=settings) if old else settings,system)
        (system/'variables.yaml').write_text('variables: {x: 1}\n')
        (system/'execute_experiment.tpl').write_text('fixed {command}\n')
        source=Path(tmp)/'experiment';source.mkdir();(source/'ramble.yaml').write_text('ramble: {}\n')
        resource=ResourceSpec('inputs/fixed.txt',hashlib.sha256(b'fixed').hexdigest(),text='fixed')
        c=ConfigurationContribution('container',{'schema_version':1,'purpose':'unchanged snapshot'},resources=(resource,))
        publish_experiment(S(extension_contributions=(c,)) if old else (c,),source,system)
        verify_manifest(source)
        snapshot={str(p.relative_to(source)):hashlib.sha256(p.read_bytes()).hexdigest()
                  for p in source.rglob('*') if p.is_file()}
        data['snapshot_files']=snapshot
        data['system_snapshot']=json.loads((system/'system.extensions.json').read_text())
    print(json.dumps(data,sort_keys=True))


if __name__=='__main__':main()
