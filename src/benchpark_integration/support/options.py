# SPDX-License-Identifier: Apache-2.0
"""Pure-data request preparation. No imports of Benchpark native classes.

Native options are sent back to the UNCHANGED native concretizer. Extension
options are validated here and attached after native concretization. This avoids
patching each pass of the native schema algorithm or mutating class registries.
"""
from pathlib import Path
from benchpark_integration.discovery import entries, load_unique, NAME
from benchpark_integration.api import (ExtensionDescriptor, OptionSpec, ExtensionError,
    PreparerDescriptor, PreparationContext, PreparationResult, plain)


def _equal(values, value):
    return [str(x) for x in values] == [str(value)]


def _validate(option, values):
    if not isinstance(values, (tuple, list)) or not values or (not option['multi'] and len(values) != 1):
        raise ExtensionError('Invalid number of values for extension option: ' + option['name'])
    for value in values:
        if option['kind'] == 'boolean':
            valid = type(value) is bool
        else:
            typ = {'string': str, 'integer': int, 'float': float}[option['kind']]
            try:
                typ(value)
                valid = type(value) in (str, int, float)
            except (ValueError, TypeError):
                valid = False
        if not valid or (option['choices'] and value not in option['choices']):
            raise ExtensionError('Invalid value for extension option: ' + option['name'])
    return list(values)


def prepare_request(data):
    data = plain(data)
    explicit = data['variants']
    native = set(data['native_names'])
    defaults, settings = data['defaults'], data['settings']
    known = set(entries('benchpark.extensions')) | set(data.get('declared_features', ()))
    schema, selection, required = {}, [], {}
    for name in sorted(set(explicit) & known):
        if name in native:
            raise ExtensionError('Extension option conflicts with existing variant: ' + name)
        values = explicit[name]
        if len(values) != 1 or type(values[0]) is not bool:
            raise ExtensionError('Select %s with +%s or ~%s' % (name, name, name))
        options = (OptionSpec(name, False, 'Enable installed extension ' + name, 'boolean', (True, False)),)
        if values[0]:
            desc = load_unique('benchpark.extensions', name)
            if not isinstance(desc, ExtensionDescriptor):
                raise ExtensionError('Invalid extension descriptor: ' + name)
            selection.append(name)
            options += tuple(desc.options)
            for key, value in plain(desc.required_variants).items():
                if key not in native:
                    raise ExtensionError('Extension requires an unknown native option: ' + key)
                if key in required and required[key] != value:
                    raise ExtensionError('Extension native-option conflict: ' + key)
                required[key] = value
        for option in options:
            if option.name in native or option.name in schema or not NAME.fullmatch(option.name):
                raise ExtensionError('Duplicate/invalid extension option: ' + option.name)
            if option.kind not in ('boolean', 'string', 'integer', 'float'):
                raise ExtensionError('Unknown option type: ' + option.kind)
            schema[option.name] = dict(owner=name, name=option.name,
                default=plain(option.default), description=option.description,
                kind=option.kind, choices=plain(option.choices), multi=option.multi)
    updates, records = {}, {}
    for name in sorted(set(settings) & set(explicit)):
        if name not in entries('benchpark.preparers'):
            raise ExtensionError('Requested preparer is not installed: ' + name)
        desc = load_unique('benchpark.preparers', name)
        if not isinstance(desc, PreparerDescriptor):
            raise ExtensionError('Invalid preparer descriptor: ' + name)
        values = explicit[name]
        if _equal(values, desc.inactive_value):
            continue
        if len(values) != 1 or not NAME.fullmatch(str(values[0])):
            raise ExtensionError('One named request preset must be selected: ' + name)
        ctx = PreparationContext(data['name'], str(values[0]), explicit, defaults,
                                 settings[name], data['source_root'])
        result = desc.prepare(ctx)
        if not isinstance(result, PreparationResult):
            raise ExtensionError('Invalid preparation result: ' + name)
        for key, value in plain(result.overrides).items():
            if key not in defaults or isinstance(value, (dict, list)):
                raise ExtensionError('Preparer must change a declared scalar native option: ' + key)
            if key in explicit and not _equal(explicit[key], value):
                raise ExtensionError('CLI/preset conflict: ' + key)
            if key in updates and updates[key] != value:
                raise ExtensionError('Preparer conflict: ' + key)
            updates[key] = value
        records[name] = plain(result.provenance)
    for key, value in required.items():
        if key in updates and updates[key] != value:
            raise ExtensionError('Preparer/extension conflict: ' + key)
        if key in explicit and not _equal(explicit[key], value):
            raise ExtensionError('Explicit option conflicts with selected extension: ' + key)
        updates[key] = value
    native_values = {k: v for k, v in explicit.items() if k not in schema}
    for key, value in updates.items():
        if key not in native_values:
            native_values[key] = [value if type(value) is bool else str(value)]
    extension_values = {}
    for name, option in schema.items():
        value = explicit.get(name, option['default'])
        if not isinstance(value, (list, tuple)):
            value = [value]
        extension_values[name] = _validate(option, value)
    return {'native': native_values, 'extension_variants': extension_values,
            'state': {'extension_schema': schema, 'extension_selection': selection,
                      'extension_explicit': explicit, 'preparation_records': records}}
