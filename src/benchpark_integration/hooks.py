# SPDX-License-Identifier: Apache-2.0
"""Lifecycle broker outside Core. Features, not Core, own integration policy.

The protocol accepted by benchpark.plugins is structural and versioned. All
contexts consist of declared data; only the generation port accepts two narrow,
lazy section callbacks. No live Benchpark classes are imported or modified.
"""
from dataclasses import dataclass
from .api import plain
from .support.options import prepare_request
from .support.generation import resolve_experiment, RambleGeneration
from .support.io import save_system_snapshot
from .support.storage import publish_experiment, WorkspaceSession


class Provider:
    api_version = 1

    def handle(self, event, context):
        if event == 'request':
            return prepare_request(context)
        if event == 'system_saved':
            save_system_snapshot(context['settings'], context['dest'])
            return None
        if event == 'experiment_resolve':
            return resolve_experiment(context)
        if event == 'generation':
            return RambleGeneration(context['values'])
        if event == 'experiment_saved':
            publish_experiment(context['values'], context['dest'], context['system_dir'],
                               upstream_root=context['upstream_root'])
            return None
        if event == 'workspace':
            return WorkspaceSession(context['source'], context['output'])
        raise ValueError('Unsupported lifecycle event: ' + event)


def provider():
    return Provider()


@dataclass(frozen=True)
class Command:
    api_version: int
    help: str
    setup_parser: object
    handler: object


def cer_command():
    from benchpark_container.cli import command_descriptor
    desc = command_descriptor()
    return Command(1, desc.help, desc.setup_parser, desc.handler)
