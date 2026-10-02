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

Package `0.10.2` adds optional fields without changing the IR version:

- `header.linetype_scale` and entity `linetype_scale` (see "Linetype patterns")
- `tables.linetypes.*.plot`

Documents without them are unchanged. Documents that use them still pass the
default validator of earlier packages, but not their `strict_jsonschema=True`
mode, because `header` and linetype definitions reject unknown keys there.

## Linetype patterns

`tables.linetypes.*.pattern_mm` follows the DXF convention: positive = dash,
negative = gap, 0 = dot. The lengths are drawing units at linetype scale 1
(millimetres when `header.units` is `mm`). A renderer draws each element

    pattern length * header.linetype_scale * entity.linetype_scale

drawing units long; both scales default to 1. DXF and DWG importers fill
`header.linetype_scale` from `$LTSCALE`, and the DXF importer fills the entity
scale from group 48. JWW and SXF store paper millimetres in a paper-scale
coordinate system, so their scale stays 1.

`$LTSCALE` is not always enough to reproduce a plot: with `PSLTSCALE = 1` a
paper-space viewport rescales the dashes, and the IR holds model space only.
Renderers should treat a pattern that is still invisible at their output scale
as "scale unknown" rather than draw it as a solid line.

A linetype with `"plot": false` is displayed by the source application but never
printed (Jw_cad construction lines). Renderers that emulate printed output should
skip entities that use it.

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
