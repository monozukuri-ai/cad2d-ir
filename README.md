# cad2d-ir

`cad2d-ir` is a typed intermediate representation and conversion toolkit
for 2D CAD data.

It provides:

- a canonical JSON Schema for 2D CAD geometry and tables;
- native DXF, DWG, DGN, DWF/DWFx, JWW, and SXF import paths;
- DXF R12 (`AC1009`) and R2010 (`AC1024`) export;
- structured import/export diagnostics;
- deterministic IR-entity to DXF-handle correspondence.

## Installation

```bash
pip install cad2d-ir
pip install "cad2d-ir[dwg]"
pip install "cad2d-ir[dgn]"
pip install "cad2d-ir[dwf]"
pip install "cad2d-ir[jww]"
pip install "cad2d-ir[sxf]"
pip install "cad2d-ir[all]"
```

Python 3.10 or newer is required. The core wheel is pure Python; native format
adapters are optional extras.

For development:

```bash
uv sync
uv sync --all-extras
uv run python -m pytest
```

## Supported data

IR entities include `LINE`, `CIRCLE`, `ARC`, `POINT`,
`ELLIPSE`, `LWPOLYLINE`, `SPLINE`, `TEXT`,
`MTEXT`, `INSERT`, `HATCH`, and `DIMENSION`.
Layer, linetype, text-style, dimension-style, and block tables are represented
by the schema.

Import adapters:

| Format | Path | Dependency |
|---|---|---|
| DXF | built-in parser | core |
| DWG | native `ezdwg` model | `cad2d-ir[dwg]` |
| DGN V7 2D / V8 | native `ezdgn` models | `cad2d-ir[dgn]` |
| DWF/DWFx 2D | normalized `ezdwf` sheet model | `cad2d-ir[dwf]` |
| JWW | native `ezjww` model | `cad2d-ir[jww]` |
| SXF SFC/P21 | backend-neutral `ezsxf` drawing | `cad2d-ir[sxf]` |

Native adapters preserve source semantics and provenance rather than flattening
through an intermediate DXF.

## CLI

```bash
# Validate IR
cad2d-ir validate examples/ir/minimal.json

# DXF -> IR with BOM/codepage/UTF-8/CP932 detection
cad2d-ir dxf2ir drawing.dxf -o drawing.json --pretty
cad2d-ir dxf2ir drawing.dxf --encoding cp932 -o drawing.json

# Auto-detected DXF/DWG/DGN/DWF/DWFx/JWW/SFC/P21 -> IR
cad2d-ir import drawing.JWW -o drawing.json --pretty
cad2d-ir import drawing.dwg -o drawing.json --pretty
cad2d-ir import drawing.dgn -o drawing.json --pretty
cad2d-ir import drawing.dwfx -o drawing.json --pretty
cad2d-ir import drawing.p21 -o drawing.json --pretty

# IR -> R2010 or R12 DXF
cad2d-ir ir2dxf drawing.json --target-version AC1024 -o drawing-r2010.dxf
cad2d-ir ir2dxf drawing.json --target-version AC1009 \
  --encoding cp932 --curve-segments 128 -o drawing-r12.dxf
```

## Python API

```python
from cad2d_ir import convert_file_to_ir, convert_ir_to_dxf_text

imported = convert_file_to_ir("drawing.dxf", strict=False)
print(imported.statistics["encoding"])
for diagnostic in imported.diagnostics:
    print(diagnostic.code, diagnostic.as_dict())

exported = convert_ir_to_dxf_text(
    imported.document,
    target_version="AC1024",
    generic_dimensions="explode",
)
for entry in exported.entity_map:
    print(entry["ir_id"], entry["handle"], entry["dxf_type"])
```

R2010 output assigns deterministic handles and emits `$HANDSEED`.
Re-importing the DXF records each handle in `entity.source.id`. R12 omits
handles and retains deterministic `index` values in the entity map.

## Export behavior

- `TABLES` contains LAYER, LTYPE, and STYLE records from IR tables.
- GENERIC dimensions expand to their preserved visual primitives by default.
- R12 declares `$DWGCODEPAGE=ANSI_932` and file output uses CP932 by
  default; LWPOLYLINE becomes POLYLINE/VERTEX, MTEXT becomes TEXT,
  ELLIPSE/SPLINE become sampled polylines, and HATCH becomes boundary polylines.
- Every skip, approximation, explosion, and normalization is represented by an
  `ExportDiagnostic`.
- The same document and options produce byte-identical DXF and entity-map output
  within a package minor version.

## Compatibility boundaries

- `constraints` are IR-only and are diagnosed when omitted from DXF.
- A complete affine `INSERT.transform` cannot be represented by plain DXF
  INSERT and is diagnosed when omitted.
- R12 has no true-color, lineweight, or `$INSUNITS` equivalent with the
  same semantics; downgrade diagnostics disclose those losses.
- HATCH support focuses on polyline-style loops.
- Ellipse start/end parameters are radians independently of
  `header.angle_unit`.
- DGN import covers V7 2D and native V8 documents. V7 text encoding
  auto-detection probes ASCII, CP932, then Latin-1 at file scope; V8 text
  arrives already decoded per element. `ezdgn` rejects V7 3D; V8 3D models
  and multi-model files are reduced to one projected model with explicit
  loss diagnostics, and V8 shared-cell definitions, fills, color tables,
  and spline knots are not decoded yet.
- DWF/DWFx import preserves sheet identity in metadata while placing supported
  2D entities and markups in one IR modelspace. Raster payloads and complex
  brush semantics remain source metadata/diagnostics rather than invented IR
  geometry.

See:

- [API guide](docs/API.md)
- [Importer behavior](docs/IMPORTERS.md)
- [Schema notes](docs/SCHEMA_NOTES.md)
- [Diagnostic code catalog](docs/DIAGNOSTICS.md)
- [Contributing](CONTRIBUTING.md)

## Repository layout

```text
src/cad2d_ir/
  api.py
  cli.py
  diagnostics.py
  schema.py
  codecs/dxf.py
  importers/
  data/ir_schema.json
ir_schema.json
tests/
examples/
docs/
```

## License

MIT ([LICENSE](LICENSE)).
