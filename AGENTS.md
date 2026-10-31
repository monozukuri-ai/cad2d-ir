# Repository Guidelines

## Project Structure & Module Organization
This repository is a small Python package using a `src/` layout.

- `src/cad2d_ir/`: package source (`__init__.py`) and typing marker (`py.typed`).
- `ir_schema.json`: canonical JSON Schema for the CAD 2D intermediate representation.
- `pyproject.toml`: project metadata and build configuration (`uv_build` backend).
- `README.md`: top-level project documentation (currently minimal; expand as features grow).

Place new tests in `tests/`, mirroring package/module names where possible (for example, `tests/test_schema.py`).

## Build, Test, and Development Commands
Use `uv` for local workflows:

- `uv sync`: create/update the virtual environment from project metadata.
- `uv build`: build source and wheel distributions from `pyproject.toml`.
- `uv run python -m pytest`: run the test suite.
- `uv run python -c "from cad2d_ir import hello; print(hello())"`: quick import smoke test.

If `pytest` is not installed yet, add it with `uv add --dev pytest`.

## Coding Style & Naming Conventions
- Target Python `>=3.13` and keep full type hints on public functions.
- Follow PEP 8: 4-space indentation, `snake_case` for functions/modules, `PascalCase` for classes, `UPPER_SNAKE_CASE` for constants.
- Keep JSON Schema keys consistent and descriptive; prefer existing naming patterns in `ir_schema.json`.
- Keep public API exports intentional in `src/cad2d_ir/__init__.py`.

## Testing Guidelines
- Framework: `pytest`.
- Naming: files `test_*.py`, test functions `test_*`.
- Add tests for every behavior change, especially schema constraints (required fields, enums, version handling).
- Include regression tests when modifying `ir_schema.json` to prevent silent schema breaks.

## Commit & Pull Request Guidelines
Git history is currently minimal (`first commit`), so establish clear conventions now:

- Write concise, imperative commit subjects (example: `schema: add arc entity constraints`).
- Keep commits focused on a single logical change.
- PRs should include: purpose, key changes, test evidence (`uv run python -m pytest`), and linked issue(s).
- For schema changes, describe compatibility impact and whether consumers need migration updates.
