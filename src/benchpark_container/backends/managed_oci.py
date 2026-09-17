# SPDX-License-Identifier: Apache-2.0
"""Materialize verified managed OCI bytes, without consulting the source registry."""
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile

from ..image_store import verify_managed
from ..util import ValidationError, atomic_json
from .base import ImageRef


def restore_oci(backend, base, timeout):
    verified = verify_managed(base)
    copier = shutil.which(base['managed']['skopeo'])
    if not copier:
        raise ValidationError('Managed OCI restore requires the declared skopeo executable on the execution node')
    version = subprocess.run([copier, '--version'], capture_output=True, text=True, timeout=30)
    if version.returncode or 'skopeo' not in version.stdout.lower():
        raise ValidationError('OCI restore copier identity check failed')
    backend.cache.mkdir(parents=True, exist_ok=True)
    event = {'type': 'managed-image-materialization', 'state': 'verifying', 'cleanup': 'not-started',
             'source': 'managed-filesystem', 'network_source_used': False,
             'stored_digest': verified['root_digest'], 'manifest_digest': verified['manifest_digest'],
             'config_digest': verified['config_digest'], 'copier_version': version.stdout.strip()}
    backend.events.append(event)
    try:
        # A single-manifest view avoids any skopeo host-default platform choice.
        # Hardlinks here point FROM an ephemeral view TO already verified store
        # bytes; the saved store never links back to a mutable import source.
        with tempfile.TemporaryDirectory(prefix='bpce-oci-restore-', dir=backend.cache) as work:
            work = Path(work)
            from ..image_store import selected_layout
            view = selected_layout(verified, work / 'view')
            archive = work / 'image.tar'
            copy_cmd = [copier, 'copy', 'oci:' + str(view), 'docker-archive:' + str(archive)]
            # No source-registry credentials are required or captured.
            copied = subprocess.run(copy_cmd, capture_output=True, timeout=timeout, env=backend.host_env())
            if copied.returncode:
                raise ValidationError('Managed OCI to Docker archive conversion failed (exit %d)' % copied.returncode)
            load_cmd = backend.cli() + ['load', '--input', str(archive)]
            event.update(copy_command=copy_cmd, load_command=load_cmd, state='loading')
            backend._run(load_cmd, timeout)
            info = backend._object(backend.cli() + ['image', 'inspect', verified['config_digest']], timeout)
            if info.get('Id') != verified['config_digest']:
                raise ValidationError('Restored Docker image ID differs from the verified OCI config digest')
            if (info.get('RootFS') or {}).get('Layers', []) != verified['config']['rootfs']['diff_ids']:
                raise ValidationError('Restored Docker layer identities differ from the saved OCI config')
            image = backend._validated_image(info, base, base['managed']['stored_digest'])
            event.update(state='verified', cleanup='temporary-archive-removed', image_id=info['Id'])
            details = dict(image.details, managed=True, stored_digest=verified['root_digest'],
                           source_digest=base.get('source_digest'), manifest_digest=verified['manifest_digest'],
                           restored_from='managed-filesystem')
            # RepoDigests may be empty after offline archive loading. The config
            # digest + layer chain, not a synthetic repository digest, proves ID.
            return ImageRef(image.kind, image.reference, image.identity, details)
    except BaseException:
        event.update(state='failed', cleanup='temporary-archive-removed')
        raise
