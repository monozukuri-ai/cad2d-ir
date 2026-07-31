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
- DGN: three V7 2D files; 321 top-level IR entities and zero conversion failures. The V7 3D seed was rejected explicitly at the documented parser boundary.
- DGN V8: the ODA-authored GDAL fixture (53 drawable source entities across 14 kinds, 3D model); 43 strict-mode top-level IR entities plus 4 block-body entities in 2 blocks, zero conversion failures, and explicit skip diagnostics for the dimension anchor and the shared-cell instance.
- DWF: seven supported DWF 6 package/DWFx files; 24,360 IR entities and zero conversion failures. Standalone W2D 00.30 and 00.50 files were recognized and rejected explicitly as unsupported versions.
- SFC: 20 files; 46,305 source/typed features, 49,929 IR entities, 1,491 semantic dimensions, zero skipped entities, and zero failures.
- P21: 20 files; 538,130 generic STEP entities, 57,094 IR entities, zero skipped entities, and zero failures.

Every successful DGN/DWF result above also passed strict JSON Schema validation. These are development corpus observations, not format-wide coverage guarantees. The upstream corpora and parser versions should be rechecked when dependency bounds change.

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
