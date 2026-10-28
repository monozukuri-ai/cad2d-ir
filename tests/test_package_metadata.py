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
    assert project["optional-dependencies"]["dgn"] == ["ezdgn>=0.3,<0.4"]
    assert project["optional-dependencies"]["dwf"] == ["ezdwf>=0.0.1,<0.1"]
    assert project["optional-dependencies"]["hpgl"] == ["ezhpgl>=0.1.0a2,<0.2"]
    assert project["optional-dependencies"]["exb"] == ["ezexb>=0.3,<0.4"]
    assert project["optional-dependencies"]["jww"] == ["ezjww>=0.2.6,<0.8"]
    assert project["optional-dependencies"]["mi"] == ["ezmi2d>=0.2,<0.3"]
    assert project["optional-dependencies"]["pdf"] == ["pypdfium2>=4.30,<6"]
    assert project["optional-dependencies"]["sxf"] == ["ezsxf>=0.2,<0.4"]
    # inventor-kit pulls cq-acis/CadQuery; it stays out of `all` and needs 3.11+
    assert project["optional-dependencies"]["idw"] == [
        "inventor-kit>=0.9.1,<0.10; python_version >= '3.11'"
    ]
    assert not any(
        "inventor-kit" in requirement
        for requirement in project["optional-dependencies"]["all"]
    )
    assert project["optional-dependencies"]["all"] == [
        "ezhpgl>=0.1.0a2,<0.2",
        "ezdwg>=0.11,<1",
        "ezdgn>=0.3,<0.4",
        "ezdwf>=0.0.1,<0.1",
        "ezexb>=0.3,<0.4",
        "ezjww>=0.2.6,<0.8",
        "ezmi2d>=0.2,<0.3",
        "pypdfium2>=4.30,<6",
        "ezsxf>=0.2,<0.4",
    ]
