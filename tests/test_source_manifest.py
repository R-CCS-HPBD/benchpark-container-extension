# SPDX-License-Identifier: Apache-2.0
"""Keep the sdist's audit and setup inputs; wheel and ZIP have separate tests."""
import subprocess
import sys
import tarfile


def test_sdist_contains_frozen_checklist_and_setup_script(repository,tmp_path):
    command=[sys.executable,'-c',
             'from setuptools.build_meta import build_sdist; build_sdist(%r)' % str(tmp_path)]
    proc=subprocess.run(command,cwd=repository,capture_output=True,text=True)
    assert proc.returncode==0,proc.stdout+proc.stderr
    archive=next(tmp_path.glob('*.tar.gz'))
    with tarfile.open(archive) as stream:
        names=stream.getnames()
    for rel in (
        'core/files/lib/benchpark/plugins.py',
        'docs/review/checklist.json',
        'docs/review/INITIAL_RECEIPT.json',
        'docs/review/round-03-notes.json',
        'examples/experiments/common-base-smoke/setup/install-demo-tool.sh',
        'tests/reference/v0.5.2.zip',
    ):
        assert any(n.endswith('/'+rel) for n in names),rel
