"""eDrawings (.edrw) drawing import.

The synthetic streams below follow the HSF grammar edrawings-kit reads (segments,
user options, modelling matrices, primitives, visibility, colours, fonts and
text anchors), shaped like the two writers seen in the field: SOLIDWORKS (one
sheet with a size, views placed by a scaled ``2dmodel`` matrix, texts as text)
and eDrawings' DXF/DWG Reader (a ``Model`` sheet beside the layouts, sheet
coordinates, colours in segment names, text stroked into polylines). The
fixtures in ``tests/data/edrw`` are edrawings-kit's self-authored drawings
exported by eDrawings 2026 (see the README there); eDrawings-authored corpus
files run through the opt-in gate at the end.
"""

from __future__ import annotations

import hashlib
import io
import math
import os
import struct
import zipfile
from pathlib import Path

import pytest

from cad2d_ir import (
    convert_edrw_file_to_ir,
    convert_file_to_ir,
    convert_ir_to_dxf_text,
    detect_source_format,
)
from cad2d_ir.cli import main
from cad2d_ir.importers import ImporterError, ImportOptions
from cad2d_ir.importers.edrawings import (
    DEFAULT_TEXT_HEIGHT_MM,
    PAPER_SIZES,
    _arc_angles,
    _bounds,
    _fit_paper,
    edrawings_document_to_ir,
    is_edrw_signature,
)
from cad2d_ir.schema import validate_ir

edrawings_kit = pytest.importorskip("edrawings_kit")

FIXTURES = Path(__file__).parent / "data" / "edrw"
TWO_LAYOUTS = FIXTURES / "two-layouts-mm.edrw"
JAPANESE = FIXTURES / "two-layouts-japanese-mm.edrw"
FIXTURE_SHA256 = {
    "two-layouts-mm.edrw": "d601dbac0130f4084800ab3cdbeb5e15d4ce8f7992a3c360b0b8d6936fc70632",
    "two-layouts-japanese-mm.edrw": (
        "3de42bf9aa6424678e3e919562573dfe89593f1ce878efde733ca4bc89516fc1"
    ),
}

# --- HSF grammar (edrawings-kit's test conventions) -------------------------

HEADER = b";; HSF V19.10 \n"
TERMINATOR = b"\0"
FACES, EDGES, LINES, MARKERS, TEXT = 0x01, 0x02, 0x04, 0x08, 0x10


def seg(name: str, *body: bytes) -> bytes:
    raw = name.encode("utf-8")
    return b"(" + bytes([len(raw)]) + raw + b"".join(body) + b")"


def option(text: str) -> bytes:
    raw = text.encode("utf-8")
    return b"U" + struct.pack("<H", len(raw)) + raw


def matrix(scale: float, tx: float, ty: float) -> bytes:
    return b"%" + struct.pack("<12f", scale, 0, 0, 0, scale, 0, 0, 0, 1, tx, ty, 0)


def points(*xy: tuple[float, float]) -> bytes:
    return b"".join(struct.pack("<3f", x, y, 0) for x, y in xy)


def line(x1: float, y1: float, x2: float, y2: float) -> bytes:
    return b"l" + points((x1, y1), (x2, y2))


def polyline(*xy: tuple[float, float]) -> bytes:
    return b"L" + struct.pack("<i", len(xy)) + points(*xy)


def polygon(*xy: tuple[float, float]) -> bytes:
    return b"G" + struct.pack("<i", len(xy)) + points(*xy)


def circle(*xy: tuple[float, float]) -> bytes:
    return b"C" + points(*xy) + b"\0"


def arc(*xy: tuple[float, float]) -> bytes:
    return b"c" + points(*xy) + b"\0"


def marker(x: float, y: float) -> bytes:
    return b"X" + points((x, y))


def text(x: float, y: float, value: str) -> bytes:
    data = value.encode("utf-16-le")
    return b"x" + points((x, y)) + b"\x05" + bytes([len(data)]) + data + b"\0"


def font(name: str, size: float, units: int = 6) -> bytes:
    raw = name.encode("ascii")
    return (
        b"F\x03\x03"
        + bytes([len(raw)])
        + raw
        + struct.pack("<f", size)
        + bytes([units])
    )


def anchor(index: int) -> bytes:
    """``*`` text alignment; 5 is ``center_right`` (what eDrawings writes)."""
    return b"*" + bytes([index])


def visibility(mask: int, value: int) -> bytes:
    return b"V" + bytes([mask, value])


def color(mask: int, rgb: tuple[int, int, int]) -> bytes:
    return b"~" + bytes([mask]) + bytes(rgb)


def stream(*body: bytes) -> bytes:
    return HEADER + b"".join(body) + TERMINATOR


def zip_container(hsf: bytes) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("eModel", hsf)
        archive.writestr("preview.jpg", b"\xff\xd8\xff\xd9")
    return buffer.getvalue()


def prefixed_container(zip_bytes: bytes) -> bytes:
    preamble = b"\x5b\x01\x00\x00\x00\xbe\x0d" + b"2008+ archive"
    preamble += bytes(92 - 4 - len(preamble)) + b"\xff\xff\xff\x7f"
    return preamble + zip_bytes


def solidworks_like() -> bytes:
    """One ANSI A sheet (size stored in metres) with a 1:2 view at (50, 50) mm."""
    frame = polyline(
        (0.0095, 0.0064),
        (0.2699, 0.0064),
        (0.2699, 0.2095),
        (0.0095, 0.2095),
        (0.0095, 0.0064),
    )
    view = seg(
        "view0",
        option("name=`Drawing View 1`"),
        seg("3dmodel", matrix(1.0, 0.0, 0.0), line(0, 0, 1, 1)),
        seg(
            "2dmodel",
            matrix(0.5, 0.05, 0.05),
            seg(
                "layer-1",
                seg("sub0", color(LINES, (0, 0, 0)), line(0, 0, 0.1, 0)),
                seg("sub1", polyline((0, 0), (0.1, 0), (0.1, 0.05))),
                seg(
                    "sub2",
                    circle((0.05, 0.03), (0.04, 0.02), (0.05, 0.01)),
                    arc((0.06, 0.02), (0.05, 0.03), (0.04, 0.02)),
                ),
                seg(
                    "sub3",
                    seg(
                        "filled",
                        visibility(FACES, FACES),
                        polygon((0, 0), (0.002, 0), (0, 0.002)),
                    ),
                ),
                seg(
                    "sub4",
                    polygon((0.01, 0.01), (0.02, 0.01), (0.02, 0.02), (0.01, 0.02)),
                ),
                seg(
                    "text0",
                    font("Century Gothic", 0.003),
                    anchor(5),
                    color(TEXT, (0, 0, 0)),
                    text(0.02, 0.03, "TITLE"),
                ),
                seg("text1", text(0.02, 0.04, "NO FONT")),
            ),
            seg(
                "hole markers",
                visibility(FACES | EDGES | LINES | MARKERS, 0),
                marker(0.01, 0.01),
                line(0, 0, 0.01, 0.01),
            ),
        ),
    )
    border = seg(
        "borderview0",
        option("name=`Border`"),
        seg(
            "2dmodel",
            seg(
                "layer0",
                option("layername=`FORMAT`,openstate=`on`"),
                seg("c16777215t2", frame),
            ),
            seg(
                "layer-1",
                seg(
                    "text2",
                    font("Century Gothic", 0.00254),
                    anchor(5),
                    text(0.2, 0.02, "SIZE"),
                ),
            ),
        ),
    )
    sheet = seg(
        "sheet0",
        option("fit width=0.279400,fit height=0.215900"),
        visibility(FACES | EDGES | LINES | MARKERS | TEXT, EDGES | LINES | TEXT),
        view,
        border,
    )
    return stream(option("length=8,angle=0"), sheet)


def dxf_reader_like() -> bytes:
    """A `Model` sheet beside two layouts, every sheet saved with everything off."""
    off = visibility(0x7F, 0)
    model = seg(
        "sheet0",
        option("name=`Model`"),
        off,
        seg(
            "view0",
            option("name=`Drawing View 1`"),
            seg(
                "2dmodel",
                visibility(FACES | EDGES, EDGES),
                seg(
                    "layer100",
                    option("layername=`0`"),
                    seg(
                        "c16777215t1",
                        polyline((0, 0), (0.1, 0), (0.1, 0.05), (0, 0.05), (0, 0)),
                    ),
                ),
            ),
        ),
    )
    sheet_a = seg(
        "sheet1",
        option("name=`Sheet A`"),
        off,
        seg(
            "view0",
            option("name=`Drawing View 1`"),
            seg(
                "2dmodel",
                visibility(FACES | EDGES, EDGES),
                seg(
                    "layer200",
                    option("layername=`M4_MODEL`"),
                    seg(
                        "c255t1",
                        polyline(
                            (0.02, 0.03),
                            (0.16, 0.03),
                            (0.16, 0.10),
                            (0.02, 0.10),
                            (0.02, 0.03),
                        ),
                        circle((0.09, 0.08), (0.08, 0.07), (0.09, 0.06)),
                    ),
                ),
                seg(
                    "layer300",
                    option("layername=`0`"),
                    seg(
                        "c255t1",
                        seg(
                            "textasgeom",
                            polyline((0.03, 0.11), (0.03, 0.115), (0.035, 0.11)),
                        ),
                    ),
                ),
                seg(
                    "layer400",
                    option("layername=`M4_DIMENSION`"),
                    seg(
                        "c255t1",
                        line(0.02, 0.02, 0.16, 0.02),
                        seg(
                            "filled",
                            visibility(FACES, FACES),
                            polygon((0.02, 0.02), (0.024, 0.021), (0.024, 0.019)),
                        ),
                    ),
                    seg("c255t1", line(0.05, 0.05, 0.050001, 0.050001)),
                ),
            ),
        ),
    )
    sheet_b = seg(
        "sheet2",
        option("name=`Sheet B`"),
        off,
        seg(
            "borderview0",
            option("name=`Border`"),
            seg(
                "2dmodel",
                visibility(FACES | EDGES, EDGES),
                seg(
                    "layer500",
                    option("layername=`VIEWPORTS`"),
                    seg(
                        "c16777215t1",
                        polygon((0.01, 0.01), (0.26, 0.01), (0.26, 0.19), (0.01, 0.19)),
                    ),
                ),
                seg("layer-1", seg("c65280t1", line(0.02, 0.02, 0.1, 0.02))),
            ),
        ),
    )
    return stream(option("length=`3`"), model, sheet_a, sheet_b)


def _by_kind(document: dict, kind: str) -> list[dict]:
    return [entity for entity in document["entities"] if entity["kind"] == kind]


def _codes(result) -> set[str]:
    return {diagnostic.code for diagnostic in result.diagnostics}


def _write(tmp_path: Path, name: str, data: bytes) -> Path:
    path = tmp_path / name
    path.write_bytes(data)
    return path


# --- signatures and containers -------------------------------------------------


def test_signature_accepts_the_three_containers_only() -> None:
    hsf = solidworks_like()
    assert is_edrw_signature(hsf[:512])
    assert is_edrw_signature(zip_container(hsf)[:512])
    assert is_edrw_signature(prefixed_container(zip_container(hsf))[:512])
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("[Content_Types].xml", "<Types/>")
    assert not is_edrw_signature(buffer.getvalue()[:512])
    assert not is_edrw_signature(b"0\nSECTION\n2\nHEADER\n")
    assert not is_edrw_signature(b"")


def test_non_edrawings_file_is_rejected(tmp_path: Path) -> None:
    path = _write(tmp_path, "not.edrw", b"0\nSECTION\n2\nENTITIES\n0\nENDSEC\n0\nEOF\n")
    with pytest.raises(ImporterError, match="Not an eDrawings file"):
        convert_edrw_file_to_ir(path)


@pytest.mark.parametrize("container", ["raw", "zip", "prefixed"])
def test_every_container_yields_the_same_drawing(
    tmp_path: Path, container: str
) -> None:
    data = solidworks_like()
    if container != "raw":
        data = zip_container(data)
    if container == "prefixed":
        data = prefixed_container(data)
    result = convert_edrw_file_to_ir(_write(tmp_path, f"{container}.edrw", data))
    validate_ir(result.document)
    assert result.statistics["input_kind"] in {"raw_hsf", "zip", "prefixed_zip"}
    assert result.statistics["converted_entity_counts"] == {
        "ARC": 1,
        "CIRCLE": 1,
        "HATCH": 1,
        "LINE": 1,
        "LWPOLYLINE": 3,
        "TEXT": 3,
    }


# --- SOLIDWORKS-like stream ------------------------------------------------------


def test_views_are_placed_and_the_sheet_is_divided_by_the_view_scale(
    tmp_path: Path,
) -> None:
    result = convert_edrw_file_to_ir(_write(tmp_path, "sw.edrw", solidworks_like()))
    document = result.document
    validate_ir(document)
    assert document["source"]["format"] == "edrw"
    assert document["source"]["version"] == "eDrawings (HSF 19.10)"
    assert document["source"]["name"] == "sw.edrw"
    meta = document["header"]["metadata"]["edrw"]
    assert meta["units"] == {
        "stored": "meter",
        "mm_per_unit": 1000.0,
        "source": "sheet",
        "display_length": "inch",
    }
    assert meta["coordinates"] == "real"
    assert meta["view_scale"] == pytest.approx(0.5)
    assert meta["paper_scale"] == pytest.approx(2.0)
    views = {(view["sheet"], view["segment"]): view for view in meta["views"]}
    placed = views[("sheet0", "view0")]
    assert placed["kind"] == "view" and placed["name"] == "Drawing View 1"
    assert placed["scale"] == pytest.approx(0.5)
    assert placed["origin"] == pytest.approx([0.05, 0.05], abs=1e-6)
    assert views[("sheet0", "borderview0")]["kind"] == "border"
    assert views[("sheet0", "borderview0")]["scale"] is None
    # the sheet size (`fit width`/`fit height`, metres) is the paper; the frame
    # drawn from (0, 0) fixes the landscape orientation; the extent is in IR units
    sheet = meta["sheet"]
    assert sheet["paper"] == "ANSI_A" and sheet["orientation"] == "landscape"
    assert sheet["width_mm"] == pytest.approx(279.4) and sheet[
        "height_mm"
    ] == pytest.approx(215.9)
    assert sheet["extent"] == pytest.approx([0.0, 0.0, 558.8, 431.8], abs=1e-3)
    assert meta["sheets"] == [
        {
            "segment": "sheet0",
            "name": "sheet0",
            "entities": 10,  # shown primitives: 7 in the view, 3 in the border
            "views": ["Drawing View 1", "Border"],
            "role": "model_space",
        }
    ]

    # a 100 mm model line at 1:2 from view (0, 0): paper (50, 50)..(100, 50) mm,
    # divided by the view scale it is 100 mm long again
    (line_entity,) = [e for e in _by_kind(document, "LINE") if e["layer"] == "0"]
    assert line_entity["p1"] == pytest.approx([100.0, 100.0], abs=1e-3)
    assert line_entity["p2"] == pytest.approx([200.0, 100.0], abs=1e-3)
    assert line_entity["color"] == 7  # black lines are colour 7
    (circle_entity,) = _by_kind(document, "CIRCLE")
    assert circle_entity["center"] == pytest.approx([150.0, 120.0], abs=1e-3)
    assert circle_entity["radius"] == pytest.approx(10.0, abs=1e-3)
    (arc_entity,) = _by_kind(document, "ARC")
    assert arc_entity["center"] == pytest.approx([150.0, 120.0], abs=1e-3)
    assert arc_entity["start_angle"] == pytest.approx(0.0, abs=1e-3)
    assert arc_entity["end_angle"] == pytest.approx(180.0, abs=1e-3)
    # the filled polygon is a solid hatch, the outlined one a closed polyline
    (hatch,) = _by_kind(document, "HATCH")
    assert hatch["solid"] is True and len(hatch["loops"][0]["vertices"]) == 3
    closed = [e for e in _by_kind(document, "LWPOLYLINE") if e.get("closed")]
    assert len(closed) == 1 and len(closed[0]["vertices"]) == 4
    # the frame of the border view is on the FORMAT layer with the t2 weight
    (frame,) = [e for e in _by_kind(document, "LWPOLYLINE") if e["layer"] == "FORMAT"]
    assert frame["lineweight_mm"] == pytest.approx(0.5)
    assert frame["vertices"][0] == pytest.approx([19.0, 12.8], abs=1e-3)
    assert set(document["tables"]["layers"]) == {"0", "FORMAT"}

    texts = {e["text"]: e for e in _by_kind(document, "TEXT")}
    title = texts["TITLE"]
    # text position goes through the view matrix; the WSU font size is a paper
    # size (3 mm) and is divided by the view scale like everything else
    assert title["insert"] == pytest.approx([120.0, 130.0], abs=1e-3)
    assert title["height"] == pytest.approx(6.0, abs=1e-4)
    assert (title["halign"], title["valign"]) == ("left", "baseline")
    assert title["style"] == "EDRW" and title["color"] == 7
    assert title["metadata"]["edrw"]["anchor"] == "center_right"
    assert title["metadata"]["edrw"]["font"] == "Century Gothic"
    assert texts["NO FONT"]["height"] == pytest.approx(DEFAULT_TEXT_HEIGHT_MM / 0.5)
    assert texts["SIZE"]["height"] == pytest.approx(2.54 / 0.5, abs=1e-4)
    assert texts["SIZE"]["insert"] == pytest.approx([400.0, 40.0], abs=1e-3)

    assert result.statistics["skipped_3d"] == 1
    assert result.statistics["hidden"] == 2
    assert result.statistics["completeness"] == "complete"
    assert {
        "EDRW_UNITS_FROM_SHEET",
        "EDRW_SHEET_DETECTED",
        "EDRW_VIEW_PLACED",
        "EDRW_SCALE_APPLIED",
        "EDRW_VIEW_3D_SKIPPED",
        "EDRW_HIDDEN_SKIPPED",
        "EDRW_POLYGON_AS_HATCH",
        "EDRW_TEXT_ANCHOR_ASSUMED",
        "EDRW_TEXT_HEIGHT_ASSUMED",
        "EDRW_LINE_WIDTH_ASSUMED",
        "EDRW_DIMENSION_AS_LINES",
    } <= _codes(result)
    assert "EDRW_MIXED_SCALES" not in _codes(result)
    sheet_diagnostic = next(
        d for d in result.diagnostics if d.code == "EDRW_SHEET_DETECTED"
    )
    assert "fit width" in sheet_diagnostic.details["source"]
    hidden = next(d for d in result.diagnostics if d.code == "EDRW_HIDDEN_SKIPPED")
    assert hidden.details["count"] == 2


def test_document_api_and_provenance_toggle(tmp_path: Path) -> None:
    document = edrawings_kit.read(solidworks_like(), filename="sw.edrw")
    result = edrawings_document_to_ir(
        document, options=ImportOptions(entity_provenance=False)
    )
    validate_ir(result.document)
    assert "name" not in result.document["source"]
    assert all(
        "source" not in e and "metadata" not in e for e in result.document["entities"]
    )
    with_provenance = edrawings_document_to_ir(document, source_name="sw.edrw")
    first = with_provenance.document["entities"][0]
    assert first["id"] == "EDRW_E00000001"
    assert first["source"]["format"] == "edrw"
    assert first["source"]["kind"].startswith("HSF_")
    assert first["metadata"]["edrw"]["sheet"] == "sheet0"


# --- DXF/DWG Reader-like stream ----------------------------------------------------


def test_model_sheet_is_skipped_and_further_sheets_become_layouts(
    tmp_path: Path,
) -> None:
    result = convert_edrw_file_to_ir(
        _write(tmp_path, "layouts.edrw", dxf_reader_like())
    )
    document = result.document
    validate_ir(document)
    meta = document["header"]["metadata"]["edrw"]
    # no plane profile and no sheet size: metres fit a standard paper best
    assert meta["units"]["source"] == "assumed" and meta["units"]["stored"] == "meter"
    assert meta["units"]["display_length"] == "mm"
    assert meta["coordinates"] == "sheet" and meta["paper_scale"] == 1.0
    assert [(s["name"], s["role"]) for s in meta["sheets"]] == [
        ("Model", "skipped"),
        ("Sheet A", "model_space"),
        ("Sheet B", "layout"),
    ]
    # the sheets' own "everything off" records do not hide anything: eDrawings
    # turns the shown sheet on; what counts is inside the sheet
    assert result.statistics["hidden"] == 0
    assert result.statistics["degenerate"] == 1  # the 1 µm gap mark
    # Sheet A: a 140 x 70 mm rectangle and a circle in red (COLORREF 255)
    (rect,) = [e for e in _by_kind(document, "LWPOLYLINE") if e["layer"] == "M4_MODEL"]
    assert rect["color"] == "#ff0000"
    assert rect["vertices"][0] == pytest.approx([20.0, 30.0], abs=1e-3)
    assert rect["vertices"][2] == pytest.approx([160.0, 100.0], abs=1e-3)
    (circle_entity,) = _by_kind(document, "CIRCLE")
    assert circle_entity["center"] == pytest.approx([90.0, 70.0], abs=1e-3)
    assert circle_entity["radius"] == pytest.approx(10.0, abs=1e-3)
    strokes = [e for e in _by_kind(document, "LWPOLYLINE") if e["layer"] == "0"]
    assert len(strokes) == 1 and strokes[0]["metadata"]["edrw"]["text_geometry"] is True
    (arrow,) = _by_kind(document, "HATCH")
    assert arrow["layer"] == "M4_DIMENSION" and arrow["color"] == "#ff0000"
    # ISO paper is preferred for a millimetre drawing: the geometry (140 x 70 plus
    # the dimension line and the stroke) fits A5 landscape, centred on it
    assert (
        meta["sheet"]["paper"] == "A5" and meta["sheet"]["orientation"] == "landscape"
    )
    extent = meta["sheet"]["extent"]
    assert extent[2] - extent[0] == pytest.approx(210.0, abs=1e-3)
    assert extent[3] - extent[1] == pytest.approx(148.0, abs=1e-3)
    min_x, min_y, max_x, max_y = _bounds(document["entities"])
    assert (extent[0] + extent[2]) / 2 == pytest.approx((min_x + max_x) / 2, abs=1e-3)
    assert (extent[1] + extent[3]) / 2 == pytest.approx((min_y + max_y) / 2, abs=1e-3)
    # Sheet B is a layout with its own paper: a 250 x 180 frame fits A4
    (layout,) = document["layouts"]
    assert layout["name"] == "Sheet B" and layout["viewports"] == []
    assert layout["paper"]["name"] == "A4"
    assert layout["paper"]["size_mm"] == pytest.approx([297.0, 210.0])
    kinds = sorted(e["kind"] for e in layout["entities"])
    assert kinds == ["LINE", "LWPOLYLINE"]
    (frame,) = [e for e in layout["entities"] if e["kind"] == "LWPOLYLINE"]
    assert (
        frame["closed"] is True
        and frame["layer"] == "VIEWPORTS"
        and frame["color"] == 7
    )
    (green,) = [e for e in layout["entities"] if e["kind"] == "LINE"]
    assert green["color"] == "#00ff00" and green["layer"] == "0"
    assert set(document["tables"]["layers"]) == {
        "0",
        "M4_MODEL",
        "M4_DIMENSION",
        "VIEWPORTS",
    }
    assert {
        "EDRW_UNITS_ASSUMED",
        "EDRW_MODEL_SHEET_SKIPPED",
        "EDRW_SHEETS_AS_LAYOUTS",
        "EDRW_TEXT_AS_GEOMETRY",
        "EDRW_POLYGON_AS_HATCH",
        "EDRW_SHEET_DETECTED",
    } <= _codes(result)
    assert {"EDRW_VIEW_PLACED", "EDRW_SCALE_APPLIED", "EDRW_HIDDEN_SKIPPED"}.isdisjoint(
        _codes(result)
    )
    layouts_diagnostic = next(
        d for d in result.diagnostics if d.code == "EDRW_SHEETS_AS_LAYOUTS"
    )
    assert layouts_diagnostic.details["sheets"] == ["Sheet B"]
    assert result.statistics["layout_entities"] == 2


def test_stream_without_sheet_geometry_is_an_empty_document(tmp_path: Path) -> None:
    data = stream(seg("sheet0", option("name=`Sheet1`")))
    result = convert_edrw_file_to_ir(_write(tmp_path, "empty.edrw", data))
    validate_ir(result.document)
    assert result.document["entities"] == [] and result.document["layouts"] == []
    assert result.document["header"]["metadata"]["edrw"]["sheet"] is None
    (error,) = [d for d in result.diagnostics if d.severity == "error"]
    assert error.code == "EDRW_NO_GEOMETRY" and error.details["hsf_version"] == "19.10"
    assert result.statistics["completeness"] == "partial"


def test_mixed_view_scales_are_reported(tmp_path: Path) -> None:
    def view(name: str, scale: float, tx: float, count: int) -> bytes:
        body = b"".join(line(0, 0.01 * i, 0.1, 0.01 * i) for i in range(count))
        return seg(
            name,
            option(f"name=`{name}`"),
            seg("2dmodel", matrix(scale, tx, 0.05), seg("layer-1", seg("sub0", body))),
        )

    data = stream(
        seg(
            "sheet0",
            option("fit width=0.42,fit height=0.297"),
            view("view0", 0.5, 0.02, 3),
            view("view1", 1.0, 0.2, 1),
        )
    )
    result = convert_edrw_file_to_ir(_write(tmp_path, "mixed.edrw", data))
    meta = result.document["header"]["metadata"]["edrw"]
    assert meta["view_scale"] == pytest.approx(0.5)  # the view with most entities
    mixed = next(d for d in result.diagnostics if d.code == "EDRW_MIXED_SCALES")
    assert mixed.details["scales"] == [pytest.approx(1.0)]


# --- the fixtures exported by eDrawings 2026 -----------------------------------------


@pytest.mark.parametrize(("name", "sha256"), sorted(FIXTURE_SHA256.items()))
def test_fixtures_are_byte_stable(name: str, sha256: str) -> None:
    assert hashlib.sha256((FIXTURES / name).read_bytes()).hexdigest() == sha256


def test_two_layout_fixture_matches_its_source_dxf() -> None:
    """The DXF had a 100 x 50 rectangle with a circle (r 10) in model space and
    two layouts: M4_SHEET_A (210 x 148 mm, viewport scale 1.375) and
    M4_SHEET_B (297 x 210 mm, 1.625)."""
    result = convert_edrw_file_to_ir(TWO_LAYOUTS)
    document = result.document
    validate_ir(document)
    assert document["source"]["version"] == (
        "eDrawings (HSF 19.10, DXF/DWG Reader AddIn (34.4.1.0003))"
    )
    meta = document["header"]["metadata"]["edrw"]
    assert meta["writer"] == "DXF/DWG Reader AddIn (34.4.1.0003)"
    assert meta["units"] == {
        "stored": "meter",
        "mm_per_unit": 1000.0,
        "source": "profile",
        "display_length": "mm",
    }
    assert [(s["name"], s["role"]) for s in meta["sheets"]] == [
        ("Model", "skipped"),
        ("M4_SHEET_A", "model_space"),
        ("M4_SHEET_B", "layout"),
    ]
    assert meta["paper_scale"] == 1.0  # the DXF/DWG Reader stores sheet coordinates
    assert (
        meta["sheet"]["paper"] == "A5" and meta["sheet"]["orientation"] == "landscape"
    )
    (rect,) = [e for e in _by_kind(document, "LWPOLYLINE") if e["layer"] == "M4_MODEL"]
    xs = [v[0] for v in rect["vertices"]]
    ys = [v[1] for v in rect["vertices"]]
    assert max(xs) - min(xs) == pytest.approx(100 * 1.375, abs=1e-3)
    assert max(ys) - min(ys) == pytest.approx(50 * 1.375, abs=1e-3)
    (circle_entity,) = [e for e in _by_kind(document, "CIRCLE")]
    assert circle_entity["radius"] == pytest.approx(10 * 1.375, abs=1e-3)
    assert circle_entity["color"] == 7  # white (c16777215) is colour 7
    # the viewport frame, dimension strokes and arrowheads
    (frame,) = [
        e for e in _by_kind(document, "LWPOLYLINE") if e["layer"] == "VIEWPORTS"
    ]
    assert frame["closed"] is True
    frame_xs = [v[0] for v in frame["vertices"]]
    assert max(frame_xs) - min(frame_xs) == pytest.approx(180.0, abs=1e-3)
    assert len(_by_kind(document, "HATCH")) == 4  # two arrowheads of two triangles
    assert result.statistics["text_geometry"] > 0 and result.statistics["texts"] == 0
    assert result.statistics["degenerate"] > 0  # the 1 µm marks on Defpoints
    (layout,) = document["layouts"]
    assert layout["name"] == "M4_SHEET_B" and layout["paper"]["name"] == "A4"
    (rect_b,) = [
        e
        for e in layout["entities"]
        if e["kind"] == "LWPOLYLINE" and e["layer"] == "M4_MODEL"
    ]
    xs_b = [v[0] for v in rect_b["vertices"]]
    assert max(xs_b) - min(xs_b) == pytest.approx(100 * 1.625, abs=1e-3)
    assert {"M4_OVERRIDDEN", "M4_PLAIN"} <= {e["layer"] for e in layout["entities"]}
    assert {
        "EDRW_UNITS_FROM_PROFILE",
        "EDRW_MODEL_SHEET_SKIPPED",
        "EDRW_SHEETS_AS_LAYOUTS",
        "EDRW_TEXT_AS_GEOMETRY",
        "EDRW_POLYGON_AS_HATCH",
    } <= _codes(result)
    dxf = convert_ir_to_dxf_text(document, target_version="AC1024")
    assert "$ACADVER\n1\nAC1024" in dxf.dxf_text and "LWPOLYLINE" in dxf.dxf_text


def test_japanese_fixture_keeps_its_text_at_the_dxf_insertion_point() -> None:
    """The DXF TEXT `寸法確認 100 mm` (3 mm, left/baseline) sat at (15, 125) on
    M4_SHEET_A whose viewport frame starts at (15, 17); eDrawings kept it as a
    text with the anchor `center_right`."""
    result = convert_edrw_file_to_ir(JAPANESE)
    document = result.document
    validate_ir(document)
    (label,) = _by_kind(document, "TEXT")
    assert label["text"] == "寸法確認 100 mm"
    assert label["layer"] == "M4_JAPANESE" and label["color"] == "#ff0000"
    assert label["height"] == pytest.approx(3.0, abs=1e-6)
    assert (label["halign"], label["valign"]) == ("left", "baseline")
    assert label["metadata"]["edrw"] == {
        "sheet": "M4_SHEET_A",
        "segment": "",
        "kind": "text_with_encoding",
        "anchor": "center_right",
        "font": "takaopgothic-gdi-vector",
    }
    (frame,) = [
        e for e in _by_kind(document, "LWPOLYLINE") if e["layer"] == "VIEWPORTS"
    ]
    frame_min = (
        min(v[0] for v in frame["vertices"]),
        min(v[1] for v in frame["vertices"]),
    )
    assert label["insert"][0] - frame_min[0] == pytest.approx(0.0, abs=1e-3)
    assert label["insert"][1] - frame_min[1] == pytest.approx(125.0 - 17.0, abs=1e-3)
    assert "EDRW_TEXT_ANCHOR_ASSUMED" in _codes(result)


# --- registry and CLI -----------------------------------------------------------------


def test_registry_and_cli_import_edrw(tmp_path: Path) -> None:
    assert detect_source_format("drawing.EDRW") == "edrw"
    result = convert_file_to_ir(TWO_LAYOUTS)
    assert result.document["source"]["format"] == "edrw"
    output = tmp_path / "two-layouts.json"
    assert main(["import", str(TWO_LAYOUTS), "-o", str(output)]) == 0
    assert '"format": "edrw"' in output.read_text(encoding="utf-8")
    forced = tmp_path / "forced.json"
    assert main(["import", str(JAPANESE), "--format", "edrw", "-o", str(forced)]) == 0
    assert "寸法確認" in forced.read_text(encoding="utf-8")


# --- helpers ------------------------------------------------------------------------


def test_paper_fit_prefers_the_series_of_the_display_unit() -> None:
    assert _fit_paper(180.0, 110.0) == ("A5", PAPER_SIZES["A5"])
    assert _fit_paper(258.0, 179.0) == ("A4", PAPER_SIZES["A4"])
    assert _fit_paper(258.0, 179.0, inch=True) == ("ANSI_A", PAPER_SIZES["ANSI_A"])
    assert _fit_paper(100.0, 250.0) == ("A4", (210.0, 297.0))  # portrait
    assert _fit_paper(5000.0, 5000.0) is None


def test_arc_angles_follow_the_middle_point() -> None:
    center = (0.0, 0.0)
    assert _arc_angles(center, (1.0, 0.0), (0.0, 1.0), (-1.0, 0.0)) == pytest.approx(
        (0.0, 180.0)
    )
    # the same three points in reverse order describe the same counter-clockwise arc
    assert _arc_angles(center, (-1.0, 0.0), (0.0, 1.0), (1.0, 0.0)) == pytest.approx(
        (0.0, 180.0)
    )
    # through the lower half instead: from 180 to 360
    start, end = _arc_angles(center, (-1.0, 0.0), (0.0, -1.0), (1.0, 0.0))
    assert (start, end) == pytest.approx((180.0, 0.0))
    assert math.isclose((end - start) % 360.0, 180.0)


LOCAL = os.environ.get("CAD2D_IR_EDRW_FIXTURES")


@pytest.mark.skipif(not LOCAL, reason="CAD2D_IR_EDRW_FIXTURES is not set")
def test_local_edrawings_drawings_import_and_validate() -> None:
    paths = sorted(Path(LOCAL or "").rglob("*.[eE][dD][rR][wW]"))
    assert paths
    for path in paths:
        result = convert_edrw_file_to_ir(path, strict=False)
        validate_ir(result.document)
        if "EDRW_NO_GEOMETRY" not in _codes(result):
            assert result.document["entities"], path
