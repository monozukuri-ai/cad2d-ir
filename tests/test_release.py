from __future__ import annotations

import importlib.util
import shutil
import subprocess
from pathlib import Path
from types import ModuleType

import pytest

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib

ROOT = Path(__file__).resolve().parents[1]
RELEASE_FILES = (
    "pyproject.toml",
    "uv.lock",
    "native/pyproject.toml",
    "native/Cargo.toml",
    "native/Cargo.lock",
)


def _script(name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, ROOT / "scripts" / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def repository(tmp_path: Path) -> Path:
    for relative in RELEASE_FILES:
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / relative, target)
    return tmp_path


def _contents(root: Path) -> dict[str, bytes]:
    return {relative: (root / relative).read_bytes() for relative in RELEASE_FILES}


def test_version_update_keeps_python_rust_and_dependency_in_step(
    repository: Path,
) -> None:
    before = _contents(repository)
    updates = _script("set_version").planned_updates(repository, "2.3.4")
    for path, text in updates.items():
        path.write_text(text, encoding="utf-8")
    project = tomllib.loads((repository / "pyproject.toml").read_text())["project"]
    cargo = tomllib.loads((repository / "native/Cargo.toml").read_text())
    assert project["version"] == cargo["workspace"]["package"]["version"] == "2.3.4"
    assert "cad2d-ir-native==2.3.4" in project["dependencies"]
    original = tomllib.loads(before["pyproject.toml"].decode())["project"]
    assert project["optional-dependencies"] == original["optional-dependencies"]
    assert (repository / "native/pyproject.toml").read_bytes() == before[
        "native/pyproject.toml"
    ]
    assert (
        "# Generated from ../pyproject.toml"
        in updates[repository / "native/Cargo.toml"]
    )


def test_sync_uses_the_root_project_version(repository: Path) -> None:
    path = repository / "pyproject.toml"
    text = path.read_text()
    old = tomllib.loads(text)["project"]["version"]
    path.write_text(text.replace(f'version = "{old}"', 'version = "2.3.4"', 1))
    updates = _script("set_version").planned_updates(repository, None)
    assert "cad2d-ir-native==2.3.4" in updates[path]
    cargo = tomllib.loads(updates[repository / "native/Cargo.toml"])
    assert cargo["workspace"]["package"]["version"] == "2.3.4"


def test_sync_is_idempotent(repository: Path) -> None:
    for path, text in _script("set_version").planned_updates(repository, None).items():
        assert path.read_text(encoding="utf-8") == text


@pytest.mark.parametrize("version", ["v1.2.3", "1.2", "01.2.3", "1.2.3rc1"])
def test_invalid_version_does_not_write_files(repository: Path, version: str) -> None:
    before = _contents(repository)
    with pytest.raises(ValueError, match="X.Y.Z"):
        _script("set_version").planned_updates(repository, version)
    assert _contents(repository) == before


@pytest.mark.parametrize("failed_tool", ["cargo", "uv"])
def test_tool_failure_restores_preexisting_edits(
    repository: Path, monkeypatch: pytest.MonkeyPatch, failed_tool: str
) -> None:
    lock = repository / "uv.lock"
    lock.write_text(lock.read_text() + "\n# A pre-existing local edit.\n")
    before = _contents(repository)
    script = _script("set_version")
    monkeypatch.setattr(script, "ROOT", repository)

    def run(command: list[str], **kwargs: object) -> None:
        (repository / "native/Cargo.lock").write_text("partially refreshed\n")
        if command[0] == failed_tool:
            lock.unlink()
            raise subprocess.CalledProcessError(1, command)

    monkeypatch.setattr(script.subprocess, "run", run)
    assert script.main(["2.3.4"]) == 1
    assert _contents(repository) == before


def test_missing_pin_fails_before_writing(repository: Path) -> None:
    path = repository / "pyproject.toml"
    path.write_text(path.read_text().replace("cad2d-ir-native==", "cad2d-ir-native>="))
    before = _contents(repository)
    with pytest.raises(ValueError, match="exact cad2d-ir-native pin"):
        _script("set_version").planned_updates(repository, "2.3.4")
    assert _contents(repository) == before


@pytest.mark.parametrize("package", ["cad2d-ir", "cad2d-ir-native"])
def test_release_check_rejects_stale_uv_lock(
    repository: Path, monkeypatch: pytest.MonkeyPatch, package: str
) -> None:
    path = repository / "uv.lock"
    text = path.read_text()
    current = tomllib.loads((repository / "pyproject.toml").read_text())["project"][
        "version"
    ]
    if package == "cad2d-ir-native":
        text = text.replace(
            f'name = "{package}"\n', f'name = "{package}"\nversion = "0.0.1"\n'
        )
    else:
        text = text.replace(
            f'name = "{package}"\nversion = "{current}"',
            f'name = "{package}"\nversion = "0.0.1"',
        )
    path.write_text(text)
    script = _script("check_release")
    monkeypatch.setattr(script, "ROOT", repository)
    _, problems = script.repository_problems()
    assert any(
        f"uv.lock has an inconsistent {package} entry" in problem
        for problem in problems
    )


def test_native_version_is_derived_from_cargo() -> None:
    project = tomllib.loads((ROOT / "native/pyproject.toml").read_text())["project"]
    assert "version" not in project
    assert "version" in project["dynamic"]
