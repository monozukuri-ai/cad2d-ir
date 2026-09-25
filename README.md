# cad2d-ir

`cad2d-ir` is a typed intermediate representation and conversion toolkit
for 2D CAD data.

It provides:

- a canonical JSON Schema for 2D CAD geometry and tables;
- native DXF, DWG, DGN, DWF/DWFx, IDW, JWW, MI/BI, and SXF import paths;
- DXF R12 (`AC1009`) and R2010 (`AC1024`) export;
- structured import/export diagnostics;
- deterministic IR-entity to DXF-handle correspondence.

## Installation

```bash
pip install cad2d-ir
pip install "cad2d-ir[dwg]"
pip install "cad2d-ir[dgn]"
pip install "cad2d-ir[dwf]"
pip install "cad2d-ir[idw]"   # Python 3.11+; see the IDW license notice below
pip install "cad2d-ir[jww]"
pip install "cad2d-ir[mi]"
pip install "cad2d-ir[sxf]"
pip install "cad2d-ir[all]"
```

Python 3.10 or newer is required. The core wheel is pure Python; native format
adapters are optional extras.

> **IDW license notice:** IDW support uses `inventor-kit`, which is offered under
> PolyForm Noncommercial 1.0.0 with separate commercial licenses. Before using
> IDW in commercial workflows, including internal business use, review the
> [IDW licensing terms](#idw-licensing-inventor-kit).

`pip install cad2d-ir` and `pip install "cad2d-ir[all]"` do not install
`inventor-kit`; install `[idw]` explicitly when needed. The development command
`uv sync --all-extras` below includes `[idw]` and installs `inventor-kit`.

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
| IDW (Autodesk Inventor drawing) | saved-display model from `inventor-kit` | `cad2d-ir[idw]` |
| JWW | native `ezjww` model | `cad2d-ir[jww]` |
| MI / gzip-wrapped BI | native `ezmi2d` document model | `cad2d-ir[mi]` |
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

# Auto-detected DXF/DWG/DGN/DWF/DWFx/IDW/JWW/MI/BI/SFC/P21 -> IR
cad2d-ir import drawing.JWW -o drawing.json --pretty
cad2d-ir import drawing.dwg -o drawing.json --pretty
cad2d-ir import drawing.dgn -o drawing.json --pretty
cad2d-ir import drawing.dwfx -o drawing.json --pretty
cad2d-ir import drawing.idw -o drawing.json --pretty
cad2d-ir import drawing.mi -o drawing.json --pretty
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
- IDW import consumes the saved sheet display returned by
  `inventor_kit.read_drawing_file` (lines, curves, filled triangles, text and
  image placements as Inventor stored them). It does not open referenced
  IPT/IAM files or regenerate views, so drawing views that Inventor saved only
  as raster caches (for example every view in 2027-era files) have no vector
  geometry in the IR; their placements are kept in `source.metadata.idw` and
  disclosed by `IDW_VIEW_RASTER_ONLY` / `IDW_IMAGE_NOT_IN_IR`. Coordinates are
  sheet paper space scaled from Inventor's internal centimetres to millimetres
  (`IDW_UNITS_ASSUMED_CM`); multiple sheets are tiled along +X. Supported
  segment majors follow `inventor-kit` (23, 24, 26, 28, 29, 31 in 0.6.0);
  other profiles raise `ImporterError("unsupported IDW profile ...")`.
  `inventor-kit` needs Python 3.11+; see the
  [IDW licensing terms](#idw-licensing-inventor-kit). It also declares
  `cq-acis` (CadQuery/OCCT, about 1 GB) for IPT/IAM geometry, which
  the IDW path never imports. Consumers that only need IDW can exclude it, for
  example with uv's `override-dependencies = ["cq-acis; python_full_version < '0'"]`
  or `pip install --no-deps inventor-kit`.
- MI import consumes `ezmi2d>=0.2,<0.3` directly. Part definitions and shared
  occurrences become IR blocks and INSERTs; source radians are normalized to
  IR degrees. `.bi` dispatch covers the gzip-wrapped MI container verified by
  `ezmi2d`, not every historical Drafting/ME10 BI compression variant.

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

`cad2d-ir` itself is licensed under MIT ([LICENSE](LICENSE)), including its IDW
adapter code. Optional parser dependencies retain their own licenses.

### IDW licensing (`inventor-kit`)

IDW support depends on `inventor-kit`, which is offered under
[PolyForm Noncommercial 1.0.0](https://polyformproject.org/licenses/noncommercial/1.0.0)
with separate commercial licenses from UnRobotics Inc. The MIT license for
`cad2d-ir` does not grant additional rights to use or redistribute `inventor-kit`.

- Use permitted by the PolyForm terms does not require a commercial agreement.
- Use outside those permissions, including commercial internal business use
  and commercial product integration, requires a separate written commercial
  agreement with UnRobotics Inc. See the upstream
  [commercial licensing guide](https://github.com/monozukuri-ai/inventor-kit/blob/main/COMMERCIAL-LICENSE.md)
  and [contact form](https://www.un-robotics.com/#contact).
- If you redistribute `inventor-kit` with your application, comply with its
  distribution terms and preserve the applicable license texts and notices.

These additional conditions concern the `inventor-kit` dependency used for IDW
support. Workflows using only other formats do not require `inventor-kit`;
their respective dependency licenses still apply. Refer to the upstream
[license notice](https://github.com/monozukuri-ai/inventor-kit/blob/main/LICENSE)
for the full scope, including earlier MIT material and third-party components.
