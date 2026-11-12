"""Check that a release is consistent before it is built or published.

``cad2d-ir`` and ``cad2d-ir-native`` (the Rust extension under ``native/``) are
released together under one version. The DXF writer and the entity conversion
of the JWW importer of ``cad2d-ir`` are that native code, and the native code
calls back into private functions of ``cad2d_ir``, so ``cad2d-ir`` pins the
exact ``cad2d-ir-native`` it was written with.

Without arguments the versions in the repository are compared. ``--tag`` also
checks the git tag of a release (``v<version>``), and ``--artifacts`` the built
files of a release bundle.

    python scripts/check_release.py
    python scripts/check_release.py --tag v0.18.0 --artifacts dist

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


def _requirement(value: str) -> str:
    """A requirement as it is compared: no spaces, the name in its usual form."""
    text = str(value).replace(" ", "")
    match = re.match(r"[A-Za-z0-9._-]+", text)
    if match is None:
        return text
    name = re.sub(r"[-_.]+", "-", match.group(0)).lower()
    return name + text[match.end() :]


def _names_cad2d_ir(requirement: str) -> bool:
    """Whether a requirement is one on ``cad2d-ir`` itself."""
    return re.match(r"cad2d-ir(?![A-Za-z0-9._-])", requirement) is not None


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

    # cad2d-ir installs the native code of its own version ...
    dependencies = [_requirement(item) for item in project.get("dependencies", [])]
    pin = f"cad2d-ir-native=={version}"
    if pin not in dependencies:
        problems.append(f"pyproject.toml does not depend on {pin}: {dependencies}")

    # ... and the native package installs nothing: a requirement on cad2d-ir
    # there would be a circle.
    native = _toml(ROOT / "native" / "pyproject.toml")["project"]
    if native["version"] != version:
        problems.append(
            f"native/pyproject.toml has version {native['version']}, pyproject.toml has {version}"
        )
    native_dependencies = [
        _requirement(item) for item in native.get("dependencies", [])
    ]
    if any(_names_cad2d_ir(item) for item in native_dependencies):
        problems.append(
            f"native/pyproject.toml depends on cad2d-ir, which depends on it: {native_dependencies}"
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

    # The Rust code is built with one version of the ezjww reader; the header
    # of a file is read with ``ezjww`` from PyPI (the ``jww`` extra), of that
    # version or later.
    reader = cargo["dependencies"]["ezjww-core"]
    tag = str(reader.get("tag", ""))
    floor = f"ezjww>={tag.removeprefix('v')},"
    extras = project.get("optional-dependencies", {})
    for extra in ("jww", "all"):
        requirements = [_requirement(item) for item in extras.get(extra, [])]
        if not tag.startswith("v") or not any(
            item.startswith(floor) for item in requirements
        ):
            problems.append(
                f"native/Cargo.toml builds with ezjww-core {tag or reader}, but the "
                f"{extra} extra of pyproject.toml does not require "
                f"{floor.rstrip(',')}: {requirements}"
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
        _requirement(line.split(":", 1)[1].strip())
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

    # The wheel of cad2d-ir installs the native code of this release, and no
    # native wheel asks for cad2d-ir in turn.
    pin = f"cad2d-ir-native=={version}"
    wheel = f"cad2d_ir-{version}-py3-none-any.whl"
    if wheel in archives:
        try:
            requirements = _wheel_requirements(directory / wheel)
        except (zipfile.BadZipFile, StopIteration):
            problems.append(f"{wheel} is not a wheel")
        else:
            if pin not in requirements:
                problems.append(f"{wheel} does not require {pin}: {requirements}")
    for name in native_wheels:
        if not name.endswith(".whl"):
            continue
        try:
            requirements = _wheel_requirements(directory / name)
        except (zipfile.BadZipFile, StopIteration):
            problems.append(f"{name} is not a wheel")
            continue
        if any(_names_cad2d_ir(item) for item in requirements):
            problems.append(
                f"{name} requires cad2d-ir, which requires it: {requirements}"
            )
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
