# API Guide

## High-level import API

Use `cad2d_ir.api` for file imports and diagnostic-aware DXF export.

### `convert_file_to_ir(path, *, source_format="auto", ir_version="0.2.0", validate=True, strict=True, curve_segments=96, encoding="auto")`

Dispatches to the DXF, DWG, DGN, DWF/DWFx, EXB, IDW, JWW, JWS, MI/BI, PDF, or SXF adapter and returns
`ImportResult`:

- `document`: imported IR document
- `diagnostics`: structured `ImportDiagnostic` records
- `statistics`: source, conversion, approximation, and skip facts
- `warnings`: compatibility property containing warning/error messages

For DXF files, `encoding="auto"` checks a BOM, then `$DWGCODEPAGE`,
then tries UTF-8, and finally falls back to CP932. A declared codepage is
ignored in favour of UTF-8 when the non-ASCII bytes are valid UTF-8 and either
`$ACADVER` is AC1021 (AutoCAD 2007) or later, where DXF text is UTF-8 by
definition, or the declared codepage cannot decode them. The chosen encoding is available
in the `DXF_ENCODING_DETECTED.details` mapping, in
`result.statistics["encoding"]`, and in
`document["source"]["metadata"]["encoding"]`. Decode replacement is reported
as `DXF_DECODE_REPLACED` with replacement-character and affected-line counts.

When LINE/POINT coordinates unambiguously describe an axis-aligned XZ or YZ
drawing and the file contains only supported LINE/POINT/CIRCLE/ARC geometry,
the DXF importer projects that plane to IR XY. CIRCLE/ARC OCS coordinates and
arc direction are transformed consistently, and
`DXF_NON_XY_PLANE_PROJECTED` records the projection.

`strict=False` skips malformed source records with an error diagnostic.
`curve_segments` must be in `[8, 4096]`.

Format-specific helpers are also public:

- `convert_dxf_file_to_ir(path, *, encoding="auto", ...)`
- `convert_dwg_file_to_ir(path, ...)`
- `convert_dgn_file_to_ir(path, *, encoding="auto", ...)`
- `convert_dwf_file_to_ir(path, ...)`
- `convert_exb_file_to_ir(path, *, entity_provenance=True, parse_options=None, ...)` (CAXA EXB, `ezexb`)
- `convert_idw_file_to_ir(path, *, entity_provenance=True, ...)`
- `convert_jww_file_to_ir(path, ...)`
- `convert_jws_file_to_ir(path, ...)` (Jw_cad registered symbol, `ezjww` 0.7+)
- `convert_mi_file_to_ir(path, *, encoding="auto", ...)`
- `convert_pdf_file_to_ir(path, *, pages=None, scale="auto", embed_images=False, ...)`
- `convert_sxf_file_to_ir(path, ...)`
- `convert_dxf_text_to_ir(dxf_text, ...)`

PDF import reads page objects through PDFium (`cad2d-ir[pdf]`). `pages` selects
1-based pages (default: all, tiled along +X); `scale="auto"` recovers the plot
scale from the dimension texts and scales the drawing to model millimetres when
enough of them agree (`PDF_SCALE_ESTIMATED`), `"none"` keeps paper millimetres and
a number applies that many model millimetres per paper millimetre;
`embed_images=True` stores raster images as PNG data. `cad2d_ir.importers.pdf.
pdf_document_to_ir` accepts an already open `pypdfium2.PdfDocument` (for example
built from bytes). The scale recovery itself is public as `cad2d_ir.paper_scale`.

EXB import reads CAXA CAD (电子图板) drawings through `ezexb` (`cad2d-ir[exb]`)
in partial mode with raised resource budgets; `parse_options` takes an
`ezexb.ParseOptions`. Coordinates are millimetres at real size and the drawing
scale and the paper measured from the frame block are in `header.metadata.exb`
(see "EXB adapter" in docs/IMPORTERS.md).

`DxfToIrResult` contains `document`, `diagnostics`,
`warnings`, and an `encoding` convenience property for file input.

DGN V7 does not store a text code page. `encoding="auto"` probes every text
element once at file scope, selecting the first strict decoder in the chain
ASCII, CP932, then Latin-1. The result is reported by `DGN_ENCODING_DETECTED`,
`result.statistics["encoding"]`, and `document["source"]["metadata"]`; pass an
explicit project encoding when it is known. An explicit encoding still fails in
strict mode and uses replacement characters with `DGN_TEXT_DECODE_REPLACED`
when `strict=False`. DWF and DWFx imports include all sheets and markup
entities. Their source sheet remains in `metadata.dwf`; multiple sheets are
disclosed with `DWF_MULTISHEET_FLATTENED`.

MI input uses `ezmi2d`'s file-scoped encoding selection. `encoding="auto"`
leaves BOM, declaration, MI-version, and heuristic selection to the parser;
an explicit value is forwarded as the parser override. The selected name,
selection source, declared name, parser version, container kind, logical and
container sizes, and source spans are retained in `source.metadata.mi` or
entity provenance. `.mi` and `.bi` suffixes dispatch to the same adapter;
current `.bi` compatibility is limited to the verified gzip-wrapped MI
container supported by `ezmi2d`.

IDW input requires the `idw` extra (`inventor-kit`, Python 3.11+). This optional
dependency has separate PolyForm Noncommercial / commercial licensing terms;
see the [IDW license notice](../README.md#idw-licensing-inventor-kit), including
the conditions for commercial internal business use. The adapter
converts the saved sheet display, scales Inventor's internal centimetres to
millimetres (`IDW_UNITS_ASSUMED_CM`), tiles multiple sheets along +X
(`IDW_MULTISHEET_TILED`) and keeps image placements and per-sheet layout in
`source.metadata.idw`. `cad2d_ir.importers.idw.idw_document_to_ir(document, ...)`
accepts an `inventor_kit.DrawingDocument` that the caller already read, plus
`unit_scale` and `sheet_gap_ratio` overrides; `convert_idw_file_to_ir` also
forwards `limits` / `drawing_limits` to `inventor_kit.read_drawing_file`. A file
whose segment major is not supported by `inventor-kit` raises `ImporterError`
with a message starting `unsupported IDW profile`.

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
- `scope`: `modelspace`, `block:<name>` or `layout:<name>`
- optional `reason_code`: diagnostic code explaining a skipped entity

One IR entity produces multiple entries when it is exploded. In AC1024 output,
re-importing the DXF places each handle in `entities[].source.id`. In AC1009
output handles are omitted and consumers use `index`.

`target_version` accepts:

| Value | CAD release | Export behavior |
|---|---|---|
| `AC1009` | R12 | LWPOLYLINE to POLYLINE; MTEXT to TEXT; ELLIPSE/SPLINE to sampled POLYLINE; HATCH to boundary POLYLINE; IMAGE skipped |
| `AC1024` | R2010 | native supported entities (IMAGE with its IMAGEDEF objects), deterministic handles, and `$HANDSEED` |

AC1009 text output declares `$DWGCODEPAGE=ANSI_932`. Use
`write_dxf_file(..., target_version="AC1009", encoding="cp932")`, or leave
`encoding="auto"` to select CP932 for AC1009 and UTF-8 for AC1024. Manual
consumers of the `str` returned by `ir_to_dxf()` must likewise encode R12
output as CP932. An explicit non-CP932 encoding is rejected for AC1009 so the
declared codepage cannot disagree with the file bytes.

`generic_dimensions="explode"` is the default. It preserves GENERIC
dimension lines, rendered paths, text, and point markers as primitives.
`"skip"` retains the previous omission behavior with a structured reason.

Raster images (`IMAGE`) are written to AC1024 output as `IMAGE` entities with
the `IMAGEDEF` objects, reactors, dictionary entries and `CLASSES` records DXF
needs; a file without images keeps the sections it always had. A DXF never
carries the pixels: a linked image keeps its `href`, an embedded image is
written as a reference to its file name (`DXF_IMAGE_REFERENCED`), and the
pixel count comes from `metadata.dxf.image`/`metadata.dwg.image` or from the
PNG/GIF/BMP/JPEG header of the embedded data (`DXF_IMAGE_SIZE_ASSUMED`
otherwise). AC1009 output skips images (`DXF_IMAGE_SKIPPED`).

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

## HP-GL paper-space conversion

`convert_hpgl_file_to_ir(path, *, strict=True, profile=None, limits=None,
geometry_limits=None, max_entities=200_000, entity_provenance=True, ...)`
returns `ImportResult` with every plot page in `document["layouts"]` in mm.
Profile and resource objects come from ezhpgl. Per-entity and document metadata
retain source spans and diagnostics; the model-space entity list stays empty.

`convert_hpgl_file_to_dxf(path, *, page=None, strict=True, profile=None,
limits=None, geometry_limits=None, max_entities=200_000, target_version="AC1024")`
returns `HpglToDxfResult(imported, exported, page)`. `page` is zero based and
mandatory for a multi-page plot. All pages remain in `imported.document`, while
`exported.dxf_text` contains only the selected paper-space sheet. Inspect both
stages' diagnostics. See [HP-GL behavior and installation](HPGL.md).
