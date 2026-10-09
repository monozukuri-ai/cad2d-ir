# Importer Architecture

## Contract

All file adapters return the same `ImportResult`:

```text
source file
  -> format adapter
  -> IR document + structured diagnostics + statistics
```

`ImportOptions` carries the IR version, validation and strictness settings, and curve approximation resolution. Optional parser dependencies are imported lazily so core DXF workflows do not require every CAD parser.

The registry in `cad2d_ir.importers.registry` currently dispatches:

| Format | Suffix | Adapter state | Parser dependency |
| --- | --- | --- | --- |
| DXF | `.dxf` | implemented | built in |
| JWW | `.jww` | implemented | `ezjww>=0.2.6,<0.3` |
| DWG | `.dwg` | implemented | `ezdwg>=0.11,<1` |
| DGN | `.dgn` | implemented (V7 2D, V8) | `ezdgn>=0.2.1,<0.3` |
| DWF | `.dwf`, `.dwfx` | implemented (2D) | `ezdwf>=0.0.1,<0.1` |
| MI | `.mi`, `.bi` | implemented (verified typed subset) | `ezmi2d>=0.2,<0.3` |
| SXF | `.sxf`, `.sfc`, `.p21` | implemented | `ezsxf>=0.2,<0.4` |

## JWW vertical slice

The JWW adapter consumes `ezjww.read_document()` rather than `read_dxf_document()`. This avoids losing source semantics before IR construction.

| JWW source | IR mapping | Fidelity handling |
| --- | --- | --- |
| `LINE` | `LINE` | direct |
| `ARC` / `CIRCLE` | `ARC`, `CIRCLE`, or `ELLIPSE` | flatness and tilt preserved |
| `POINT` | `POINT` | temporary marker state preserved |
| `TEXT` | `TEXT` | font, size, endpoint, and spacing metadata preserved |
| image placement `TEXT` (`^@BM`) | `IMAGE` | lower-left corner, drawn size and rotation; a file embedded in the version-700 archive is decoded into `data` (base64) with `mime_type` (`JWW_IMAGE_EMBEDDED`), an external or missing file keeps its path in `href` (`JWW_IMAGE_LINKED`); Jw_cad's trailing parameters stay in `metadata.jww.image_params` |
| internal setting `TEXT` at `(0, -1000)` | `header.metadata.jww.settings` | known printer/view keys remain metadata and do not affect drawing geometry |
| `SOLID` | solid `HATCH` | quadrilateral vertex order normalized |
| `CIRCLE_SOLID` | solid `HATCH` | polyline approximation recorded and diagnosed |
| `BLOCK` | `INSERT` plus block table | signed scale and rotation preserved |
| `DIMENSION` | `GENERIC DIMENSION` | line, text, SXF mode, auxiliary lines, and auxiliary points preserved in `definition.source_geometry` |

JWW pen color, pen style, pen width, layer/group state, source indices, file version, memo, paper size, and internal printer/view settings are retained through IR style fields, tables, provenance, or JWW metadata. `JWW_METADATA_SETTING_EXTRACTED` reports the source-entity-to-metadata separation.

The entity pen style is the Jw_cad line type number:

| Pen style | Jw_cad line type | IR linetype | `pattern_mm` |
| --- | --- | --- | --- |
| 1 | solid | `CONTINUOUS` | - |
| 2, 3, 4 | dashed 1-3 | `JWW_DASHED1`-`3` | 0.625/0.625, 1.25/1.25, 1.875/0.625 |
| 5, 6 | chain 1-2 | `JWW_DASHDOT1`-`2` | 3.125 and 8.125 long dash, then 0.625 gap, dash, gap |
| 7, 8 | double-dot chain 1-2 | `JWW_DIVIDE1`-`2` | 2.5 and 7.5 long dash, 0.3125 dots, 0.625 gaps |
| 9 | construction line | `JWW_CONSTRUCTION`, `plot: false` | 0.3125/0.9375 |
| 11-15 | random (freehand-looking) line | `BYLAYER` | not modelled |
| 16-19 | double-length chain, double-dot chain, dashed | `JWW_DASHDOT_X2`, `JWW_DIVIDE_X2`, `JWW_DASHED_X2`, `JWW_DASHED_X4` | 20 mm period (40 mm for X4) |
| 31-45 | SXF line type 1-15 | `CONTINUOUS`, `SXF_DASHED`, ... | SXF Ver.3.1 reference pitches |
| 47-62 | SXF user-defined | `SXF_USER_17`-`32` | segment lengths stored in the file; an empty slot stays `BYLAYER` |

The patterns are millimetres on paper, like JWW coordinates, so no linetype scale applies. They come from the default line type patterns stored in every JWW header (a bit mask per line type): one pattern bit prints as `printer pitch / 32` mm. Jw_cad stores, for its SXF-compatible line types, both such a bit pattern with its printer pitch and the SXF segment lengths in millimetres, and the two agree at exactly that ratio. Construction lines are displayed but never printed, which `plot: false` on the linetype records.

The table lists the Jw_cad defaults. A file records its own line type settings, and with `ezjww` 0.3.3 or later (`header["line_types"]`) the importer uses them:

- line types 2-9 and 16-19 take the bit pattern and the printer pitch of the file. A pattern the user turned solid gets an empty `pattern_mm`;
- SXF-compatible line types take the segment lengths stored with them, and keep the SXF reference pitches when the file names them without lengths;
- user-defined SXF line types (47-62) become `SXF_USER_<code>` (code 17-32) with their segment lengths, and the name given in Jw_cad goes into the description.

`header.metadata.jww.line_type_settings` is `"file"` when the settings were read and `"default"` otherwise (an older `ezjww`, or a header that ends before them). In the default case the Jw_cad default printer pitch (10; 20 or 40 for the double-length types) is assumed, so a drawing saved with another pitch prints proportionally shorter or longer dashes than the IR pattern says, and user-defined line types stay `BYLAYER`.

## DWG adapter

The DWG adapter consumes `ezdwg.read()` and enumerates every entity through `Document.entities().query()` (falling back to `modelspace().query()` on `ezdwg` releases before the placement-aware layouts), then partitions them itself: entities owned by a named block record become block-definition bodies, paper-space entities (layout frames, viewports, title blocks; `entmode == 1` or owned by a `*Paper_Space*` record) become `layouts`, and the rest form the IR modelspace. Low-level public table decoders are used only to recover layer names/colors, linetypes and block-header names.

The owner of an entity is the placement stored in its common entity data (`Document.entity_placement()`: owner handle, paper space or model space). The `owner_handle` of the type-specific decoders is only the fallback when no placement is available (R13/R14 files with `ezdwg` before 0.12.11): it is absent for several entity types (`HATCH`, `SPLINE`, `SOLID`, `ELLIPSE`, ...), which used to move block contents into model space, and it can name another object for a model-space `INSERT`.

Block names are unique in the IR. When two block headers carry the same name (anonymous blocks are stored without their number), the last one keeps the name, as a name reference resolved before, and the others are renamed: the next free number for anonymous names (`*D12`), `<name>_<HANDLE>` otherwise. The original name stays in `metadata.dwg.source_name` and `DWG_DUPLICATE_BLOCK_NAME_RENAMED` reports the count.

| DWG source | IR mapping | Fidelity handling |
| --- | --- | --- |
| `LINE`, `CIRCLE`, `ARC`, `ELLIPSE`, `POINT` | matching IR entity | direct XY mapping |
| `LWPOLYLINE`, `POLYLINE_2D` | `LWPOLYLINE` | bulges and widths metadata retained; fitted interpolation is diagnosed |
| `POLYLINE_3D` | `LWPOLYLINE` | projected to XY with straight segments, like the DXF importer reads it; one that leaves the XY plane counts as projected (`DWG_NONPLANAR_PROJECTED`); one whose vertices cannot be read is skipped |
| `POLYLINE_MESH`, `POLYLINE_PFACE` | none | 3D surfaces; skipped with `DWG_UNSUPPORTED_ENTITY` |
| `SPLINE` | `SPLINE` | degree, controls, knots, weights, closure retained |
| `TEXT`, `MTEXT` | `TEXT` / `MTEXT` | placement and exposed formatting retained; a justified `TEXT` is anchored at its alignment point |
| `TOLERANCE` | `LWPOLYLINE`, `LINE`, `TEXT` | the feature control frame drawn as the box of each row, the lines between its compartments and one centered `TEXT` per compartment, with the symbols of the GDT font as Unicode characters (`DWG_TOLERANCE_EXPLODED`); the text height is the one `ezdwg` reports (stored with the entity in R13/R14, otherwise the one of the dimension style) and compartment widths are estimated; skipped with `DWG_UNSUPPORTED_ENTITY` when no height is known |
| `VIEWPORT` | `viewports` of its layout | window and view of a paper-space viewport (`ezdwg` 0.12.12 or later); the viewport that stands for the sheet itself is left out |
| `ATTRIB` | `attribute_texts` and `attributes` of its `INSERT` | attached by owner handle, with its own position, size, justification, layer and color; invisible ones carry `visible: false`; one whose `INSERT` is unknown stays a `TEXT` (and is skipped when invisible) |
| `ATTDEF` | `TEXT` | definitions inside a block definition (templates) are skipped with `DWG_HIDDEN_ATTRIBUTE_SKIPPED`, constant definitions stay in the block with their value; a definition outside of a block is shown by its tag |
| `HATCH`, `SOLID`, `TRACE`, planar `3DFACE` | `HATCH` | polygon loops retained; pattern fills carry `pattern_lines` |
| `INSERT`, `MINSERT` | `INSERT` | signed scale retained; MINSERT array parameters remain metadata |
| `DIMENSION` | semantic `DIMENSION` | subtype and complete native geometry payload retained; `definition.block` names the block with the saved graphics |
| block-owned entities | block table body | grouped when owner handles are exposed |

Linetypes need `ezdwg` 0.12.10 or later. `tables.linetypes` holds the linetype table of the drawing with its dash patterns (`pattern_mm` in drawing units, as in DXF), layers carry their linetype, and entities carry `linetype` (`BYLAYER`, `BYBLOCK`, `CONTINUOUS` or a table entry) and `linetype_scale` when it is not 1. `$LTSCALE` fills `header.linetype_scale` when it is not 1. With an older `ezdwg` every entity is `BYLAYER` on `CONTINUOUS` layers and the table holds no patterns.

A `DIMENSION` refers to the anonymous block that holds its saved graphics by handle. When that block is in the IR, `definition.block` names it, the same reference the DXF codec keeps (group 2), so renderers and the DXF export use the saved graphics instead of rebuilding them from the definition points.

R13/R14 files need `ezdwg` 0.12.11 or later for block references, multi-line text, hatches, solids, splines, attributes and dimensions, and for block membership. With an older `ezdwg` these files only yield lines, arcs, circles, ellipses, points, lightweight polylines and single-line text, all in model space. R13/R14 have neither lineweights nor a layer plot flag, and their symbol names (layers, blocks, linetypes) are upper case.

Pattern fills carry the definition lines of their pattern (`pattern_lines`, with `pattern_angle` and `pattern_scale`; see docs/SCHEMA_NOTES.md), again with `ezdwg` 0.12.10 or later.

Layer state and lineweights need `ezdwg` 0.12.11 or later. A layer that is off or frozen gets `visible: false` (which of the two, and whether it is locked, is in `metadata.dwg`), a layer that is not plotted `plot: false`, and a layer with a lineweight of its own `lineweight_mm`. Entities carry `lineweight_mm` when they have one of their own and `visible: false` when their invisibility flag is set. With an older `ezdwg` every layer is visible and plotted and no lineweight is known.

A justified `TEXT` is anchored at its alignment point (see "Text anchor" in docs/SCHEMA_NOTES.md); both stored points stay in `metadata.dwg`.

Each `ATTRIB` is attached to the `INSERT` that owns it, wherever that reference lives: in model space, inside a block definition or on a sheet. `statistics["attached_attributes"]` counts them.

Paper space becomes `layouts` (see "Layouts" in docs/SCHEMA_NOTES.md), one per sheet that holds entities or a viewport onto model space. With `ezdwg` 0.12.12 or later, `Document.layouts()` gives each sheet its name, tab order and paper, and tells which sheet was current when the file was saved (its entities are stored without an owner). Viewports need that release as well: older ones decode no viewport geometry, and those viewports are counted under `DWG_UNSUPPORTED_ENTITY`. Without layout objects (older `ezdwg`, or files written before R2000) the sheets are told apart by their block record and named `Layout1`, `Layout2`, ... When model space holds no entity, the active sheet becomes `entities` (`DWG_LAYOUT_PROMOTED`). `statistics["layouts"]` and `statistics["converted_layout_entities"]` count what stays in `layouts`. R13/R14 keep the view of a viewport in extended data, which is not read: their viewports have a window but no view.

DWG units come from the `$INSUNITS` header variable (`ezdwg` >= 0.11 `Document.header_variables()`); mapped codes fill `header.units` (and therefore `$INSUNITS` on DXF export), the raw code is recorded in header metadata, and unmapped codes or R14 files (no `$INSUNITS`) fall back to `unknown` with the reason in metadata plus a diagnostic. Non-zero Z coordinates are projected to XY and reported. Unsupported 3D/presentation entities are skipped with aggregate diagnostics. Block base points are also not exposed; recovered block bodies use `[0, 0]` and state that limitation in metadata.

## DGN adapter

The DGN adapter opens files with `ezdgn.open_document()` and routes each
generation to its own mapping: the V7 2D drawing model documented below, and a
native V8 document model (`cad2d_ir.importers.dgn_v8`). Both use master-unit
coordinates where available and fall back to precise UOR values without
routing through DXF.

| DGN source | IR mapping | Fidelity handling |
| --- | --- | --- |
| line, line string | `LINE`, `LWPOLYLINE` | direct |
| shape | closed `LWPOLYLINE` or solid `HATCH` | fill linkage and resolved V7 color retained |
| ellipse, arc | `CIRCLE`, `ARC`, or `ELLIPSE` | native axes, rotation, and sweep retained |
| text | `TEXT` | raw bytes, font ID, justification, selected encoding, and width factor retained; anchored left because the stored V7 origin is the string's bottom-left corner |
| cell | block table plus `INSERT` | design-space children remain exact; origin and native placement matrix are retained without double-transforming them |
| B-spline curve | `SPLINE` | open interior knots are expanded to an exact clamped knot vector; closed non-uniform knots remain metadata and are diagnosed |
| type-11 curve | `LWPOLYLINE` | control-polyline approximation is diagnosed |
| complex chain/shape, text node | child IR entities | parent record indexes retained and flattening diagnosed |

V7 level numbers become deterministic layer names (`DGN_LEVEL_<n>`). V7
line-style and line-weight indexes remain metadata because they do not have a
reliable physical-mm mapping. V7 files do not store a text code page;
`encoding="auto"` probes all text bytes once using ASCII, CP932, then Latin-1,
and records the selected encoding in source metadata and statistics. An
explicit `encoding=` remains available for project-specific code pages.
`ezdgn` still rejects V7 3D in its semantic reader, so those file imports fail
explicitly. If a compatible native drawing model does report `dimension=3`,
the adapter projects coordinates to XY and emits `DGN_3D_FLATTENED` rather
than silently dropping Z.

### V8 mapping

The V8 path converts one model per file: the first model containing drawable
entities. Additional models are reported with `DGN_V8_EXTRA_MODELS_SKIPPED`,
and 3D models are projected to XY with `DGN_3D_FLATTENED`.

| V8 source | IR mapping | Fidelity handling |
| --- | --- | --- |
| line, line string | `LINE`, `LWPOLYLINE` | direct |
| shape | closed `LWPOLYLINE` | fill linkages are not semantically decoded yet, so shapes stay outlines with their linkage kind codes in metadata |
| ellipse, arc | `CIRCLE`, `ARC`, or `ELLIPSE` | native axes, rotation, and sweep retained |
| text | `TEXT` | the stored V8 origin is the justification-dependent user origin (unlike V7's bottom-left corner), so both `halign` and `valign` derive from the justification code; per-element encoding, raw bytes, and 3D orientation remain metadata |
| text node | child `TEXT` entities | text children are lifted directly from the entity view |
| point string | one `POINT` per vertex | per-point orientation retained as metadata |
| cell | block table plus `INSERT` | design-space children remain exact; the native placement matrix and translation stay metadata |
| shared-cell instance | skipped | definitions are not decoded yet; `DGN_SHARED_CELL_UNRESOLVED` reports the count and names |
| B-spline curve | `LWPOLYLINE` | the stream does not expose order or knots yet, so the pole control polyline stands in and is diagnosed |
| type-11 curve | `LWPOLYLINE` | control-polyline approximation is diagnosed |
| complex chain/shape | child IR entities | parent element indexes retained and flattening diagnosed |
| dimension | skipped | only a bounded anchor is decoded upstream |

V8 level IDs become `DGN_LEVEL_<n>` layer names; level name tables and the
active color table live in control objects that are not decoded yet, so
entities carry their color index as metadata without a resolved RGB. V8 text
arrives already decoded by `ezdgn` (UTF-8, Windows-1252, or the escaped
Windows-1252 marker), so the `encoding=` option does not apply to V8 input.

## DWF adapter

The DWF adapter consumes `ezdwf`'s normalized bottom-left, Y-up paper-space
model for legacy DWF, DWF 6 ePlot packages, and DWFx fixed pages. Primary and
markup entities are both imported.

| DWF source | IR mapping | Fidelity handling |
| --- | --- | --- |
| line, polyline, polygon | `LINE`, `LWPOLYLINE`, `HATCH` | paper coordinates and resolved style retained |
| circle, arc, ellipse | matching IR curve | orthogonal axes remain semantic; sheared bases are sampled and diagnosed |
| PolyBezier | `SPLINE` | cubic controls and exact composite-Bezier knots retained |
| text/glyph run | `TEXT` / `MTEXT` | MTEXT formatting codes are detected without stripping the source text; placement, font metadata, bounds, and glyph-outline count retained |
| path | `LWPOLYLINE` / `HATCH` | line segments direct; Bezier/elliptical segments sampled and diagnosed |
| triangle strips/contour fills | solid `HATCH` | sliding triangle topology, contours, and resolved fill color retained |
| raster image | skipped | resource, diagnostic, and aggregate count expose the unsupported boundary |

DWFx DIP coordinates use IR `units="custom"` with
`unit_scale_to_mm=25.4/96`. Uniform DWF sheet units map directly. Mixed-unit
multi-sheet documents retain each sheet's native coordinates, set document
units to `unknown`, and emit a diagnostic. Because the current IR has one
modelspace, multiple sheets overlap in that space by design; every entity
retains its sheet index/name and the drawing-level source metadata contains the
sheet table. Clip paths, opacity masks, and compositing groups are not applied
to IR geometry; their counts remain entity metadata and an aggregate diagnostic
discloses that appearance boundary.

DWF RGBA colors remain eight-digit `#RRGGBBAA` values in IR. Renderers must
composite or otherwise honor the alpha channel; reducing them to seven-character
RGB is a consumer-side loss. Standalone legacy W2D 00.30/00.50 support remains
an `ezdwf` parser boundary rather than an IR-adapter concern.

## MI adapter

The MI adapter consumes the public `ezmi2d.Document` model directly. It does
not route through DXF and does not inspect private Rust records. MI source
angles are radians; converted IR angles are degrees so the resulting document
is compatible with the existing DXF export path, while the original unit is
retained in header metadata.

| MI source | IR mapping | Fidelity handling |
| --- | --- | --- |
| `LIN` | `LINE` | resolved endpoints map directly |
| `ARC`, `FIL` | `ARC` | center, radius, normalized angles, and explicit `ccw` map directly; unknown orientation is not guessed |
| `CIR` | `CIRCLE` | center and radius map directly |
| `BSPL` | `SPLINE` | degree, controls, knots, optional weights, and presence-aware closure map when representable; verified evaluation is sampled only as a diagnosed fallback |
| `TEX` | `TEXT` / `MTEXT` | decoded content, origin, height, rotation, alignment, width factor, font, transform, and mirror metadata are retained |
| dimension families | `GENERIC DIMENSION` | typed reference points/IDs, text position, measurement, formatted text, property/style IDs, and tolerances remain in the flexible definition |
| `LED` | `LWPOLYLINE` | vertices map to visible geometry; arrow type/size remain metadata and flattening is diagnosed |
| `HAT` + `COC`/`PFA`/`HAPP` | patterned `HATCH` | ordered curve boundaries are sampled with an approximation record; outer/inner association and source pattern lines remain metadata |
| `ASSE` part hierarchy | block table + `INSERT` | nested/shared definitions, sheets, signed scale, rotation, translation, and exact affine transforms are retained without world-space duplication |

MI color codes 0 through 7 map to their named RGB values. Named linetypes are
retained, but their physical dash lengths are not exposed by the parser, so
linetype definitions use empty patterns and disclose that boundary. The source
lineweight value is retained in metadata instead of being mislabeled as
millimeters. Multiple source layers, unknown visibility codes, mirrored text,
presence-aware spline flags, and hatch pattern definitions likewise stay in
MI metadata with one aggregate `MI_SOURCE_SEMANTICS_PRESERVED` diagnostic.

`ezmi2d` parser diagnostics are forwarded as `MI_PARSER_DIAGNOSTIC` without
discarding their upstream code, action, severity, or byte/line span. Raw
fallback entities and symbols without a lossless IR equivalent are skipped
explicitly. Strict mode raises on malformed typed geometry or invalid assembly
graphs; lenient mode skips the affected entity/instance and emits a stable
diagnostic.

Both `.mi` and `.bi` suffixes dispatch to this adapter. The `.bi` route means
the gzip-wrapped product-generated MI envelope currently verified by
`ezmi2d`; zlib, ZIP, UNIX `compress`, UNIX `pack`, and arbitrary historical BI
encodings are not inferred.

## IDW adapter

**Licensing:** The optional `inventor-kit` dependency is offered under PolyForm
Noncommercial 1.0.0 with separate commercial licenses. Review the
[IDW licensing terms](../README.md#idw-licensing-inventor-kit) before commercial
use, including internal business use, or redistribution with your application.
`cad2d-ir` itself remains MIT-licensed.

The IDW adapter consumes `inventor_kit.DrawingDocument` (the saved sheet display
that Inventor wrote into the file). `convert_idw_file_to_ir` reads the file with
`inventor_kit.read_drawing_file`; `idw_document_to_ir` accepts an already-read
document so callers that also need the image bytes read the file once. Nothing is
reprojected or regenerated and no referenced IPT/IAM file is opened.

| IDW display item | IR mapping | Fidelity handling |
| --- | --- | --- |
| `polyline` (2 points) | `LINE` | direct; repeated closing point removed |
| `polyline` (3+ points) | `LWPOLYLINE` | closed when the last point repeats the first; splines arrive pre-sampled from the parser |
| `curve` (affine ellipse arc `C + u cos t + v sin t`) | `CIRCLE` / `ARC` / `ELLIPSE` | the XY projection of a 3D circle is generally a skewed ellipse; a 2x2 SVD recovers the principal axes and the parameter range is recovered numerically. Edge-on projections (minor axis ~0) become `LINE` with an `approximation` record |
| `curve` with `filled: true` | solid `HATCH` | the arc closes along its chord; the loop is sampled with `curve_segments` |
| `triangles` | solid `HATCH` per triangle | filled arrowheads and similar saved fills |
| `text` | `TEXT` | cap-height candidate becomes `height`; rotation from the direction vector; multi-line strings split into one entity per line (1.2 x height); AIGDT `n`/`x` map to U+2300/U+21A7; mirrored up vectors are recorded, not applied |
| `image` | none | placements (origin, u, v in mm) and image descriptors stay in `source.metadata.idw`; raster-only views are diagnosed |

Coordinates are Inventor's internal units, which are centimetres for every
observed file (A-series and ANSI sheets match exactly). The adapter multiplies by
`unit_scale` (default 10) and reports `IDW_UNITS_ASSUMED_CM` with the matched
paper size. Coordinates are paper space with view scales already applied, so
DXF output is the sheet at 1:1. Multiple available sheets are tiled along +X with
a gap of `sheet_gap_ratio` times the widest sheet (`IDW_MULTISHEET_TILED`);
sheets without a decodable display are skipped (`IDW_SHEET_UNAVAILABLE`) and a
document without any decodable sheet raises `ImporterError` naming the segment
major. Elements lying entirely outside the sheet (unclipped projected curves)
are dropped with `IDW_OUT_OF_SHEET_DROPPED`.

Style handling: entity colors come from stored RGBA values (most items inherit
an unknown layer color and stay unset), stored line widths become
`lineweight_mm`, dash arrays become `IDW_LTYPE_nnnn` linetypes with nominal
millimetre patterns, and font families become `IDW_<family>[_BOLD][_ITALIC]`
text styles. Parser-side omissions (`hidden_by_stored_attribute`,
`display_type_not_decoded`, ...) and unresolved style reasons are forwarded as
counted diagnostics; `inventor-kit` document diagnostics are forwarded as
`IDW_DRAWING_WARNING`. Supported segment majors are those of the installed
`inventor-kit` (23, 24, 26, 28, 29 and 31 in 0.6.0).

`ImportOptions.entity_provenance=False` omits per-entity `source`/`metadata`
for render-only imports of large drawings.

## SXF adapter

The SXF adapter parses either SFC or AP202/P21 and consumes `ezsxf.build_drawing()` (the private `ezsxf._drawing` entry point on ezsxf before 0.3.1). No DXF text is produced or reparsed.

| SXF drawing primitive | IR mapping | Fidelity handling |
| --- | --- | --- |
| two-point path | `LINE` | direct |
| other path | `LWPOLYLINE` | SFC curve source kinds carry `approximation`; P21 paths disclose the flattening boundary |
| fill with outer/inner rings | solid `HATCH` | holes retained |
| text | `TEXT` / `MTEXT` | layer, RGB color, line width, font, anchor, angle, width retained |
| marker/symbol insertion point | `POINT` | code, scale, and symbol name retained |
| SFC dimension feature | semantic `DIMENSION` | native feature plus grouped rendered world-space paths/text retained |

Line types keep their SXF name (`dashed`, `chain`, a user-defined name, ...). Predefined ones get the SXF Ver.3.1 reference pitches as `pattern_mm`; user-defined ones get the pitch list of their `user_defined_font` feature, which only SFC exposes. Lengths are millimetres on the sheet.

SFC `typed_features` allow dimension kinds and source curve kinds to be recovered. P21 currently exposes generic STEP entities but no equivalent typed feature model, so the adapter preserves rendered primitives and emits `SXF_P21_SEMANTICS_FLATTENED`. Externally defined hatch/symbol limitations reported by `ezsxf` are forwarded as diagnostics.

### Sheet and partial drawings

`ezsxf` flattens every compound-figure placement into sheet coordinates, so
`entities` is the drawing as it appears on the paper (millimetres, origin at
the lower-left corner of the sheet). The sheet and the partial drawings are
kept so that a writer can put the entities back at their original scale:

- `header.metadata.sxf.sheet` is the drawing sheet: `name`, `sheet_type` (0-4 =
  A0-A4, 9 = FREE), `paper` (`"A3"`, ..., `"FREE"`), `orientation`
  (`"landscape"` / `"portrait"`) and `width_mm` / `height_mm`. SFC reads it
  from `drawing_sheet_feature`; P21 from `DRAWING_SHEET_REVISION` (its name,
  `A3_horizontal` for example), the `PLANAR_BOX` of the sheet and the
  `DRAUGHTING_TITLE` of the drawing. It is absent when the file has no sheet.
- `header.metadata.sxf.partial_drawings` lists the partial drawings placed on
  the sheet (compound figures of kind 1, the mathematical system, and kind 2,
  the geodetic system; drawing groups and parts are not listed): `name`,
  `coordinate_system` (`"mathematical"` / `"geodetic"`), the placement
  `position` (sheet mm), `angle_deg`, `ratio_x` / `ratio_y`, and
  `scale_denominator` (100 for 1:100) when both ratios are equal;
  `entity_count` says how many entities were rendered through it. A local
  point `p` of the partial drawing appears on the sheet at
  `position + R(angle) * (ratio_x * p.x, ratio_y * p.y)`.
- `metadata.sxf.partial_drawing` on an entity names the partial drawing it was
  rendered through (also through nested groups and parts inside it); entities
  drawn directly on the sheet have no such key. A part that is placed in more
  than one partial drawing cannot be attributed, so its entities carry no name
  and `SXF_PARTIAL_DRAWING_AMBIGUOUS` is reported.

`SXF_PARTIAL_DRAWING_FLATTENED` (info) states how many partial drawings and
entities were flattened this way.

## Validation gates

The implementation was exercised against the current local upstream corpora with strict parsing and IR schema validation:

- DWG: 55 files spanning AC1014 through AC1027; 143 decoded source entities, 139 top-level entities, two recovered block-body entities, and zero failures.
- DGN: three V7 2D files; 321 top-level IR entities and zero conversion failures. The V7 3D seed was rejected explicitly at the documented parser boundary.
- DGN V8: the ODA-authored GDAL fixture (53 drawable source entities across 14 kinds, 3D model); 43 strict-mode top-level IR entities plus 4 block-body entities in 2 blocks, zero conversion failures, and explicit skip diagnostics for the dimension anchor and the shared-cell instance.
- DWF: seven supported DWF 6 package/DWFx files; 24,360 IR entities and zero conversion failures. Standalone W2D 00.30 and 00.50 files were recognized and rejected explicitly as unsupported versions.
- MI: three byte-stable synthetic `ezmi2d` v0.2.0 fixtures cover direct geometry, UTF-8 text, fillet/B-spline geometry, generic dimensions, leaders, associative hatches, nested/shared parts, and two sheet occurrences. Plain MI and a test-created gzip BI envelope pass strict JSON Schema validation. These adapter fixtures do not expand `ezmi2d`'s documented format-family or corpus guarantees.
- IDW: inventor-kit 0.6.0 public regression inputs (segment majors 23, 24, 26, 28, 29 and 31; 10 decodable files, 13 sheets, about 50,000 display items including one 28,820-item sheet) plus 136 Inventor 2027 control drawings; 146 of 151 local inputs converted, every result passed strict JSON Schema validation with zero conversion failures and zero curve sampling fallbacks, and DXF re-import reproduced the entity kind counts. The remaining five inputs (three copies of the major21 VISE drawing and two holdout major31 resaves) have no decodable stored display in `inventor-kit` and raise `unsupported IDW profile`. Raster-only views are disclosed, not converted.
- SFC: 20 files; 46,305 source/typed features, 49,929 IR entities, 1,491 semantic dimensions, zero skipped entities, and zero failures.
- P21: 20 files; 538,130 generic STEP entities, 57,094 IR entities, zero skipped entities, and zero failures.

Every successful DGN/DWF result and each MI fixture above also passed strict
JSON Schema validation. These are development corpus observations, not
format-wide coverage guarantees. The upstream corpora and parser versions
should be rechecked when dependency bounds change.

## Adding another importer

An additional adapter should:

1. consume the parser's native public document model;
2. map entities directly into schema-supported IR entities;
3. attach drawing/entity provenance;
4. represent unavoidable approximations in `approximation` and diagnostics;
5. preserve unsupported semantics in metadata or emit an explicit skip diagnostic;
6. return source and converted entity counts;
7. keep its parser dependency in an optional extra and load it lazily;
8. add synthetic mapping tests plus a real-corpus validation gate.

Register the suffix and dispatch branch only after the adapter passes those gates. A parser-to-DXF-to-IR shortcut should not be used where it destroys dimensions, block semantics, or source metadata.
