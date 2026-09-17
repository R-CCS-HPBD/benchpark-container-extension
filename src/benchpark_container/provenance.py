# SPDX-License-Identifier: Apache-2.0
"""Non-mutating optional Git provenance. A snapshot hash is not a Git claim."""
from pathlib import Path
import subprocess


def source_provenance(root, scripts):
    root = Path(root).resolve()
    def git(*args):
        return subprocess.run(['git', '-C', str(root), *args], capture_output=True,
                              text=True, timeout=10)
    try:
        probe = git('rev-parse', '--show-toplevel')
        if probe.returncode:
            return {'git_status': 'not-available', 'script_content': 'fixed-in-resources',
                    'script_semantics': 'trusted-source-review-required'}
        top = Path(probe.stdout.strip())
        commit = git('rev-parse', 'HEAD')
        status = git('status', '--porcelain', '--', str(root))
        tracked = {}
        for script in scripts:
            rel = str((root / script).relative_to(top))
            tracked[script] = git('-C', str(top), 'ls-files', '--error-unmatch', '--', rel).returncode == 0
        return {'git_status': 'available', 'commit': commit.stdout.strip() if commit.returncode == 0 else None,
                'worktree_dirty': bool(status.stdout.strip()), 'setup_scripts_tracked': tracked,
                'script_content': 'fixed-in-resources', 'script_semantics': 'trusted-source-review-required'}
    except (OSError, ValueError, subprocess.TimeoutExpired):
        return {'git_status': 'not-available', 'script_content': 'fixed-in-resources',
                'script_semantics': 'trusted-source-review-required'}
