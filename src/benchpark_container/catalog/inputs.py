# SPDX-License-Identifier: Apache-2.0
"""CLI-only defaults from content identity, never from a guessed GPU or host ISA."""
from pathlib import Path
from urllib.parse import unquote, urlparse
import json
import re
from ..util import ValidationError, sha256
from .store import Skopeo, remote_reference, registry_repository


def complete_registration(args):
    source = args.source
    if not source and not args.oci_layout:
        raise ValidationError('A source image or OCI layout is required')
    if args.kind is None:
        args.kind = 'sif' if source and (source.startswith('file://') or Path(source).is_file() or source.endswith('.sif')) else 'oci'
    digest = args.sha256 or args.digest
    if args.kind == 'sif':
        path = Path(unquote(urlparse(source).path)) if source.startswith('file://') else Path(source).expanduser()
        if path.is_symlink() or not path.is_file(): raise ValidationError('SIF source must be a regular file')
        digest = digest or sha256(path)
        args.name = args.name or path.stem
        # SIF platform/accelerator cannot be inferred from a filename/host.
    elif source:
        reference = remote_reference(source)
        repo = registry_repository(reference)
        args.name = args.name or repo.rsplit('/', 1)[-1]
        if '@' in reference: digest = digest or reference.rsplit('@',1)[1]
        if (not args.release and not digest) or not args.platform:
            tool = Skopeo(args.skopeo or 'skopeo', args.timeout)
            pinned, raw = tool.pin_reference(reference, args.digest)
            digest = pinned.rsplit('@',1)[1]
            args.source = pinned
            if not args.platform:
                doc = json.loads(raw)
                candidates = {x.get('platform',{}).get('os','')+'/'+x.get('platform',{}).get('architecture','') for x in doc.get('manifests',[])}
                candidates &= {'linux/amd64','linux/arm64'}
                if len(candidates) == 1: args.platform = next(iter(candidates))
                elif not doc.get('manifests'):
                    config = json.loads(tool.run(['inspect','--config','docker://'+pinned]))
                    value = config.get('os','')+'/'+config.get('architecture','')
                    if value in ('linux/amd64','linux/arm64'): args.platform = value
    if args.release is None and digest:
        args.release = 'sha256-' + digest.removeprefix('sha256:')[:16]
    if args.accelerator is None:
        # A CPU-only registration claim, NOT inference that the image lacks GPUs.
        args.accelerator = 'none'
    if args.name and not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', args.name):
        raise ValidationError('Cannot derive a safe image name; supply --name')
