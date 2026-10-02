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

Package `0.10.4` adds:

- `tables.layers.*.visible` (see "Visibility")
- `INSERT.attribute_texts` (see "Block attributes")

Package `0.10.5` adds:

- `layouts` (see "Layouts")

Documents without them are unchanged. Documents that use them still pass the
default validator of earlier packages, but not their `strict_jsonschema=True`
mode, because the document, `header`, layer and linetype definitions and
entities reject unknown keys there.

## Layouts

`entities` is model space: the drawing at full size. A drawing can also have
sheets (paper space). A sheet has entities of its own, in paper coordinates
(the frame, the title block, notes) and viewports: windows that show model
space at some scale. `layouts` holds them:

    {"name": "A3", "tab_order": 1, "active": true,
     "paper": {"name": "ISO_A3", "size_mm": [420.0, 297.0], "units": "mm"},
     "entities": [...],
     "viewports": [{"center": [200.0, 150.0], "width": 300.0, "height": 200.0,
                    "view_center": [2500.0, 0.0], "view_height": 4000.0}]}

A consumer that reads `entities` alone sees the model and nothing of the
sheets. The two must not be mixed: the coordinates of a sheet are millimetres
or inches on paper, those of the model are drawing units.

A viewport shows the model-space region of height `view_height` around
`view_center`, rotated by `rotation` (the view twist, counter-clockwise). A
model point `p` appears on the sheet at

    center + R(rotation) * (p - view_center) * (height / view_height)

so `height / view_height` is the scale of the view (0.05 for 1:20). `visible:
false` marks a viewport that is switched off, and `frozen_layers` names the
layers that are hidden in this viewport only. `view_center` and `view_height`
are missing when the source shows a view the IR cannot express (a view from
another direction than the top, or a perspective) or does not state the view;
the values of the source stay in `metadata`. Every sheet of a DXF or DWG file
also has one viewport that stands for the sheet itself; it shows no model space
and is not part of the IR.

`active` marks the sheet that was current when the source was saved, and
`tab_order` is its position among the layout tabs. A sheet that holds neither
entities nor a viewport is left out.

Some drawings are drafted on a sheet and leave model space empty. Their sheet
is the drawing, so the importers make its entities `entities` when model space
holds none: the active sheet, or the first one with entities. That layout is
removed from `layouts`, and `header.metadata.<format>.promoted_layout` names
it.

The DXF codec reads and writes layouts. The export holds one paper space (the
writer produces no `OBJECTS` section): the active sheet with its viewports,
or the first sheet with content. Other sheets are reported with
`DXF_LAYOUT_OMITTED`.

## Text anchor

`TEXT.insert` is the point that `halign` and `valign` refer to: the left end of
the baseline for the default `left` / `baseline`, the center of the text for
`center` / `middle`, and so on. A renderer places the text with that point and
needs no other.

DXF and DWG store two points for a text. The first (DXF group 10) is always the
left end of the baseline, and for a justified text the second (group 11) is the
point of the justification. The importers put the second point into `insert`
for a justified text and the first one otherwise. "Aligned" and "fit" texts
(group 72 = 3, 5) run from the first point to the second, so they are `left` /
`baseline` at the first point; "middle" (72 = 4) is `center` / `middle` at the
second. The DXF export writes `insert` as the second point of a justified text.

## Visibility

There are three independent reasons for an entity not to appear:

- `visible: false` on the entity: it is hidden on its own.
- `visible: false` on its layer: the layer is off or frozen in the source. The
  entities keep their own `visible`; a renderer has to look at the layer.
- `plot: false` on its layer or linetype: it is displayed on screen but not
  printed (see below for linetypes).

For a block reference the source applications differ by the reason: AutoCAD
hides the whole reference when the layer of the `INSERT` is frozen, while block
contents on other layers stay visible when that layer is only off. The IR does
not tell off from frozen (the DWG importer keeps both flags in `metadata.dwg`
of the layer), so a renderer has to pick one behaviour for an `INSERT` on an
invisible layer.

## Block attributes

An `INSERT` can carry attributes: texts that belong to that one reference, such
as the values of a title block. `attributes` maps each tag to its value.
`attribute_texts` holds what the drawing shows:

    {"tag": "DWG_NO", "text": "A-100", "insert": [412.0, 18.5], "height": 3.5,
     "halign": "center", "valign": "middle", "layer": "TITLE"}

Each item has the fields of a `TEXT` (`insert`, `height`, `rotation`, `style`,
`halign`, `valign`, `width_factor`, `oblique_deg`) plus its own `layer` and
`color`. The coordinates are those of the `INSERT` itself, not of the block: the
source stores every attribute at its final position, so the position, rotation
and scale of the `INSERT` must not be applied again. An `INSERT` inside a block
definition has its attributes in the coordinates of that block.

`visible: false` marks an invisible attribute: it carries a value but is not
drawn. A multi-line attribute keeps its line breaks as `\n` in `text`, and its
`insert` is the point of its first line.

`attribute_texts` is omitted when the source gives no geometry (the `attributes`
map alone says nothing about where a value is drawn). The DXF codec reads and
writes it (`ATTRIB` records), and the DWG importer fills it.

Attribute definitions (`ATTDEF`) are not kept as such. Inside a block they are
templates for new references and are left out, except for constant definitions,
which every reference shows: those are a `TEXT` of the block with their value.
A definition outside of a block is displayed by its tag and becomes a `TEXT`
with the tag as its text.

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
- Feature control frames (`TOLERANCE`) have no entity of their own: the importers draw them as the lines of their frame and one `TEXT` per compartment, with the geometric characteristic symbols as Unicode characters. Compartment widths are estimated.
- Polygon meshes and polyface meshes are 3D surfaces and are skipped. 3D polylines are projected to XY.
- Ellipse start/end parameters are always radians, independent of `header.angle_unit`, matching the DXF ellipse parameter convention.
