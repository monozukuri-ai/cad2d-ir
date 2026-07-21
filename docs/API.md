# API Guide

## High-level API

Use `cad2d_ir.api` for diagnostic-aware file imports and warning-aware DXF export.

```python
from cad2d_ir import convert_file_to_ir, convert_ir_to_dxf_text
```

### `convert_file_to_ir(path, *, source_format="auto", ir_version="0.2.0", validate=True, strict=True, curve_segments=96)`

Dispatches to the registered DXF, DWG, JWW, or SXF adapter and returns `ImportResult`:

- `document`: imported IR document (`dict`)
- `diagnostics`: structured `ImportDiagnostic` records
- `statistics`: source, converted, skipped, and approximated entity counts
- `warnings`: compatibility property containing warning/error messages

`source_format` accepts `auto`, `dxf`, `dwg`, `jww`, or `sxf`. Auto detection recognizes `.dxf`, `.dwg`, `.jww`, `.sfc`, `.sxf`, and `.p21`; `.sxf` content is sniffed to distinguish SFC from AP202/P21.

`strict=False` skips malformed source records with an error diagnostic. Unsupported source entity types are skipped with an explicit warning in either mode. `curve_segments` controls the full-curve resolution for source geometry that must be approximated.

### `convert_jww_file_to_ir(path, ...)`

Runs the native JWW adapter directly. It requires `cad2d-ir[jww]` and raises `MissingOptionalDependencyError` when `ezjww` is unavailable.

### `convert_dwg_file_to_ir(path, ...)`

Runs the native DWG adapter directly. It requires `cad2d-ir[dwg]` and consumes `ezdwg.Document` entities without a DXF serialization step.

### `convert_sxf_file_to_ir(path, ...)`

Runs the native SXF adapter for SFC, P21, or sniffed `.sxf` input. It requires `cad2d-ir[sxf]` and consumes the backend-neutral `ezsxf` drawing model without creating an intermediate DXF file.

### `convert_dxf_text_to_ir(dxf_text, *, ir_version="0.2.0", validate=True)`

Returns `DxfToIrResult`:

- `document`: parsed IR document (`dict`)
- `warnings`: conversion warnings (`list[str]`)

### `convert_ir_to_dxf_text(document, *, validate=True)`

Returns `IrToDxfResult`:

- `dxf_text`: output DXF text
- `warnings`: conversion warnings (`list[str]`)

### File-based helpers

- `convert_dxf_file_to_ir(path, ...)`
- `convert_dwg_file_to_ir(path, ...)`
- `convert_file_to_ir(path, ...)`
- `convert_jww_file_to_ir(path, ...)`
- `convert_sxf_file_to_ir(path, ...)`
- `convert_ir_file_to_dxf(path, ...)`
- `load_ir_json(path, validate=True)`
- `dump_ir_json(document, path, pretty=True, validate=True)`

## Low-level API

`cad2d_ir.codecs.dxf` exposes lower-level conversion functions:

- `dxf_to_ir(...)`
- `ir_to_dxf(...)`

Use these when you need direct control over conversion options.

## Import diagnostics

Each `ImportDiagnostic` contains:

- `code`: stable machine-readable identifier
- `severity`: `info`, `warning`, or `error`
- `message`: human-readable detail
- optional `source_id` and `source_kind`
- optional `action`, such as `skipped`, `normalized`, or `approximated`

Call `diagnostic.as_dict()` when serializing a diagnostic report.
