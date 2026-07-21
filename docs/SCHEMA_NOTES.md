# Schema Notes

## Canonical schema

The canonical schema source is:

- `ir_schema.json`

For package runtime use, a copy is bundled at:

- `src/cad2d_ir/data/ir_schema.json`

`load_schema()` resolves schema data in this order:

1. Explicit path argument
2. Packaged schema data
3. Repository-root fallback

## Versioning

IR document version follows semantic version format (`X.Y.Z`) in the `version` field.

Schema `0.2.0` adds:

- drawing-level and entity-level `source` provenance
- `POINT` and `ELLIPSE`
- `GENERIC` dimensions for source formats without a safe subtype mapping
- signed, non-zero `INSERT.scale` values and optional affine `transform`
- entity `approximation` metadata
- `unitless` and `unknown` unit states

Existing `0.1.x` documents remain valid. New conversions default to `0.2.0`.

Entity IDs are unique within modelspace and independently within each block definition. `validate_ir()` rejects duplicate IDs in one scope.

When changing schema behavior:

- Add/adjust tests first
- Document compatibility impact in PR and changelog
- Keep unsupported mappings explicit via structured conversion diagnostics

## Current conversion limitations

- `constraints` are IR-only metadata today and are omitted on DXF export.
- `GENERIC` dimensions are exported as visual LINE/TEXT/POINT/polyline primitives by default; they are not mislabeled as native DXF `DIMENSION` entities.
- `HATCH` mapping currently focuses on polyline-like loops.
- Ellipse start/end parameters are always radians, independent of `header.angle_unit`, matching the DXF ellipse parameter convention.
