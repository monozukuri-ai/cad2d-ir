"""Check that a release is consistent before it is built or published.

``cad2d-ir`` (pure Python) and ``cad2d-ir-native`` (the Rust extension under
``native/``) are released together under one version: the Python code of the
native package calls private functions of ``cad2d_ir.importers.jww``, so it pins
the exact ``cad2d-ir`` it was written against.

Without arguments the versions in the repository are compared. ``--tag`` also
checks the git tag of a release (``v<version>``), and ``--artifacts`` the built
files of a release bundle.

    python scripts/check_release.py
    python scripts/check_release.py --tag v0.17.21 --artifacts dist

``--ezjww-tag`` only prints the tag of ``ezjww`` the Rust code is built with (the
workflows check out its sample drawings at that tag).
"""

from __future__ import annotations

import argparse
import re
import sys
import zipfile
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib

ROOT = Path(__file__).resolve().parents[1]

#: The platforms a release has a wheel of ``cad2d-ir-native`` for. A wheel names
#: its platform with one or more tags joined by dots
#: (``manylinux2014_x86_64.manylinux_2_17_x86_64``): every tag has to match the
#: first pattern, and one of them the second.
NATIVE_PLATFORMS = {
    "Linux x86_64": (r"manylinux[0-9_]*_x86_64", r"manylinux[0-9_]*_x86_64"),
    "Linux aarch64": (r"manylinux[0-9_]*_aarch64", r"manylinux[0-9_]*_aarch64"),
    "Windows x86_64": (r"win_amd64", r"win_amd64"),
    "macOS universal2": (
        r"macosx_[0-9]+_[0-9]+_(universal2|x86_64|arm64)",
        r"macosx_[0-9]+_[0-9]+_universal2",
    ),
}


def _toml(path: Path) -> dict:
    return tomllib.loads(path.read_text(encoding="utf-8"))


def repository_problems() -> tuple[str, list[str]]:
    """The release version and what in the repository disagrees with it."""
    problems: list[str] = []
    project = _toml(ROOT / "pyproject.toml")["project"]
    version = str(project["version"])

    init = (ROOT / "src" / "cad2d_ir" / "__init__.py").read_text(encoding="utf-8")
    match = re.search(r'^__version__\s*=\s*"([^"]+)"', init, re.MULTILINE)
    if match is None or match.group(1) != version:
        found = match.group(1) if match else "no __version__"
        problems.append(
            f"src/cad2d_ir/__init__.py has {found}, pyproject.toml has {version}"
        )

    native = _toml(ROOT / "native" / "pyproject.toml")["project"]
    if native["version"] != version:
        problems.append(
            f"native/pyproject.toml has version {native['version']}, pyproject.toml has {version}"
        )
    dependencies = [
        str(item).replace(" ", "") for item in native.get("dependencies", [])
    ]
    pin = f"cad2d-ir[jww]=={version}"
    if pin not in dependencies:
        problems.append(
            f"native/pyproject.toml does not depend on {pin}: {dependencies}"
        )

    cargo = _toml(ROOT / "native" / "Cargo.toml")["workspace"]
    if cargo["package"]["version"] != version:
        problems.append(
            f"native/Cargo.toml has workspace version {cargo['package']['version']}, "
            f"pyproject.toml has {version}"
        )
    lock = _toml(ROOT / "native" / "Cargo.lock")
    for package in lock.get("package", []):
        if package["name"].startswith("cad2d-ir") and package["version"] != version:
            problems.append(
                f"native/Cargo.lock has {package['name']} {package['version']} "
                f"(run `cargo build` in native/ after changing the version)"
            )

    # The Rust code is built with one version of the ezjww reader; the Python
    # package reads the header with ``ezjww`` from PyPI, of that version or later.
    reader = cargo["dependencies"]["ezjww-core"]
    tag = str(reader.get("tag", ""))
    floor = f"ezjww>={tag.removeprefix('v')},"
    if not tag.startswith("v") or not any(
        item.startswith(floor) for item in dependencies
    ):
        problems.append(
            f"native/Cargo.toml builds with ezjww-core {tag or reader}, but "
            f"native/pyproject.toml does not require {floor.rstrip(',')}: {dependencies}"
        )
    return version, problems


def tag_problems(version: str, tag: str) -> list[str]:
    if tag != f"v{version}":
        return [f"the tag is {tag}, the version is {version} (expected v{version})"]
    return []


def _wheel_requirements(path: Path) -> list[str]:
    with zipfile.ZipFile(path) as archive:
        name = next(
            item for item in archive.namelist() if item.endswith(".dist-info/METADATA")
        )
        metadata = archive.read(name).decode("utf-8")
    return [
        line.split(":", 1)[1].strip().replace(" ", "")
        for line in metadata.splitlines()
        if line.startswith("Requires-Dist:")
    ]


def artifact_problems(version: str, directory: Path) -> list[str]:
    """What a release bundle lacks or has too much of."""
    problems: list[str] = []
    files = sorted(path.name for path in directory.iterdir() if path.is_file())
    archives = [name for name in files if name.endswith((".whl", ".tar.gz"))]
    expected = {
        f"cad2d_ir-{version}-py3-none-any.whl",
        f"cad2d_ir-{version}.tar.gz",
        f"cad2d_ir_native-{version}.tar.gz",
    }
    for name in sorted(expected):
        if name not in archives:
            problems.append(f"missing {name}")

    prefix = f"cad2d_ir_native-{version}-cp310-abi3-"
    native_wheels = [
        name for name in archives if name.startswith(f"cad2d_ir_native-{version}-")
    ]
    for label, (every, one) in NATIVE_PLATFORMS.items():
        matching = []
        for name in native_wheels:
            if not (name.startswith(prefix) and name.endswith(".whl")):
                continue
            tags = name[len(prefix) : -len(".whl")].split(".")
            if all(re.fullmatch(every, tag) for tag in tags) and any(
                re.fullmatch(one, tag) for tag in tags
            ):
                matching.append(name)
        if len(matching) != 1:
            problems.append(
                f"expected one cad2d-ir-native wheel for {label}, found {matching}"
            )
        expected.update(matching)
    for name in archives:
        if name not in expected:
            problems.append(f"unexpected file {name}")

    pin = f"cad2d-ir[jww]=={version}"
    for name in native_wheels:
        try:
            requirements = _wheel_requirements(directory / name)
        except (zipfile.BadZipFile, StopIteration):
            problems.append(f"{name} is not a wheel")
            continue
        if pin not in requirements:
            problems.append(f"{name} does not require {pin}: {requirements}")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument(
        "--tag", default="", help="the git tag of the release (v<version>)"
    )
    parser.add_argument(
        "--artifacts", type=Path, help="a directory with the built files"
    )
    parser.add_argument(
        "--ezjww-tag",
        action="store_true",
        help="print the tag of ezjww the Rust code is built with, and nothing else",
    )
    arguments = parser.parse_args(argv)
    if arguments.ezjww_tag:
        workspace = _toml(ROOT / "native" / "Cargo.toml")["workspace"]
        print(workspace["dependencies"]["ezjww-core"]["tag"])
        return 0

    version, problems = repository_problems()
    if arguments.tag:
        problems += tag_problems(version, arguments.tag)
    if arguments.artifacts is not None:
        problems += artifact_problems(version, arguments.artifacts)
    for problem in problems:
        print(f"error: {problem}", file=sys.stderr)
    if problems:
        return 1
    print(f"cad2d-ir and cad2d-ir-native {version}: consistent")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
