# SPDX-License-Identifier: Apache-2.0
"""Repository paths supplied by trusted, normally installed locator providers.

Only a checkout locator is installed. Benchmark definitions remain in their
checkout and are discovered by native repository readers on each invocation.
No native Benchpark imports, configuration writes, network access, or monkey
patches are performed here.
"""
from copy import deepcopy
import importlib.metadata
import json
import os
from pathlib import Path, PurePosixPath
import re

from .api import ExtensionError

GROUP = 'benchpark.repositories.v1'
KINDS = {
    'experiments': ('experiments', 'experiment.py'),
    'applications': ('repos/ramble_applications', 'application.py'),
    'packages': ('repos/spack_repo/benchpark', 'package.py'),
    'systems': ('systems', 'system.py'),
}
PROVIDER_NAME = re.compile(r'^[a-z][a-z0-9-]*$')
FEATURE_NAME = re.compile(r'^[A-Za-z][A-Za-z0-9_-]*$')
FEATURE_NAME = re.compile(r'^[A-Za-z][A-Za-z0-9_-]*$')
FEATURE_NAME = re.compile(r'^[A-Za-z][A-Za-z0-9_-]*$')


def _points():
    points = importlib.metadata.entry_points()
    return list(points.select(group=GROUP)) if hasattr(points, 'select') else list(points.get(GROUP, ()))


def installed_providers():
    """Return explicitly installed locators, not every directory on the machine."""
    found = {}
    for ep in _points():
        if ep.name in found:
            raise ExtensionError('Duplicate repository provider: ' + ep.name)
        try:
            provider = ep.load()()
        except Exception as exc:
            raise ExtensionError('Cannot load repository provider ' + ep.name) from exc
        if getattr(provider, 'api_version', None) != 1:
            raise ExtensionError('Unsupported repository provider API: ' + ep.name)
        found[ep.name] = provider
    return found


def registered_roots():
    return {Path(p.source_root).resolve() for p in installed_providers().values()
            if isinstance(getattr(p, 'source_root', None), str)}


def select_repository(context):
    """Resolve one explicitly feature-bound repository implementation."""
    if not isinstance(context, dict):
        raise ExtensionError('Invalid repository selection context')

    kind = context.get('kind')
    name = context.get('name')
    features = context.get('features', ())

    if kind not in KINDS or not isinstance(name, str):
        raise ExtensionError('Invalid repository selection target')
    if not isinstance(features, (tuple, list)) or not all(
            isinstance(feature, str) for feature in features):
        raise ExtensionError('Invalid repository selection features')

    selected = []
    activated = []

    for provider_name, candidate in sorted(installed_providers().items()):
        bound_features = tuple(getattr(candidate, 'features', ()) or ())
        if bound_features and set(bound_features).issubset(set(features)):
            activated.append(provider_name)

        value = candidate.handle(
            'repository_select',
            {
                'kind': kind,
                'name': name,
                'features': list(features),
            },
        )
        if value is None:
            continue
        if value.get('provider') != provider_name:
            raise ExtensionError(
                'Repository selection owner mismatch: ' + provider_name
            )
        selected.append(value)

    if len(selected) > 1:
        raise ExtensionError(
            'Ambiguous repository provider selection (%s): %s'
            % (kind, name)
        )

    if selected:
        return selected[0]

    if activated:
        raise ExtensionError(
            'No repository implementation for %s (%s) in activated provider(s): %s'
            % (name, kind, ', '.join(activated))
        )

    return None


def _directory(path):
    path = Path(path)
    if not path.is_dir() or path.is_symlink():
        raise ExtensionError('Repository directory is missing or symlinked: ' + str(path))
    return path


def repository_object_root(path, kind):
    """Inspect native layout without importing any application/experiment code."""
    import yaml
    path = _directory(path)
    descriptor = path / 'repo.yaml'
    if not descriptor.is_file() or descriptor.is_symlink():
        raise ExtensionError('Repository descriptor missing or symlinked: ' + str(descriptor))
    data = yaml.safe_load(descriptor.read_text(encoding='utf-8'))
    repo = data.get('repo') if isinstance(data, dict) else None
    if not isinstance(repo, dict) or not isinstance(repo.get('namespace'), str) or not repo['namespace']:
        raise ExtensionError('Invalid repository descriptor: ' + str(descriptor))
    default = 'packages' if kind == 'packages' else ''
    sub = repo.get('subdirectory', default)
    if not isinstance(sub, str) or Path(sub).is_absolute() or '..' in PurePosixPath(sub).parts:
        raise ExtensionError('Unsafe repository subdirectory: ' + str(descriptor))
    current = path
    for part in PurePosixPath(sub).parts:
        current = _directory(current / part)
    return current


def repository_names(path, kind):
    current = repository_object_root(path, kind)
    filename = KINDS[kind][1]
    result = set()
    for child in sorted(current.iterdir()):
        if child.is_symlink():
            raise ExtensionError('Repository object symlink is not supported: ' + str(child))
        if child.is_dir() and (child / filename).is_file():
            if (child / filename).is_symlink():
                raise ExtensionError('Repository definition symlink: ' + str(child / filename))
            result.add(child.name.replace('-', '_') if kind == 'packages' else child.name)
    return result


class RepositoryProvider:
    api_version = 1

    def __init__(self, source_root, name, baseline=None, display_name=None, features=()):
        path = Path(source_root)
        if not path.is_absolute():
            raise ExtensionError('Installed repository locator must use an absolute checkout path')
        if not PROVIDER_NAME.fullmatch(name):
            raise ExtensionError('Invalid repository provider name: ' + str(name))
        self.source_root = str(path)
        self.name = name
        self.baseline = baseline or {}
        if display_name is not None and (not isinstance(display_name, str) or not display_name.strip()):
            raise ExtensionError('Invalid repository display name')
        self.display_name = display_name.strip() if isinstance(display_name, str) else name
        if isinstance(features, str):
            features = (features,)
        if not isinstance(features, (tuple, list)) or not all(
                isinstance(feature, str) and FEATURE_NAME.fullmatch(feature)
                for feature in features):
            raise ExtensionError('Invalid repository activation features')
        self.features = tuple(sorted(set(features)))

    def paths(self):
        root = _directory(self.source_root)
        result = {}
        for kind, (rel, _) in KINDS.items():
            path = root / rel
            # Experiment and Application repositories are required. The others
            # remain optional, e.g. a container-only application needs no build.
            if kind in ('experiments', 'applications') or path.exists() or path.is_symlink():
                repository_names(path, kind)
                result[kind] = str(path.resolve())
        return result

    def handle(self, event, context):
        if event == 'repository_select':
            kind = context.get('kind')
            name = context.get('name')
            requested = context.get('features', ())
            if kind not in KINDS or not isinstance(name, str):
                raise ExtensionError('Invalid repository selection request')
            if not isinstance(requested, (tuple, list)) or not all(
                    isinstance(feature, str) for feature in requested):
                raise ExtensionError('Invalid repository selection features')
            # A repository without activation features remains an ordinary
            # external repository and never overrides a native object.
            if not self.features or not set(self.features).issubset(set(requested)):
                return None
            paths = self.paths()
            if kind not in paths:
                return None
            repo_path = Path(paths[kind])
            if name not in repository_names(repo_path, kind):
                return None
            parent = repository_object_root(repo_path, kind)
            object_name = name.replace('-', '_') if kind == 'packages' else name
            object_dir = parent / object_name
            filename = KINDS[kind][1]
            if not (object_dir / filename).is_file():
                return None
            return {
                'provider': self.name,
                'display_name': self.display_name,
                'kind': kind,
                'name': name,
                'repository': str(repo_path.resolve()),
                'object_root': str(parent.resolve()),
                'object_dir': str(object_dir.resolve()),
                'source_root': str(Path(self.source_root).resolve()),
                'features': list(self.features),
            }

        if event == 'repository_list_group':
            kind = context['kind']
            if kind not in ('benchmarks', 'experiments'):
                return None
            paths = self.paths()
            if 'experiments' not in paths:
                return None
            names = repository_names(Path(paths['experiments']), 'experiments')
            native_benchmarks = context.get('native_benchmarks', ())
            if not isinstance(native_benchmarks, (tuple, list)) or not all(
                    isinstance(name, str) for name in native_benchmarks):
                raise ExtensionError('Invalid native benchmark provenance')
            native_benchmarks = set(native_benchmarks)
            items = []
            for item in context['collection']:
                benchmark = item.split('+', 1)[0] if kind == 'experiments' else item
                # Same logical name may intentionally have Native and feature-
                # bound implementations. Listing remains logical: the provider
                # groups only objects that are external-only, while request-time
                # repository_select chooses the alternate same-name provider.
                if benchmark in names and benchmark not in native_benchmarks:
                    items.append(item)
            return {'label': self.display_name, 'items': items}
        if event == 'repository_entries':
            kind = context['kind']
            entries = deepcopy(context['entries'])
            paths = self.paths()
            if kind not in paths:
                return entries
            repo_path = Path(paths[kind])
            parent = repository_object_root(repo_path, kind)
            names = repository_names(repo_path, kind)
            seen = {name: Path(directory).resolve() for name, directory in entries}
            for name in sorted(names):
                if name in seen:
                    if seen[name] != parent:
                        # Feature-bound repositories may intentionally provide
                        # another implementation of an existing logical
                        # benchmark. The normal listing keeps one logical name;
                        # request-time provider selection chooses the
                        # implementation.
                        if self.features and kind != 'systems':
                            continue
                        raise ExtensionError('Repository name collision (%s): %s' % (kind, name))
                else:
                    entries.append([name, str(parent)])
            return entries
        if event != 'repositories':
            raise ExtensionError('Unsupported repository event: ' + str(event))
        data = deepcopy(context['repositories'])
        base = Path(context['config_dir']).resolve()
        if not isinstance(data, dict) or not data:
            raise ExtensionError('Native Benchpark repository configuration is missing')
        paths = self.paths()
        for kind, external in paths.items():
            values = data.get(kind, [])
            if not isinstance(values, list) or not all(isinstance(v, str) for v in values):
                raise ExtensionError('Native repository group must be a list: ' + kind)
            target = Path(external)
            external_names = repository_names(target, kind)
            existing = [(base / Path(v).expanduser()).resolve() for v in values]
            # Preserve native ordering and reject ambiguity; an already listed
            # identical path is reused, never appended twice.
            for path in existing:
                if path == target:
                    continue
                overlap = external_names & repository_names(path, kind)
                if overlap:
                    # Feature-bound Experiment/Application/Package repositories
                    # may intentionally shadow the same logical benchmark.
                    # Systems remain unique and unconditionally fail closed.
                    if not self.features or kind == 'systems':
                        raise ExtensionError('Repository name collision (%s): %s; %s and %s' %
                                             (kind, ', '.join(sorted(overlap)), path, target))
            if target not in existing:
                # Relative strings work with Benchpark versions whose resolver
                # only returns a path for relative configuration entries.
                data[kind] = values + [os.path.relpath(target, base)]
        return data


def from_locator(filename):
    data = json.loads(Path(filename).read_text(encoding='utf-8'))
    if data.get('schema_version') != 1 or data.get('kind') != 'benchpark-repository-locator':
        raise ExtensionError('Invalid installed repository locator')
    return RepositoryProvider(
        data['source_root'],
        data['name'],
        data.get('baseline'),
        data.get('display_name'),
        data.get('features', ()),
    )
