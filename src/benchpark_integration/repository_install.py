# SPDX-License-Identifier: Apache-2.0
"""Build/install a small repository locator wheel, never benchmark source files.

This one-time connection is ordinary Python package installation, not editable
installation. New benchmark directories are not part of this wheel and require
no reinstallation. No Benchpark configuration is written.
"""
import argparse
import base64
import csv
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import zipfile

from .api import ExtensionError
from .repository_provider import (
    RepositoryProvider,
    PROVIDER_NAME,
    FEATURE_NAME,
    installed_providers,
)
from .source_repository import capture_source, read_index


def distribution_name(name):
    if not PROVIDER_NAME.fullmatch(name):
        raise ExtensionError('Use a lowercase repository name starting with a letter')
    return 'benchpark-repository-' + name.replace('_', '-')


def normalize_features(features):
    if isinstance(features, str):
        features = (features,)
    if features is None:
        features = ()
    if not isinstance(features, (tuple, list)) or not all(
            isinstance(feature, str) and FEATURE_NAME.fullmatch(feature)
            for feature in features):
        raise ExtensionError('Invalid repository activation features')
    return tuple(sorted(set(features)))


def baseline(root):
    return {name: capture_source(root, name).manifest
            for name in sorted(read_index(root)['benchmarks'])}


def build_locator(root, name, output, snapshots=None, display_name=None, features=()):
    root = Path(root).expanduser().resolve()
    features = normalize_features(features)
    provider = RepositoryProvider(
        str(root), name, display_name=display_name, features=features
    )
    provider.paths()
    dist = distribution_name(name).replace('-', '_')
    module = 'bp_repository_' + name.replace('-', '_')
    info = dist + '-1.0.dist-info'
    locator = {'schema_version': 1, 'kind': 'benchpark-repository-locator',
               'name': name, 'source_root': str(root), 'display_name': display_name or name,
               'features': list(features),
               'baseline': baseline(root) if snapshots is None else snapshots}
    files = {
        module + '/__init__.py': b'',
        module + '/provider.py': (
            '# Generated checkout locator; benchmark files are not packaged.\n'
            'from pathlib import Path\n'
            'from benchpark_integration.repository_provider import from_locator\n\n'
            'def provider():\n'
            '    return from_locator(Path(__file__).with_name("locator.json"))\n').encode(),
        module + '/locator.json': (json.dumps(locator, indent=2, sort_keys=True) + '\n').encode(),
        info + '/METADATA': ('Metadata-Version: 2.1\nName: ' + distribution_name(name) +
            '\nVersion: 1.0\nSummary: Benchpark external repository locator\n'
            'Requires-Python: >=3.10\nRequires-Dist: benchpark-container-extension\n\n').encode(),
        info + '/WHEEL': b'Wheel-Version: 1.0\nGenerator: benchpark-repository-locator\nRoot-Is-Purelib: true\nTag: py3-none-any\n',
        info + '/entry_points.txt': ('[benchpark.repositories.v1]\n' + name + ' = ' + module + '.provider:provider\n').encode(),
    }
    record = io.StringIO(newline='')
    writer = csv.writer(record, lineterminator='\n')
    for filename, data in sorted(files.items()):
        h = base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b'=').decode()
        writer.writerow([filename, 'sha256=' + h, len(data)])
    writer.writerow([info + '/RECORD', '', ''])
    files[info + '/RECORD'] = record.getvalue().encode()
    output = Path(output)
    output.mkdir(parents=True, exist_ok=True)
    wheel = output / (dist + '-1.0-py3-none-any.whl')
    # A wheel RECORD binds all installation payloads. Deterministic timestamps
    # avoid injecting a build-time change into an unchanged locator.
    with zipfile.ZipFile(wheel, 'w', compression=zipfile.ZIP_DEFLATED) as z:
        for filename, data in sorted(files.items()):
            member = zipfile.ZipInfo(filename, date_time=(1980, 1, 1, 0, 0, 0))
            member.external_attr = (0o100644 << 16)
            member.compress_type = zipfile.ZIP_DEFLATED
            z.writestr(member, data)
    return wheel


def verify_seam(checkout):
    """Check the specific read-time port, not mere plugins.py existence."""
    checkout = Path(checkout).resolve()
    files = {'lib/benchpark/plugins.py': 'def repository_config(',
             'lib/benchpark/config.py': 'plugins.repository_config(',
             'lib/benchpark/accounting.py': 'plugins.repository_entries(',
             'lib/benchpark/cmd/list.py': 'plugins.repository_list_groups('}
    for rel, anchor in files.items():
        p = checkout / rel
        if not p.is_file() or p.is_symlink() or anchor not in p.read_text(encoding='utf-8'):
            raise ExtensionError('Repository discovery port missing: ' + str(p) + '; run updated 02 first')
    # Old no-C state is not removed automatically: it may contain site edits.
    for directory in (checkout / 'user-config', checkout.parent / 'benchpark-config'):
        if (directory / 'benchmark-sources.json').exists():
            raise ExtensionError('Legacy v1/v1.1 registration exists: ' + str(directory) +
                                 '; use a fresh workspace or review/restore the old owned configuration first')


def connect(root, name, checkout=None, refresh=False, replace_root=False,
            display_name=None, features=()):
    root = Path(root).expanduser().resolve()
    features = normalize_features(features)
    if checkout is not None:
        verify_seam(checkout)
    requested_display = display_name.strip() if isinstance(display_name, str) and display_name.strip() else name
    existing = installed_providers().get(name)
    metadata_updated = False
    baseline_refreshed = bool(refresh)
    snapshots = None
    if existing is not None:
        previous_root = getattr(existing, 'source_root', None)
        if previous_root != str(root) and not replace_root:
            raise ExtensionError('Provider name already belongs to another checkout; use --replace-root explicitly')
        if previous_root == str(root):
            # Validate the live checkout before deciding that no package update is
            # necessary. Discovery itself remains independent of this locator.
            existing.paths()
            current_display = getattr(existing, 'display_name', getattr(existing, 'name', name))
            current_features = tuple(getattr(existing, 'features', ()) or ())
            metadata_updated = (
                current_display != requested_display
                or current_features != features
            )
            if not refresh and not metadata_updated:
                return {'status': 'ALREADY_CONNECTED', 'name': name, 'source_root': str(root),
                        'display_name': current_display,
                        'features': list(current_features),
                        'metadata_updated': False,
                        'baseline_refreshed': False}
            # A display-only provider upgrade must not silently accept current
            # source changes as the new comparison baseline.
            snapshots = baseline(root) if refresh else dict(getattr(existing, 'baseline', {}) or {})
        else:
            # Moving an explicitly replaceable provider establishes a new source
            # root and therefore a new baseline for that root.
            snapshots = baseline(root)
            baseline_refreshed = True
            metadata_updated = True
    else:
        snapshots = baseline(root)
        baseline_refreshed = True
        metadata_updated = True
    with tempfile.TemporaryDirectory(prefix='benchpark-repository-install-') as scratch:
        if features:
            wheel = build_locator(
                root, name, scratch, snapshots, requested_display,
                features=features,
            )
        else:
            # Preserve the pre-feature call contract for ordinary repositories
            # and display-only reconnects.
            wheel = build_locator(
                root, name, scratch, snapshots, requested_display
            )
        h = hashlib.sha256(wheel.read_bytes()).hexdigest()
        subprocess.run([sys.executable, '-m', 'pip', 'install', '--no-index', '--no-deps',
                        '--force-reinstall', str(wheel)], check=True)
    return {'status': 'UPDATED' if existing is not None else 'CONNECTED',
            'name': name, 'display_name': requested_display,
            'features': list(features), 'source_root': str(root),
            'distribution': distribution_name(name), 'locator_wheel_sha256': h,
            'metadata_updated': metadata_updated, 'baseline_refreshed': baseline_refreshed,
            'editable': False, 'benchpark_config_written': False}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--source-root', type=Path, required=True)
    p.add_argument('--name', default='container-apps')
    p.add_argument('--display-name')
    p.add_argument(
        '--feature',
        action='append',
        default=[],
        help='Feature required to select this repository, e.g. container',
    )
    p.add_argument('--benchpark-root', type=Path)
    p.add_argument('--refresh-baseline', action='store_true')
    p.add_argument('--replace-root', action='store_true')
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument('--install', action='store_true')
    mode.add_argument('--wheel-dir', type=Path)
    a = p.parse_args()
    try:
        if a.install:
            if a.benchpark_root is None:
                p.error('--install requires --benchpark-root')
            result = connect(
                a.source_root,
                a.name,
                a.benchpark_root,
                a.refresh_baseline,
                a.replace_root,
                a.display_name,
                a.feature,
            )
        else:
            result = {
                'wheel': str(
                    build_locator(
                        a.source_root,
                        a.name,
                        a.wheel_dir,
                        display_name=a.display_name,
                        features=a.feature,
                    )
                )
            }
        print(json.dumps(result, indent=2, sort_keys=True))
        return 0
    except (OSError, ValueError, subprocess.CalledProcessError) as exc:
        print('Repository connection failed: ' + str(exc), file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
