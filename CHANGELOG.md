# Changelog

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
