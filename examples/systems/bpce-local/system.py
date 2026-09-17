# SPDX-License-Identifier: Apache-2.0
"""Single-runtime local example; images are selected by Experiment/Catalog."""
from pathlib import Path
import platform
import sys
from benchpark.directives import variant
from benchpark.system import System


class BpceLocal(System):
    variant('image_cache', default='~/benchpark-containers/cache', description='Disposable image cache')
    variant('gpu_passthrough', default='none', values=('none','nvidia','amd'), description='GPU policy')
    variant('runtime_executable', default='apptainer', description='Site Apptainer executable')
    variant('worker_python', default=sys.executable, description='Compute-node host Python')
    id_to_resources={'default':{'sys_cores_per_node':1,'scheduler':'mpi'}}

    def __init__(self,spec):
        super().__init__(spec)
        self.sys_cores_per_node=1
        self.scheduler='mpi'
        self.extension_settings={'container':{'schema_version':3,
            'platform':{'aarch64':'linux/arm64','x86_64':'linux/amd64'}[platform.machine()],
            'default_runtime':'apptainer',
            'runtimes':{'apptainer':{'executable':spec.variants['runtime_executable'][0]}},
            'execution':{'worker_python':spec.variants['worker_python'][0],
                         'image_cache':str(Path(spec.variants['image_cache'][0]).expanduser().resolve()),
                         'gpu':spec.variants['gpu_passthrough'][0]},'artifact_roots':{}}}

    def compute_software_section(self):
        return {'software':{'packages':{}}}
