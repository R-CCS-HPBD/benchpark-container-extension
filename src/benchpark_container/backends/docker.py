# SPDX-License-Identifier: Apache-2.0
"""Local Linux Docker Engine backend. Remote daemons do not share host mounts."""
import csv
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import tempfile

from .oci import OCIRuntimeBackend
from ..util import ValidationError


class DockerBackend(OCIRuntimeBackend):
    name = 'docker'

    @classmethod
    def validate_config(cls, settings):
        options = settings.get('backend_options', {})
        allowed = {'endpoint', 'config_dir', 'gpu_devices', 'group_add', 'seccomp',
                   'ipc', 'shm_size', 'network', 'cgroup_parent', 'control_timeout_seconds'}
        unknown = set(options) - allowed
        if unknown:
            raise ValidationError('Unknown Docker backend_options: ' + ', '.join(sorted(unknown)))
        endpoint = options.get('endpoint') or os.environ.get('DOCKER_HOST') or 'unix:///var/run/docker.sock'
        if (not isinstance(endpoint, str) or not endpoint.startswith('unix:///')
                or any(c in endpoint for c in '\n\r\x00')
                or '..' in PurePosixPath(endpoint[7:]).parts):
            raise ValidationError('Docker endpoint must be an absolute local unix:// socket; remote daemons are unsupported')
        for key in ('config_dir', 'cgroup_parent'):
            if key in options and (not isinstance(options[key], str) or not options[key].startswith('/')
                                  or any(c in options[key] for c in '\n\r\x00')):
                raise ValidationError(key + ' must be an absolute declared path')
        for key, values in (('seccomp', {'default', 'unconfined'}), ('ipc', {'private', 'host'}),
                            ('network', {'bridge', 'host', 'none'})):
            if key in options and (not isinstance(options[key], str) or options[key] not in values):
                raise ValidationError('Invalid Docker ' + key)
        if 'shm_size' in options and not re.fullmatch(r'[1-9][0-9]*[bkmg]?', str(options['shm_size'])):
            raise ValidationError('shm_size must be a positive integer with optional b/k/m/g suffix')
        if options.get('ipc') == 'host' and 'shm_size' in options:
            raise ValidationError('shm_size is not meaningful with ipc=host')
        timeout = options.get('control_timeout_seconds', 30)
        if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or not 0 < timeout <= 300:
            raise ValidationError('control_timeout_seconds must be in (0, 300]')
        groups = options.get('group_add', [])
        if not isinstance(groups, (list, tuple)) or any(not re.fullmatch(r'[0-9]+', str(x)) for x in groups):
            raise ValidationError('group_add must contain numeric host group IDs')
        devices = options.get('gpu_devices')
        if devices is not None:
            if (not isinstance(devices, (list, tuple)) or not devices
                    or any(not isinstance(d, str) for d in devices) or len(set(devices)) != len(devices)):
                raise ValidationError('gpu_devices must be a nonempty unique list')
            gpu = settings.get('gpu', 'none')
            pattern = r'(?:[0-9]+|GPU-[a-zA-Z0-9-]+|MIG-[a-zA-Z0-9/-]+)' if gpu == 'nvidia' else r'/dev/dri/renderD[0-9]+'
            if gpu == 'none' or any(not isinstance(d, str) or not re.fullmatch(pattern, d) for d in devices):
                raise ValidationError('gpu_devices do not match the declared GPU backend')

    def __init__(self, settings):
        super().__init__(settings)
        self.options = dict(self.settings.get('backend_options', {}))
        # Resolve the effective local Docker endpoint once, then freeze it.
        # Ambient Docker client settings are not allowed to redirect later
        # runtime commands after backend construction.
        self.endpoint = (
            self.options.get('endpoint')
            or os.environ.get('DOCKER_HOST')
            or 'unix:///var/run/docker.sock'
        )
        self.private_config = None
        configured = self.options.get('config_dir')
        if configured:
            self.config_dir = Path(configured)
            if not self.config_dir.is_dir():
                raise ValidationError('Declared Docker config_dir does not exist')
            config_file = self.config_dir / 'config.json'
            config = json.loads(config_file.read_text()) if config_file.is_file() else {}
            if config.get('proxies'):
                raise ValidationError('Docker config proxies can inject undeclared container environment; declare proxies explicitly in the experiment')
        else:
            self.cache.mkdir(parents=True, exist_ok=True)
            self.private_config = tempfile.TemporaryDirectory(prefix='bpce-docker-config-', dir=self.cache)
            self.config_dir = Path(self.private_config.name)
            (self.config_dir / 'config.json').write_text('{}\n')
        self.daemon = None
        self.rootless = False

    def cli(self):
        # --host and --config prevent environment/currentContext from selecting
        # a different daemon or silently injecting client proxy configuration.
        return [self.executable, '--host', self.endpoint, '--config', str(self.config_dir)]

    def host_env(self):
        env = super().host_env()
        for key in list(env):
            if key.startswith('DOCKER_'):
                env.pop(key)
        return env

    def observe_runtime(self):
        result = super().observe_runtime()
        response = self._run(self.cli() + ['info', '--format', '{{json .}}'])
        try:
            info = json.loads(response.stdout)
        except ValueError as error:
            raise ValidationError('Docker daemon info is not JSON') from error
        if not isinstance(info, dict) or info.get('OSType') != 'linux':
            raise ValidationError('This backend requires a local Linux Docker Engine')
        security = info.get('SecurityOptions') or []
        self.rootless = any('rootless' in str(v) for v in security)
        userns_remap = any('userns' in str(v) for v in security) and not self.rootless

        if userns_remap:
            raise ValidationError(
                'userns-remapped Docker is not validated for host bind ownership'
            )

        self.daemon = {
            key: info[key]
            for key in (
                'ID',
                'Name',
                'ServerVersion',
                'OSType',
                'Architecture',
                'SecurityOptions',
            )
            if key in info
        }

        result.update(
            endpoint=self.endpoint,
            mode='rootless' if self.rootless else 'rootful',
            daemon=self.daemon,
        )
        return result

    def _cuda(self, environment):
        devices = self.options.get('gpu_devices')
        visible = os.environ.get('CUDA_VISIBLE_DEVICES')
        if visible in ('', '-1', 'none', 'void', 'NoDevFiles'):
            raise ValidationError('CUDA was requested but the host allocation exposes no GPU')
        if devices is None and visible is not None:
            devices = visible.split(',')
        if devices is not None:
            if any(not re.fullmatch(r'(?:[0-9]+|GPU-[a-zA-Z0-9-]+|MIG-[a-zA-Z0-9/-]+)', d) for d in devices):
                raise ValidationError('Invalid CUDA GPU device selection')
            if visible is not None and self.options.get('gpu_devices') is not None:
                if not set(devices).issubset(set(visible.split(','))):
                    raise ValidationError('gpu_devices conflict with CUDA_VISIBLE_DEVICES; resolve host UUIDs explicitly')
        if os.environ.get('SLURM_JOB_ID'):
            # Slurm may renumber numeric CUDA ordinals inside its cgroup. A
            # rootful daemon uses host IDs, so do not guess or expand to all.
            if not devices or any(not d.startswith(('GPU-', 'MIG-')) for d in devices):
                raise ValidationError('Docker under Slurm requires GPU UUID allocation, not ambiguous numeric ordinals or all GPUs')
        if devices is None:
            environment['NVIDIA_VISIBLE_DEVICES'] = 'all'
            return ['--gpus', 'all']
        environment['NVIDIA_VISIBLE_DEVICES'] = ','.join(devices)
        # Use UUIDs inside CUDA when available. Numeric daemon selections are
        # re-enumerated inside the container, so expose contiguous ordinals.
        environment['CUDA_VISIBLE_DEVICES'] = (','.join(devices) if all(d.startswith(('GPU-', 'MIG-')) for d in devices)
                                                else ','.join(str(i) for i in range(len(devices))))
        stream = io.StringIO()
        csv.writer(stream, lineterminator='').writerow(['device=' + ','.join(devices)])
        return ['--gpus', stream.getvalue()]

    def _rocm(self, environment):
        devices = self.options.get('gpu_devices')
        for key in ('ROCR_VISIBLE_DEVICES', 'HIP_VISIBLE_DEVICES'):
            if os.environ.get(key) in ('', '-1'):
                raise ValidationError('ROCm requested but the host allocation exposes no GPU')
        if (os.environ.get('SLURM_JOB_ID') or any(k in os.environ for k in ('ROCR_VISIBLE_DEVICES', 'HIP_VISIBLE_DEVICES'))) and not devices:
            raise ValidationError('Declare ROCm render-node gpu_devices for a restricted allocation; never expose all /dev/dri implicitly')
        args = ['--device', '/dev/kfd']
        for path in devices or ['/dev/dri']:
            args += ['--device', path]
        return args

    def create_options(self, request, environment):
        args = [
            '--network',
            self.options.get('network', 'bridge'),
            '--ipc',
            self.options.get('ipc', 'private'),
        ]

        # Rootful Docker needs the host UID/GID explicitly so bind-mounted
        # output remains owned by the invoking user. Rootless Docker maps
        # container root to the invoking host user, so passing host UID/GID
        # inside the user namespace is incorrect and can make bind mounts
        # unwritable.
        if not self.rootless:
            args[0:0] = [
                '--user',
                str(os.getuid()) + ':' + str(os.getgid()),
            ]
        if self.options.get('ipc', 'private') != 'host':
            args += ['--shm-size', str(self.options.get('shm_size', '64m'))]
        if 'cgroup_parent' in self.options:
            args += ['--cgroup-parent', self.options['cgroup_parent']]
        if self.options.get('seccomp', 'default') == 'unconfined':
            args += ['--security-opt', 'seccomp=unconfined']
        if self.rootless and self.options.get('group_add'):
            raise ValidationError(
                'group_add is not supported by the validated rootless Docker mode'
            )

        groups = (
            []
            if self.rootless
            else self.options.get('group_add', sorted(set(os.getgroups())))
        )
        for group in groups:
            args += ['--group-add', str(group)]
        if request.accelerator == 'nvidia':
            args += self._cuda(environment)
        elif request.accelerator == 'amd':
            environment['NVIDIA_VISIBLE_DEVICES'] = 'void'
            args += self._rocm(environment)
        elif request.accelerator == 'none':
            environment['NVIDIA_VISIBLE_DEVICES'] = 'void'
        else:
            raise ValidationError('Unsupported Docker accelerator')
        return args

    def close(self):
        try:
            super().close()
        finally:
            if self.private_config is not None:
                self.private_config.cleanup()
