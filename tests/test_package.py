# SPDX-License-Identifier: Apache-2.0
import subprocess
import sys
import zipfile
from pathlib import Path
from verify_package import verify


def test_real_wheel_install_and_entry_points(repository,tmp_path):
    proc = subprocess.run([sys.executable,'-m','pip','wheel',str(repository),'--no-deps','--no-build-isolation','-w',str(tmp_path)],
                          capture_output=True, text=True)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    wheel=next(tmp_path.glob('*.whl'))
    with zipfile.ZipFile(wheel) as z:
        assert not any('__pycache__' in n or n.endswith('.pyc') for n in z.namelist())
        assert any(n.startswith('benchpark_integration/') for n in z.namelist())
        assert not any(n.startswith('benchpark/') for n in z.namelist())
    data=verify(wheel)
    assert data['installed_origin_verified'] is True and len(data['cli_fixture_commands'])==4
