"""iCAD SX (.icd) 2D drawing import.

The fixtures in ``tests/data/icad`` are synthetic documents written with
``icadkit``'s ``DrawingWriter`` (see the README there): a sheet with a frame at
1/1 and a part view at 1/2 placed on it, with every entity kind the importer
maps. They are not CAD-authored, so the real-file behaviour (legacy text runs,
angle dimensions, registered parts) is covered by the opt-in gate at the end.
"""

from __future__ import annotations

import hashlib
import math
import os
from pathlib import Path

import pytest

from cad2d_ir import (
    convert_file_to_ir,
    convert_icad_file_to_ir,
    convert_ir_to_dxf_text,
    detect_source_format,
)
from cad2d_ir.cli import main
from cad2d_ir.importers import ImporterError, ImportOptions
from cad2d_ir.importers.icad import (
    LINE_STYLES,
    LINE_WIDTH_MM,
    _is_arrowhead,
    is_icd_signature,
)
from cad2d_ir.schema import validate_ir

icadkit = pytest.importorskip("icadkit")

FIXTURES = Path(__file__).parent / "data" / "icad"
DRAWING = FIXTURES / "drawing-v8l3.icd"
MIXED = FIXTURES / "drawing-mixed-v8l3.icd"
MODEL_ONLY = FIXTURES / "model-only-v7l7.icd"
FIXTURE_SHA256 = {
    "drawing-v8l3.icd": "12cad5c7e0beaaaf3e1a08c913c142f38d22e23af2c6a48ea496f5af41954a7a",
    "drawing-mixed-v8l3.icd": (
        "d271cb575504de8f3db4d98ffd8cadba05d7d20d4efa74f8e13321dedea3538b"
    ),
    "model-only-v7l7.icd": (
        "5492af0ed997d425de0c283039771e271cb83ff8b1abd5c781fcb93dd2ee59aa"
    ),
    "drawing-symbols-v8l3.icd": (
        "fac8c5e45306408b23b29686d06048e1c1d38dea4a7883af63422368d1c42960"
    ),
}


@pytest.mark.parametrize(("name", "sha256"), sorted(FIXTURE_SHA256.items()))
def test_fixtures_are_byte_stable(name: str, sha256: str) -> None:
    assert hashlib.sha256((FIXTURES / name).read_bytes()).hexdigest() == sha256


def _by_kind(document: dict, kind: str) -> list[dict]:
    return [entity for entity in document["entities"] if entity["kind"] == kind]


def test_sheet_is_composed_at_real_size_from_the_placed_view() -> None:
    result = convert_icad_file_to_ir(DRAWING)
    document = result.document
    validate_ir(document)
    assert document["source"]["format"] == "icd"
    assert document["source"]["version"] == "iCAD SX V8L3"
    assert document["source"]["name"] == "drawing-v8l3.icd"
    meta = document["header"]["metadata"]["icd"]
    # The view XY (1/2) carries most entities, so the sheet is divided by 0.5
    assert meta["coordinates"] == "real" and meta["paper_scale"] == 2.0  # 1/2
    assert meta["view_scale"] == 0.5 and meta["units_assumed"] is True
    views = {view["name"]: view for view in meta["views"]}
    assert views["XY"]["origin"] == [80.0, 60.0] and views["XY"]["scale"] == 0.5
    assert views["XY"]["placed"] is True and views["XY"]["entities"] == 15
    assert (
        views["!!GLOBAL"]["kind"] == "2d_global" and views["!!GLOBAL"]["entities"] == 8
    )
    # The frame (10..287 × 10..200 on the sheet) sits inside an A4 landscape sheet,
    # fitted concentrically; everything is in sheet mm / 0.5
    sheet = meta["sheet"]
    assert (sheet["paper"], sheet["orientation"]) == ("A4", "landscape")
    assert sheet["extent"] == pytest.approx([0.0, 0.0, 594.0, 420.0])
    frame = _by_kind(document, "LINE")[0]
    assert (frame["p1"], frame["p2"]) == ([20.0, 20.0], [574.0, 20.0])
    assert frame["lineweight_mm"] == LINE_WIDTH_MM[3] and frame["layer"] == "1"
    # A part line at view-local (0,0)-(120,0) lands at (80,60)/0.5 .. (140,60)/0.5:
    # real = origin / scale + view-local, so the part keeps its real size
    part_lines = [line for line in _by_kind(document, "LINE") if line["layer"] == "1"][
        4:
    ]
    assert part_lines[0]["p1"] == [160.0, 120.0] and part_lines[0]["p2"] == [
        280.0,
        120.0,
    ]
    centre = next(line for line in _by_kind(document, "LINE") if line["layer"] == "3")
    assert (
        centre["linetype"] == "CENTER" and centre["lineweight_mm"] == LINE_WIDTH_MM[2]
    )
    hidden = next(line for line in _by_kind(document, "LINE") if line["layer"] == "4")
    assert hidden["linetype"] == "DASHED"
    assert document["tables"]["linetypes"]["CENTER"]["pattern_mm"] == LINE_STYLES[3][1]
    circle = _by_kind(document, "CIRCLE")[0]
    assert circle["center"] == [190.0, 160.0] and circle["radius"] == pytest.approx(
        12.0
    )
    arc = _by_kind(document, "ARC")[0]
    assert arc["center"] == [250.0, 160.0] and arc["radius"] == pytest.approx(20.0)
    assert (arc["start_angle"], arc["end_angle"]) == pytest.approx((0.0, 90.0))
    point = _by_kind(document, "POINT")[0]
    assert point["position"] == [250.0, 160.0]
    assert set(document["tables"]["layers"]) == {"1", "2", "3", "4"}
    assert result.statistics["converted_entity_counts"] == {
        "ARC": 1,
        "CIRCLE": 1,
        "DIMENSION": 2,
        "LINE": 24,
        "POINT": 1,
        "TEXT": 8,
    }
    assert result.statistics["skipped_entities"] == 0
    assert result.statistics["completeness"] == "complete"
    codes = {diagnostic.code for diagnostic in result.diagnostics}
    assert {
        "ICD_2D_UNITS_ASSUMED_MM",
        "ICD_2D_VIEW_PLACED",
        "ICD_2D_SCALE_APPLIED",
        "ICD_2D_SHEET_DETECTED",
        "ICD_2D_LINE_WIDTH_ASSUMED",
        "ICD_2D_COLOR_PALETTE",
    } <= codes
    assert "ICD_2D_MIXED_SCALES" not in codes and "ICD_2D_TYPE_UNSUPPORTED" not in codes
    assert "ICD_2D_PLACEMENT_MISSING" not in codes


def test_texts_keep_their_line_origins_and_vertical_writing_is_split() -> None:
    document = convert_icad_file_to_ir(DRAWING).document
    texts = _by_kind(document, "TEXT")
    by_text = {text["text"]: text for text in texts}
    title = by_text["図面ゲート 試験図面"]
    assert title["insert"] == [380.0, 60.0] and title["height"] == pytest.approx(8.0)
    assert (title["halign"], title["valign"]) == ("left", "bottom")
    assert title["style"] == "ICD"
    # Two lines become two TEXTs stacked by height + row space (3 + 1.5 mm on paper)
    assert by_text["品名 ブラケット"]["insert"] == pytest.approx([380.0, 53.0])
    assert by_text["尺度 1/2"]["insert"] == pytest.approx([380.0, 44.0])
    # The 8 mm text in the 1/2 view is 8 mm real (4 mm on paper), placed by its origin
    assert by_text["A"]["insert"] == [258.0, 186.0] and by_text["A"]["height"] == 8.0
    # Vertical writing: one TEXT per character going downwards
    column = [by_text[ch] for ch in "たて書き"]
    assert [text["insert"][0] for text in column] == [272.0] * 4
    ys = [text["insert"][1] for text in column]
    assert ys == sorted(ys, reverse=True) and ys[0] - ys[1] == pytest.approx(6.0)
    assert document["tables"]["text_styles"]["ICD"]["font"] == "msgothic.ttc"


def test_dimensions_become_blocks_with_arrowheads_lines_and_value_text() -> None:
    document = convert_icad_file_to_ir(DRAWING).document
    dimensions = {d["dim_kind"]: d for d in _by_kind(document, "DIMENSION")}
    linear = dimensions["LINEAR"]
    assert linear["definition"]["text"] == "１２０"
    assert linear["definition"]["measurement"] == pytest.approx(120.0)
    assert linear["definition"]["points"]["location"] == [160.0, 120.0]
    block = document["tables"]["blocks"][linear["definition"]["block"]]
    kinds = sorted(entity["kind"] for entity in block["entities"])
    # two arrowheads (solid fills), the dimension line, two extension lines, the text
    assert kinds == ["HATCH", "HATCH", "LINE", "LINE", "LINE", "TEXT"]
    arrow = next(entity for entity in block["entities"] if entity["kind"] == "HATCH")
    assert arrow["solid"] is True and len(arrow["loops"][0]["vertices"]) == 3
    value = next(entity for entity in block["entities"] if entity["kind"] == "TEXT")
    assert value["text"] == "１２０" and value["height"] == pytest.approx(8.0)
    assert block["metadata"]["icd"]["dimension_presentation"] is True
    diameter = dimensions["DIAMETER"]
    assert diameter["definition"]["text"] == "２４"
    assert diameter["definition"]["measurement"] == pytest.approx(24.0)
    assert diameter["definition"]["source_geometry"]["kind"] == "diameter"
    # the hidden copies of the measured circle are not drawn: arrowheads, the
    # dimension line and the value text only
    assert sorted(
        entity["kind"]
        for entity in document["tables"]["blocks"][diameter["definition"]["block"]][
            "entities"
        ]
    ) == ["HATCH", "HATCH", "LINE", "TEXT"]


def test_hatches_become_their_saved_lines_and_the_dxf_round_trips() -> None:
    result = convert_icad_file_to_ir(DRAWING)
    document = result.document
    hatch_lines = [
        line
        for line in _by_kind(document, "LINE")
        if (line.get("metadata") or {}).get("icd", {}).get("hatch")
    ]
    assert len(hatch_lines) >= 5
    for line in hatch_lines:
        dx, dy = line["p2"][0] - line["p1"][0], line["p2"][1] - line["p1"][1]
        assert math.isclose(abs(dx), abs(dy), rel_tol=1e-6)  # 45°
    diagnostic = next(
        d for d in result.diagnostics if d.code == "ICD_2D_HATCH_AS_LINES"
    )
    assert diagnostic.details["count"] == 1 and diagnostic.details["segments"] == len(
        hatch_lines
    )
    dxf = convert_ir_to_dxf_text(document, target_version="AC1024")
    text = dxf.dxf_text
    assert "図面ゲート 試験図面" in text and "DIMENSION" in text and "*ICD_D1" in text


def test_mixed_view_scales_keep_the_sheet_and_report_the_other_scale() -> None:
    result = convert_icad_file_to_ir(MIXED)
    meta = result.document["header"]["metadata"]["icd"]
    assert meta["paper_scale"] == 2.0
    views = {view["name"]: view for view in meta["views"]}
    assert views["DET"]["scale"] == 2.0 and views["DET"]["entities"] == 2
    mixed = next(d for d in result.diagnostics if d.code == "ICD_2D_MIXED_SCALES")
    assert mixed.details["scales"] == [2.0]
    # The detail circle (view-local radius 6 at scale 2) is 12 mm on the sheet, 24 real
    circles = _by_kind(result.document, "CIRCLE")
    detail = next(c for c in circles if c["center"] == [440.0, 300.0])
    assert detail["radius"] == pytest.approx(24.0)


def test_entity_provenance_can_be_dropped() -> None:
    full = convert_icad_file_to_ir(DRAWING).document["entities"][0]
    assert full["source"]["format"] == "icd" and full["source"]["kind"] == "ICD_TYPE_2"
    assert full["metadata"]["icd"]["view"] == "!!GLOBAL"
    lean = convert_icad_file_to_ir(DRAWING, entity_provenance=False).document[
        "entities"
    ][0]
    assert "source" not in lean and "metadata" not in lean


def test_model_only_document_has_no_two_d_view() -> None:
    result = convert_icad_file_to_ir(MODEL_ONLY)
    assert result.document["entities"] == []
    assert result.statistics["views_2d"] == 0 and result.statistics["entities_2d"] == 0
    assert {d.code for d in result.diagnostics} >= {"ICD_2D_NO_VIEWS"}
    assert result.document["header"]["metadata"]["icd"]["sheet"] is None


def test_signature_suffix_and_errors() -> None:
    data = DRAWING.read_bytes()
    assert is_icd_signature(data[:256])
    assert detect_source_format("part.icd") == "icd"
    assert convert_file_to_ir(DRAWING).document["source"]["format"] == "icd"
    with pytest.raises(ImporterError, match="MOD0/MOD1"):
        convert_icad_file_to_ir(FIXTURES / "README.md")
    # An unqualified version (V8L4) fails closed before any geometry is read
    patched = bytearray(data)
    patched[12:16] = b"\0\x08\0\x04"
    with pytest.raises(ImporterError, match="Unqualified iCAD SX version V8L4"):
        from cad2d_ir.importers.icad import icad_document_to_ir

        icad_document_to_ir(icadkit.read(bytes(patched)), source_name="new.icd")


def test_arrowhead_detection() -> None:
    tip = (10.0, 0.0)
    arrow = ((7.0, 0.8), tip, (7.0, -0.8))
    assert _is_arrowhead(arrow, [])
    extension = ((10.0, -60.0), tip, (10.0, 5.5))  # measured point, foot, overshoot
    assert not _is_arrowhead(extension, [])
    assert not _is_arrowhead(arrow, [(7.0, 0.8)])  # a measured point is never an arrow


def test_cli_imports_icd(tmp_path: Path) -> None:
    output = tmp_path / "drawing.json"
    assert main(["import", str(DRAWING), "-o", str(output), "--format", "icd"]) == 0
    assert '"format": "icd"' in output.read_text(encoding="utf-8")


LOCAL = os.environ.get("CAD2D_IR_ICAD_FIXTURES")


@pytest.mark.skipif(not LOCAL, reason="CAD2D_IR_ICAD_FIXTURES is not set")
def test_local_icad_drawings_import_and_validate() -> None:
    paths = sorted(Path(LOCAL or "").rglob("*.[iI][cC][dD]"))
    assert paths
    for path in paths:
        try:
            result = convert_icad_file_to_ir(path, options=ImportOptions(strict=False))
        except ImporterError as exc:
            assert "Unqualified" in str(exc) or "Failed to read" in str(exc), path
            continue
        validate_ir(result.document)


SYMBOLS = FIXTURES / "drawing-symbols-v8l3.icd"


def test_ellipses_curves_notes_angles_and_symbols_are_mapped() -> None:
    """The record types icadkit 0.3.9 decodes (2026-10-27): ellipses, free curves,
    leader notes, angle dimensions and symbols. The view sits at (80, 60) on the
    sheet at 1/2 and the IR is divided by that scale, so a view point (x, y) is
    (x + 160, y + 120) and lengths are unchanged."""
    result = convert_icad_file_to_ir(SYMBOLS)
    document = result.document
    validate_ir(document)
    ellipses = _by_kind(document, "ELLIPSE")
    assert len(ellipses) == 2
    full = next(e for e in ellipses if e["end_param"] == pytest.approx(math.tau))
    assert full["center"] == pytest.approx([200.0, 150.0])
    assert full["ratio"] == pytest.approx(0.5)
    assert full["major_axis"] == pytest.approx(
        [12.0 * math.cos(math.radians(30)), 12.0 * math.sin(math.radians(30))]
    )
    half = next(e for e in ellipses if e is not full)
    assert half["start_param"] == 0.0 and half["end_param"] == pytest.approx(math.pi)
    assert half["major_axis"] == pytest.approx([10.0, 0.0])
    assert half["ratio"] == pytest.approx(0.4)
    (curve,) = _by_kind(document, "LWPOLYLINE")
    # a smooth interpolation through the four knots: 1 + 6 vertices per segment
    assert len(curve["vertices"]) == 19 and curve["closed"] is False
    assert curve["vertices"][0] == [160.0, 200.0] and curve["vertices"][-1] == [
        190.0,
        200.0,
    ]
    assert curve["vertices"][6] == pytest.approx([170.0, 204.0])
    assert curve["vertices"][12] == pytest.approx([180.0, 197.0])
    assert max(abs(v[1] - 200.0) for v in curve["vertices"]) < 5.0
    texts = {t["text"]: t for t in _by_kind(document, "TEXT")}
    assert {"注記 1", "２行目", "Ｍ１"} <= set(texts)
    assert texts["注記 1"]["height"] == pytest.approx(8.0)  # paper 4 mm at 1/2
    hatches = [h for h in _by_kind(document, "HATCH") if h["solid"]]
    arrow = next(h for h in hatches if h["loops"][0]["vertices"][1] == [210.0, 170.0])
    assert len(arrow["loops"][0]["vertices"]) == 3
    lines = _by_kind(document, "LINE")
    assert any(
        ln["p1"] == [210.0, 170.0] and ln["p2"] == [230.0, 190.0] for ln in lines
    )
    assert any(
        ln["p1"] == [230.0, 190.0] and ln["p2"] == [260.0, 190.0] for ln in lines
    )
    (angle,) = [
        d for d in _by_kind(document, "DIMENSION") if d["dim_kind"] == "ANGULAR"
    ]
    points = angle["definition"]["points"]
    assert points["p1"] == points["p3"] == [160.0, 120.0]
    assert points["p2"] == [190.0, 120.0] and points["p4"] == [180.0, 145.0]
    expected = math.degrees(math.atan2(25.0, 20.0))
    assert angle["definition"]["measurement"] == pytest.approx(expected)
    assert angle["definition"]["text"] == "５１．３４０°"
    block = document["tables"]["blocks"][angle["definition"]["block"]]
    kinds = sorted(entity["kind"] for entity in block["entities"])
    # two arrowheads, the drawn arc, the drawn aux line and the value text; the
    # hidden arcs and the hidden second aux line are not drawn
    assert kinds == ["ARC", "HATCH", "HATCH", "LINE", "TEXT"]
    assert angle["definition"]["source_geometry"]["arc"]["radius"] == 15.0
    marker = next(
        p for p in _by_kind(document, "POINT") if p["position"] == [260.0, 130.0]
    )
    assert marker["metadata"]["icd"]["raw_type"] == 212
    codes = {d.code for d in result.diagnostics}
    assert {
        "ICD_2D_CURVE_AS_POLYLINE",
        "ICD_2D_LEADER_AS_LINES",
        "ICD_2D_SYMBOL_ITEMS",
        "ICD_2D_DIMENSION_FROM_SAVED",
    } <= codes
    assert "ICD_2D_DIMENSION_PARTIAL" not in codes
    assert "ICD_2D_TYPE_UNSUPPORTED" not in codes
    stats = result.statistics
    assert stats["ellipses"] == 2 and stats["curves"] == 1 and stats["notes"] == 1
    assert stats["symbols"] == 1 and stats["dimensions"] == 1
    assert stats["skipped_entities"] == 0
    # DXF round trip keeps the ellipses and the angular dimension
    from cad2d_ir import convert_dxf_text_to_ir

    text = convert_ir_to_dxf_text(document, target_version="AC1024").dxf_text
    again = convert_dxf_text_to_ir(text).document
    assert len(_by_kind(again, "ELLIPSE")) == 2
    assert [d["dim_kind"] for d in _by_kind(again, "DIMENSION")] == ["ANGULAR"]


def test_line_information_and_attribute_records(tmp_path: Path) -> None:
    """P&ID line information (type 224) is drawn from its framed items; records
    without items and motion-verification records are attribute records."""
    import struct

    import icadkit
    from icadkit import DrawingWriter
    from icadkit.write import _item

    from cad2d_ir.importers.icad import icad_document_to_ir

    def line_information(name: str, items: bytes) -> bytes:
        header = bytearray(32)
        header[4] = 1
        header[12] = 0x40
        header[14] = 7
        header[15] = 224
        struct.pack_into("<I", header, 16, 0x80000401)
        header[24:32] = bytes.fromhex("080200fe00000001")
        body = bytearray(name.encode("utf-16-le")) + b"\0" * (300 - 2 * len(name))
        record = header + body + items
        struct.pack_into("<I", record, 0, len(record))
        return bytes(record)

    writer = DrawingWriter(icadkit.read(SYMBOLS))
    items = _item("<", 0x0100, 2, 1, (10.0, 10.0), point=True)
    items += _item("<", 0x0244, 2, 1, (10.0, 10.0, 1.0, 0.0, 42.0))
    items += _item("<", 0x0444, 2, 1, (10.0, 10.0, 0.5, 0.0, math.tau))
    writer.add_raw_entity("XY", line_information("Line", items))
    writer.add_raw_entity("XY", line_information("Cable", b""))
    motion = bytearray(216)
    motion[4] = 1
    motion[12] = 0x40
    motion[15] = 230
    struct.pack_into("<I", motion, 16, 0x80000402)
    motion[24:32] = bytes.fromhex("300000fd00000001")
    struct.pack_into("<I", motion, 0, len(motion))
    writer.add_raw_entity("XY", bytes(motion))
    result = icad_document_to_ir(icadkit.read(writer.to_bytes()))
    document = result.document
    validate_ir(document)
    lines = [ln for ln in _by_kind(document, "LINE") if ln["p1"] == [170.0, 130.0]]
    assert lines and lines[0]["p2"] == [212.0, 130.0]
    assert lines[0]["metadata"]["icd"]["symbol"] == "Line"
    circles = [c for c in _by_kind(document, "CIRCLE") if c["center"] == [170.0, 130.0]]
    assert circles and circles[0]["radius"] == pytest.approx(0.5)
    codes = {d.code: d for d in result.diagnostics}
    assert codes["ICD_2D_ATTRIBUTE_RECORDS"].details["count"] == 2
    assert "ICD_2D_TYPE_UNSUPPORTED" not in codes
    assert result.statistics["attribute_records"] == 2
    assert result.statistics["symbols"] == 2  # the fixture's symbol and the line


def test_chamfer_dimensions_and_balloons_from_legacy_layouts() -> None:
    """Chamfer dimensions (type 29) keep the chamfer size as the measurement of
    a GENERIC dimension without a partial note; balloons (type 34) draw their
    circle, arrowhead, leader and number."""
    import struct

    import icadkit
    from icadkit import DrawingWriter
    from icadkit.write import _item, _run

    from cad2d_ir.importers.icad import icad_document_to_ir

    def header(
        record_type: int, kind_byte: int, blocks: int, entity_id: int
    ) -> bytearray:
        head = bytearray(32)
        head[4] = 1
        head[12] = 0x40
        head[14] = kind_byte
        head[15] = record_type
        struct.pack_into("<I", head, 16, entity_id)
        struct.pack_into("<H", head, 24, 16 + blocks)
        head[27] = 0xFD
        head[28:32] = b"\0\0\0\x01"
        return head

    def block(number: int, length: int, doubles: dict[int, float]) -> bytes:
        body = bytearray(length)
        body[0:4] = bytes([0xFF, number, 1, length])
        for at, value in doubles.items():
            struct.pack_into("<d", body, at, value)
        return bytes(body)

    blocks = block(6, 80, {48: 1.5}) + block(4, 48, {24: 1.5, 40: 0.2618})
    half = 0.4
    items = _run("<", "Ｃ１．５", (18.0, -9.0), 4.0, 2, 1)
    items += _item("<", 0x0344, 3, 1, (1.5, -10.0 - half, -1.5, half, 0.0, 2 * half))
    items += _item("<", 0x0300, 3, 1, (38.5, -10.0 + half, 1.5, -half, 0.0, -2 * half))
    items += _item("<", 0x0244, 3, 1, (0.0, -10.0, 1.0, 0.0, 40.0))
    items += _item("<", 0x0100, 3, 1, (20.0, -10.0), point=True)
    items += _item("<", 0x0100, 3, 1, (20.0, -9.0), point=True)
    items += _item("<", 0x0300, 3, 1, (0.0, 0.0, 0.0, -10.0, 0.0, -12.8))
    items += _item("<", 0x0300, 3, 1, (0.0, 0.0, 0.0, -10.0, 0.0, -12.8))
    items += _item("<", 0x0100, 3, 1, (0.0, 0.0), point=True)
    items += _item("<", 0x0100, 3, 1, (0.0, 0.0), point=True)
    items += _item("<", 0x0200, 3, 1, (0.0, -10.0, 1.0, 0.0, 40.0))
    chamfer = header(29, 11, len(blocks), 0x80000701) + b"\0" * 8 + blocks + items
    struct.pack_into("<I", chamfer, 0, len(chamfer))

    note_blocks = (
        block(9, 48, {}) + block(11, 48, {}) + block(4, 48, {24: 1.5, 40: 0.2618})
    )
    note_items = _run("<", "３", (32.0, 31.0), 4.0, 2, 1, flags=0xF1)
    note_items += _item("<", 0x0344, 3, 1, (12.0, 11.0, -2.0, -1.0, -1.0, 1.0))
    note_items += _item("<", 0x0344, 3, 1, (10.0, 10.0, 20.0, 20.0))
    note_items += _item("<", 0x0444, 3, 1, (34.0, 33.0, 5.0, 0.0, math.tau))
    balloon = (
        header(34, 4, len(note_blocks), 0x80000702)
        + b"\0" * 8
        + note_blocks
        + note_items
    )
    struct.pack_into("<I", balloon, 0, len(balloon))

    writer = DrawingWriter(icadkit.read(SYMBOLS))
    writer.add_raw_entity("XY", bytes(chamfer))
    writer.add_raw_entity("XY", bytes(balloon))
    result = icad_document_to_ir(icadkit.read(writer.to_bytes()))
    document = result.document
    validate_ir(document)
    generic = [d for d in _by_kind(document, "DIMENSION") if d["dim_kind"] == "GENERIC"]
    assert len(generic) == 1 and generic[0]["definition"]["text"] == "Ｃ１．５"
    assert generic[0]["definition"]["measurement"] == pytest.approx(1.5)
    assert generic[0]["definition"]["source_geometry"]["kind"] == "chamfer"
    block_entities = document["tables"]["blocks"][generic[0]["definition"]["block"]][
        "entities"
    ]
    # one drawn arrowhead, the dimension line and the text; hidden items are not drawn
    assert sorted(e["kind"] for e in block_entities) == ["HATCH", "LINE", "TEXT"]
    circles = [c for c in _by_kind(document, "CIRCLE") if c["center"] == [194.0, 153.0]]
    assert circles and circles[0]["radius"] == pytest.approx(5.0)
    assert any(t["text"] == "３" for t in _by_kind(document, "TEXT"))
    codes = {d.code for d in result.diagnostics}
    assert "ICD_2D_LEADER_AS_LINES" in codes and "ICD_2D_DIMENSION_PARTIAL" not in codes


def test_unplaced_views_are_laid_out_side_by_side_and_private_use_is_counted() -> None:
    """Two 2D views without a placement record no longer overlap at the
    origin, vector primitives (type 12) draw two segments and private-use
    characters are counted."""
    import struct

    import icadkit
    from icadkit import DrawingWriter, Line2D, Text2D

    from cad2d_ir.importers.icad import icad_document_to_ir

    writer = DrawingWriter(icadkit.read(SYMBOLS))
    writer.add_view("V1", scale=1.0, origin=(0.0, 0.0))
    writer.add_view("V2", scale=1.0, origin=(0.0, 0.0))
    writer.add_entities(
        "V1", [Line2D((0.0, 0.0), (50.0, 0.0)), Text2D("", (10.0, 5.0), 4.0)]
    )
    writer.add_entities("V2", [Line2D((0.0, 0.0), (30.0, 0.0))])
    vectors = bytearray(80)
    struct.pack_into("<I", vectors, 0, 80)
    vectors[4] = 1
    vectors[12] = 0x40
    vectors[14] = 1
    vectors[15] = 12
    struct.pack_into("<I", vectors, 16, 0x80000901)
    struct.pack_into("<H", vectors, 24, 56)
    vectors[26:32] = bytes([0x44, 3, 3, 0x11, 0, 0])
    struct.pack_into("<6d", vectors, 32, 5.0, 5.0, 2.0, 0.0, 0.0, 2.0)
    writer.add_raw_entity("XY", bytes(vectors))
    document_bytes = writer.to_bytes()
    doc = icadkit.read(document_bytes)
    # drop the placement records of V1 and V2 so that they are "missing"
    import dataclasses

    views = doc.read_views()
    assert all(v.placement is not None for v in views.views if v.kind == "2d_view")
    container = doc.container()
    # the 2D global view's placement list lives in its V/W record; remove the
    # records that name V1 and V2 by blanking their name bytes
    data = bytearray(document_bytes)
    for name in (b"V1", b"V2"):
        padded = name.ljust(8, b" ")
        at = data.find(padded)
        while at >= 0:
            data[at : at + 8] = b"Z9".ljust(8, b" ")
            at = data.find(padded, at + 8)
    doc = icadkit.read(bytes(data))
    unplaced = [
        v for v in doc.read_views().views if v.kind == "2d_view" and v.placement is None
    ]
    assert len(unplaced) >= 2
    result = icad_document_to_ir(doc)
    document = result.document
    validate_ir(document)
    lines = _by_kind(document, "LINE")
    starts = sorted(
        {tuple(ln["p1"]) for ln in lines if ln["p1"][1] == 0.0 and ln["p2"][1] == 0.0}
    )
    # the two unplaced lines start at different x: laid out side by side
    xs = sorted({s[0] for s in starts})
    assert len(xs) >= 2 and xs[1] - xs[0] >= 50.0
    codes = {d.code: d for d in result.diagnostics}
    assert codes["ICD_2D_PLACEMENT_MISSING"].details["layout"] == "tiled"
    assert codes["ICD_2D_TEXT_PRIVATE_USE"].details["characters"] == 1
    assert any(t["text"] == "" for t in _by_kind(document, "TEXT"))
    segments = [ln for ln in lines if ln["p1"] == [165.0, 125.0]]
    assert sorted(ln["p2"] for ln in segments) == [[165.0, 127.0], [167.0, 125.0]]
    assert dataclasses is not None and container is not None


def test_unplaced_views_that_do_not_overlap_keep_their_coordinates() -> None:
    """Views without a placement record whose saved extents do not overlap
    (old drawings keep the sheet layout in the view coordinates) stay where
    they are, and an underlined view label draws its underline."""
    import struct

    import icadkit
    from icadkit import DrawingWriter, Line2D, Text2D
    from icadkit.write import EntityStyle, _encode_text, _item

    from cad2d_ir.importers.icad import icad_document_to_ir

    writer = DrawingWriter(icadkit.read(SYMBOLS))
    writer.add_view("V1", scale=1.0, origin=(0.0, 0.0))
    writer.add_view("V2", scale=1.0, origin=(0.0, 0.0))
    writer.add_entities("V1", [Line2D((0.0, 0.0), (50.0, 0.0))])
    writer.add_entities("V2", [Line2D((200.0, 0.0), (230.0, 0.0))])
    record, _ = _encode_text(
        "<", Text2D("A-A", (10.0, 5.0), 4.0), EntityStyle(), 0x80000905, 1.0
    )
    label = bytearray(record) + _item("<", 0x0244, 2, 1, (10.0, 3.0, 1.0, 0.0, 12.0))
    struct.pack_into("<I", label, 0, len(label))
    writer.add_raw_entity("XY", bytes(label))
    data = bytearray(writer.to_bytes())
    for name in (b"V1", b"V2"):
        padded = name.ljust(8, b" ")
        at = data.find(padded)
        while at >= 0:
            data[at : at + 8] = b"Z9".ljust(8, b" ")
            at = data.find(padded, at + 8)
    doc = icadkit.read(bytes(data))
    assert (
        len(
            [
                v
                for v in doc.read_views().views
                if v.kind == "2d_view" and v.placement is None
            ]
        )
        >= 2
    )
    result = icad_document_to_ir(doc)
    document = result.document
    validate_ir(document)
    lines = _by_kind(document, "LINE")
    # the sheet is divided by the dominant scale (XY at 1/2): raw x doubles
    assert any(ln["p1"] == [0.0, 0.0] and ln["p2"] == [100.0, 0.0] for ln in lines)
    assert any(ln["p1"] == [400.0, 0.0] and ln["p2"] == [460.0, 0.0] for ln in lines)
    codes = {d.code: d for d in result.diagnostics}
    assert codes["ICD_2D_PLACEMENT_MISSING"].details["layout"] == "kept"
    assert any(t["text"] == "A-A" for t in _by_kind(document, "TEXT"))
    assert any(
        ln["p1"] == [170.0, 123.0] and ln["p2"] == [182.0, 123.0] for ln in lines
    )
