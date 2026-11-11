# SPDX-License-Identifier: Apache-2.0
"""Launcher connection, not a launcher reimplementation. Providers own differences."""
import re
import shlex
from ..util import ValidationError
from ..providers import load_provider, validate_descriptors


class RambleLauncher:
    api_version=1
    name='ramble-launcher-v1'

    @staticmethod
    def command(plan, worker_argv):
        # Preserve the trusted, already-expanded upstream shell prefix verbatim.
        # No shlex.split / scheduler reconstruction / extra mpirun is performed.
        return ['/bin/bash','-c',plan['launcher']+' '+shlex.join(worker_argv)]

    @staticmethod
    def binding(plan, env):
        fields={}
        for field,name in plan['rank_environment'].items():
            value=env.get(name)
            if not isinstance(value,str) or not re.fullmatch(r'[0-9]+',value):
                raise ValidationError('Missing/invalid task binding: '+name)
            fields[field]=int(value)
        if fields['size'] != plan['tasks'] or fields['rank'] >= fields['size'] or fields['local_rank'] >= fields['size']:
            raise ValidationError('Launcher task binding does not match expected task world')
        return fields



class ContainerLocal:
    """One local task; application inside container owns MPI."""
    api_version=1
    name='container-local-v1'

    @staticmethod
    def check(plan):
        if (plan['nodes']!=1 or plan['tasks']!=1
                or plan['spawn_owner']!='application'):
            raise ValidationError(
                'Container-local requires one node/task '
                'and application-owned workers'
            )

    @staticmethod
    def command(plan,worker_argv):
        ContainerLocal.check(plan)
        return list(worker_argv)

    @staticmethod
    def binding(plan,env):
        ContainerLocal.check(plan)
        return {'rank':0,'size':1,'local_rank':0}

def session_provider(plan, descriptors=()):
    validate_descriptors(list(descriptors))
    selected=[d for d in descriptors if d['kind']=='session' and d['name']==plan['provider']]
    if plan['provider']==RambleLauncher.name:
        if selected:raise ValidationError('Session provider shadows a built-in')
        return RambleLauncher
    if plan['provider']==ContainerLocal.name:
        if selected:
            raise ValidationError('Session provider shadows a built-in')
        return ContainerLocal
    if len(selected)!=1:raise ValidationError('Unknown session provider: '+plan['provider'])
    cls=load_provider(selected[0])
    if getattr(cls,'api_version',None)!=1 or getattr(cls,'name',None)!=plan['provider'] or not all(callable(getattr(cls,k,None)) for k in ('command','binding')):
        raise ValidationError('Incompatible session provider contract')
    return cls
