# Contributing

Thank you for contributing to `cad2d-ir`.

## Development setup

```bash
uv sync
```

## Local checks

```bash
uv run pytest -q
uv run cad2d-ir --help
uv run python scripts/sync_packaged_schema.py
```

## Branch and PR guidelines

- Keep each PR focused on one logical change.
- Include tests for behavior changes.
- For schema updates, include compatibility notes in the PR description.
- For DXF mapping changes, include at least one round-trip test case.

## Commit style

Use short imperative commit messages. Examples:
- `schema: add spline weight validation`
- `dxf: map mtext attachment code`
- `docs: expand api examples`

## Project structure notes

- `ir_schema.json` is the canonical schema source in the repository root.
- `src/cad2d_ir/data/ir_schema.json` is the packaged copy used at runtime after installation.
- Converter logic lives in `src/cad2d_ir/codecs/dxf.py`.
