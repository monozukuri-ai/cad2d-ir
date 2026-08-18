# Changelog

## 0.9.2

- DXF import is now best-effort per entity: a malformed record (missing group
  codes, unparsable numbers, degenerate geometry such as a zero-radius CIRCLE)
  is skipped with the new `DXF_ENTITY_CONVERSION_FAILED` diagnostic instead of
  rejecting the whole drawing. Non-positive TEXT/MTEXT heights are replaced by
  a unit-based default (`DXF_TEXT_HEIGHT_DEFAULTED`).
- HATCH boundary paths of edge type (line/arc/ellipse/spline edges — the form
  AutoCAD writes for most hatches) are imported. Line and arc edges keep exact
  geometry through bulges (clockwise arcs honour the DXF complementary-angle
  convention); ellipse/spline edges are approximated
  (`DXF_HATCH_EDGE_APPROXIMATED`) and unusable loops are skipped
  (`DXF_HATCH_LOOP_SKIPPED`). Previously such hatches failed the import with
  "HATCH requires at least one polyline loop".
- DXF text splitting only recognizes CR/LF breaks, ignores leading blank lines,
  data after `EOF` (`DXF_TRAILING_DATA_IGNORED`) and a single unpaired trailing
  line from truncated or padded files (`DXF_TRAILING_LINE_IGNORED`) instead of
  raising "DXF text must contain an even number of lines".
- Fixed HATCH group 70 handling: pattern hatches (`70` = 0) were imported as
  solid fills and re-exported as SOLID.
- Added `cad2d_ir.schema.validate_entity()` for per-entity validation.
- JWW import maps `ezjww` parser diagnostics onto stable codes: a main entity
  list that `ezjww>=0.2.8` could only read partially is reported as
  `JWW_ENTITY_LIST_TRUNCATED` (error) instead of failing the import, and CP932
  replacements surface as `JWW_DECODE_REPLACED`.

## 0.9.1

- DWG import enumerates entities through `ezdwg.Document.entities()` (with a
  `modelspace()` fallback for older `ezdwg`), so block-definition bodies keep
  working with `ezdwg` releases whose `modelspace()` is filtered by entity
  placement.
- Paper-space DWG entities (layout frames, viewports, title blocks) are no
  longer mixed into the IR modelspace; they are skipped with the new
  `DWG_PAPERSPACE_ENTITY_SKIPPED` info diagnostic.

## 0.9.0

- Added native MI import through `ezmi2d>=0.2,<0.3`, with `.mi` and
  gzip-wrapped `.bi` registry/CLI dispatch, a public
  `convert_mi_file_to_ir()` helper, an optional `mi` extra, and MI provenance
  values in the canonical and packaged schemas.
- Mapped lines, arcs/fillets, circles, B-splines, text, generic dimensions,
  leaders, and associative hatches directly from the typed parser model.
  Source radians normalize to IR degrees; approximations and unsupported
  annotation boundaries use stable structured diagnostics.
- Preserved nested/shared MI parts and sheet occurrences as block definitions
  and INSERTs, including exact affine-transform fallback. Added byte-stable
  synthetic fixtures for geometry, UTF-8 text, annotations, and assemblies.

## 0.8.0

- Added native MicroStation V8 DGN import through `ezdgn>=0.2.1,<0.3`. `.dgn`
  files now route through `ezdgn.open_document()`: V7 keeps its existing
  mapping, while V8 models map lines, line strings, shapes, ellipses, arcs,
  type-11 curves, texts, text nodes, point strings, cells, and complex
  chains/shapes into IR with model metadata provenance.
- V8 text maps both `halign` and `valign` from the justification code: the
  stored V8 origin is the justification-dependent user origin, unlike V7's
  fixed bottom-left corner (verified against the ODA-authored GDAL fixture
  and the GDAL DGNv8 driver anchor mapping).
- Multi-model V8 files convert the first model with drawable entities and
  report the rest (`DGN_V8_EXTRA_MODELS_SKIPPED`); 3D models are projected
  with `DGN_3D_FLATTENED`; shared-cell instances are skipped explicitly
  (`DGN_SHARED_CELL_UNRESOLVED`) until definitions are decoded upstream; V8
  B-spline curves fall back to their pole control polylines because the
  stream does not expose order or knots yet.

## 0.7.2

- Added file-scoped DGN text encoding probing (ASCII, CP932, then Latin-1)
  with a `DGN_ENCODING_DETECTED` diagnostic, an explicit 3D-to-XY projection
  diagnostic (`DGN_3D_FLATTENED`), and text `width_factor` from the V7
  length/height multipliers.
- DGN text now always carries `halign: "left"`: the stored V7 origin is the
  bottom-left corner of the string regardless of the justification code,
  which stays available in entity metadata.
- DWF text carrying MTEXT formatting codes is now emitted as `MTEXT` while
  preserving the original formatting stream.

## 0.7.0

- Added native MicroStation V7 2D DGN import through `ezdgn>=0.1.2,<0.2`,
  including levels/styles, cells as blocks and inserts, B-splines, text byte
  provenance, and explicit complex/curve approximation diagnostics.
- Added native 2D DWF and DWFx import through `ezdwf>=0.0.1,<0.1`, including
  multiple sheets, markups, paper units, core geometry, cubic Beziers, paths,
  fills, parser diagnostics, and explicit unsupported raster boundaries.
- Added `dgn` and `dwf` optional extras, suffix detection, generic registry
  dispatch, public helpers, CLI format choices, schema provenance values, and
  packaged `all` dependency coverage.

## 0.5.0

- Resolved DWG header units from `$INSUNITS` via `ezdwg`
  `Document.header_variables()` (adapter dependency raised to `ezdwg>=0.11,<1`):
  mapped codes fill IR `header.units` (and therefore `$INSUNITS` on DXF
  export) with the raw code kept in header metadata; unmapped codes and R14
  files (no `$INSUNITS`) fall back to `unknown` with structured diagnostics
  (`DWG_UNSUPPORTED_INSUNITS`, `DWG_HEADER_UNITS_UNREADABLE`).
- Expanded supported Python versions to 3.10 through 3.14 and added an
  all-extras CI matrix for every supported interpreter.
- Updated the JWW adapter dependency to `ezjww>=0.2.6,<0.3`.

## 0.4.0

- Added ANSI_932 declaration and target-aware CP932 file encoding for R12
  Japanese text output.

## 0.3.0

- Added deterministic R2010 entity handles, $HANDSEED, and IR-to-DXF
  entity-map results with 1:N and skipped-entity records.
- Added structured ExportDiagnostic records while retaining compatibility
  warning strings.
- Added selectable R12 (AC1009) and R2010 (AC1024) output, including
  documented R12 explosion and approximation rules.
- Added CP932-aware DXF file decoding with BOM/codepage/probe detection and
  structured replacement diagnostics.
- Added LAYER, LTYPE, and STYLE table write/read round-tripping.
- Added R2010 AcDb subclass markers and generated geometry blocks for native DIMENSION entities so independent DXF audits require no repairs.
- Changed GENERIC dimension export to visual primitive expansion by default,
  with an explicit generic_dimensions=skip compatibility option.
- Published the stable diagnostic-code catalog and Python code registry.
- Documented and golden-tested deterministic export.
- Enforced entity-ID uniqueness per modelspace or block scope and disambiguated
  repeated input DXF handles.

## 0.2.0

- Added a common importer result, options, diagnostics, statistics, format detection, and `cad2d-ir import` CLI.
- Added native JWW import through the optional `cad2d-ir[jww]` dependency.
- Added native DWG import through `cad2d-ir[dwg]`, including direct core geometry, dimension, style, block-owner, provenance, projection, and unsupported-entity handling.
- Added native SXF SFC/P21 import through `cad2d-ir[sxf]`, including semantic SFC dimensions, drawing styles, hatches, markers, curve approximation records, and explicit P21 flattening diagnostics.
- Added `convert_dwg_file_to_ir()` and `convert_sxf_file_to_ir()` public helpers and registry/CLI dispatch for `.dwg`, `.sfc`, `.sxf`, and `.p21`.
- Preserved JWW dimensions as `GENERIC` dimensions instead of flattening them into line/text entities.
- Added JWW layer, linetype, text style, block, provenance, and source metadata mapping.
- Added explicit approximation records for JWW circular solid boundaries.
- Extended the IR schema with `POINT`, `ELLIPSE`, provenance, approximation metadata, signed insert scales, affine transforms, and unknown/unitless units.
- Added DXF read/write support for `POINT` and `ELLIPSE`; generic dimensions are safely omitted with a warning on DXF export.
- Raised the supported Python version to 3.13 to match the JWW adapter dependency.

## 0.1.0

- Defined CAD 2D IR schema (`ir_schema.json`).
- Added DXF <-> IR conversion for:
  - `LINE`, `CIRCLE`, `ARC`, `LWPOLYLINE`, `TEXT`
  - `MTEXT`, `INSERT`, `HATCH`
  - staged `SPLINE`, `DIMENSION`
- Added IR validation API and CLI (`validate`, `dxf2ir`, `ir2dxf`).
- Added round-trip and milestone tests.

## 0.1.1 (Milestone 4 public-release prep)

- Refined public Python API in `cad2d_ir.api`.
- Added result objects with warning collection for conversions.
- Added packaged schema data (`src/cad2d_ir/data/ir_schema.json`) and schema loader fallback strategy.
- Added contributor/public docs (`README`, `docs/`, `CONTRIBUTING`).
- Added CI workflow for automated test checks.
