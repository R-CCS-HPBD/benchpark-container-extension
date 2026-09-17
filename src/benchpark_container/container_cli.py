# SPDX-License-Identifier: Apache-2.0
"""Thin external CLI; catalog and storage work lives in the domain modules."""
import argparse
import json
from pathlib import Path
import sys
from .util import ValidationError


def command_descriptor():
    from benchpark_integration.api import CommandDescriptor
    return CommandDescriptor(name='container', api_version=2, help='Manage retained container images and catalogs',
                             setup_parser=setup_parser, handler=command)


def cli_command():
    from benchpark_integration.hooks import Command
    return Command(1, 'Manage retained container images and catalogs', setup_parser, command)


def setup_parser(parser):
    parser.add_argument('--catalog-config', type=Path, help='Visible catalog registration file (default: ~/benchpark-containers/catalogs.yaml)')
    sub = parser.add_subparsers(dest='container_action', required=True)
    sub.add_parser('list', help='List named releases across registered catalogs')
    for action in ('show', 'validate'):
        p = sub.add_parser(action)
        p.add_argument('image', help='catalog:name or an unambiguous name')
        p.add_argument('--release', required=True)
    p = sub.add_parser('register', help='Import bytes, verify, then publish an immutable release')
    p.add_argument('--catalog', required=True)
    p.add_argument('--manifest', type=Path, help='YAML declaration containing all platform/runtime bindings')
    p.add_argument('--name'); p.add_argument('--release')
    p.add_argument('--kind', choices=('sif', 'oci'))
    p.add_argument('--source', help='Local SIF path/file URI or registry/repository:tag or repository@sha256:digest (tag pinned at registration)')
    p.add_argument('--oci-layout', type=Path)
    p.add_argument('--platform', choices=('linux/amd64', 'linux/arm64'))
    p.add_argument('--accelerator', choices=('none','nvidia','amd'))
    p.add_argument('--sha256'); p.add_argument('--digest')
    p.add_argument('--runtime', action='append', default=[])
    p.add_argument('--python', dest='base_python', default='python3')
    p.add_argument('--shell', dest='base_shell', default='bash')
    p.add_argument('--skopeo', help='Explicit host OCI copier executable')
    p.add_argument('--timeout', type=int, default=3600)
    p = sub.add_parser('catalog')
    c = p.add_subparsers(dest='catalog_action', required=True)
    c.add_parser('list')
    for action in ('init', 'add'):
        a = c.add_parser(action); a.add_argument('path', type=Path); a.add_argument('--name', required=True)
    a = c.add_parser('remove', help='Unregister an alias; never delete images or catalog files')
    a.add_argument('name')


def declaration(args):
    from .catalog.config import load_yaml
    if args.manifest:
        if any((args.name, args.release, args.kind, args.source, args.oci_layout, args.platform,
                args.accelerator, args.sha256, args.digest, args.runtime)):
            raise ValidationError('--manifest cannot be combined with inline image declarations')
        data = load_yaml(args.manifest)
        # Relative source references are anchored to the declaration, not CWD.
        for item in data.get('artifacts', []):
            if 'oci_layout' in item and not Path(item['oci_layout']).is_absolute():
                item['oci_layout'] = str((args.manifest.resolve().parent / item['oci_layout']).resolve())
        return data
    if not all((args.name, args.release, args.kind, args.platform, args.accelerator)):
        raise ValidationError('Inline registration requires --name --release --kind --platform --accelerator')
    item = {'kind': args.kind, 'platform': args.platform, 'accelerator': args.accelerator,
            'runtimes': args.runtime, 'tools': {'python': args.base_python, 'shell': args.base_shell}}
    if args.source:
        source = args.source
        if args.kind == 'sif' and not source.startswith('file://'):
            source = Path(source).expanduser().resolve(strict=True).as_uri()
        item['uri'] = source
    if args.oci_layout: item['oci_layout'] = str(args.oci_layout.expanduser().resolve(strict=True))
    if args.sha256: item['sha256'] = args.sha256
    if args.digest: item['digest'] = args.digest
    return {'schema_version': 1, 'name': args.name, 'release': args.release, 'artifacts': [item]}


def command(args):
    from .catalog.config import read_config, config_path, update_registration
    from .catalog.manager import Catalog, init_catalog, lookup
    try:
        config = read_config(args.catalog_config)
        action = args.container_action
        if action == 'catalog':
            if args.catalog_action == 'init':
                result = init_catalog(args.path, args.name)
            elif args.catalog_action == 'add':
                result = update_registration(args.name, args.path, config=args.catalog_config)
            elif args.catalog_action == 'remove':
                result = update_registration(args.name, remove=True, config=args.catalog_config)
            else:
                result = {'configuration': str(config_path(args.catalog_config)), 'catalogs': config['catalogs']}
        elif action == 'list':
            result = [dict(item, catalog=alias, image=alias + ':' + item['name'])
                      for alias, path in sorted(config['catalogs'].items()) for item in Catalog(path).list()]
        elif action in ('show', 'validate'):
            alias, catalog, entry = lookup(args.image, args.release, args.catalog_config)
            result = entry if action == 'show' else catalog.validate(entry['name'], entry['release'])
        else:
            if args.catalog not in config['catalogs']:
                raise ValidationError('Register the catalog alias first: ' + args.catalog)
            if args.timeout <= 0:
                raise ValidationError('--timeout must be positive')
            result = Catalog(config['catalogs'][args.catalog]).register(declaration(args),
                      skopeo=args.skopeo or config['skopeo'], timeout=args.timeout)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (ValidationError, OSError, ValueError, KeyError) as error:
        print('Container catalog error: ' + str(error), file=sys.stderr)
        return 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    setup_parser(parser)
    return command(parser.parse_args())


if __name__ == '__main__':
    raise SystemExit(main())
