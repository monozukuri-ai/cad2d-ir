from __future__ import annotations

from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib

import cad2d_ir


def test_release_metadata_keeps_license_and_adapter_extras() -> None:
    root = Path(__file__).resolve().parents[1]
    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))[
        "project"
    ]

    assert project["version"] == cad2d_ir.__version__
    assert project["requires-python"] == ">=3.10"
    assert project["license"] == "MIT"
    assert project["license-files"] == ["LICENSE"]
    assert (root / "LICENSE").is_file()
    assert project["optional-dependencies"]["dwg"] == ["ezdwg>=0.11,<1"]
    assert project["optional-dependencies"]["dgn"] == ["ezdgn>=0.2.1,<0.3"]
    assert project["optional-dependencies"]["dwf"] == ["ezdwf>=0.0.1,<0.1"]
    assert project["optional-dependencies"]["jww"] == ["ezjww>=0.2.6,<0.3"]
    assert project["optional-dependencies"]["mi"] == ["ezmi2d>=0.2,<0.3"]
    assert project["optional-dependencies"]["sxf"] == ["ezsxf>=0.1,<0.2"]
    assert project["optional-dependencies"]["all"] == [
        "ezdwg>=0.11,<1",
        "ezdgn>=0.2.1,<0.3",
        "ezdwf>=0.0.1,<0.1",
        "ezjww>=0.2.6,<0.3",
        "ezmi2d>=0.2,<0.3",
        "ezsxf>=0.1,<0.2",
    ]
