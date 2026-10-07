# SPDX-License-Identifier: Apache-2.0
"""Materialize verified managed OCI bytes, without consulting the source registry."""
import json
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

from ..image_store import verify_managed
from ..util import ValidationError, atomic_json
from .base import ImageRef


def _docker_runtime_cache_path(backend, verified):
    daemon_id = (backend.daemon or {}).get('ID')
    if not daemon_id:
        # Runtime observation normally supplies this. Do not reuse a persistent
        # mapping when the daemon identity is unknown.
        return None

    material = '\0'.join((
        backend.endpoint,
        daemon_id,
        verified['root_digest'],
    )).encode('utf-8')

    key = hashlib.sha256(material).hexdigest()
    directory = backend.cache / 'managed-docker-runtime-v1'
    directory.mkdir(parents=True, exist_ok=True)
    return directory / (key + '.json')


def _write_docker_runtime_cache(path, record):
    temporary = path.with_name(
        path.name + '.tmp-' + str(os.getpid())
    )
    try:
        temporary.write_text(
            json.dumps(
                record,
                sort_keys=True,
                separators=(',', ':'),
            ) + '\n'
        )
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def restore_oci(backend, base, timeout):
    # The Managed Store remains authoritative. Always verify it first even
    # when a previously materialized Docker image can be reused.
    verified = verify_managed(base)
    backend.cache.mkdir(parents=True, exist_ok=True)

    event = {
        'type': 'managed-image-materialization',
        'state': 'verifying',
        'cleanup': 'not-started',
        'source': 'managed-filesystem',
        'network_source_used': False,
        'stored_digest': verified['root_digest'],
        'manifest_digest': verified['manifest_digest'],
        'config_digest': verified['config_digest'],
    }
    backend.events.append(event)

    cache_path = _docker_runtime_cache_path(backend, verified)
    cache_record = None

    if cache_path is not None and cache_path.is_file():
        try:
            candidate = json.loads(cache_path.read_text())
        except (OSError, ValueError):
            candidate = None

        expected = {
            'schema_version': 1,
            'endpoint': backend.endpoint,
            'daemon_id': (backend.daemon or {}).get('ID'),
            'stored_digest': verified['root_digest'],
            'manifest_digest': verified['manifest_digest'],
            'config_digest': verified['config_digest'],
        }

        if (
            isinstance(candidate, dict)
            and all(candidate.get(k) == v for k, v in expected.items())
            and isinstance(candidate.get('runtime_image_id'), str)
            and candidate['runtime_image_id'].startswith('sha256:')
        ):
            cache_record = candidate
        else:
            cache_path.unlink(missing_ok=True)
            event['runtime_cache_invalid'] = True

    if cache_record is not None:
        runtime_id = cache_record['runtime_image_id']

        try:
            info = backend._object(
                backend.cli() + ['image', 'inspect', runtime_id],
                timeout,
            )
        except ValidationError as error:
            # An image removed from the daemon is a normal stale-cache case.
            # Other daemon/permission failures remain fatal.
            if 'no such image' not in str(error).lower():
                raise
            cache_path.unlink(missing_ok=True)
            event['runtime_cache_stale'] = True
        else:
            if info.get('Id') != runtime_id:
                raise ValidationError(
                    'Cached Docker runtime image ID changed unexpectedly'
                )

            if (
                (info.get('RootFS') or {}).get('Layers', [])
                != verified['config']['rootfs']['diff_ids']
            ):
                raise ValidationError(
                    'Cached Docker layer identities differ from the saved OCI config'
                )

            image = backend._validated_image(
                info,
                base,
                base['managed']['stored_digest'],
            )

            descriptor = info.get('Descriptor') or {}
            descriptor_digest = descriptor.get('digest')
            cached_descriptor = cache_record.get(
                'runtime_descriptor_digest'
            )

            if (
                cached_descriptor
                and descriptor_digest
                and cached_descriptor != descriptor_digest
            ):
                raise ValidationError(
                    'Cached Docker runtime descriptor changed unexpectedly'
                )

            event.update(
                state='verified',
                cleanup='not-needed',
                runtime_cache_hit=True,
                runtime_cache_key=cache_path.stem,
                image_id=runtime_id,
                descriptor_digest=descriptor_digest,
                loaded_reference=runtime_id,
            )

            details = dict(
                image.details,
                managed=True,
                stored_digest=verified['root_digest'],
                source_digest=base.get('source_digest'),
                manifest_digest=verified['manifest_digest'],
                config_digest=verified['config_digest'],
                runtime_image_id=runtime_id,
                runtime_descriptor_digest=descriptor_digest,
                runtime_cache_hit=True,
                restored_from='managed-filesystem',
            )

            return ImageRef(
                image.kind,
                image.reference,
                image.identity,
                details,
            )

    # No reusable verified Docker image exists. Restore from the Managed Store.
    copier = shutil.which(base['managed']['skopeo'])
    if not copier:
        raise ValidationError(
            'Managed OCI restore requires the declared skopeo executable '
            'on the execution node'
        )

    version = subprocess.run(
        [copier, '--version'],
        capture_output=True,
        text=True,
        timeout=30,
    )
    if version.returncode or 'skopeo' not in version.stdout.lower():
        raise ValidationError(
            'OCI restore copier identity check failed'
        )

    event['copier_version'] = version.stdout.strip()

    try:
        # A single-manifest view avoids any skopeo host-default platform choice.
        # Hardlinks here point FROM an ephemeral view TO already verified store
        # bytes; the saved store never links back to a mutable import source.
        with tempfile.TemporaryDirectory(
            prefix='bpce-oci-restore-',
            dir=backend.cache,
        ) as work:
            work = Path(work)

            from ..image_store import selected_layout

            view = selected_layout(
                verified,
                work / 'view',
            )
            archive = work / 'image.tar'

            copy_cmd = [
                copier,
                'copy',
                'oci:' + str(view),
                'docker-archive:' + str(archive),
            ]

            copied = subprocess.run(
                copy_cmd,
                capture_output=True,
                timeout=timeout,
                env=backend.host_env(),
            )
            if copied.returncode:
                raise ValidationError(
                    'Managed OCI to Docker archive conversion failed '
                    '(exit %d)' % copied.returncode
                )

            load_cmd = backend.cli() + [
                'load',
                '--input',
                str(archive),
            ]
            event.update(
                copy_command=copy_cmd,
                load_command=load_cmd,
                state='loading',
            )

            loaded = backend._run(
                load_cmd,
                timeout,
            )

            stdout = loaded.stdout or ''
            if isinstance(stdout, bytes):
                stdout = stdout.decode(
                    'utf-8',
                    'replace',
                )

            loaded_refs = []
            for line in stdout.splitlines():
                line = line.strip()
                for prefix in (
                    'Loaded image ID:',
                    'Loaded image:',
                ):
                    if line.startswith(prefix):
                        ref = line[len(prefix):].strip()
                        if ref:
                            loaded_refs.append(ref)
                        break

            if len(loaded_refs) != 1:
                raise ValidationError(
                    'Docker load did not report exactly one loaded image reference'
                )

            loaded_ref = loaded_refs[0]

            info = backend._object(
                backend.cli()
                + ['image', 'inspect', loaded_ref],
                timeout,
            )

            runtime_id = info.get('Id')
            if (
                not isinstance(runtime_id, str)
                or not runtime_id.startswith('sha256:')
            ):
                raise ValidationError(
                    'Restored Docker image has no immutable runtime image ID'
                )

            if (
                loaded_ref.startswith('sha256:')
                and runtime_id != loaded_ref
            ):
                raise ValidationError(
                    'Restored Docker image ID differs from docker load result'
                )

            if (
                (info.get('RootFS') or {}).get('Layers', [])
                != verified['config']['rootfs']['diff_ids']
            ):
                raise ValidationError(
                    'Restored Docker layer identities differ from the saved OCI config'
                )

            image = backend._validated_image(
                info,
                base,
                base['managed']['stored_digest'],
            )

            descriptor = info.get('Descriptor') or {}
            descriptor_digest = descriptor.get('digest')

            event.update(
                state='verified',
                cleanup='temporary-archive-removed',
                runtime_cache_hit=False,
                runtime_cache_key=(
                    cache_path.stem
                    if cache_path is not None
                    else None
                ),
                loaded_reference=loaded_ref,
                image_id=runtime_id,
                descriptor_digest=descriptor_digest,
            )

            if cache_path is not None:
                _write_docker_runtime_cache(
                    cache_path,
                    {
                        'schema_version': 1,
                        'endpoint': backend.endpoint,
                        'daemon_id': (
                            backend.daemon or {}
                        ).get('ID'),
                        'stored_digest': verified['root_digest'],
                        'manifest_digest': verified['manifest_digest'],
                        'config_digest': verified['config_digest'],
                        'runtime_image_id': runtime_id,
                        'runtime_descriptor_digest': descriptor_digest,
                    },
                )

            details = dict(
                image.details,
                managed=True,
                stored_digest=verified['root_digest'],
                source_digest=base.get('source_digest'),
                manifest_digest=verified['manifest_digest'],
                config_digest=verified['config_digest'],
                runtime_image_id=runtime_id,
                runtime_descriptor_digest=descriptor_digest,
                runtime_cache_hit=False,
                restored_from='managed-filesystem',
            )

            return ImageRef(
                image.kind,
                image.reference,
                image.identity,
                details,
            )

    except BaseException:
        event.update(
            state='failed',
            cleanup='temporary-archive-removed',
        )
        raise
