# Paper-space CAD conversion

The M6 adapter lives in **cad2d-ir**, which depends on ezhpgl through its optional
`hpgl` extra. ezhpgl and its Rust core have no dependency on cad2d-ir or ezdxf.
The adapter maps the Rust-generated plot model; it does not parse HP-GL again.

## Install the local builds

These are development builds, not registry releases. Build both packages, then
install their wheels into a fresh environment:

```bash
# In ezhpgl
maturin build --release --locked --out dist
# In the adjacent cad2d-ir checkout containing the HP-GL adapter
uv build
# In an environment of your choice, install both wheel paths together:
uv pip install /path/to/ezhpgl/dist/ezhpgl-*.whl /path/to/cad2d-ir/dist/cad2d_ir-*.whl
```

cad2d-ir's development lock uses the adjacent `../ezhpgl` source checkout until
the initial registry release. In that checkout, `uv sync --extra hpgl --dev`
builds the native extension (Rust and maturin are required). The wheel metadata
contains the versioned `ezhpgl>=0.1.0a1,<0.2` extra, never that local path.
Registry installation and remote CI remain a release gate after publication;
the current lock requires the sibling source even when used by CI.

## Convert a plot

```python
import ezhpgl
from cad2d_ir import convert_hpgl_file_to_ir, convert_hpgl_file_to_dxf

profile = ezhpgl.DeviceProfile(plot_size_mm=(200, 100), paper_size_mm=(210, 297))
result = convert_hpgl_file_to_ir("drawing.plt", profile=profile)
assert result.document["entities"] == []  # plot content is in layouts
for page in result.document["layouts"]:
    print(page["name"], page["paper"], len(page["entities"]))
print(result.statistics, [d.as_dict() for d in result.diagnostics])

# Python page indices are zero based; all pages remain in output.imported.
output = convert_hpgl_file_to_dxf("drawing.plt", page=0, profile=profile)
with open("page1.dxf", "w", encoding="utf-8") as stream:
    stream.write(output.exported.dxf_text)
```

```bash
# Every page -> one IR document
cad2d-ir import drawing.plt --format hpgl --pretty -o drawing.ir.json
# One selected page -> DXF; CLI page numbers are one based
cad2d-ir hpgl2dxf drawing.plt --page 1 -o page1.dxf \
  --ir-output drawing.ir.json --report page1-report.json
# Device-dependent plot area, if required by the input commands
cad2d-ir hpgl2dxf drawing.plt --page 2 --plot-size 200 100 \
  --paper-size 210 297 -o page2.dxf
```

`hpgl2dxf` requires `--page` for multi-page inputs. Exit status is 0 for complete,
1 for a written partial/approximate result, and 2 for an error. Read both import
and export diagnostics in the report. The current DXF writer does not serialize
the layout name and paper setup, so it reports that loss and returns status 1.
`--lenient` returns the valid prefix at the first uncertain HP-GL command. Known
font approximations and container omissions are nonfatal even in strict mode.
Resource limits always fail; no success is reported for a truncated budget.

## Mapping and bounds

Each `PlotPage` becomes a separate `layouts` entry in millimetres, with operation
order intact. No plot is tiled into model space and no original CAD scale is
inferred. `model_page=<n>` (zero based) makes that page's entities the model
entities and moves its page metadata (plot size, paper, operations) to
`header.metadata.hpgl`, reported as `HPGL_PAGE_PROMOTED`; the other pages stay
layouts. Unknown paper sizes remain unknown; plot extents are separate metadata.
Input suffixes `.hpgl`, `.hpg`, `.plt`, and `.pcl` select the adapter, but ezhpgl
still inspects the contents. An unrelated file named `.plt` is rejected.

| Plot geometry | IR | Additional conversion limits |
| --- | --- | --- |
| Lines, polylines | `LINE`, `LWPOLYLINE` | Rectangular clipping trims centerlines; stroke edges/caps can differ |
| Circular/elliptical arcs | `CIRCLE`, `ARC`, `ELLIPSE` | Unclipped analytic geometry retained; clipped/degenerate curves use bounded preview vertices |
| Cubic Bézier | degree-3 `SPLINE` | Control points and clamped knots retained; clipped curves use preview vertices |
| Even-odd solid polygons | solid `HATCH` | Holes retained; curved boundaries use preview vertices |
| Nonzero or clip-crossing solid fills | omitted with diagnostic | No guessed winding conversion or arbitrary polygon clipping |
| Collinear solid fills | omitted with diagnostic | Device-specific stroke fallback is outside this adapter |
| Hatch/crosshatch | explicit lines | Core hatch segments retained, with nominal pen attributes |
| Labels | per-glyph substitute-font `TEXT`; mirrored `INSERT` blocks | Font metrics/outlines are approximate; glyphs crossing/outside their clip are omitted |
| Labels with `label_mode="label"` | one `TEXT` per label line from its first glyph cell | Lines are grouped by glyph geometry (LF/CR); bytes outside Roman-8 are re-decoded as Shift_JIS (`HPGL_TEXT_ENCODING_GUESSED`); a line crossing its clip is kept whole |
| Pen dots / `LT0` | `POINT` | CAD point-display semantics differ from physical pen marks; the pen-down mark at the start of a drawn vector is not repeated as a POINT |

Pen identity maps to `HPGL_PEN_<number>` layers; these are not recovered CAD
layers. Color, pen width, and nominal dash patterns are retained. Dash phase,
adaptive patterns, caps and joins have explicit approximation diagnostics.
DXF export rounds widths to supported lineweights; IR keeps the original width.
Entity order is retained through serialization; application-specific display or
plot behavior is not independently guaranteed. R12 output uses the existing
cad2d-ir exporter and can introduce additional diagnosed approximations.

Layout `metadata.hpgl` retains ordered operations, original byte spans, command
indices, clip windows, line attributes, and raw label bytes/attributes. Entity
`source.metadata` identifies its page, operation, pen and source span. Document
`source.metadata.hpgl` carries completeness, original diagnostic codes, input
events and statistics; `source.sha256` identifies the complete input. The
`entity_provenance=False` option omits per-entity provenance only. Per-operation
and document metadata remain available; `operation_metadata=False` omits the
operation and label tables (they grow with the plot).

The adapter defaults to at most 200,000 emitted entities, including block
contents; `max_entities` changes that limit. Input bytes, parser commands, preview
vertices and hatch output are separately bounded by `ezhpgl.Limits` and
`ezhpgl.GeometryLimits`. CLI exposes `--max-input-bytes` and `--max-entities`.
Statistics count every emitted entity, including mirrored-label block contents.

The generic `cad2d-ir ir2dxf` command retains its existing single-layout behavior.
Use `hpgl2dxf --page` for explicit selection and HP-GL-specific width/loss handling.
Keep the IR and diagnostic report alongside DXF; the DXF file alone does not carry
all source metadata or losses.

## Installed-package qualification

On Linux, install ezdxf as an independent reader and run from the ezhpgl checkout:

```bash
uv pip install ezdxf
python scripts/check_cad2d_integration.py --examples examples --output /tmp/hpgl-check
```

The script requires site-packages imports, validates the packaged JSON schema,
checks page/entity order and line endpoints, reads each page's DXF with ezdxf,
and requires zero audit errors and fixes. It also measures a synthetic 10,000
command import and tests its output cap. `--corpus PATH` audits an already fetched
manifest and preserves upstream hashes. Synthetic fixtures and upstream files
are reported separately. This establishes independent DXF-reader compatibility,
not native CAD/plotter fidelity or universal real-world PLT support.
