# Changelog

## 0.10.5

- Paper space is kept apart from model space. `entities` is model space; the
  sheets of a DXF or DWG drawing (frame, title block, notes on paper) are
  `layouts`, each with its name, tab order, paper and its own entities in
  paper coordinates. See "Layouts" in docs/SCHEMA_NOTES.md. Both importers
  mixed the two. The DXF importer read the entities of the current sheet
  (group 67 = 1) as model space. The DWG importer dropped those, and read the
  entities of every other sheet as model space, because it did not know the
  block records of the sheets. Paper coordinates are millimetres or inches on
  the sheet and have nothing to do with model coordinates, so a 420 x 297
  frame landed inside or beside a model that is thousands of units wide. In
  real-world drawings this concerns 60 of 1,342 DXF files (19,870 entities in
  model space; in 33 of them the sheet overlaps the model) and 62 of 483 DWG
  files (26,344 entities in model space), while the DWG importer dropped
  17,281 entities of current sheets.
- Viewports. A layout lists the windows through which it shows model space
  (`viewports`): the window on the sheet, the model-space point at its center,
  the height of the shown region and the view twist, the layers that are
  frozen in it and whether it is switched off. `height / view_height` is the
  scale of the view. The DXF codec reads and writes them, including the view
  that R12-R14 files keep in extended data; the DWG importer needs `ezdwg`
  0.12.12, which decodes viewport geometry and layout objects for the first
  time. The viewport that stands for the sheet itself is left out. A DWG and
  the DXF export of the same drawing give the same layouts, sheet entities and
  viewports.
- A drawing that is drafted on a sheet is the drawing. When model space holds
  no entity, the active sheet (or the first one with entities) becomes
  `entities`, and `header.metadata.<format>.promoted_layout` names it
  (`DXF_LAYOUT_PROMOTED`, `DWG_LAYOUT_PROMOTED`). 4 of the DWG drawings above
  came out empty before (5,147 entities); 7 DXF drawings (12,773 entities)
  keep the result they had by accident.
- The DXF export writes one layout: the active sheet, or the first one with
  content. Its entities carry group 67 = 1 and, in R2010 output, its viewports
  follow as `VIEWPORT` records (R12 keeps the view in extended data, which is
  not written: `DXF_R12_VIEWPORT_OMITTED`). The writer produces no `OBJECTS`
  section and therefore has one paper space; other sheets are reported with
  `DXF_LAYOUT_OMITTED`. `entity_map` entries of a sheet have the scope
  `layout:<name>`. Exported and read back, 309 real-world drawings with
  layouts return the same model space, the written sheet and all 539 of its
  viewports.
- DWG 3D polylines are read: projected to XY with straight segments, like the
  DXF importer reads them (`DWG_NONPLANAR_PROJECTED` counts the ones that
  leave the plane). They were skipped as unsupported: 9,974 polylines in 32 of
  483 real-world drawings, more than half of all entities in one of them. One
  whose vertices cannot be read is still skipped, in strict mode too. R13/R14
  files need `ezdwg` 0.12.12.
- DXF meshes are skipped with `DXF_MESH_SKIPPED` instead of being joined into
  a path (39 meshes in 6 of 1,342 real-world drawings). A polyface mesh keeps
  its face records between its `VERTEX` records, and the importer read them as
  vertices at the origin: 18 such meshes had become polylines with 28,721
  vertices, 19,004 of them at (0, 0). A polygon mesh is a grid of points, not
  a path either. The DWG importer skips both as before.
- A spline-fit DXF `POLYLINE` follows its fitted vertices. The control points
  of its frame (`VERTEX` flag 16), which the file stores in front of them, were
  part of the path: 1,255 points in 99 polylines of 10 real-world drawings.
- Feature control frames (`TOLERANCE`) are drawn: the box of each row, the
  lines between its compartments and one centered `TEXT` per compartment, with
  the characters of the GDT font as Unicode symbols (position, flatness,
  diameter, maximum material condition, ...). The IR has no entity for them:
  the DXF importer skipped them, and the DWG importer produced an `MTEXT`
  holding the raw string with its formatting codes. The text height is the
  one of the dimension style (DXF: `DIMTXT` times `DIMSCALE` from the
  `DIMSTYLE` table, with the overrides of the entity; DWG: as `ezdwg` reports
  it) and the gap is `DIMGAP`, half the text height when unknown. Compartment
  widths are estimated, since no font is at hand (`DXF_TOLERANCE_EXPLODED`,
  `DWG_TOLERANCE_EXPLODED`). The parts keep `source.kind == "TOLERANCE"` and
  the string in `metadata.<format>.tolerance_text`.
- DXF 3D polylines that leave the XY plane are reported
  (`DXF_POLYLINE_3D_PROJECTED`); they were projected silently.
- A DWF text with formatting codes (`MTEXT`) carries `attach: bottom_left`: its
  position is the start of its baseline, and without `attach` an `MTEXT` hangs
  below its insert point.
- `statistics["layouts"]` and `statistics["converted_layout_entities"]` of the
  DWG importer count what stays in `layouts`. `DWG_PAPERSPACE_ENTITY_SKIPPED` is
  no longer emitted.
- A strict conversion applies to sheets as it does to model space: a malformed
  entity on a sheet stops it. None of the real-world drawings that converted
  in strict mode before fails for that reason.

## 0.10.4

- Justified text sits where the drawing has it. DXF and DWG store two points
  for a text: the left end of its baseline and, for any justification other
  than left / baseline, the point it is justified at. Both importers put the
  first point into `insert` and kept `halign` / `valign`, so every renderer
  (and the DXF export, which writes `insert` as the justification point) moved
  the text: half a text height down for `middle`, a full height for `top`, the
  whole text width to the left for `right`. `insert` is now the point the
  justification refers to. "Aligned" and "fit" texts (DXF group 72 = 3, 5)
  become `left` / `baseline` at the first point and "middle" (4) `center` /
  `middle` at the second; the DWG importer used to report `right` for the first
  two. In real-world drawings, 5,452 of 9,191 DWG texts and 1,272 of 6,327 DXF
  texts are justified. With the fix, the anchor and justification of all 742
  texts compared between a DWG and the DXF export of the same drawing agree.
  See "Text anchor" in docs/SCHEMA_NOTES.md.
- Block attributes keep their place. `INSERT.attribute_texts` holds one text
  per attribute with its position, height, rotation, justification, layer and
  color, and `visible: false` for an invisible attribute. `attributes` alone
  (tag to value) said nothing about where a value is drawn, so the DXF importer
  dropped the geometry and the DXF export wrote every attribute at the
  insertion point with height 1. The DXF codec reads and writes the `ATTRIB`
  records in full, including multi-line attributes (the text of their embedded
  `MTEXT`). In DXF drawings with attributes, they are a fifth of all text.
- DWG attributes belong to their block reference. Each `ATTRIB` is attached to
  the `INSERT` that owns it (`attribute_texts` and `attributes`), wherever that
  reference lives. It used to become a `TEXT` in model space even when its
  reference sits inside a block definition, at block-local coordinates, or in
  paper space. `statistics["attached_attributes"]` counts them. R2007 and later
  files need `ezdwg` 0.12.11 to decode their attributes at all. The attributes
  of a DWG and of the DXF export of the same drawing agree in tag, value,
  position, height, justification, layer and visibility (77 of 77 compared).
- Attribute definitions (`ATTDEF`) appear where the drawing shows them, in DXF
  and DWG alike. A constant definition inside a block is a `TEXT` of that block
  with its value. A definition outside of a block is a `TEXT` with its tag,
  which is what the source application displays there (the DWG importer used
  the default value). Templates inside blocks and invisible definitions are not
  drawn. The DXF importer used to skip every `ATTDEF`.
- Layers that are off or frozen carry `visible: false` (DXF: a negative color
  number or flag bit 1; DWG with `ezdwg` 0.12.11 or later). The DXF export
  writes such a layer as off.
- DWG layers carry `plot` and `lineweight_mm`, and DWG entities `lineweight_mm`
  and `visible: false` for the invisibility flag (`ezdwg` 0.12.11 or later).
  Every DWG layer used to be plotted and visible, and no lineweight was known.
  Compared against DXF exports of the same drawings, the visibility, plot flag
  and lineweight of 1,673 layers and the visibility and lineweight of 32,824
  entities agree.
- The text of DWG `TEXT` and `MTEXT` entities of R2007 and later is right with
  `ezdwg` 0.12.11: earlier releases returned another string for about 6% of
  the `TEXT` and 11% of the `MTEXT` entities of such drawings. Dimension text
  overrides of these versions are filled as well (`definition.text`).
- A DWG `TOLERANCE` without a text height is skipped with
  `DWG_UNSUPPORTED_ENTITY` instead of failing the conversion. `ezdwg` 0.12.11
  decodes these entities for the first time (earlier releases never did) and
  takes the height from the dimension style; the height is 0 when that style
  cannot be read. With an earlier cad2d-ir, such an entity stops a strict
  conversion.
- R13/R14 DWG files (`ezdwg` 0.12.11 or later) yield their block references,
  multi-line text, hatches, solids, splines, attributes and dimensions, with
  block contents in their block. See docs/IMPORTERS.md.
- The default (non-strict) validator checks `attribute_texts` and the layer
  flags.

Older `ezdwg` releases keep working; they only lack the new data.

## 0.10.3

- Hatch pattern definitions reach the IR. A pattern fill (`solid: false`) carries
  `pattern_lines`: the families of parallel lines that make up the pattern
  (direction, a base point, the offset from one line to the next and an optional
  dash pattern), in drawing coordinates and already rotated and scaled.
  `pattern_angle` and `pattern_scale` record the rotation and scale the source
  states. Until now the IR only had the pattern name, so a renderer could not
  draw the lines that are in the drawing. The DXF codec reads and writes the
  definition, and the DWG importer fills it (`ezdwg` 0.12.10 or later). A hatch
  without a definition in its source stays as before. See docs/SCHEMA_NOTES.md.
- DWG linetypes (`ezdwg` 0.12.10 or later). `tables.linetypes` holds the
  linetype table with its dash patterns, layers carry their linetype, and
  entities carry `linetype` and `linetype_scale`. Every DWG entity used to be
  `BYLAYER` on `CONTINUOUS` layers. Checked against DXF exports of the same
  drawings: 300 linetype patterns, 1,648 layer linetypes and the layer, linetype
  and linetype scale of 31,334 entities agree.
- DWG block contents stay in their block. The owner of an entity now comes from
  the placement stored in its common entity data. The owner reported by the
  type-specific decoders is absent for splines, hatches, solids and ellipses, so
  those entities left their block for model space, at block-local coordinates,
  and a model-space insert could land in a block. In drawings compared against
  their DXF export, 1,996 of 28,857 entities sat in the wrong place; all
  entities of R2000 and later files agree now. R13/R14 files have no stored
  placement and keep the old behaviour.
- DWG blocks that share a name no longer overwrite each other. The last one keeps
  the name, the others are renamed (`DWG_DUPLICATE_BLOCK_NAME_RENAMED`), and no
  block body is lost.
- A DWG `DIMENSION` names the block that holds its saved graphics in
  `definition.block`, like DXF-derived dimensions. The reference is resolved by
  handle, so it is right even when block names repeat. Renderers and the DXF
  export then use the saved graphics instead of rebuilding them from the
  definition points.
- DWG attribute definitions inside block definitions are no longer drawn. They
  are templates: a block reference shows the values of its own `ATTRIB`
  entities, so the definition text doubled every attribute of a title block.
  Constant definitions stay, and invisible attributes are skipped as well
  (`DWG_HIDDEN_ATTRIBUTE_SKIPPED`).
- JWW line types follow the settings of the file (`ezjww` 0.3.3 or later): the
  bit pattern and printer pitch of line types 2-9 and 16-19, the segment lengths
  of SXF-compatible line types, and user-defined SXF line types, which were
  `BYLAYER` and are now `SXF_USER_17`-`32` with their pattern.
  `header.metadata.jww.line_type_settings` tells whether the file's settings
  (`"file"`) or the Jw_cad defaults (`"default"`) were used. In 808 real-world
  JWW files, 37% use another printer pitch than the default and 139 define
  line types of their own.
- DXF export keeps one value per line. A line break inside a text value used to
  be written as is, which shifted every following group code and made the file
  unreadable; multi-line `MTEXT` from the DWG and MI importers triggered it.
  `MTEXT` breaks become `\P`, and other values use the caret notation (`^J`).
- The default (non-strict) validator checks the new hatch fields.

Older `ezdwg` and `ezjww` releases keep working; they only lack the new data.

## 0.10.2

- Linetype scale reaches the IR. `header.linetype_scale` carries the global scale
  (DXF and DWG `$LTSCALE`) and entities carry `linetype_scale` (DXF group 48);
  both are omitted when they are 1. A dash is
  `pattern_mm * header.linetype_scale * entity.linetype_scale` drawing units
  long. The scale used to be dropped, so the dash patterns of real-size model
  space drawings (`$LTSCALE` 50 to 2000 in architectural files) came out hundreds
  of times too short for any renderer. DXF export writes both back; R12 output
  keeps `$LTSCALE` and reports a per-entity scale with
  `DXF_R12_LINETYPE_SCALE_OMITTED`. Group 48 after an entity-specific subclass
  marker (the MTEXT column width) is not read as a linetype scale.
- JWW line types follow the Jw_cad numbering: 2-4 are dashed lines, 5-6 chain
  lines, 7-8 double-dot chain lines and 9 the construction line type. The
  importer had named 3-9 after a different sequence (`DASHDOT`, `CENTER`, `DOT`,
  ...), so a chain line (5) became a dotted line and construction lines became
  `DOT2`. The linetypes are now `JWW_DASHED1`-`3`, `JWW_DASHDOT1`-`2`,
  `JWW_DIVIDE1`-`2`, `JWW_CONSTRUCTION`, the double-length types
  `JWW_DASHDOT_X2`, `JWW_DIVIDE_X2`, `JWW_DASHED_X2`, `JWW_DASHED_X4` (pen
  styles 16-19) and `SXF_*` for SXF-compatible line types (pen style 30 + SXF
  code, listed only when used). Their `pattern_mm` is the default pattern stored
  in JWW headers at the default printer pitch, in millimetres on paper (see
  docs/IMPORTERS.md). **This renames the linetypes of JWW-derived IR**; consumers
  that match on the old names need updating.
- `tables.linetypes.*.plot` (boolean, default true). `false` marks a linetype that
  is shown on screen but never printed; the JWW construction line type sets it.
- SXF linetype definitions carry `pattern_mm`: predefined line types use the SXF
  Ver.3.1 reference pitches and user-defined ones the pitch list of their
  `user_defined_font` feature (SFC only). They previously had a description only.
- The default (non-strict) validator checks the new fields.
- DXF encoding detection no longer trusts `$DWGCODEPAGE` over the bytes. DXF text
  is UTF-8 from AC1021 (AutoCAD 2007) on, yet writers keep declaring the system
  codepage (`ANSI_932` on Japanese systems); such files were decoded as CP932, so
  every Japanese layer name and text came out as mojibake with U+FFFD
  replacements. UTF-8 is now selected when the non-ASCII bytes are valid UTF-8
  and the file is AC1021 or later, or the declared codepage cannot decode them
  (`encoding_source` reads `utf-8-probe ($DWGCODEPAGE=... ignored)`). The same
  rule applies to binary DXF strings. In a corpus of 1,342 real-world DXF files
  this affected about 180.

## 0.10.1

- IDW: `IDW_VIEW_RASTER_ONLY` now recognizes views whose only stored display item
  is their own raster cache image (every view in Inventor 2027 files). 0.10.0
  counted such views as vector views because the image item is listed in the
  view's item ids.

## 0.10.0

- New IDW (Autodesk Inventor drawing) importer behind the `idw` extra
  (`inventor-kit>=0.6,<0.7`, Python 3.11+). It maps the saved sheet display
  (`inventor_kit.read_drawing_file`) directly to IR: polylines, affine ellipse
  arcs (exact `CIRCLE`/`ARC`/`ELLIPSE` recovery through a 2x2 SVD), filled arcs
  and triangle batches as solid `HATCH`, and text with cap-height sizing,
  line splitting and AIGDT symbol mapping. Inventor's internal centimetres are
  scaled to millimetres and disclosed (`IDW_UNITS_ASSUMED_CM`); multiple
  sheets are tiled along +X; raster view caches and image placements stay in
  `source.metadata.idw` with `IDW_IMAGE_NOT_IN_IR` / `IDW_VIEW_RASTER_ONLY`.
  `.idw` dispatches through `convert_file_to_ir` and the CLI; the new
  `convert_idw_file_to_ir` and `idw_document_to_ir` entry points are public.
- `source.format` accepts `idw`.
- The `idw` extra is intentionally not part of `all`: `inventor-kit` declares
  `cq-acis` (CadQuery/OCCT) for IPT/IAM geometry that the IDW path never
  imports. See the README for excluding it.

## 0.9.11

- SXF (SFC/P21) import keeps circles, arcs and ellipses as IR `CIRCLE` / `ARC` /
  `ELLIPSE` entities instead of sampled `LWPOLYLINE`s. `ezsxf` 0.1.2 attaches the
  exact curve to each curved path, already transformed through compound-figure
  placements; a placement with unequal X/Y ratios turns a circle into an
  `ELLIPSE` (principal axes are recovered from the conjugate semi-diameters) and
  a mirrored placement flips the arc direction. DXF output of SFC drawings is
  therefore editable again: a real drawing with 2,057 arcs previously produced
  2,057 polylines. Splines and clothoids are still approximated, and with
  `ezsxf` < 0.1.2 the importer falls back to the sampled polyline as before.
- DGN (V7) text: strings stored in MicroStation's 16-bit form (`FF FD` marker
  followed by one little-endian word per character, used by Japanese drawings for
  multi-byte text) are unpacked before codepage decoding. They previously came
  out as mojibake and pushed the whole file onto the latin-1 fallback.

## 0.9.9

- DXF import decodes AutoCAD text escapes for characters outside the file
  codepage: `\U+XXXX` (Unicode) and the MIF form `\M+nXXXX` (CJK codepages).
  Writers such as ezjww emit these for non-ASCII text in `ANSI_1252` files;
  they previously reached the IR as literal escape sequences.
- The `jww` / `all` extras accept `ezjww>=0.3` (ezjww 0.3.0 adds JWC reading;
  the JWW reader API used by the importer is unchanged).
- `source.format` accepts `jwc` so converters that read DOS-era Jw_cad JWC
  files through an intermediate DXF can record the real provenance.
- JWW import peak memory: `ImportOptions.entity_provenance=False`
  (`convert_file_to_ir(..., entity_provenance=False)`) omits per-entity
  `source`/`metadata.jww`, which were about 1.5 KB of the 2.5 KB per entity, and
  file imports release each raw `ezjww` entity as soon as it is converted instead
  of holding the raw document and the IR at the same time. A 32 MB JWW with
  570k entities previously needed more than 1.5 GB to render.

## 0.9.5

- DWG import now consumes native entities as a stream instead of retaining a
  second full source-entity list alongside the IR, while preserving top-level
  and block entity ordering and IDs.
- After file-based DWG import, the importer calls the optional
  `ezdwg.clear_decode_caches()` lifecycle hook so batch converters can release
  decoded per-file tables before rendering or exporting the IR.

## 0.9.4

- DWF import maps `POLYMARKER` entities (draw-polymarker opcodes, emitted by
  `ezdwf>=0.0.3`) to one IR `POINT` per marker.
- DWF zero-radius circles/arcs (dot markers) become IR `POINT` entities instead
  of failing with "ellipse axes must be non-zero".

## 0.9.3

- Binary DXF (`AutoCAD Binary DXF` sentinel, R12 1-byte and R13+ 2-byte
  group codes) is imported through the same pipeline (`DXF_BINARY_DETECTED`).
- A corrupted group-code line (e.g. two lines merged by a missing line break)
  no longer aborts the import: parsing skips to the next `0`/record-name pair
  (`DXF_STREAM_RESYNCED`).
- Two-vertex arc loops (AutoCAD's circular hatches: two half circles, or an
  arc plus its chord) are subdivided instead of being dropped.
- New entity mappings: `SOLID`/`TRACE` become solid HATCH loops in outline
  order, `LEADER` becomes its polyline path (`DXF_LEADER_APPROXIMATED`).

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
