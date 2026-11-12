"""Set the release version and refresh its derived metadata and lockfiles.

    uv run --no-sync python scripts/set_version.py 0.18.2

Without an argument, synchronize from the version in the root pyproject.toml.
Requires uv and Cargo. Does not install packages, commit, tag, or publish.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.10
    import tomli as tomllib

ROOT = Path(__file__).resolve().parents[1]


def _table_version(text: str, table: str, version: str) -> str:
    """Replace a table's version without rewriting comments or other TOML."""
    section = re.search(
        rf"(?ms)^\[{re.escape(table)}\][ \t]*\n(?P<body>.*?)(?=^\[|\Z)", text
    )
    if section is None:
        raise ValueError(f"missing [{table}] table")
    body, count = re.subn(
        r"""(?m)^(version\s*=\s*)(["'])[^"']*\2""",
        lambda match: f"{match[1]}{match[2]}{version}{match[2]}",
        section["body"],
    )
    if count != 1:
        raise ValueError(f"expected one version in [{table}], found {count}")
    return text[: section.start("body")] + body + text[section.end("body") :]


def planned_updates(root: Path, version: str | None) -> dict[Path, str]:
    """Validate all editable fields before returning any changes."""
    project_path = root / "pyproject.toml"
    project_text = project_path.read_text(encoding="utf-8")
    if version is None:
        version = str(tomllib.loads(project_text)["project"]["version"])
    # One spelling must work as both a Python version and a Rust SemVer.
    if (
        re.fullmatch(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)", version)
        is None
    ):
        raise ValueError("use a stable release version X.Y.Z, without a v prefix")
    project_text = _table_version(project_text, "project", version)
    project_text, count = re.subn(
        r"""(?m)^(\s*["']cad2d-ir-native==)[^"']+(["'][, \t]*(?:\#.*)?)$""",
        lambda match: f"{match[1]}{version}{match[2]}",
        project_text,
    )
    if count != 1:
        raise ValueError(f"expected one exact cad2d-ir-native pin, found {count}")
    cargo_path = root / "native" / "Cargo.toml"
    return {
        project_path: project_text,
        cargo_path: _table_version(
            cargo_path.read_text(encoding="utf-8"), "workspace.package", version
        ),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n", 1)[0])
    parser.add_argument("version", nargs="?", help="stable release version X.Y.Z")
    arguments = parser.parse_args(argv)
    try:
        updates = planned_updates(ROOT, arguments.version)
    except ValueError as error:
        parser.error(str(error))

    # Restore exactly the previous contents if a tool fails, including any
    # pre-existing edits. Neither tool is asked to upgrade other dependencies.
    paths = [*updates, ROOT / "uv.lock", ROOT / "native" / "Cargo.lock"]
    originals = {path: path.read_bytes() for path in paths}
    try:
        for path, content in updates.items():
            if content != path.read_text(encoding="utf-8"):
                path.write_text(content, encoding="utf-8")
        subprocess.run(
            ["cargo", "update", "--workspace"], cwd=ROOT / "native", check=True
        )
        subprocess.run(["uv", "lock"], cwd=ROOT, check=True)
        subprocess.run(
            [sys.executable, str(ROOT / "scripts" / "check_release.py")],
            cwd=ROOT,
            check=True,
        )
    except (OSError, subprocess.CalledProcessError) as error:
        for path, content in originals.items():
            if not path.exists() or path.read_bytes() != content:
                path.write_bytes(content)
        print(
            f"error: {error}; restored release metadata and lockfiles", file=sys.stderr
        )
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
