# SPDX-License-Identifier: Apache-2.0
"""Extra list records from installed repositories; Native accounting stays Native.

No Core imports, config writes, snapshots of live classes or monkey patches.
System inspection is via two explicitly supplied read-only callbacks. These
callbacks return only variant values and programming-model names.
"""
from pathlib import Path
import yaml

from .api import ExtensionError
from .repository_provider import repository_names, repository_object_root


def additions(provider, context):
    kind = context['kind']
    if kind not in ('benchmarks', 'experiments', 'systems', 'modifiers'):
        raise ExtensionError('Unknown accounting kind: ' + str(kind))
    paths = provider.paths()
    repo_kind = 'systems' if kind == 'systems' else 'experiments'
    if repo_kind not in paths:
        return None
    repository = Path(paths[repo_kind])
    root = repository_object_root(repository, repo_kind)
    names = sorted(repository_names(repository, repo_kind))
    native_root = Path(context['source_root']) / repo_kind
    # Validate collisions through the existing real repository contract before
    # applying a filter; filtered-out Native names must not be shadowed either.
    provider.handle('repository_entries', {'kind': repo_kind, 'entries': [
        [p.name, str(native_root)] for p in sorted(native_root.iterdir())]})
    native_benchmarks = set(context.get('native_benchmarks', ()))
    ns = yaml.safe_load((repository / 'repo.yaml').read_text())['repo']['namespace']
    result = []
    for name in names:
        definition = root / name / ('system.py' if repo_kind == 'systems' else 'experiment.py')
        if definition.resolve() == (native_root / name / definition.name).resolve():
            continue
        if kind == 'benchmarks':
            if name not in native_benchmarks:
                result.append(name)
        elif kind == 'systems':
            result.extend(_system(name, context))
        elif kind == 'experiments':
            display = (ns + '.' + name) if context['namespaced'] and ns != 'builtin' else name
            if not context['namespaced'] and name in native_benchmarks:
                continue  # alternate implementation selected only via feature
            text = definition.read_text(encoding='utf-8')
            selected = {}
            for token, (category, value) in context['exp_dict'].items():
                if token in text:
                    selected.setdefault(category, []).append(value)
            suffix = ''
            for category in ('programming_model', 'scaling'):
                if not selected.get(category):
                    break
                suffix += '+[' + '|'.join(selected[category]) + ']'
            entry = display + suffix
            if entry not in context.get('native_experiments', ()):
                result.append(entry)
        else:
            # Preserve Native output and append external benchmark references
            # only to existing non-global modifiers. No new modifier registry.
            text = definition.read_text(encoding='utf-8')
            for modifier in context.get('native_modifiers', ()):
                if not modifier.startswith('\t') and modifier not in ('affinity', 'allocation', 'hwloc') and modifier in text:
                    entry = '\t!' + modifier + ': ' + name
                    if entry not in result:
                        result.append(entry)
    return {'label': provider.display_name, 'items': result}


def _system(name, context):
    variant_name = 'instance_type' if 'aws' in name else 'cluster'
    variants = context['system_variants'](name)
    if not isinstance(variants, dict):
        raise ExtensionError('Invalid system variant description')
    has_clusters = variant_name in variants
    clusters = variants[variant_name] if has_clusters else [name]
    model = context.get('programming_model')
    if model:
        clusters = [c for c in clusters if model in context['system_models'](
            f'{name} {variant_name}={c}' if has_clusters else name)]
    if not clusters:
        return []
    return [name + ' ' + variant_name + '=[' + '|'.join(clusters) + ']' if has_clusters else name]
