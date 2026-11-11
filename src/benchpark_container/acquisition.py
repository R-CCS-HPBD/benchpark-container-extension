# SPDX-License-Identifier: Apache-2.0
"""Image retention is independent of logical Catalog selection.

Ephemeral uses a private cache owned by this attempt. No shared Engine image
or global cache is ever pruned. Daemon image lifecycle needs an explicit lease
provider and is rejected for ephemeral in this release.
"""
from pathlib import Path
import shutil
import tempfile
from .util import ValidationError


def validate_policy(policy, backend):
    if policy not in ('cache', 'ephemeral'):
        raise ValidationError('image_policy must be cache or ephemeral')
    if policy == 'ephemeral' and not getattr(backend, 'private_image_cache', False):
        raise ValidationError('Selected runtime has no safe private ephemeral image cache; use cache')


class ImageLease:
    def __init__(self, runtime, policy, owner_dir):
        validate_policy(policy, type(runtime))
        self.runtime, self.policy = runtime, policy
        self.original = runtime.cache
        self.private = None
        if policy == 'ephemeral':
            self.private = Path(tempfile.mkdtemp(prefix='image-lease-', dir=owner_dir))
            runtime.cache = self.private

    def close(self):
        self.runtime.cache = self.original
        if self.private is not None and self.private.exists():
            if self.private.is_symlink():
                raise ValidationError('Image lease path replaced by symlink; refusing cleanup')
            shutil.rmtree(self.private)

    def record(self):
        return {'schema_version': 1, 'policy': self.policy,
                'ownership': 'attempt-private' if self.private else 'runtime-cache',
                'shared_cache_pruned': False}
