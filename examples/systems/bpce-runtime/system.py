# SPDX-License-Identifier: Apache-2.0
"""One initialized System, several selectable runtimes. No image inventory."""
import json
from pathlib import Path
import platform
import sys
from benchpark.directives import variant
from benchpark.system import System


class BpceRuntime(System):
    variant('default_runtime', default='singularity', description='Default runtime, overridden per experiment instance')
    variant('singularity_executable', default='singularity', description='Actual SingularityCE executable; Apptainer alias is rejected')
    variant('apptainer_executable', default='apptainer', description='Apptainer executable')
    variant('docker_executable', default='docker', description='Docker executable')
    variant('container_platform', default={'aarch64':'linux/arm64','x86_64':'linux/amd64'}.get(platform.machine(), 'unsupported'),
            description='Compute-node OS/architecture (not necessarily login-node architecture)')
    variant('image_cache', default='~/benchpark-containers/cache', description='Disposable derived/runtime cache, not retained catalog objects')
    variant('gpu_passthrough', default='none', values=('none','nvidia','amd'), description='GPU family')
    variant('backend_options_file', default='none', description='Optional JSON runtime-name -> backend_options mapping')
    variant('worker_python', default=sys.executable, description='Host Python for the stdlib-only worker')
    id_to_resources={'default':{'sys_cores_per_node':1,'scheduler':'mpi'}}

    def __init__(self, spec):
        super().__init__(spec)
        self.sys_cores_per_node=1
        self.scheduler='mpi'
        value=lambda name:spec.variants[name][0]
        runtimes={name:{'executable':value(name+'_executable')} for name in ('singularity','apptainer','docker')}
        if value('backend_options_file')!='none':
            path=Path(value('backend_options_file'))
            if not path.is_absolute():raise ValueError('backend_options_file must be absolute')
            for name, options in json.loads(path.read_text()).items():
                if name not in runtimes:raise ValueError('Unknown runtime options: '+name)
                runtimes[name]['backend_options']=options
        self.extension_settings={'container':{'schema_version':3,
            'default_runtime':value('default_runtime'),'runtimes':runtimes,'platform':value('container_platform'),
            'execution':{'worker_python':value('worker_python'),
                         'image_cache':str(Path(value('image_cache')).expanduser().resolve()),'gpu':value('gpu_passthrough')},
            'artifact_roots':{}}}

    def compute_software_section(self):
        return {'software':{'packages':{}}}
