# Diagnostic code catalog

Diagnostic codes are stable machine-readable identifiers. They are part of the
public semantic-versioning contract:

- adding a code is a minor-version change;
- removing a code or changing its meaning is a major-version change;
- message wording may be clarified without changing the code's meaning;
- consumers should preserve unknown codes so newer producers remain usable.

The severity and action below are defaults. A diagnostic instance may add
entity/source identifiers and structured details.

| Code | Default severity | Action | Adapter | Emitted when |
|---|---|---|---|---|
| `DGN_3D_FLATTENED` | warning | projected | DGN import | A native drawing model reports a 3D design whose coordinates are projected to the IR XY plane. |
| `DGN_COMPLEX_FLATTENED` | warning | flattened | DGN import | A complex chain, complex shape, or text node was expanded while retaining parent provenance. |
| `DGN_CURVE_APPROXIMATED` | warning | approximated | DGN import | A type-11 control curve was represented as a polyline, or closed non-uniform B-spline knots remained metadata. |
| `DGN_ENCODING_DETECTED` | info | detected | DGN import | `encoding="auto"` selected ASCII, CP932, or Latin-1 by probing all DGN text elements. |
| `DGN_ENTITY_CONVERSION_FAILED` | error | skipped | DGN import | A malformed DGN entity was skipped. |
| `DGN_SHARED_CELL_UNRESOLVED` | warning | skipped | DGN import | V8 shared-cell instances were skipped because shared-cell definitions are not decoded yet. |
| `DGN_TEXT_DECODE_REPLACED` | warning | normalized | DGN import | DGN text bytes could not be decoded with the selected encoding. |
| `DGN_UNKNOWN_UNITS` | warning | - | DGN import | The DGN master-unit label had no IR unit mapping. |
| `DGN_UNSUPPORTED_ENTITY` | warning | skipped | DGN import | An unsupported DGN graphic entity was skipped. |
| `DGN_V8_LEVEL_TABLE_APPLIED` | info | preserved_metadata | DGN import | The V8 level table (`ezdgn` >= 0.2.8) was applied to the layers: `tables.layers[*].metadata.dgn` carries the level name, number and ByLevel line weight, style and color index; `details.undefined_levels` lists level IDs used by elements but missing from the table. |
| `DGN_V8_EXTRA_MODELS_SKIPPED` | warning | skipped | DGN import | A multi-model V8 file was reduced to the first model containing drawable entities. |
| `DWG_CURVE_APPROXIMATED` | warning | approximated | DWG import | Source geometry was approximated. |
| `DWG_DUPLICATE_BLOCK_NAME_RENAMED` | info | renamed | DWG import | Blocks that share their name with another block were renamed; the last one keeps the name, and dimensions reach their block through `definition.block`. |
| `DWG_ENTITY_CONVERSION_FAILED` | error | skipped | DWG import | A malformed DWG entity was skipped. |
| `DWG_HIDDEN_ATTRIBUTE_SKIPPED` | info | skipped | DWG import | Attribute definitions inside block definitions (templates; block references show their own attribute values) and invisible attributes without a block reference were skipped. Attributes of a block reference are kept on the `INSERT`, invisible ones with `visible: false`. |
| `DWG_HEADER_UNITS_UNREADABLE` | warning | - | DWG import | The DWG header variables could not be decoded for units. |
| `DWG_IMAGE_AXES_APPROXIMATED` | warning | approximated | DWG import | The pixel axes of an `IMAGE` are mirrored or not at a right angle; the `IMAGE` keeps its width, height and the rotation of the column axis only (the vectors stay in `metadata.dwg.image`). |
| `DWG_IMAGE_CLIP_IGNORED` | warning | approximated | DWG import | The clip boundary of an `IMAGE` was not applied; the `IMAGE` spans its full extent and the boundary stays in `metadata.dwg.image`. |
| `DWG_IMAGE_LINKED` | info | - | DWG import | `IMAGE` entities reference raster files by path (a DWG never embeds the pixels, `ezdwg` 0.16 or later decodes them); each keeps the path in `href`, the pixel size and placement vectors in `metadata.dwg.image`, and has no pixels. |
| `DWG_LAYOUT_PROMOTED` | info | promoted | DWG import | Model space holds no entity, so the active paper-space layout (or the first one with entities) became `entities`; `header.metadata.dwg.promoted_layout` names it. |
| `DWG_MINSERT_ARRAY_PRESERVED` | warning | preserved_metadata | DWG import | MINSERT array data was preserved as metadata. |
| `DWG_NONPLANAR_PROJECTED` | warning | projected | DWG import | Non-planar geometry was projected to XY. |
| `DWG_PAPERSPACE_ENTITY_SKIPPED` | info | skipped | DWG import | Paper-space entities were skipped. Emitted by packages before 0.10.5 only; they are kept in `layouts` now (`DWG_PAPERSPACE_LAYOUT_PRESERVED`). |
| `DWG_PAPERSPACE_LAYOUT_PRESERVED` | info | preserved_layout | DWG import | Paper-space entities (layout frames, title blocks) and viewports were kept in `layouts`, apart from the model-space `entities`. |
| `DWG_TOLERANCE_EXPLODED` | warning | exploded | DWG import | Feature control frames (`TOLERANCE`) were drawn as the lines of their frame and one `TEXT` per compartment; compartment widths are estimated. |
| `DWG_UNSUPPORTED_INSUNITS` | warning | normalized | DWG import | A `$INSUNITS` code without an IR units mapping fell back to unknown. |
| `DWG_UNRESOLVED_BLOCK_REFERENCE` | warning | - | DWG import | A referenced DWG block was not resolved. |
| `DWG_UNSUPPORTED_ENTITY` | warning | skipped | DWG import | An unsupported DWG entity was skipped. |
| `DWG_ZERO_INSERT_SCALE_NORMALIZED` | warning | normalized | DWG import | A zero insert scale was replaced. |
| `DWF_APPEARANCE_EFFECTS_FLATTENED` | warning | flattened | DWF import | Clipping, opacity-mask, or compositing effects remain metadata counts and are not applied to IR geometry. |
| `DWF_COLOR_GRADIENT_FLATTENED` | warning | flattened | DWF import | Per-vertex colors were retained only as metadata. |
| `DWF_CURVE_APPROXIMATED` | warning | approximated | DWF import | A path curve or sheared ellipse was sampled as a polyline. |
| `DWF_DRAWING_WARNING` | warning | - | DWF import | The native DWF parser reported a diagnostic. |
| `DWF_ENTITY_CONVERSION_FAILED` | error | skipped | DWF import | A malformed DWF entity was skipped. |
| `DWF_MIXED_SHEET_UNITS` | warning | - | DWF import | Sheets used different paper units, so coordinates were retained per sheet with unknown document units. |
| `DWF_MULTISHEET_FLATTENED` | warning | flattened | DWF import | Multiple sheets were placed in one IR modelspace with sheet metadata retained. |
| `DWF_UNSUPPORTED_ENTITY` | warning | skipped | DWF import | An unsupported DWF entity, such as a raster image, was skipped. |
| `DXF_BINARY_DETECTED` | info | detected | DXF import | The input carried the `AutoCAD Binary DXF` sentinel and was decoded from the binary group-code stream (`details.group_code_bytes` is 1 for R12, 2 for R13+). |
| `DXF_CONSTRAINTS_OMITTED` | warning | skipped | DXF export | IR-only constraints were omitted. |
| `DXF_DECODE_REPLACED` | warning | normalized | DXF import | Undecodable input bytes were replaced while decoding. |
| `DXF_DIMENSION_BLOCK_GENERATED` | info | normalized | DXF export | A required DIMENSION geometry block was generated. |
| `DXF_ENCODING_DETECTED` | info | detected | DXF import | The file encoding was selected. |
| `DXF_ENTITY_CONVERSION_FAILED` | error | skipped | DXF import | A malformed DXF entity (missing group codes, unparsable numbers, degenerate geometry) was skipped instead of rejecting the file. |
| `DXF_GENERIC_DIMENSION_DEFAULTED` | warning | normalized | DXF export | Missing GENERIC dimension text placement was defaulted. |
| `DXF_GENERIC_DIMENSION_EXPLODED` | info | exploded | DXF export | A GENERIC dimension was expanded to primitives. |
| `DXF_GENERIC_DIMENSION_GEOMETRY_MISSING` | warning | skipped | DXF export | A GENERIC dimension had no renderable geometry. |
| `DXF_GENERIC_DIMENSION_SKIPPED` | warning | skipped | DXF export | A GENERIC dimension was intentionally omitted. |
| `DXF_HATCH_EDGE_APPROXIMATED` | warning | approximated | DXF import | HATCH ellipse/spline boundary edges were approximated by polyline segments (line and arc edges stay exact). |
| `DXF_HATCH_LOOP_SKIPPED` | warning | skipped | DXF import | A HATCH boundary loop could not be reconstructed (unknown edge type or too few vertices) and was skipped. |
| `DXF_IMPORT_WARNING` | warning | - | DXF import | Legacy wrapper for a DXF parser warning. |
| `DXF_INSERT_TRANSFORM_OMITTED` | warning | skipped | DXF export | An affine INSERT transform was omitted. |
| `DXF_LAYOUT_OMITTED` | warning | skipped | DXF export | A paper-space layout other than the written one was omitted: the output holds one paper space (the active layout). |
| `DXF_LAYOUT_PROMOTED` | info | promoted | DXF import | Model space holds no entity, so the active paper-space layout (or the first one with entities) became `entities`; `header.metadata.dxf.promoted_layout` names it. |
| `DXF_LEADER_APPROXIMATED` | warning | approximated | DXF import | A LEADER entity was imported as an open polyline through its vertices; the arrowhead and the link to its annotation were omitted. |
| `DXF_MESH_SKIPPED` | warning | skipped | DXF import | A `POLYLINE` that is a polygon mesh or a polyface mesh (a 3D surface) was skipped; its `VERTEX` records are no path. |
| `DXF_NON_XY_PLANE_PROJECTED` | warning | projected | DXF import | Axis-aligned XZ/YZ WCS geometry was projected to IR XY, with CIRCLE/ARC OCS coordinates transformed into the same plane. |
| `DXF_PAPERSPACE_LAYOUT_PRESERVED` | info | preserved_layout | DXF import | Paper-space entities (group 67 = 1, and the bodies of `*Paper_Space<n>` blocks) and viewports were kept in `layouts`, apart from the model-space `entities`. |
| `DXF_POLYLINE_3D_PROJECTED` | warning | projected | DXF import | A 3D `POLYLINE` whose vertices leave the XY plane was projected to XY. |
| `DXF_R12_ELLIPSE_APPROXIMATED` | warning | approximated | DXF R12 export | ELLIPSE was approximated by a polyline. |
| `DXF_R12_HATCH_EXPLODED` | warning | exploded | DXF R12 export | HATCH was emitted as boundary polylines. |
| `DXF_R12_INSUNITS_OMITTED` | warning | skipped | DXF R12 export | INSUNITS was omitted from R12 output. |
| `DXF_R12_LINETYPE_SCALE_OMITTED` | warning | skipped | DXF R12 export | A per-entity linetype scale was omitted from R12 output. |
| `DXF_R12_LINEWEIGHT_OMITTED` | warning | skipped | DXF R12 export | Lineweight was omitted from R12 output. |
| `DXF_R12_LWPOLYLINE_EXPLODED` | info | exploded | DXF R12 export | LWPOLYLINE was emitted as POLYLINE records. |
| `DXF_R12_MTEXT_EXPLODED` | warning | exploded | DXF R12 export | MTEXT was emitted as one or more TEXT entities. |
| `DXF_R12_MTEXT_FORMATTING_NORMALIZED` | warning | normalized | DXF R12 export | MTEXT formatting codes were removed. |
| `DXF_R12_SPLINE_APPROXIMATED` | warning | approximated | DXF R12 export | SPLINE was approximated by a polyline. |
| `DXF_R12_TRUE_COLOR_APPROXIMATED` | warning | approximated | DXF R12 export | True color was approximated by an ACI color. |
| `DXF_R12_VIEWPORT_OMITTED` | warning | skipped | DXF R12 export | The viewports of the written layout were omitted from R12 output. |
| `DXF_STREAM_RESYNCED` | warning | skipped | DXF import | A group-code line could not be parsed (e.g. a missing line break merged two lines); the lines up to the next `0`/record-name pair were skipped and parsing resumed there. |
| `DXF_TABLE_DEFAULTED` | info | normalized | DXF export | A required table record was synthesized. |
| `DXF_TEXT_HEIGHT_DEFAULTED` | warning | normalized | DXF import | A TEXT/MTEXT record carried a non-positive height, which was replaced by a unit-based default (2.5 mm equivalent). |
| `DXF_TOLERANCE_EXPLODED` | warning | exploded | DXF import | A feature control frame (`TOLERANCE`) was drawn as the lines of its frame and one `TEXT` per compartment; compartment widths are estimated. |
| `DXF_TRAILING_DATA_IGNORED` | info | skipped | DXF import | Lines after the `0`/`EOF` marker were ignored. |
| `DXF_TRAILING_LINE_IGNORED` | warning | skipped | DXF import | The text ended with an unpaired line (truncated file or stray data) that was ignored. |
| `DXF_IMAGE_SKIPPED` | warning | skipped | DXF export | An `IMAGE` entity was not written: DXF R12 has no raster images, or the image has no positive width and height. |
| `DXF_IMAGE_REFERENCED` | warning | approximated | DXF export | An image the IR carried embedded (`data`) was written as an `IMAGE` that references its file name; a DXF never carries the pixels, so the file has to be placed next to the DXF. |
| `DXF_IMAGE_SIZE_ASSUMED` | warning | approximated | DXF export | The size of an image in pixels was not known (no DXF/DWG metadata, no readable PNG/GIF/BMP/JPEG header) and one pixel per drawing unit was assumed; the drawn size is kept. |
| `DXF_IMAGE_LINKED` | info | - | DXF import | An `IMAGE` entity references its raster file by path (a DXF never embeds the pixels); the `IMAGE` keeps the path in `href`, the DXF values in `metadata.dxf.image`, and has no pixels. |
| `DXF_IMAGE_DEF_MISSING` | warning | - | DXF import | The IMAGEDEF an `IMAGE` refers to (group 340) is not in OBJECTS; the placement is kept without a file path. |
| `DXF_IMAGE_CLIP_IGNORED` | warning | approximated | DXF import | The clip boundary of an `IMAGE` was not applied; the `IMAGE` spans its full extent and the boundary stays in `metadata.dxf.image`. |
| `DXF_IMAGE_AXES_APPROXIMATED` | warning | approximated | DXF import | The pixel axes of an `IMAGE` are mirrored or not at a right angle; the `IMAGE` keeps its width, height and the rotation of the column axis only (the vectors stay in `metadata.dxf.image`). |
| `DXF_UNKNOWN_ENTITY_SKIPPED` | warning | skipped | DXF export | An unknown IR entity kind was skipped. |
| `EXB_ATTRIBUTE_DEFINITIONS_SKIPPED` | info | skipped | EXB import | Attribute definitions (title block templates) were not converted; the values a block reference carries are kept as `attribute_texts` on the INSERT. |
| `EXB_BLOCK_SOLID_ATTACHED` | info | - | EXB import | A block definition's attached fill was converted as a solid HATCH inside the block. |
| `EXB_DIMENSION_FROM_CACHE` | info | flattened | EXB import | Dimensions were rebuilt from the display records CAXA cached (lines, arcs, arrow fills, text) in a block referenced by `definition.block`; the stored value and text are kept, the construction, associativity and tolerance semantics are not decoded. |
| `EXB_ELLIPSE_AXES_ASSUMED` | warning | approximated | EXB import | An ellipse's major axis was taken as the stored x axis rotated by the stored rotation. |
| `EXB_ENTITY_CONVERSION_FAILED` | error | skipped | EXB import | A malformed EXB entity was skipped. |
| `EXB_HATCH_BOUNDARY_SAMPLED` | warning | approximated | EXB import | Hatch or solid boundaries with arcs, ellipses, NURBS or bulges were sampled as polyline loops. |
| `EXB_HATCH_PATTERN_PRESERVED` | info | preserved_metadata | EXB import | The CAXA hatch pattern name, angle, scale and stored line definitions are kept in `metadata.exb.hatch`; the pattern lines are not regenerated. |
| `EXB_LAYOUTS_PRESERVED` | info | preserved_metadata | EXB import | Spaces after the first were kept as `layouts`. |
| `EXB_LAYOUT_PROMOTED` | warning | normalized | EXB import | The first space had no entities; the space with the most entities became the model entities. |
| `EXB_LINE_WIDTH_PRESET` | info | assumed | EXB import | Symbolic CAXA line widths (thin / mid / wide / double wide) were mapped to the default 0.25 / 0.35 / 0.5 / 0.7 mm. |
| `EXB_PAPER_DETECTED` | info | detected | EXB import | The paper size was measured from the `*FRAME…` block placed in the space and recorded in `header.metadata.exb.paper`. |
| `EXB_PARTIAL_READ` | warning | - | EXB import | `ezexb` read the drawing in partial mode: some records could not be decoded and the dimension semantics are not decoded. |
| `EXB_POLYLINE_WIDTH_DROPPED` | warning | approximated | EXB import | A polyline had segment widths, which the IR polyline does not carry. |
| `EXB_TEXT_ALIGNMENT_ASSUMED` | info | assumed | EXB import | Texts use an alignment code that was not verified against CAXA: 2 (top-right by the 3×3 grid) or an unknown code read as top-left. |
| `EXB_TEXT_ESCAPE_NORMALIZED` | info | normalized | EXB import | CAXA text escapes were rewritten: `%c` / `%d` / `%p` / `%x` as ⌀ ° ± ×, deviation (`\D…\|b;`) and fit (`\V…\|a;`) tolerances as DXF-style `\S上^下;` stacks. |
| `EXB_TEXT_FIT_WIDTH_FACTOR` | info | approximated | EXB import | A fit-to-box text got a `width_factor` derived from its stored box width, since CAXA compresses the glyphs to the box. |
| `EXB_TEXT_STYLE_UNRESOLVED` | warning | normalized | EXB import | Texts reference text style ids missing from the text style table; no style was assigned. |
| `EXB_TEXT_SYMBOL_DROPPED` | warning | skipped | EXB import | A surface roughness symbol embedded in text (`\R…;`) has no text form and was removed. |
| `EXB_TEXT_VERTICAL_IGNORED` | warning | approximated | EXB import | A vertical-writing text was converted as horizontal text; the writing direction is in `metadata.exb`. |
| `EXB_UNITS_ASSUMED_MM` | info | assumed | EXB import | An EXB drawing carries no unit; coordinates were taken as millimetres at real size (the drawing scale is `header.metadata.exb.paper_scale`). |
| `EXB_UNRESOLVED_LAYER` | warning | normalized | EXB import | Entities reference layer ids missing from the layer table; placeholder layer names `EXB_LAYER_<id>` were used. |
| `EXB_UNSUPPORTED_ENTITY` | warning | skipped | EXB import | Records of a type `ezexb` cannot decode (CAXA annotations such as roughness, geometric tolerance, chamfer and table objects) were skipped; `details.type_id` is the record GUID. |
| `EXB_ZERO_INSERT_SCALE_NORMALIZED` | warning | normalized | EXB import | A zero block reference scale was replaced with 1.0. |
| `IDW_CURVE_APPROXIMATED` | warning | approximated | IDW import | A curve whose ellipse parameters could not be recovered was sampled as a polyline. |
| `IDW_CURVE_EDGE_ON_LINE` | warning | approximated | IDW import | An edge-on projected circle (minor axis ~0 after XY projection) was represented as a line segment. |
| `IDW_DRAWING_WARNING` | warning | forwarded | IDW import | An `inventor-kit` drawing diagnostic (for example `drawing.unverified`) was forwarded with its upstream code in `details`. The instance severity follows the upstream diagnostic. |
| `IDW_ELEMENT_OMITTED` | warning | skipped | IDW import | `inventor-kit` omitted stored elements from the sheet display; the omission reason is the source kind. Elements hidden by a stored attribute are reported as info. |
| `IDW_ENTITY_CONVERSION_FAILED` | error | skipped | IDW import | A malformed IDW element was skipped in lenient mode. |
| `IDW_FILLED_CURVE_APPROXIMATED` | warning | approximated | IDW import | A filled arc (closed along its chord) was represented as a solid `HATCH` with a sampled polyline loop. |
| `IDW_IMAGE_NOT_IN_IR` | warning | skipped | IDW import | Image placements (raster view caches, logos) have no IR representation; their placements and descriptors remain in `source.metadata.idw`. |
| `IDW_MULTISHEET_TILED` | warning | flattened | IDW import | Multiple sheets were placed side by side along +X in one IR modelspace; offsets remain in `source.metadata.idw.sheets`. |
| `IDW_OUT_OF_SHEET_DROPPED` | warning | skipped | IDW import | Elements lying entirely outside the sheet (typically unclipped projected curves) were dropped. |
| `IDW_SHEET_UNAVAILABLE` | warning | skipped | IDW import | A sheet without a decodable stored display was skipped while other sheets were converted. |
| `IDW_STYLE_UNRESOLVED` | info | preserved_metadata | IDW import | Style attributes the parser could not interpret (for example undecoded layer dash patterns) are listed by reason and count in `details`. |
| `IDW_TEXT_MIRROR_IGNORED` | warning | preserved_metadata | IDW import | A text item's up vector indicates a mirrored placement; the mirror is recorded in metadata but not applied. |
| `IDW_TEXT_MULTILINE_SPLIT` | info | exploded | IDW import | A multi-line text item was split into one `TEXT` entity per line with a 1.2 x height line spacing. |
| `IDW_TEXT_SYMBOL_MAPPED` | info | normalized | IDW import | AIGDT symbol-font glyphs (`n` diameter, `x` depth) were mapped to Unicode characters. |
| `IDW_UNITS_ASSUMED_CM` | info | normalized | IDW import | Coordinates were scaled from Inventor's internal centimetres to millimetres; the physical unit is not verified by the parser. Warning when a sheet does not match a standard paper size. |
| `IDW_UNSUPPORTED_ELEMENT` | warning | skipped | IDW import | An element kind unknown to the adapter was skipped. |
| `IDW_VIEW_RASTER_ONLY` | warning | preserved_metadata | IDW import | Drawing views stored only as raster view caches have no vector geometry in the IR. |
| `JWS_DRAWING_SETTINGS_DEFAULTED` | info | normalized | JWS import | A JWS symbol stores no layer names, pen palette, line type patterns, paper size or memo; layers are named `<group>-<layer>` and line types use the Jw_cad defaults. |
| `JWS_IMAGE_REFERENCE_ONLY` | warning | - | JWS import | A `^@BM` image placement in a symbol refers to a file that a JWS cannot carry; the `IMAGE` entity keeps the path in `href` and has no pixels. |
| `JWS_SYMBOL_COORDINATES` | info | - | JWS import | The symbol's coordinates were kept in the paper millimetres of the drawing it was registered from; the insertion origin and the layer group scales are recorded in `header.metadata.jws` and were not applied. |
| `JWW_CURVE_APPROXIMATED` | warning | approximated | JWW / JWS import | Source geometry was approximated. |
| `JWW_DECODE_REPLACED` | warning | normalized | JWW / JWS import | `ezjww` replaced undecodable CP932 byte sequences in a JWW string with U+FFFD (details carry field and byte offset). |
| `JWW_ENTITY_CONVERSION_FAILED` | error | skipped | JWW / JWS import | A malformed JWW entity was skipped. |
| `JWW_ENTITY_LIST_TRUNCATED` | error | skipped | JWW import | `ezjww` could not read the main entity list to its end (truncated upload, unknown record layout, corrupt tag); entities parsed before the error were kept, the rest and all block definitions were skipped. |
| `JWW_IMAGE_DECODE_FAILED` | warning | skipped | JWW import | An embedded image could not be decompressed; the `IMAGE` keeps the path in `href` and has no pixels. |
| `JWW_IMAGE_EMBEDDED` | info | converted | JWW import | A `^@BM` image placement became an `IMAGE` entity with the file bytes decoded from the drawing's image archive into `data`. |
| `JWW_IMAGE_LINKED` | warning | - | JWW import | A `^@BM` image placement refers to a file that is not embedded in the drawing (external link, or a `%temp%` name missing from the archive); the `IMAGE` keeps the path in `href` and has no pixels. |
| `JWW_METADATA_SETTING_EXTRACTED` | info | preserved_metadata | JWW / JWS import | A sentinel-positioned Jw_cad internal setting record was retained under `header.metadata.jww.settings` instead of becoming drawing TEXT. |
| `JWW_PEN_TABLE_PRESERVED` | info | preserved_metadata | JWW import | The printer pen table of the file (the line width Jw_cad prints each pen color with, `ezjww` >= 0.7.1) was kept under `header.metadata.jww.pens`; entities with pen width 0 take their width from it. `details.widths_in_mm` counts the pens whose width could be expressed in millimetres. |
| `JWW_TEXT_HEIGHT_DEFAULTED` | warning | normalized | JWW / JWS import | A non-positive text height was replaced. |
| `JWW_UNRESOLVED_BLOCK_REFERENCE` | warning | - | JWW / JWS import | A referenced JWW block was not resolved. |
| `JWW_UNSUPPORTED_ENTITY` | warning | skipped | JWW / JWS import | An unsupported JWW entity was skipped. |
| `JWW_ZERO_BLOCK_SCALE_NORMALIZED` | warning | normalized | JWW / JWS import | A zero block scale was replaced. |
| `JWW_ZERO_FLATNESS_NORMALIZED` | warning | normalized | JWW / JWS import | A zero ellipse flatness was replaced. |
| `MI_ANNOTATION_FLATTENED` | warning | flattened | MI import | An MI annotation such as a leader was represented by simpler IR geometry while its source semantics remain in metadata. |
| `MI_CURVE_APPROXIMATED` | warning | approximated | MI import | An MI spline fallback or curved hatch boundary was sampled as a polyline. |
| `MI_ENTITY_CONVERSION_FAILED` | error | skipped | MI import | A malformed typed MI entity was skipped in lenient mode. |
| `MI_INSTANCE_SKIPPED` | error | skipped | MI import | An unresolved, cyclic, or non-affine MI assembly instance was skipped in lenient mode. |
| `MI_PARSER_DIAGNOSTIC` | warning | forwarded | MI import | An `ezmi2d` diagnostic was forwarded with its upstream code, action, and source span in `details`. The instance severity follows the upstream diagnostic. |
| `MI_SOURCE_SEMANTICS_PRESERVED` | warning | preserved_metadata | MI import | MI semantics without an exact operational IR field, such as unscaled lineweight values or hatch pattern definitions, were retained in metadata. |
| `MI_UNKNOWN_UNITS` | warning | preserved_metadata | MI import | The MI drawing-unit label had no IR unit mapping. |
| `MI_UNSUPPORTED_ANNOTATION` | warning | skipped | MI import | An MI annotation without a lossless IR equivalent was skipped while referenced graphics remained available. |
| `MI_UNSUPPORTED_ENTITY` | warning | skipped | MI import | An `ezmi2d.UnsupportedEntity` raw fallback had no typed mapping and was skipped. |
| `PDF_PAGES_TILED` | warning | flattened | PDF import | Multiple pages were placed side by side in one IR modelspace with page offsets retained in metadata. |
| `PDF_PAGE_ROTATED` | info | normalized | PDF import | The page /Rotate was applied so coordinates follow the page as displayed. |
| `PDF_LAYERS_FROM_OCG` | info | detected | PDF import | Layers were taken from optional content group names. |
| `PDF_POLYLINE_ARC_REFITTED` | info | normalized | PDF import | Evenly turning polyline runs that trace a circle were replaced by arcs or circles. |
| `PDF_POINT_FROM_TINY_SHAPE` | info | normalized | PDF import | Closed shapes smaller than 0.1 mm (plotted point markers) were read as points. |
| `PDF_FILL_AS_LINE` | info | normalized | PDF import | Thin filled rectangles were read as lines with the rectangle width as lineweight. |
| `PDF_HATCH_FLATTENED` | warning | approximated | PDF import | Curved boundaries of filled shapes were flattened into polygon hatch loops. |
| `PDF_TEXT_AS_OUTLINES` | warning | flattened | PDF import | Many small filled curve shapes look like text converted to outlines; they stay hatches. |
| `PDF_CLIP_APPROXIMATED` | warning | approximated | PDF import | Entities crossing a non-rectangular clip were kept whole. |
| `PDF_CLIPPED_OUT` | info | skipped | PDF import | Entities entirely outside their clip were dropped. |
| `PDF_TEXT_INVISIBLE_SKIPPED` | info | skipped | PDF import | Invisible text (render mode 3, typically an OCR layer) was skipped. |
| `PDF_TEXT_UNMAPPED_SKIPPED` | warning | skipped | PDF import | Text characters without a Unicode mapping were skipped. |
| `PDF_IMAGE_PLACEHOLDER` | warning | preserved_metadata | PDF import | A raster image keeps its placement only; pixels were not embedded. |
| `PDF_IMAGE_TOO_LARGE` | warning | preserved_metadata | PDF import | A raster image exceeded the embedding pixel limit and keeps its placement only. |
| `PDF_IMAGE_EMBED_FAILED` | warning | preserved_metadata | PDF import | A raster image could not be decoded and keeps its placement only. |
| `PDF_UNSUPPORTED_OBJECT` | warning | skipped | PDF import | An unsupported page object, such as a shading, was skipped. |
| `PDF_FORM_DEPTH_EXCEEDED` | warning | skipped | PDF import | A form XObject nested deeper than the supported depth was skipped. |
| `PDF_ANNOTATIONS_SKIPPED` | info | skipped | PDF import | Annotations were skipped; only page content is read. |
| `PDF_DUPLICATE_REMOVED` | info | skipped | PDF import | Exact duplicate lines, arcs or circles were removed. |
| `PDF_SCALE_ESTIMATED` | info | normalized | PDF import | The plot scale recovered from dimension texts was applied; coordinates are model millimetres. |
| `PDF_SCALE_APPLIED` | info | normalized | PDF import | The requested plot scale was applied; coordinates are model millimetres. |
| `PDF_SCALE_UNKNOWN` | info | - | PDF import | Too few dimension texts agreed on a plot scale; coordinates stay paper millimetres. |
| `PDF_ENTITY_CONVERSION_FAILED` | error | skipped | PDF import | A PDF page object that could not be converted was skipped. |
| `SXF_CURVE_APPROXIMATED` | warning | approximated | SXF import | Source geometry was approximated. |
| `SXF_DIMENSION_CONVERSION_FAILED` | error | skipped | SXF import | A malformed SXF dimension was skipped. |
| `SXF_DRAWING_WARNING` | warning | - | SXF import | The SXF drawing backend reported a warning. |
| `SXF_P21_SEMANTICS_FLATTENED` | warning | flattened | SXF import | P21 semantics were flattened to drawing primitives. |
| `SXF_PARTIAL_DRAWING_AMBIGUOUS` | warning | flattened | SXF import | Entities of a compound figure placed in several partial drawings were left without a partial drawing. |
| `SXF_PARTIAL_DRAWING_FLATTENED` | info | flattened | SXF import | Partial drawings were flattened to sheet coordinates; their placements are kept in `header.metadata.sxf.partial_drawings` and each entity names its partial drawing in `metadata.sxf.partial_drawing`. |
| `SXF_PRIMITIVE_CONVERSION_FAILED` | error | skipped | SXF import | A malformed SXF primitive was skipped. |
| `SXF_PRIMITIVE_SKIPPED` | warning | skipped | SXF import | An unsupported SXF primitive was skipped. |

## Python access

Use `cad2d_ir.diagnostics.ALL_CODES` for exhaustive CI checks and
`cad2d_ir.diagnostics.DIAGNOSTIC_CODES` for catalog metadata.

## HP-GL diagnostics

| Code | Severity | Action | Condition |
|---|---|---|---|
| `HPGL_CLIP_APPROXIMATED` | warning | approximated | Rectangle clipping applies to centerlines; painted stroke edges and clipped caps may differ. |
| `HPGL_CURVE_APPROXIMATED` | warning | approximated | A clipped/degenerate curve or polygon boundary uses core preview vertices. |
| `HPGL_DOT_APPROXIMATED` | warning | approximated | A pen dot uses POINT display semantics. |
| `HPGL_DXF_LAYOUT_METADATA_OMITTED` | warning | skipped | The current DXF writer does not serialize layout name or paper setup. |
| `HPGL_DXF_WIDTH_APPROXIMATED` | warning | approximated | HP-GL direct DXF export maps arbitrary pen widths to supported DXF lineweights; IR keeps original widths. |
| `HPGL_FILL_OMITTED` | warning | skipped | A collinear fill, a fill crossing its clip, or a nonzero winding fill is omitted because IR HATCH cannot express that case safely. |
| `HPGL_HATCH_EXPLODED` | warning | exploded | Hatch/crosshatch fill uses explicit core paint segments. |
| `HPGL_LINE_STYLE_APPROXIMATED` | warning | approximated | Nominal dash lengths are retained; per-path phase, adaptive repeats, caps or joins may differ. |
| `HPGL_PAGE_PROMOTED` | warning | normalized | The requested page became the model entities and its page metadata moved to header.metadata.hpgl; other pages remain layouts. |
| `HPGL_SOURCE_APPROXIMATION` | warning | approximated | An entity depends on an approximate source label/cursor; per-entity approximation metadata retains this marker. |
| `HPGL_SOURCE_DIAGNOSTIC` | warning | source_action | ezhpgl reports a source error, approximation or known omission; details retain its code and original byte span. |
| `HPGL_TEXT_APPROXIMATED` | warning | approximated | Positioned glyphs use substitute-font TEXT; reflected glyphs use block INSERT. |
| `HPGL_TEXT_CLIP_APPROXIMATED` | warning | approximated | In label mode a label line crossing its clip is kept whole; IR TEXT has no clip. |
| `HPGL_TEXT_CLIP_OMITTED` | warning | skipped | A glyph not wholly inside its clip envelope is omitted; original label bytes remain in layout metadata. |
| `HPGL_TEXT_ENCODING_GUESSED` | warning | approximated | In label mode label bytes outside Roman-8 (0x80-0x9F) were decoded as Shift_JIS (cp932); the original bytes remain in layout metadata. |

## iCAD SX diagnostics

`cad2d-ir[icad]` composes the 2D drawing area of an iCAD SX file from the
saved views (see [the ICD adapter](IMPORTERS.md#icad-sx-adapter)).

| Code | Severity | Action | Condition |
|---|---|---|---|
| `ICD_2D_UNITS_ASSUMED_MM` | info | assumed | iCAD SX stores view-local coordinates in millimetres; the sheet was composed in millimetres. |
| `ICD_2D_VIEW_PLACED` | info | normalized | 2D views were placed on the sheet by their saved placement records (origin and scale); `details.views` lists them. |
| `ICD_2D_PLACEMENT_MISSING` | warning | approximated | A 2D view has no placement record in the 2D global view; it was placed at the sheet origin with the scale of its header. |
| `ICD_2D_REGISTERED_PART` | info | normalized | The file is a registered part (a 2D library symbol); its view was read as the drawing at scale 1. |
| `ICD_2D_SCALE_APPLIED` | info | normalized | The sheet was divided by the dominant view scale so the drawing is at real size; the denominator is `header.metadata.icd.paper_scale`. |
| `ICD_2D_MIXED_SCALES` | warning | approximated | Views drawn at a scale other than the dominant one keep their place on the sheet but are not at real size; `details.scales` lists the other scales. |
| `ICD_2D_SHEET_DETECTED` | info | detected | The paper was taken from the extent of the 2D global view (an ISO A size, or the smallest one fitted around the frame) or from the geometry bounds; it is `header.metadata.icd.sheet`. |
| `ICD_2D_NO_VIEWS` | warning | skipped | The file has no 2D view (a 3D model only); the document is empty. |
| `ICD_2D_TYPE_UNSUPPORTED` | warning | skipped | Saved records of types icadkit does not decode (symbols, projection controls, legacy text layouts) were skipped; `details.types` counts them per raw type. |
| `ICD_2D_ENTITY_INVALID` | warning | skipped | A saved entity with invalid or non-finite geometry was skipped. |
| `ICD_2D_TEXT_UNPLACED` | warning | skipped | A text run has no saved line origin (rotated or unobserved layout) and was skipped. |
| `ICD_2D_TEXT_LAYOUT_PARTIAL` | info | approximated | A text with a partially qualified layout uses its saved origin and default metrics. |
| `ICD_2D_TEXT_VERTICAL_SPLIT` | info | approximated | Vertical writing was split into one `TEXT` per character stacked downwards. |
| `ICD_2D_DIMENSION_FROM_SAVED` | info | flattened | Dimensions were rebuilt from the items iCAD SX saved (arrowheads as solid fills, dimension and extension lines, the value text) in a block referenced by `definition.block`; the construction semantics are not decoded. |
| `ICD_2D_DIMENSION_PARTIAL` | warning | approximated | Angle, leader and unobserved dimension layouts keep their saved items only; the kind is `GENERIC`. |
| `ICD_2D_HATCH_AS_LINES` | info | flattened | Hatches were converted to the parallel lines iCAD SX saved (`metadata.icd.hatch`); the boundary and pattern are not kept as a `HATCH`. |
| `ICD_2D_LINE_WIDTH_ASSUMED` | info | assumed | Line width codes were mapped to provisional millimetre widths (`LINE_WIDTH_MM`: 2 = 0.25 mm, 3 = 0.35 mm, …). |
| `ICD_2D_LINE_STYLE_UNKNOWN` | warning | approximated | Entities with an unobserved line style code were drawn as continuous lines; `details.codes` counts them. |
| `ICD_2D_COLOR_PALETTE` | info | skipped | Colours are iCAD SX palette indices (kept in `metadata.icd.color_index`) and were not mapped to RGB. |
