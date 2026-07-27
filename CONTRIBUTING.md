# Contributing

Thank you for contributing to `cad2d-ir`.

## Development setup

```bash
uv sync
```

## Local checks

```bash
uv run pytest -q
uv run ruff check src tests
uv run cad2d-ir --help
uv run --extra dwg cad2d-ir import path/to/sample.dwg -o /tmp/sample.json
uv run --extra dgn cad2d-ir import path/to/sample.dgn -o /tmp/sample.json
uv run --extra dwf cad2d-ir import path/to/sample.dwfx -o /tmp/sample.json
uv run --extra jww cad2d-ir import path/to/sample.jww -o /tmp/sample.json
uv run --extra sxf cad2d-ir import path/to/sample.sfc -o /tmp/sample.json
uv run python scripts/sync_packaged_schema.py
```

## Branch and PR guidelines

- Keep each PR focused on one logical change.
- Include tests for behavior changes.
- For schema updates, include compatibility notes in the PR description.
- For DXF mapping changes, include at least one round-trip test case.
- For importer changes, include synthetic mapping tests and record the real-file corpus used for validation.

## Commit style

Use short imperative commit messages. Examples:
- `schema: add spline weight validation`
- `dxf: map mtext attachment code`
- `docs: expand api examples`

## Project structure notes

- `ir_schema.json` is the canonical schema source in the repository root.
- `src/cad2d_ir/data/ir_schema.json` is the packaged copy used at runtime after installation.
- Converter logic lives in `src/cad2d_ir/codecs/dxf.py`.
- Importer contracts and adapters live in `src/cad2d_ir/importers/`.
