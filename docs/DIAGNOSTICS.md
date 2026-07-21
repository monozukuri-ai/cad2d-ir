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
| `DWG_CURVE_APPROXIMATED` | warning | approximated | DWG import | Source geometry was approximated. |
| `DWG_ENTITY_CONVERSION_FAILED` | error | skipped | DWG import | A malformed DWG entity was skipped. |
| `DWG_HEADER_UNITS_UNREADABLE` | warning | - | DWG import | The DWG header variables could not be decoded for units. |
| `DWG_MINSERT_ARRAY_PRESERVED` | warning | preserved_metadata | DWG import | MINSERT array data was preserved as metadata. |
| `DWG_NONPLANAR_PROJECTED` | warning | projected | DWG import | Non-planar geometry was projected to XY. |
| `DWG_UNSUPPORTED_INSUNITS` | warning | normalized | DWG import | A `$INSUNITS` code without an IR units mapping fell back to unknown. |
| `DWG_UNRESOLVED_BLOCK_REFERENCE` | warning | - | DWG import | A referenced DWG block was not resolved. |
| `DWG_UNSUPPORTED_ENTITY` | warning | skipped | DWG import | An unsupported DWG entity was skipped. |
| `DWG_ZERO_INSERT_SCALE_NORMALIZED` | warning | normalized | DWG import | A zero insert scale was replaced. |
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
| `SXF_CURVE_APPROXIMATED` | warning | approximated | SXF import | Source geometry was approximated. |
| `SXF_DIMENSION_CONVERSION_FAILED` | error | skipped | SXF import | A malformed SXF dimension was skipped. |
| `SXF_DRAWING_WARNING` | warning | - | SXF import | The SXF drawing backend reported a warning. |
| `SXF_P21_SEMANTICS_FLATTENED` | warning | flattened | SXF import | P21 semantics were flattened to drawing primitives. |
| `SXF_PRIMITIVE_CONVERSION_FAILED` | error | skipped | SXF import | A malformed SXF primitive was skipped. |
| `SXF_PRIMITIVE_SKIPPED` | warning | skipped | SXF import | An unsupported SXF primitive was skipped. |

## Python access

Use `cad2d_ir.diagnostics.ALL_CODES` for exhaustive CI checks and
`cad2d_ir.diagnostics.DIAGNOSTIC_CODES` for catalog metadata.
