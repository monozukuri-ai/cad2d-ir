from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any

import pytest
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib

import cad2d_ir

ROOT = Path(__file__).resolve().parents[1]
ADAPTER_PACKAGES = {
    "dwg": "ezdwg",
    "dgn": "ezdgn",
    "dwf": "ezdwf",
    "exb": "ezexb",
    "hpgl": "ezhpgl",
    "icad": "icadkit",
    "idw": "inventor-kit",
    "jww": "ezjww",
    "mi": "ezmi2d",
    "pdf": "pypdfium2",
    "sxf": "ezsxf",
}
# These adapters require explicit opt-in for licensing/dependency or platform reasons.
EXPLICIT_EXTRAS = {"idw", "icad"}


@pytest.fixture(scope="module")
def project() -> dict[str, Any]:
    return tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))[
        "project"
    ]


def test_release_metadata_keeps_version_and_license(project: dict[str, Any]) -> None:
    assert project["version"] == cad2d_ir.__version__
    assert project["requires-python"] == ">=3.10"
    assert project["license"] == "MIT"
    assert project["license-files"] == ["LICENSE"]
    assert (ROOT / "LICENSE").is_file()


@pytest.mark.parametrize(("extra", "package_name"), ADAPTER_PACKAGES.items())
def test_adapter_extra_requires_expected_package(
    project: dict[str, Any], extra: str, package_name: str
) -> None:
    requirements = [
        Requirement(value) for value in project["optional-dependencies"][extra]
    ]
    assert [canonicalize_name(requirement.name) for requirement in requirements] == [
        package_name
    ]


def test_all_extra_matches_individual_adapters(project: dict[str, Any]) -> None:
    extras = project["optional-dependencies"]
    assert set(extras) == set(ADAPTER_PACKAGES) | {"all"}
    # Compare complete requirements, including ranges and markers, without copying
    # their versions into the tests. Counter also detects duplicate requirements.
    expected = Counter(
        Requirement(value)
        for extra in ADAPTER_PACKAGES
        if extra not in EXPLICIT_EXTRAS
        for value in extras[extra]
    )
    assert Counter(Requirement(value) for value in extras["all"]) == expected


@pytest.mark.parametrize(
    ("python_version", "enabled"), [("3.10", False), ("3.11", True), ("3.14", True)]
)
def test_idw_extra_keeps_python_compatibility_marker(
    project: dict[str, Any], python_version: str, enabled: bool
) -> None:
    requirement = Requirement(project["optional-dependencies"]["idw"][0])
    assert requirement.marker is not None
    assert (
        requirement.marker.evaluate(
            {
                "python_version": python_version,
                "python_full_version": f"{python_version}.0",
            }
        )
        is enabled
    )
