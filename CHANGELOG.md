# Changelog

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
