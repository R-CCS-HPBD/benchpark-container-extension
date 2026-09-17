# SPDX-License-Identifier: Apache-2.0
"""Visible catalog registration, explicit aliases, no hidden precedence search."""
from contextlib import contextmanager
import fcntl
import json
import os
from pathlib import Path
import stat
import yaml

from ..util import ValidationError, atomic_json, safe_name, strict, check_no_secrets


class UniqueLoader(yaml.SafeLoader):
    pass


def mapping(loader, node, deep=False):
    result = {}
    for key_node, val_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if not isinstance(key, str) or key in result:
            raise ValidationError('Catalog YAML has duplicate/non-string key')
        result[key] = loader.construct_object(val_node, deep=deep)
    return result


UniqueLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, mapping)


def load_yaml(path):
    path = Path(path)
    if path.is_symlink() or not stat.S_ISREG(path.stat().st_mode) or path.stat().st_size > 16*1024*1024:
        raise ValidationError('Catalog metadata must be a bounded regular file')
    try:
        value = yaml.load(path.read_text('utf-8'), Loader=UniqueLoader)
    except yaml.YAMLError as error:
        raise ValidationError('Invalid catalog YAML: ' + str(path)) from error
    check_no_secrets(value)
    return value


def config_path(path=None):
    return Path(path or os.environ.get('BPCE_CONFIG') or
                (Path.home() / 'benchpark-containers/catalogs.yaml')).expanduser().absolute()


def read_config(path=None):
    path = config_path(path)
    if not path.exists():
        return {'schema_version': 1, 'catalogs': {}, 'skopeo': 'skopeo'}
    data = load_yaml(path)
    strict(data, ('schema_version', 'catalogs', 'skopeo'), 'catalog registration', ('schema_version', 'catalogs'))
    if data['schema_version'] != 1 or not isinstance(data['catalogs'], dict):
        raise ValidationError('Unsupported catalog registration schema')
    for name, value in data['catalogs'].items():
        safe_name(name, 'catalog alias')
        if not isinstance(value, str) or not Path(value).is_absolute():
            raise ValidationError('Registered catalog path must be absolute')
    from ..contracts import executable_value
    executable_value(data.get('skopeo', 'skopeo'), 'skopeo')
    return dict(data, skopeo=data.get('skopeo', 'skopeo'))


def sync_dir(path):
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


@contextmanager
def locked(path):
    """Advisory POSIX lock; the selected filesystem must support flock/rename."""
    path = Path(path)
    fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValidationError('Lock path is not a regular file')
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        os.close(fd)


def update_registration(alias, location=None, remove=False, config=None):
    safe_name(alias, 'catalog alias')
    path = config_path(config)
    path.parent.mkdir(parents=True, exist_ok=True)
    with locked(path.with_name(path.name + '.lock')):
        data = read_config(path)
        if remove:
            if alias not in data['catalogs']:
                raise ValidationError('Unknown catalog alias: ' + alias)
            del data['catalogs'][alias]
        else:
            root = Path(location).expanduser().resolve(strict=True)
            from .manager import Catalog
            Catalog(root)
            old = data['catalogs'].get(alias)
            if old is not None and old != str(root):
                raise ValidationError('Catalog alias already points elsewhere; remove it explicitly first')
            data['catalogs'][alias] = str(root)
        atomic_json(path, data, replace=True)  # JSON is also valid YAML.
        sync_dir(path.parent)
    return {'configuration': str(path), 'catalogs': data['catalogs']}


def ensure_directories(root, relative):
    """Create owned catalog subdirectories without traversing existing symlinks."""
    root = Path(root).resolve(strict=True)
    rel = Path(relative)
    if rel.is_absolute() or '..' in rel.parts:
        raise ValidationError('Unsafe catalog directory path')
    current = root
    for part in rel.parts:
        current = current / part
        if current.is_symlink():
            raise ValidationError('Catalog/store directory may not be a symlink')
        current.mkdir(exist_ok=True)
        if not current.is_dir():
            raise ValidationError('Catalog/store parent is not a directory')
    return current
