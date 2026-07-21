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
| DWG | `.dwg` | implemented | `ezdwg>=0.9,<1` |
| SXF | `.sxf`, `.sfc`, `.p21` | implemented | `ezsxf>=0.1,<0.2` |

## JWW vertical slice

The JWW adapter consumes `ezjww.read_document()` rather than `read_dxf_document()`. This avoids losing source semantics before IR construction.

| JWW source | IR mapping | Fidelity handling |
| --- | --- | --- |
| `LINE` | `LINE` | direct |
| `ARC` / `CIRCLE` | `ARC`, `CIRCLE`, or `ELLIPSE` | flatness and tilt preserved |
| `POINT` | `POINT` | temporary marker state preserved |
| `TEXT` | `TEXT` | font, size, endpoint, and spacing metadata preserved |
| `SOLID` | solid `HATCH` | quadrilateral vertex order normalized |
| `CIRCLE_SOLID` | solid `HATCH` | polyline approximation recorded and diagnosed |
| `BLOCK` | `INSERT` plus block table | signed scale and rotation preserved |
| `DIMENSION` | `GENERIC DIMENSION` | line, text, SXF mode, auxiliary lines, and auxiliary points preserved in `definition.source_geometry` |

JWW pen color, pen style, pen width, layer/group state, source indices, file version, memo, and paper size are retained through IR style fields, tables, provenance, or JWW metadata.

## DWG adapter

The DWG adapter consumes `ezdwg.read()` and `Document.modelspace().query()` directly. Low-level public table decoders are used only to recover layer names/colors and block-header names.

| DWG source | IR mapping | Fidelity handling |
| --- | --- | --- |
| `LINE`, `CIRCLE`, `ARC`, `ELLIPSE`, `POINT` | matching IR entity | direct XY mapping |
| `LWPOLYLINE`, `POLYLINE_2D` | `LWPOLYLINE` | bulges and widths metadata retained; fitted interpolation is diagnosed |
| `SPLINE` | `SPLINE` | degree, controls, knots, weights, closure retained |
| `TEXT`, `MTEXT`, `TOLERANCE` | `TEXT` / `MTEXT` | placement and exposed formatting retained |
| `HATCH`, `SOLID`, `TRACE`, planar `3DFACE` | `HATCH` | polygon loops retained |
| `INSERT`, `MINSERT` | `INSERT` | signed scale retained; MINSERT array parameters remain metadata |
| `DIMENSION` | semantic `DIMENSION` | subtype and complete native geometry payload retained |
| block-owned entities | block table body | grouped when owner handles are exposed |

DWG units come from the `$INSUNITS` header variable (`ezdwg` >= 0.11 `Document.header_variables()`); mapped codes fill `header.units` (and therefore `$INSUNITS` on DXF export), the raw code is recorded in header metadata, and unmapped codes or R14 files (no `$INSUNITS`) fall back to `unknown` with the reason in metadata plus a diagnostic. Non-zero Z coordinates are projected to XY and reported. Unsupported 3D/presentation entities are skipped with aggregate diagnostics. Block base points are also not exposed; recovered block bodies use `[0, 0]` and state that limitation in metadata.

## SXF adapter

The SXF adapter parses either SFC or AP202/P21 and consumes `ezsxf._drawing.build_drawing()`. No DXF text is produced or reparsed.

| SXF drawing primitive | IR mapping | Fidelity handling |
| --- | --- | --- |
| two-point path | `LINE` | direct |
| other path | `LWPOLYLINE` | SFC curve source kinds carry `approximation`; P21 paths disclose the flattening boundary |
| fill with outer/inner rings | solid `HATCH` | holes retained |
| text | `TEXT` / `MTEXT` | layer, RGB color, line width, font, anchor, angle, width retained |
| marker/symbol insertion point | `POINT` | code, scale, and symbol name retained |
| SFC dimension feature | semantic `DIMENSION` | native feature plus grouped rendered world-space paths/text retained |

SFC `typed_features` allow dimension kinds and source curve kinds to be recovered. P21 currently exposes generic STEP entities but no equivalent typed feature model, so the adapter preserves rendered primitives and emits `SXF_P21_SEMANTICS_FLATTENED`. Externally defined hatch/symbol limitations reported by `ezsxf` are forwarded as diagnostics.

## Validation gates

The implementation was exercised against the current local upstream corpora with strict parsing and IR schema validation:

- DWG: 55 files spanning AC1014 through AC1027; 143 decoded source entities, 139 top-level entities, two recovered block-body entities, and zero failures.
- SFC: 20 files; 46,305 source/typed features, 49,929 IR entities, 1,491 semantic dimensions, zero skipped entities, and zero failures.
- P21: 20 files; 538,130 generic STEP entities, 57,094 IR entities, zero skipped entities, and zero failures.

These are development corpus observations, not format-wide coverage guarantees. The upstream corpora and parser versions should be rechecked when dependency bounds change.

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
