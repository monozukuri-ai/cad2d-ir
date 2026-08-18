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
| `DGN_V8_EXTRA_MODELS_SKIPPED` | warning | skipped | DGN import | A multi-model V8 file was reduced to the first model containing drawable entities. |
| `DWG_CURVE_APPROXIMATED` | warning | approximated | DWG import | Source geometry was approximated. |
| `DWG_ENTITY_CONVERSION_FAILED` | error | skipped | DWG import | A malformed DWG entity was skipped. |
| `DWG_HEADER_UNITS_UNREADABLE` | warning | - | DWG import | The DWG header variables could not be decoded for units. |
| `DWG_MINSERT_ARRAY_PRESERVED` | warning | preserved_metadata | DWG import | MINSERT array data was preserved as metadata. |
| `DWG_NONPLANAR_PROJECTED` | warning | projected | DWG import | Non-planar geometry was projected to XY. |
| `DWG_PAPERSPACE_ENTITY_SKIPPED` | info | skipped | DWG import | Paper-space entities (layout frames, viewports, title blocks) were skipped; the IR represents model space. |
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
| `DXF_CONSTRAINTS_OMITTED` | warning | skipped | DXF export | IR-only constraints were omitted. |
| `DXF_DECODE_REPLACED` | warning | normalized | DXF import | Undecodable input bytes were replaced while decoding. |
| `DXF_DIMENSION_BLOCK_GENERATED` | info | normalized | DXF export | A required DIMENSION geometry block was generated. |
| `DXF_ENCODING_DETECTED` | info | detected | DXF import | The file encoding was selected. |
| `DXF_GENERIC_DIMENSION_DEFAULTED` | warning | normalized | DXF export | Missing GENERIC dimension text placement was defaulted. |
| `DXF_GENERIC_DIMENSION_EXPLODED` | info | exploded | DXF export | A GENERIC dimension was expanded to primitives. |
| `DXF_GENERIC_DIMENSION_GEOMETRY_MISSING` | warning | skipped | DXF export | A GENERIC dimension had no renderable geometry. |
| `DXF_GENERIC_DIMENSION_SKIPPED` | warning | skipped | DXF export | A GENERIC dimension was intentionally omitted. |
| `DXF_IMPORT_WARNING` | warning | - | DXF import | Legacy wrapper for a DXF parser warning. |
| `DXF_INSERT_TRANSFORM_OMITTED` | warning | skipped | DXF export | An affine INSERT transform was omitted. |
| `DXF_R12_ELLIPSE_APPROXIMATED` | warning | approximated | DXF R12 export | ELLIPSE was approximated by a polyline. |
| `DXF_R12_HATCH_EXPLODED` | warning | exploded | DXF R12 export | HATCH was emitted as boundary polylines. |
| `DXF_R12_INSUNITS_OMITTED` | warning | skipped | DXF R12 export | INSUNITS was omitted from R12 output. |
| `DXF_R12_LINEWEIGHT_OMITTED` | warning | skipped | DXF R12 export | Lineweight was omitted from R12 output. |
| `DXF_R12_LWPOLYLINE_EXPLODED` | info | exploded | DXF R12 export | LWPOLYLINE was emitted as POLYLINE records. |
| `DXF_R12_MTEXT_EXPLODED` | warning | exploded | DXF R12 export | MTEXT was emitted as one or more TEXT entities. |
| `DXF_R12_MTEXT_FORMATTING_NORMALIZED` | warning | normalized | DXF R12 export | MTEXT formatting codes were removed. |
| `DXF_R12_SPLINE_APPROXIMATED` | warning | approximated | DXF R12 export | SPLINE was approximated by a polyline. |
| `DXF_R12_TRUE_COLOR_APPROXIMATED` | warning | approximated | DXF R12 export | True color was approximated by an ACI color. |
| `DXF_TABLE_DEFAULTED` | info | normalized | DXF export | A required table record was synthesized. |
| `DXF_UNKNOWN_ENTITY_SKIPPED` | warning | skipped | DXF export | An unknown IR entity kind was skipped. |
| `JWW_CURVE_APPROXIMATED` | warning | approximated | JWW import | Source geometry was approximated. |
| `JWW_ENTITY_CONVERSION_FAILED` | error | skipped | JWW import | A malformed JWW entity was skipped. |
| `JWW_TEXT_HEIGHT_DEFAULTED` | warning | normalized | JWW import | A non-positive text height was replaced. |
| `JWW_UNRESOLVED_BLOCK_REFERENCE` | warning | - | JWW import | A referenced JWW block was not resolved. |
| `JWW_UNSUPPORTED_ENTITY` | warning | skipped | JWW import | An unsupported JWW entity was skipped. |
| `JWW_ZERO_BLOCK_SCALE_NORMALIZED` | warning | normalized | JWW import | A zero block scale was replaced. |
| `JWW_ZERO_FLATNESS_NORMALIZED` | warning | normalized | JWW import | A zero ellipse flatness was replaced. |
| `MI_ANNOTATION_FLATTENED` | warning | flattened | MI import | An MI annotation such as a leader was represented by simpler IR geometry while its source semantics remain in metadata. |
| `MI_CURVE_APPROXIMATED` | warning | approximated | MI import | An MI spline fallback or curved hatch boundary was sampled as a polyline. |
| `MI_ENTITY_CONVERSION_FAILED` | error | skipped | MI import | A malformed typed MI entity was skipped in lenient mode. |
| `MI_INSTANCE_SKIPPED` | error | skipped | MI import | An unresolved, cyclic, or non-affine MI assembly instance was skipped in lenient mode. |
| `MI_PARSER_DIAGNOSTIC` | warning | forwarded | MI import | An `ezmi2d` diagnostic was forwarded with its upstream code, action, and source span in `details`. The instance severity follows the upstream diagnostic. |
| `MI_SOURCE_SEMANTICS_PRESERVED` | warning | preserved_metadata | MI import | MI semantics without an exact operational IR field, such as unscaled lineweight values or hatch pattern definitions, were retained in metadata. |
| `MI_UNKNOWN_UNITS` | warning | preserved_metadata | MI import | The MI drawing-unit label had no IR unit mapping. |
| `MI_UNSUPPORTED_ANNOTATION` | warning | skipped | MI import | An MI annotation without a lossless IR equivalent was skipped while referenced graphics remained available. |
| `MI_UNSUPPORTED_ENTITY` | warning | skipped | MI import | An `ezmi2d.UnsupportedEntity` raw fallback had no typed mapping and was skipped. |
| `SXF_CURVE_APPROXIMATED` | warning | approximated | SXF import | Source geometry was approximated. |
| `SXF_DIMENSION_CONVERSION_FAILED` | error | skipped | SXF import | A malformed SXF dimension was skipped. |
| `SXF_DRAWING_WARNING` | warning | - | SXF import | The SXF drawing backend reported a warning. |
| `SXF_P21_SEMANTICS_FLATTENED` | warning | flattened | SXF import | P21 semantics were flattened to drawing primitives. |
| `SXF_PRIMITIVE_CONVERSION_FAILED` | error | skipped | SXF import | A malformed SXF primitive was skipped. |
| `SXF_PRIMITIVE_SKIPPED` | warning | skipped | SXF import | An unsupported SXF primitive was skipped. |

## Python access

Use `cad2d_ir.diagnostics.ALL_CODES` for exhaustive CI checks and
`cad2d_ir.diagnostics.DIAGNOSTIC_CODES` for catalog metadata.
