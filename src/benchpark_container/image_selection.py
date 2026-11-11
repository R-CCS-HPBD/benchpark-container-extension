# SPDX-License-Identifier: Apache-2.0
"""System capability + instance selection + immutable image request resolution."""
from pathlib import Path
from .contracts import runtime_settings, container_tools
from .util import ValidationError, strict, safe_name, identity
from .reproducibility import pin_image
from .catalog.manager import lookup, fixed_release
from .catalog.config import read_config
from .provenance import source_provenance
from .backends.registry import backend_class
from .image_store import verify_managed


def choice(variants, key):
    value = variants.get(key, ['default'])
    if not isinstance(value, (list, tuple)) or len(value) != 1 or not isinstance(value[0], str):
        raise ValidationError('Select one value per initialized experiment: ' + key)
    return value[0]


def select_runtime(system, variants):
    if not isinstance(system, dict) or system.get('schema_version') != 3:
        raise ValidationError('System schema_version=3 is required; migrate system.py and re-initialize')
    strict(system, ('schema_version', 'execution', 'runtimes', 'default_runtime', 'platform', 'artifact_roots', 'parallel', 'build_environment'),
           'System', ('schema_version', 'execution', 'runtimes', 'platform'))
    if system['schema_version'] != 3:
        raise ValidationError('System schema_version=3 is required; migrate system.py and re-initialize')
    strict(system['execution'], ('worker_python', 'image_cache', 'gpu', 'providers', 'image_policy'), 'common execution', ('worker_python', 'image_cache'))
    if system['platform'] not in ('linux/arm64', 'linux/amd64'):
        raise ValidationError('System requires a canonical linux/arm64 or linux/amd64 platform')
    if "build_environment" in system:
        from .image_build import validate_build_environment
        validate_build_environment(system["build_environment"])
    runtimes = system['runtimes']
    if not isinstance(runtimes, dict) or not runtimes:
        raise ValidationError('System must declare at least one runtime')
    for name, data in runtimes.items():
        safe_name(name, 'runtime')
        strict(data, ('executable', 'backend_options'), 'runtime capability', ('executable',))
        runtime_settings(dict(system['execution'], **data, runtime=name))
    default = system.get('default_runtime')
    if default is not None and default not in runtimes:
        raise ValidationError('System default_runtime is not registered')
    selected = choice(variants, 'container_runtime')
    origin = 'explicit'
    if selected == 'default':
        selected, origin = default, 'system-default'
        if selected is None and len(runtimes) == 1:
            selected, origin = next(iter(runtimes)), 'only-registered-runtime'
        if selected is None:
            raise ValidationError('Multiple runtimes are available; set container_runtime or System default_runtime')
    if selected not in runtimes:
        raise ValidationError('Runtime is not registered on this System: ' + selected)
    runtime = runtime_settings(dict(system['execution'], **runtimes[selected], runtime=selected))
    return runtime, {'selected': selected, 'selection_origin': origin,
                     'system_default': default, 'available': sorted(runtimes)}


def select_image(system, request, variants, runtime):
    name = choice(variants, 'container_image')
    release = choice(variants, 'container_release')
    image_origin = 'explicit' if name != 'default' else 'experiment-default'
    if name == 'default':
        name = request['default_image']
    direct_uri = isinstance(name, str) and ('@sha256:' in name or name.startswith(('docker://', 'file://')))
    if direct_uri:
        default = request.get('default_image')
        tools = request.get('image_tools') or (default.get('tools') if isinstance(default, dict) else None) or {'shell': 'bash'}
        accelerator = request.get('image_accelerator')
        if accelerator is None and isinstance(default, dict): accelerator = default.get('accelerator')
        name = {'uri': name, 'tools': tools, 'managed': False, 'platform': system['platform']}
        if accelerator is not None: name['accelerator'] = accelerator
        if release == 'default':
            frozen = pin_image(name)
            content = frozen.get('digest', frozen.get('sif_sha256', '')).removeprefix('sha256:')
            release = 'direct-' + content[:16]
    elif image_origin == 'explicit' and release == 'default':
        raise ValidationError('An image override requires an explicit container_release')
    if release == 'default':
        release = request['default_release']
    fixed_release(release)
    if isinstance(name, dict):
        # Explicit non-managed definition is retained as a development option,
        # never misrepresented as Catalog-preserved or used as a fallback.
        strict(name, ('uri', 'tools', 'sha256', 'platform', 'managed', 'accelerator'), 'direct image', ('uri', 'tools', 'managed'))
        if name['managed'] is not False:
            raise ValidationError('Direct images must explicitly declare managed=False; register for retention')
        image = pin_image(name)
        image['tools'] = container_tools(name['tools'])
        if image['kind'] not in backend_class(runtime['runtime'], runtime.get('providers', [])).capabilities.image_kinds:
            raise ValidationError('Selected runtime does not support this image kind')
        if image.get('platform', 'unverified') not in ('unverified', system['platform']):
            raise ValidationError('Direct image platform mismatch')
        accelerator = name.get('accelerator')
        if accelerator is not None and accelerator not in ('none', 'nvidia', 'amd'):
            raise ValidationError('Unknown direct image accelerator family')
        if runtime['gpu'] != 'none' and accelerator is None:
            raise ValidationError('Direct GPU image must declare accelerator=nvidia or accelerator=amd')
        if accelerator is not None and runtime['gpu'] != 'none' and accelerator != runtime['gpu']:
            raise ValidationError('Direct image accelerator does not match System GPU policy')
        image.update(logical_name='direct', release=release)
        if accelerator is not None:
            image['accelerator'] = accelerator
        return image, {'source': 'experiment-direct', 'managed': False, 'release': release,
                       'accelerator': accelerator, 'selection_origin': image_origin,
                       'warning': 'image bytes are not retained by Catalog'}
    if not isinstance(name, str):
        raise ValidationError('default_image must be a catalog name or explicit direct-image declaration')
    alias, catalog, entry = lookup(name, release)
    cls = backend_class(runtime['runtime'], runtime.get('providers', []))
    if entry['schema_version'] == 2:
        from .catalog.environments import select
        image, artifact = select(catalog, entry, runtime, system['platform'], cls)
    else:
        candidates = [a for a in entry['artifacts'] if a['platform'] == system['platform']
                      and (runtime['gpu'] == 'none' or a['accelerator'] == runtime['gpu'])
                      and a['kind'] in cls.capabilities.image_kinds
                      and (not a['runtimes'] or runtime['runtime'] in a['runtimes'])]
        # Backend preference is part of its capability contract, not a runtime-name
        # switch in resolver. SIF families prefer exact stored SIF over conversion.
        for kind in cls.preferred_image_kinds:
            selected = [a for a in candidates if a['kind'] == kind]
            if selected:
                candidates = selected
                break
        if len(candidates) != 1:
            raise ValidationError('Catalog must resolve exactly one platform/accelerator/runtime image binding')
        artifact = candidates[0]
        image = catalog.image(artifact)
    image['tools'] = container_tools(image['tools'])
    verify_managed(image)
    if image['kind'] == 'oci':
        image['managed']['skopeo'] = read_config()['skopeo']
    image.update(logical_name=entry['name'], release=release)
    provenance = source_provenance(catalog.root, [])
    return image, {'source': 'catalog', 'managed': True, 'catalog_alias': alias,
                   'catalog_id': catalog.header['catalog_id'], 'catalog_root': str(catalog.root),
                   'catalog_provenance': provenance, 'entry_sha256': entry['entry_sha256'],
                   'entry_snapshot': entry, 'name': entry['name'], 'release': release,
                   'accelerator': artifact['accelerator'], 'selection_origin': image_origin}


def select_base_candidate(system, request, variants):
    """Pure per-platform/backend selection; no host/GPU probing or downloads.

    Explicit container_image overrides remain authoritative. A candidate is a
    direct image declaration or an existing Catalog name/release, not a new
    Catalog or a workload-specific resolver.
    """
    candidates = request.get('base_candidates')
    if not isinstance(candidates, list) or not candidates:
        raise ValidationError('base_candidates must be a nonempty list')
    matches = []
    for candidate in candidates:
        strict(candidate, ('platform', 'accelerator', 'runtime', 'image', 'release', 'image_build'),
               'base candidate', ('platform', 'accelerator', 'runtime', 'image', 'release'))
        if candidate['platform'] not in ('linux/amd64', 'linux/arm64'):
            raise ValidationError('Invalid Base candidate platform')
        if candidate['accelerator'] not in ('none', 'nvidia', 'amd'):
            raise ValidationError('Invalid Base candidate accelerator')
        if not isinstance(candidate['runtime'], str) or not candidate['runtime']:
            raise ValidationError('Invalid Base candidate runtime')
        fixed_release(candidate['release'])
    runtime, _ = select_runtime(system, variants)
    for candidate in candidates:
        if (candidate['platform'] == system['platform']
                and candidate['accelerator'] == runtime['gpu']
                and candidate['runtime'] == runtime['runtime']):
            matches.append(candidate)
    if choice(variants, 'container_image') != 'default':
        return dict(request)  # No implicit fallback from an explicit selection.
    if len(matches) != 1:
        raise ValidationError('Base candidates must match exactly one platform/accelerator/runtime')
    selected = matches[0]
    image = selected['image']
    if isinstance(image, dict):
        image = dict(image)
        if image.get('platform', selected['platform']) != selected['platform']:
            raise ValidationError('Base candidate/image platform mismatch')
        if image.get('accelerator', selected['accelerator']) != selected['accelerator']:
            raise ValidationError('Base candidate/image accelerator mismatch')
        image.update(platform=selected['platform'], accelerator=selected['accelerator'])
    result = dict(request, default_image=image, default_release=selected['release'])
    if 'image_build' in selected:
        if 'image_build' in request:
            raise ValidationError('Declare image_build either globally or in a candidate, not both')
        result['image_build'] = selected['image_build']
    return result
