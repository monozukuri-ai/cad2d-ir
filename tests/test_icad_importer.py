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
    assert "ARC" in [
        entity["kind"]
        for entity in document["tables"]["blocks"][diameter["definition"]["block"]][
            "entities"
        ]
    ]


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
