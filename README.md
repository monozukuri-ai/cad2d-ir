# cad2d-ir

`cad2d-ir` is an intermediate representation (IR) for 2D CAD data, plus converters between DXF and that IR.

The goal is to provide a stable, machine-friendly format for:
- CAD ingestion pipelines
- geometric post-processing
- round-trip conversion workflows

## Status

The project currently supports:
- `LINE`, `CIRCLE`, `ARC`, `LWPOLYLINE`, `TEXT`
- `MTEXT`, `INSERT`, `HATCH`
- staged support for `SPLINE`, `DIMENSION`, and `constraints`

See `ir_schema.json` for the canonical schema.

## Installation

### With pip

```bash
pip install cad2d-ir
```

### For development (uv)

```bash
uv sync
uv run pytest
```

## CLI

```bash
# Validate IR JSON
cad2d-ir validate examples/ir/minimal.json

# DXF -> IR
cad2d-ir dxf2ir examples/dxf/simple_line.dxf -o /tmp/out.json --pretty

# IR -> DXF
cad2d-ir ir2dxf examples/ir/minimal.json -o /tmp/out.dxf
```

Warnings (for example when `constraints` cannot be represented in plain DXF) are printed to stderr.

## Python API

```python
from cad2d_ir import convert_dxf_text_to_ir, convert_ir_to_dxf_text

dxf_text = "0\nSECTION\n2\nENTITIES\n0\nLINE\n10\n0\n20\n0\n11\n1\n21\n1\n0\nENDSEC\n0\nEOF\n"
to_ir = convert_dxf_text_to_ir(dxf_text)
print(to_ir.document["entities"][0]["kind"])  # LINE

to_dxf = convert_ir_to_dxf_text(to_ir.document)
print(len(to_dxf.warnings))
```

Lower-level conversion functions (`dxf_to_ir`, `ir_to_dxf`) are also available.

## Repository layout

```text
src/cad2d_ir/
  api.py             # public high-level API
  cli.py             # CLI entrypoint
  schema.py          # schema loading + validation
  codecs/dxf.py      # DXF <-> IR conversion
  data/ir_schema.json
ir_schema.json       # canonical schema source
tests/
examples/
docs/
```

## Compatibility and limitations

- `constraints` are preserved in IR but omitted when exporting to DXF.
- `DIMENSION` support is semantic/staged and may not preserve every CAD-system-specific detail.
- `HATCH` currently focuses on polyline-style loops.

## Development

```bash
uv run pytest -q
uv run cad2d-ir --help
```

See:
- `CONTRIBUTING.md`
- `docs/API.md`
- `docs/SCHEMA_NOTES.md`

## License

MIT (`LICENSE`)
