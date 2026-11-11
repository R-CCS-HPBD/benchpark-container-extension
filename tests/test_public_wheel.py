
import configparser
import io
import subprocess
import sys
import tomllib
import zipfile
from pathlib import Path


def test_public_wheel_contents_and_entrypoints(tmp_path):
    root = Path(__file__).resolve().parents[1]
    out = tmp_path / "wheels"
    out.mkdir()

    result = subprocess.run(
        [
            sys.executable, "-m", "pip", "wheel",
            "--no-deps", "--no-build-isolation",
            "-w", str(out), str(root),
        ],
        capture_output=True, text=True,
    )
    assert result.returncode == 0, (
        result.stdout[-2000:] + result.stderr[-2000:]
    )

    wheels = list(out.glob("*.whl"))
    assert len(wheels) == 1

    with zipfile.ZipFile(wheels[0]) as wheel:
        names = set(wheel.namelist())

        assert "benchpark_container/__init__.py" in names
        assert any(n.startswith("benchpark_integration/") for n in names)
        assert not any(n.startswith("benchpark/") for n in names)

        for archive in ("runtime.pyz", "catalog_publish.pyz"):
            name = "benchpark_container/resources/" + archive
            assert name in names
            with zipfile.ZipFile(io.BytesIO(wheel.read(name))) as z:
                assert z.testzip() is None
                assert len(z.namelist()) > 0

        project = tomllib.loads(
            (root / "pyproject.toml").read_text()
        )
        scripts = project.get("project", {}).get("scripts", {})
        if scripts:
            entries = [
                n for n in names
                if n.endswith(".dist-info/entry_points.txt")
            ]
            assert len(entries) == 1
            config = configparser.ConfigParser(interpolation=None)
            config.optionxform = str
            config.read_string(wheel.read(entries[0]).decode())
            for name, target in scripts.items():
                assert config.get("console_scripts", name) == target
