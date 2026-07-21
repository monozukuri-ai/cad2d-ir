# cad2d-ir

`cad2d-ir` is an intermediate representation (IR) for 2D CAD data, with a common importer API and DXF conversion support.

The goal is to provide a stable, machine-friendly format for:

- CAD ingestion pipelines
- geometric post-processing
- round-trip conversion workflows

## Status

The project currently supports:

- IR entities: `LINE`, `CIRCLE`, `ARC`, `POINT`, `ELLIPSE`, `LWPOLYLINE`, `SPLINE`, `TEXT`, `MTEXT`, `INSERT`, `HATCH`, and `DIMENSION`
- DXF import and export
- Native DWG import through the optional `ezdwg` adapter
- Native JWW import through the optional `ezjww` adapter
- Native SXF SFC/P21 import through the optional `ezsxf` adapter
- Structured importer diagnostics, statistics, and source provenance

DWG and JWW are converted from their native parser models rather than through an intermediate DXF. SFC dimensions are regrouped from the `ezsxf` drawing model into semantic IR dimensions with their rendered world-space geometry attached. P21 uses the same backend-neutral drawing model; its current semantic-flattening boundary is explicitly diagnosed.

See `ir_schema.json` for the canonical schema.

## Installation

### With pip

```bash
pip install cad2d-ir
```

Install one adapter or all adapters with:

```bash
pip install "cad2d-ir[dwg]"
pip install "cad2d-ir[jww]"
pip install "cad2d-ir[sxf]"
pip install "cad2d-ir[all]"
```

### For development (uv)

```bash
uv sync
uv sync --all-extras  # when working with all native CAD adapters
uv run pytest
```

## CLI

```bash
# Validate IR JSON
cad2d-ir validate examples/ir/minimal.json

# DXF -> IR
cad2d-ir dxf2ir examples/dxf/simple_line.dxf -o /tmp/out.json --pretty

# Auto-detected DXF/DWG/JWW/SFC/P21 -> IR
cad2d-ir import drawing.JWW -o /tmp/drawing.json --pretty
cad2d-ir import drawing.dwg -o /tmp/drawing.json --pretty
cad2d-ir import drawing.p21 -o /tmp/drawing.json --pretty

# Explicit adapter and approximation resolution
cad2d-ir import drawing.jww --format jww --curve-segments 128 -o /tmp/drawing.json

# IR -> DXF
cad2d-ir ir2dxf examples/ir/minimal.json -o /tmp/out.dxf
```

Structured importer diagnostics and export warnings are printed to stderr. `--lenient` skips malformed source entities and reports them instead of failing the whole import.

## Python API

```python
from cad2d_ir import convert_file_to_ir, convert_ir_to_dxf_text

to_ir = convert_file_to_ir("drawing.dwg")
print(to_ir.document["source"]["format"])  # dwg
print(to_ir.statistics["converted_entity_counts"])
for diagnostic in to_ir.diagnostics:
    print(diagnostic.code, diagnostic.message)

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
  importers/         # common importer contract and format adapters
  data/ir_schema.json
ir_schema.json       # canonical schema source
tests/
examples/
docs/
```

## Compatibility and limitations

- `constraints` are preserved in IR but omitted when exporting to DXF.
- JWW `DIMENSION` records are preserved as `GENERIC` dimensions; generic dimensions are omitted with a warning on DXF export rather than being mislabeled as linear dimensions.
- JWW `CIRCLE_SOLID` boundaries are approximated as polyline `HATCH` loops. The chosen segment count is recorded in `approximation` and reported as a diagnostic.
- DWG drawing units are currently reported as `unknown` because `ezdwg` does not expose the DWG unit header. Non-zero Z geometry is projected to XY with a diagnostic.
- DWG block bodies are reconstructed when `ezdwg` exposes an owner handle. Block base points are not exposed and are recorded as unresolved metadata with `[0, 0]` as the IR table value.
- SFC dimensions remain semantic `DIMENSION` entities. Other SFC curves are emitted as polylines with explicit approximation records.
- P21 currently exposes generic STEP entities plus flattened drawing primitives in `ezsxf`; the adapter preserves the primitives and emits `SXF_P21_SEMANTICS_FLATTENED` rather than claiming semantic dimensions.
- `HATCH` currently focuses on polyline-style loops.

## Development

```bash
uv run pytest -q
uv run cad2d-ir --help
```

See:

- `CONTRIBUTING.md`
- `docs/API.md`
- `docs/IMPORTERS.md`
- `docs/SCHEMA_NOTES.md`

## License

MIT (`LICENSE`)
