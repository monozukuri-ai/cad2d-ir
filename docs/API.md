# API Guide

## High-level import API

Use `cad2d_ir.api` for file imports and diagnostic-aware DXF export.

### `convert_file_to_ir(path, *, source_format="auto", ir_version="0.2.0", validate=True, strict=True, curve_segments=96, encoding="auto")`

Dispatches to the DXF, DWG, DGN, DWF/DWFx, JWW, or SXF adapter and returns
`ImportResult`:

- `document`: imported IR document
- `diagnostics`: structured `ImportDiagnostic` records
- `statistics`: source, conversion, approximation, and skip facts
- `warnings`: compatibility property containing warning/error messages

For DXF files, `encoding="auto"` checks a BOM, then `$DWGCODEPAGE`,
then tries UTF-8, and finally falls back to CP932. The chosen encoding is available
in the `DXF_ENCODING_DETECTED.details` mapping, in
`result.statistics["encoding"]`, and in
`document["source"]["metadata"]["encoding"]`. Decode replacement is reported
as `DXF_DECODE_REPLACED` with replacement-character and affected-line counts.

`strict=False` skips malformed source records with an error diagnostic.
`curve_segments` must be in `[8, 4096]`.

Format-specific helpers are also public:

- `convert_dxf_file_to_ir(path, *, encoding="auto", ...)`
- `convert_dwg_file_to_ir(path, ...)`
- `convert_dgn_file_to_ir(path, *, encoding="auto", ...)`
- `convert_dwf_file_to_ir(path, ...)`
- `convert_jww_file_to_ir(path, ...)`
- `convert_sxf_file_to_ir(path, ...)`
- `convert_dxf_text_to_ir(dxf_text, ...)`

`DxfToIrResult` contains `document`, `diagnostics`,
`warnings`, and an `encoding` convenience property for file input.

DGN V7 does not store a text code page. `encoding="auto"` uses ASCII and emits
`DGN_TEXT_DECODE_REPLACED` when bytes are not ASCII; pass the project encoding
(for example `cp932`) when it is known. DWF and DWFx imports include all sheets
and markup entities. Their source sheet remains in `metadata.dwf`; multiple
sheets are disclosed with `DWF_MULTISHEET_FLATTENED`.

## High-level DXF export API

### `convert_ir_to_dxf_text(document, *, validate=True, target_version="AC1024", curve_segments=96, generic_dimensions="explode")`

Returns `IrToDxfResult`:

- `dxf_text`: Unicode DXF text; encode it according to the target version when writing bytes
- `diagnostics`: structured `ExportDiagnostic` records
- `warnings`: compatibility list of warning/error messages
- `entity_map`: IR entity to emitted DXF entity correspondence
- `target_version`: the selected DXF version

Each `entity_map` entry contains:

- `ir_id`: source IR entity ID
- `handle`: deterministic R2010 handle, or `null` for R12/skipped output
- `dxf_type`: emitted entity type, or `null` when skipped
- `index`: zero-based emitted-entity order
- `scope`: `modelspace` or `block:<name>`
- optional `reason_code`: diagnostic code explaining a skipped entity

One IR entity produces multiple entries when it is exploded. In AC1024 output,
re-importing the DXF places each handle in `entities[].source.id`. In AC1009
output handles are omitted and consumers use `index`.

`target_version` accepts:

| Value | CAD release | Export behavior |
|---|---|---|
| `AC1009` | R12 | LWPOLYLINE to POLYLINE; MTEXT to TEXT; ELLIPSE/SPLINE to sampled POLYLINE; HATCH to boundary POLYLINE |
| `AC1024` | R2010 | native supported entities, deterministic handles, and `$HANDSEED` |

AC1009 text output declares `$DWGCODEPAGE=ANSI_932`. Use
`write_dxf_file(..., target_version="AC1009", encoding="cp932")`, or leave
`encoding="auto"` to select CP932 for AC1009 and UTF-8 for AC1024. Manual
consumers of the `str` returned by `ir_to_dxf()` must likewise encode R12
output as CP932. An explicit non-CP932 encoding is rejected for AC1009 so the
declared codepage cannot disagree with the file bytes.

`generic_dimensions="explode"` is the default. It preserves GENERIC
dimension lines, rendered paths, text, and point markers as primitives.
`"skip"` retains the previous omission behavior with a structured reason.

`convert_ir_file_to_dxf(path, ...)` accepts the same export options.

## Low-level API

`cad2d_ir.codecs.dxf` exposes:

- `dxf_to_ir(...)`
- `read_dxf_file(..., encoding="auto", diagnostics=None)`
- `resolve_dxf_output_encoding(target_version=..., encoding="auto")`
- `ir_to_dxf(..., target_version="AC1024", curve_segments=96, generic_dimensions="explode", diagnostics=None, entity_map=None)`
- `write_dxf_file(..., encoding="auto")`

The low-level `ir_to_dxf()` return type remains `str`. Pass mutable
`diagnostics` and `entity_map` lists to collect the added result data.

## Determinism contract

For the same IR document and identical options, `ir_to_dxf()` returns
byte-identical text and the same entity map within a package minor version.
Entity and block order are preserved where semantically meaningful; table and
block names are emitted in a stable case-insensitive sort order. A checked-in
golden DXF test enforces this contract.

## Diagnostics

`ImportDiagnostic` includes `code`, `severity`, `message`,
optional source fields, optional `action`, and optional structured
`details`. `ExportDiagnostic` includes `code`, `severity`,
`message`, optional `entity_id`, and optional `action`.

See [the diagnostic code catalog](DIAGNOSTICS.md) for the complete stable list
and semantic-versioning policy.

## JSON helpers

- `load_ir_json(path, validate=True)`
- `dump_ir_json(document, path, pretty=True, validate=True)`
