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

Package `0.10.3` adds, in the same way:

- `HATCH.pattern_lines`, `HATCH.pattern_angle` and `HATCH.pattern_scale` (see
  "Hatch patterns")

Documents without them are unchanged. Documents that use them still pass the
default validator of earlier packages, but not their `strict_jsonschema=True`
mode, because `header`, linetype definitions and entities reject unknown keys
there.

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

## Hatch patterns

A `HATCH` with `solid: false` is a pattern fill. `pattern` is the name of the
pattern (`ANSI31`, ...), which says nothing about how it looks unless the
renderer knows that pattern file. `pattern_lines` is the definition itself, as
DXF and DWG store it with every hatch:

    {"angle": 45, "base": [0, 0], "offset": [-2.2451, 2.2451], "dashes": [6, -3]}

Each item is one family of parallel lines. For every integer `k` a line runs
through `base + k * offset` in direction `angle` (`header.angle_unit`); the
hatch shows the parts of those lines inside its loops. The component of
`offset` across the lines is their spacing, and the component along the lines
staggers the dashes from one line to the next. `dashes` is a dash-gap pattern in
the linetype convention (positive = dash, negative = gap, 0 = dot), starting at
`base + k * offset`; it is omitted for continuous lines.

The lines are in drawing coordinates and already include the rotation and the
scale of the pattern; `pattern_angle` and `pattern_scale` only record what the
source says (DXF groups 52 and 41) and must not be applied again. Linetype
scales do not apply to hatch dashes.

`pattern_lines` is omitted when the source does not define the pattern (a
hatch written with its name only, or a format without pattern definitions);
a renderer then has to fall back to its own rendering of `pattern`. The DXF
codec reads and writes the definition (groups 78 and 53/43/44/45/46/79/49), and
the DWG importer fills it with `ezdwg` 0.12.10 or later. A family whose offset
has no component across its lines is dropped on import, because all of its
lines would coincide.

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
