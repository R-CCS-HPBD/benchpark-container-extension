# SPDX-License-Identifier: Apache-2.0
"""Public, side-effect-free declaration validator used by init and CI/manual.

Static validation reads declared files but neither downloads nor installs. It
never claims that the target Base environment was actually built or executed.
"""
import argparse
import json
from pathlib import Path
import re
import sys
from urllib.parse import urlparse, unquote

from .requirements import scan_requirements
from .reproducibility_rules import (RULESET_VERSION, ReproducibilityError,
    fixed_hash, validate_model_artifacts, validate_additions)
from .util import ValidationError, check_no_secrets, sha256, inside

IMAGE = re.compile(r"^(?:docker://)?[A-Za-z0-9][A-Za-z0-9._:/-]*@sha256:([0-9a-f]{64})$")


def pin_image(value):
    """Validate a concrete System selection, preserving a local SIF's identity."""
    if not isinstance(value, dict) or not isinstance(value.get('uri'), str):
        raise ValidationError('image requires a URI string')
    check_no_secrets(value)
    if isinstance(value.get('managed'), dict):
        from .image_store import verify_managed
        verify_managed(value)
        return dict(value)
    uri = value['uri']
    match = IMAGE.fullmatch(uri)
    result = {'uri': uri, 'platform': value.get('platform', 'unverified')}
    if match:
        if uri.split('@', 1)[0].rsplit('/', 1)[-1].endswith(':latest'):
            raise ValidationError('latest is not permitted; use the digest reference without a latest tag')
        digest = match.group(1)
        if len(set(digest)) == 1:
            raise ValidationError('Placeholder image digest is not executable')
        result.update(kind='oci', digest='sha256:' + digest)
    elif uri.startswith('file://'):
        parsed = urlparse(uri)
        if parsed.netloc or parsed.query or parsed.fragment:
            raise ValidationError('Local SIF must be file:///absolute.sif without host/query/fragment')
        path = Path(unquote(parsed.path))
        if not path.is_absolute() or not path.is_file():
            raise ValidationError('Local SIF must be an existing absolute file')
        value_hash = value.get('sha256', value.get('sif_sha256'))
        if value_hash is not None:
            fixed_hash(value_hash, 'image.sha256')
        actual = sha256(path)
        if value_hash and value_hash != actual:
            raise ValidationError('SIF hash does not match declaration')
        result.update(kind='sif', source=str(path.resolve()), sif_sha256=actual,
                      origin=value.get('origin', 'not-recorded'))
    else:
        raise ValidationError('Use immutable repo@sha256:<64-hex> or file:///absolute.sif; mutable tags/ranges are rejected')
    return result


def validate_inputs(source_root, requirements, artifacts=(), variants=None):
    """Shared static checks after image selection; no Base environment required."""
    root = Path(source_root).resolve()
    if not root.is_dir():
        raise ValidationError('Experiment source_root must be an existing directory')
    checked_artifacts = []
    for artifact in artifacts:
        a = dict(artifact)
        # Direct local model files become fixed source snapshots at init.
        if a.get('kind') in ('model', 'tokenizer') and 'source' in a:
            path = inside(root, a['source'], must_exist=True)
            if not path.is_file():
                raise ValidationError('Source models must be files; directory models require System root + revision/manifest')
            actual = sha256(path)
            if a.get('sha256') and a['sha256'] != actual:
                raise ValidationError('Model source checksum mismatch: ' + a.get('name', 'unnamed'))
            a['sha256'] = actual
        checked_artifacts.append(a)
    req = scan_requirements(root, requirements)
    models = validate_model_artifacts(checked_artifacts, variants)
    return {'ruleset_version': RULESET_VERSION, 'status': 'passed-static-validation',
            'requirements': req, 'models': models,
            'pending': ['dependency-resolution-in-selected-base',
                        'base-version-conflict-check', 'mounted-artifact-verification']}


def validate_request(request, relative_to=None):
    """A data-only CI request; never executes system.py/experiment.py."""
    if not isinstance(request, dict):
        raise ValidationError('Validation input must be an object')
    allowed = {'schema_version', 'source_root', 'image', 'requirements', 'artifacts', 'variants'}
    if set(request) - allowed or request.get('schema_version') != 1:
        raise ValidationError('Unsupported validation request schema/fields')
    root = Path(request.get('source_root', '.'))
    if not root.is_absolute():
        root = Path(relative_to or '.') / root
    image = pin_image(request.get('image'))
    result = validate_inputs(root, request.get('requirements', []),
                             request.get('artifacts', []), request.get('variants', {}))
    return dict(result, image=image)


def validate_saved_plan(path, resources):
    """Re-check a generated plan and its immutable resources without modifying it."""
    path, resources = Path(path), Path(resources).resolve()
    plan = json.loads(path.read_text(encoding='utf-8'))
    if plan.get('schema_version') != 1:
        raise ValidationError('Unsupported execution-plan schema')
    for relative, expected in plan['resources'].items():
        item = inside(resources, relative, must_exist=True)
        if sha256(item) != expected:
            raise ValidationError('Fixed resource changed: ' + relative)
    # Input requirements' local wheel references use inputs/ as their CWD.
    requested = []
    for item in plan['requirements']:
        if not item.startswith('inputs/'):
            raise ValidationError('Unsupported staged requirements location')
        requested.append(item[len('inputs/'):])
    image = pin_image(plan['base'])
    result = validate_inputs(resources / 'inputs', requested, plan['artifacts'],
        {k: v if isinstance(v, list) else [v] for k, v in plan['parameters'].items()})
    if result['requirements']['pins'] != plan.get('requirement_pins'):
        raise ValidationError('Recorded pins differ from the staged requirements')
    return dict(result, image=image, plan=str(path))


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument('--request', type=Path, help='JSON declaration for CI (paths relative to this file)')
    modes.add_argument('--plan', type=Path, help='Generated container.json or concrete execution plan')
    modes.add_argument('--image', help='Fixed image URI for direct checks')
    modes.add_argument('--pip-report', type=Path, help='Saved pip dry-run report; no installation')
    parser.add_argument('--pins', type=Path, help='JSON {package: [exact versions]}, with --pip-report')
    parser.add_argument('--base-inventory', type=Path, help='Saved Base inventory or environment.json, with --pip-report')
    parser.add_argument('--resources', type=Path, help='Staged resources, required with --plan')
    parser.add_argument('--source-root', type=Path)
    parser.add_argument('-r', '--requirements', action='append', default=[])
    parser.add_argument('--model-revision', help='Optional full immutable model commit to check')
    args = parser.parse_args(argv)
    if args.pip_report and (not args.pins or not args.base_inventory):
        parser.error('--pip-report requires --pins and --base-inventory')
    if not args.pip_report and (args.pins or args.base_inventory):
        parser.error('--pins/--base-inventory are only valid with --pip-report')
    if args.plan and not args.resources:
        parser.error('--plan requires --resources')
    if not args.plan and args.resources:
        parser.error('--resources is only valid with --plan')
    if not args.image and (args.source_root or args.requirements or args.model_revision):
        parser.error('--source-root/-r/--model-revision are only valid with --image')
    try:
        if args.pip_report:
            from packaging.version import Version, InvalidVersion
            def equal(left, right):
                try:
                    return Version(left) == Version(right)
                except InvalidVersion:
                    return left == right
            base = json.loads(args.base_inventory.read_text(encoding='utf-8'))
            base = base.get('base', base)
            result = validate_additions(
                json.loads(args.pip_report.read_text(encoding='utf-8')),
                json.loads(args.pins.read_text(encoding='utf-8')),
                base['packages'], version_equal=equal)
        elif args.request:
            request = json.loads(args.request.read_text(encoding='utf-8'))
            result = validate_request(request, args.request.parent)
        elif args.plan:
            result = validate_saved_plan(args.plan, args.resources)
        else:
            artifacts = ([{'name': 'model', 'kind': 'model', 'revision': args.model_revision}]
                         if args.model_revision is not None else [])
            result = validate_request({'schema_version': 1, 'image': {'uri': args.image},
                'source_root': str(args.source_root or Path.cwd()),
                'requirements': args.requirements, 'artifacts': artifacts})
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (ValueError, OSError, KeyError, TypeError) as error:
        result = {'ruleset_version': RULESET_VERSION, 'status': 'failed',
                  'code': getattr(error, 'code', 'DECLARATION_INVALID'), 'error': str(error),
                  'details': getattr(error, 'details', {})}
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
