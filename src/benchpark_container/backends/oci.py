# SPDX-License-Identifier: Apache-2.0
"""OCI CLI lifecycle shared by compatible backends, without workload semantics.

Separate create/inspect/start prevents execution before image identity is checked.
Every invocation is labelled and removed by verified ID, never by a global prune.
"""
from contextlib import contextmanager
import json
import os
from pathlib import Path
import platform
import re
import subprocess
import sys
import uuid

from .base import ImageRef, RuntimeBackend, RuntimeCapabilities, oci_reference
from ..util import ValidationError, atomic_json

LABEL = "org.benchpark.bpce.invocation"
HASH_ID = re.compile(r"sha256:[0-9a-f]{64}")
CONTAINER_ID = re.compile(r"[0-9a-f]{64}")


def canonical_reference(value):
    """Normalize Docker Hub aliases, not arbitrary repositories sharing a hash."""
    repository, digest = value.rsplit('@', 1)
    first = repository.split('/')[0]
    if '/' not in repository or ('.' not in first and ':' not in first and first != 'localhost'):
        repository = 'docker.io/' + repository
    repository = repository.replace('index.docker.io/', 'docker.io/', 1)
    if repository.startswith('docker.io/') and repository.count('/') == 1:
        repository = 'docker.io/library/' + repository.split('/', 1)[1]
    return repository + '@' + digest


class OCIRuntimeBackend(RuntimeBackend):
    capabilities = RuntimeCapabilities(image_kinds=frozenset({'oci'}))
    preferred_image_kinds = ('oci',)

    def __init__(self, settings):
        super().__init__(settings)
        self.pending = {}
        self.event_sequence = 0
        self.closed = False
        self.image_environments = {}
        self.control_timeout = self.settings.get('backend_options', {}).get('control_timeout_seconds', 30)

    def cli(self):
        return [self.executable]

    def _run(self, command, timeout=None, check=True):
        result = subprocess.run(command, capture_output=True, text=True, env=self.host_env(),
                                timeout=self.control_timeout if timeout is None else timeout)
        if check and result.returncode:
            raise ValidationError(self.name + ' command failed: ' + ' '.join(command) + ': ' + result.stderr.strip())
        return result

    def _object(self, command, timeout=None, allow_absent=False):
        result = self._run(command, timeout, check=False)
        if result.returncode:
            # A daemon/permission/transport failure must NOT become "not found".
            if allow_absent and re.search(r'No such (?:image|container|object)\b', result.stderr, re.I):
                return None
            raise ValidationError(self.name + ' inspect failed: ' + result.stderr.strip())
        try:
            data = json.loads(result.stdout)
        except (TypeError, ValueError) as error:
            raise ValidationError(self.name + ' inspect did not return JSON') from error
        if not isinstance(data, list) or len(data) != 1 or not isinstance(data[0], dict):
            raise ValidationError(self.name + ' inspect must return exactly one object')
        return data[0]

    def resolve_image(self, base, timeout):
        if isinstance(base.get('managed'), dict):
            from .managed_oci import restore_oci
            return restore_oci(self, base, timeout)
        reference = oci_reference(base)
        info = self._object(self.cli() + ['image', 'inspect', reference], timeout, allow_absent=True)
        if info is None:
            args = self.cli() + ['pull']
            declared = base.get('platform', 'unverified')
            if declared != 'unverified':
                args += ['--platform', declared]
            self._run(args + [reference], timeout)
            info = self._object(self.cli() + ['image', 'inspect', reference], timeout)
        digests = info.get('RepoDigests')
        if (not isinstance(digests, list) or not all(isinstance(d, str) and '@sha256:' in d for d in digests)
                or canonical_reference(reference) not in {canonical_reference(d) for d in digests}):
            raise ValidationError('Inspected image RepoDigests do not contain the requested pinned repository digest')
        return self._validated_image(info, base, reference)

    def _validated_image(self, info, base, reference):
        digests = info.get('RepoDigests') or []
        image_id = info.get('Id', '')
        if not isinstance(image_id, str) or not HASH_ID.fullmatch(image_id):
            raise ValidationError('OCI image inspect returned an invalid content-addressed image ID')
        observed = info.get('Os', '') + '/' + info.get('Architecture', '')
        variant = info.get('Variant')
        if variant:
            observed += '/' + variant
        declared = base.get('platform', 'unverified')
        native = {'x86_64': 'linux/amd64', 'aarch64': 'linux/arm64'}.get(platform.machine())
        if native is None or observed.split('/')[:2] != native.split('/'):
            raise ValidationError('OCI image platform does not match the execution host; implicit emulation is not allowed')
        if declared != 'unverified' and not (observed == declared or (declared.count('/') == 1 and observed.startswith(declared + '/'))):
            raise ValidationError('OCI image platform differs from the declared platform')
        # An image-defined anonymous writable volume could hide the Base or
        # bypass the explicit artifact contract even with a read-only rootfs.
        if (info.get('Config') or {}).get('Volumes'):
            raise ValidationError('Image-defined VOLUME mounts are not supported; declare mounts explicitly as artifacts')
        image_environment = (info.get('Config') or {}).get('Env') or []
        if not isinstance(image_environment, list) or any(not isinstance(x, str) or '=' not in x for x in image_environment):
            raise ValidationError('Invalid image environment metadata')
        # Retain only in memory for verification, never dump image credentials.
        self.image_environments[image_id] = dict(x.split('=', 1) for x in image_environment)
        return ImageRef('oci', image_id, reference, {'oci_digest': reference, 'image_id': image_id,
                        'repo_digests': sorted(digests), 'platform': observed})

    def create_options(self, request, environment):
        """Concrete backend owns device, user, security and CLI differences."""
        raise NotImplementedError

    def build_command(self, request):
        self.validate_request(request)
        if self.closed:
            raise ValidationError("Cannot reuse a closed runtime backend")
        if not isinstance(request.image, ImageRef) or request.image.kind != 'oci':
            raise ValidationError('OCI build_command requires a resolved ImageRef, not a SIF or mutable tag')
        if not HASH_ID.fullmatch(request.image.reference):
            raise ValidationError('Execute OCI images only by the verified image ID')
        environment = dict(request.environment)
        options = self.create_options(request, environment)
        token = uuid.uuid4().hex
        name = 'bpce-' + token
        create = self.cli() + ['create', '--name', name, '--label', LABEL + '=' + token,
                '--pull', 'never', '--read-only', '--no-healthcheck', '--workdir', request.workdir,
                '--tmpfs', '/tmp:rw,nosuid,nodev', '--tmpfs', '/var/tmp:rw,nosuid,nodev'] + options
        for mount in request.mounts:
            create += ['--mount', 'type=bind,src=' + mount.source + ',dst=' + mount.target +
                        (',readonly' if mount.readonly else '')]
        for key, value in sorted(environment.items()):
            create += ['--env', key + '=' + value]
        # Setting ENTRYPOINT and explicit argv avoids image ENTRYPOINT/CMD
        # rewriting the requested Python/pip/Bash command.
        create += ['--entrypoint', request.command[0], request.image.reference] + list(request.command[1:])
        start = self.cli() + ['start', '--attach', name]
        event = {'name': name, 'invocation_id': token, 'creation_command': create,
                 'command': start, 'image_id': request.image.reference, 'image_identity': request.image.identity,
                 'environment': environment, 'state': 'planned', 'cleanup': 'not-started'}
        self.pending[tuple(start)] = (event, request)
        self.events.append(event)
        return start

    def _owned(self, event, allow_absent=False):
        target = event.get('container_id', event['name'])
        info = self._object(self.cli() + ['container', 'inspect', target], allow_absent=allow_absent)
        if info is None:
            return None
        container_id = info.get('Id', '')
        if not isinstance(container_id, str) or not CONTAINER_ID.fullmatch(container_id):
            raise ValidationError('Invalid container ID; refusing cleanup')
        labels = (info.get('Config') or {}).get('Labels') or {}
        if labels.get(LABEL) != event['invocation_id']:
            raise ValidationError('Container ownership label mismatch; refusing to delete another container')
        if event.get('container_id', container_id) != container_id:
            raise ValidationError('Container ID changed; refusing cleanup')
        event['container_id'] = container_id
        return info

    def _verify_created(self, event, request):
        info = self._owned(event)
        if info.get('Image') != event['image_id']:
            raise ValidationError('Created container image ID differs from the resolved immutable image')
        config = info.get('Config') or {}
        if config.get('Entrypoint') != [request.command[0]] or (config.get('Cmd') or []) != list(request.command[1:]):
            raise ValidationError('Created container command differs from the requested argv')
        actual = {m.get('Destination'): m for m in info.get('Mounts', []) if m.get('Type') == 'bind'}
        if set(actual) != {m.target for m in request.mounts}:
            raise ValidationError('Created container bind mount set differs from the resolved plan')
        for mount in request.mounts:
            seen = actual[mount.target]
            if seen.get('Source') != mount.source or seen.get('RW') is not (not mount.readonly):
                raise ValidationError('Created container mount source/mode differs from the resolved plan')
        environment = config.get('Env') or []
        if not isinstance(environment, list) or any(not isinstance(x, str) or '=' not in x for x in environment):
            raise ValidationError('Invalid created container environment')
        actual_environment = dict(x.split('=', 1) for x in environment)
        expected_environment = dict(self.image_environments.get(event['image_id'], {}), **event['environment'])
        if actual_environment != expected_environment:
            raise ValidationError('Created container environment differs from image plus explicit execution environment')
        event['image_verified_before_start'] = True
        event['state'] = 'created-verified'

    def _persist(self):
        if self.attempt is not None:
            # Immutable event snapshots, never overwrite a prior state. The
            # final summary is published exactly once by close().
            self.event_sequence += 1
            atomic_json(self.attempt / ('runtime-event-%06d.json' % self.event_sequence), self.events)

    def _cleanup(self, event):
        if event['state'] == 'planned':
            event['cleanup'] = 'not-created'
            return
        info = self._owned(event, allow_absent=True)
        if info is None:
            if event['state'] == 'creating':
                raise ValidationError('Container creation outcome is unknown; inspect the recorded name on the daemon before retrying')
            event['cleanup'] = 'already-absent'
            return
        state = info.get('State') or {}
        event['container_state'] = {key: state[key] for key in ('Status', 'Running', 'ExitCode', 'OOMKilled', 'Error') if key in state}
        if info.get('Image') != event['image_id']:
            event['image_mismatch'] = True
        self._run(self.cli() + ['rm', '--force', '--volumes', event['container_id']])
        event['cleanup'] = 'removed'

    @contextmanager
    def command_scope(self, command):
        key = tuple(command)
        if key not in self.pending:
            raise ValidationError('Unknown or reused runtime invocation')
        event, request = self.pending[key]
        error = None
        try:
            event['state'] = 'creating'
            self._persist()
            created = self._run(event['creation_command'])
            identifier = created.stdout.strip()
            if not CONTAINER_ID.fullmatch(identifier):
                raise ValidationError('create did not return a full container ID')
            event['container_id'] = identifier
            self._verify_created(event, request)
            # Start the verified ID, never a name which could be reassigned.
            command[:] = self.cli() + ['start', '--attach', event['container_id']]
            event['command'] = list(command)
            self._persist()
            yield
            event['state'] = 'process-returned'
        except BaseException as caught:
            error = caught
            event['error_type'] = type(caught).__name__
            event['error'] = str(caught)
            raise
        finally:
            try:
                self._cleanup(event)
            except Exception as cleanup_error:
                event['cleanup'] = 'failed'
                event['cleanup_error'] = str(cleanup_error)
                if error is None:
                    raise
                print('BPCE runtime cleanup failed: ' + str(cleanup_error), file=sys.stderr)
            finally:
                self._persist()
                self.pending.pop(key, None)

    def close(self):
        if self.closed:
            return
        failures = []
        for key, (event, request) in list(self.pending.items()):
            try:
                self._cleanup(event)
            except Exception as error:
                event.update(cleanup='failed', cleanup_error=str(error))
                failures.append(str(error))
            finally:
                self.pending.pop(key, None)
        self._persist()
        if self.attempt is not None:
            atomic_json(self.attempt / 'runtime-invocations.json', self.events)
        self.closed = True
        if failures:
            raise ValidationError('Runtime cleanup failed: ' + '; '.join(failures))
