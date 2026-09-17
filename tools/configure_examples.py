#!/usr/bin/env python3
# SPDX-License-Identifier: Apache-2.0
"""Create a separate standard Benchpark -C scope. Never edit upstream config."""
import argparse
import os
from pathlib import Path
import yaml

ROOT=Path(__file__).resolve().parents[1]


def create_config(checkout,output,bootstrap):
    checkout,output,bootstrap=map(lambda p:Path(p).resolve(),(checkout,output,bootstrap))
    source=checkout/'config/repos.yaml'
    data=yaml.safe_load(source.read_text())
    groups=data['repos']
    for kind,paths in groups.items():
        groups[kind]=[str((source.parent/path).resolve()) if not Path(path).is_absolute() else path for path in paths]
    for kind in ('systems','experiments','applications'):
        groups[kind].insert(0,str(ROOT/'examples'/kind))
    # RIKEN 30d698d resolves only relative repository references.
    groups = {kind: [os.path.relpath(path, start=output) for path in paths]
              for kind, paths in groups.items()}
    data['repos'] = groups
    output.mkdir(parents=True,exist_ok=False)
    (output/'repos.yaml').write_text(yaml.safe_dump(data,sort_keys=False))
    (output/'bootstrap.yaml').write_text(yaml.safe_dump({'bootstrap':{'location':str(bootstrap)}}))
    return output


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('checkout',type=Path);p.add_argument('output',type=Path);p.add_argument('--bootstrap',type=Path,required=True)
    a=p.parse_args();print(create_config(a.checkout,a.output,a.bootstrap))

if __name__=='__main__':main()
