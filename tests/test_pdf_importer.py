from __future__ import annotations

import base64
import json
import math
import sys
from pathlib import Path

import pytest

from cad2d_ir import convert_file_to_ir, detect_source_format, validate_ir
from cad2d_ir import paper_scale
from cad2d_ir.cli import main
from cad2d_ir.importers import (
    ImporterError,
    ImportOptions,
    MissingOptionalDependencyError,
)
from cad2d_ir.importers import pdf as pdf_importer
from cad2d_ir.importers.pdf import convert_pdf_file_to_ir

sys.path.insert(0, str(Path(__file__).resolve().parent))
from pdf_builder import (  # noqa: E402
    PNG_RED_1X1,
    PdfPage,
    PdfSpec,
    arc_bezier,
    build_pdf,
    cid_text,
    circle_bezier,
    polyline_arc,
    pt,
    text,
)

pytest.importorskip("pypdfium2")

MM = 25.4 / 72.0


def _write(tmp_path: Path, spec: PdfSpec, name: str = "drawing.pdf") -> Path:
    path = tmp_path / name
    path.write_bytes(build_pdf(spec))
    return path


def _convert(tmp_path: Path, content: bytes, **kwargs):
    spec_kwargs = {
        k: kwargs.pop(k)
        for k in ("forms", "images", "ocgs", "cid_fonts")
        if k in kwargs
    }
    page_kwargs = {
        k: kwargs.pop(k)
        for k in ("rotate", "cropbox", "annots", "width", "height")
        if k in kwargs
    }
    spec = PdfSpec(pages=[PdfPage(content, **page_kwargs)], **spec_kwargs)
    return convert_pdf_file_to_ir(_write(tmp_path, spec), scale="none", **kwargs)


def _kinds(result):
    return [e["kind"] for e in result.document["entities"]]


def _of(result, kind):
    return [e for e in result.document["entities"] if e["kind"] == kind]


def _codes(result):
    return {d.code for d in result.diagnostics}


def test_lines_polylines_width_colour_and_dash(tmp_path: Path) -> None:
    content = (
        b"q 1 0 0 RG 2 w 50 50 m 200 150 l S Q\n"
        b"q [6 3] 0 d 0.5 w 300 50 m 500 50 l S Q\n"
        b"q 100 300 80 40 re S Q\n"
        b"q 600 300 m 650 320 l 700 300 l 750 340 l S Q\n"
    )
    result = _convert(tmp_path, content)
    document = result.document
    validate_ir(document)
    assert document["source"]["format"] == "pdf"
    assert document["header"]["units"] == "mm"
    assert document["header"]["metadata"]["pdf"]["coordinate_space"] == "paper"
    assert sorted(_kinds(result)) == ["LINE", "LINE", "LWPOLYLINE", "LWPOLYLINE"]

    red, dashed = _of(result, "LINE")
    assert red["p1"] == pytest.approx([50 * MM, 50 * MM])
    assert red["p2"] == pytest.approx([200 * MM, 150 * MM])
    assert red["color"] == "#FF0000"
    assert red["lineweight_mm"] == pytest.approx(2 * MM, abs=1e-4)
    assert red["linetype"] == "CONTINUOUS"
    assert dashed["linetype"] == "DASHED"
    assert document["tables"]["linetypes"]["DASHED"]["pattern_mm"] == pytest.approx(
        [6 * MM, -3 * MM], abs=1e-3
    )
    assert dashed["lineweight_mm"] == pytest.approx(0.5 * MM, abs=1e-4)

    rect, open_poly = _of(result, "LWPOLYLINE")
    assert rect["closed"] is True
    assert len(rect["vertices"]) == 4
    assert rect["vertices"][0] == pytest.approx([100 * MM, 300 * MM])
    assert "closed" not in open_poly
    assert len(open_poly["vertices"]) == 4
    assert open_poly["vertices"][-1] == pytest.approx([750 * MM, 340 * MM])

    assert result.statistics["source_entity_counts"] == {"PATH": 4}
    assert result.statistics["converted_entities"] == 4
    assert document["source"]["metadata"]["pdf"]["producer"] == "cad2d-ir tests"
    assert document["source"]["metadata"]["pdf"]["pages"][0][
        "size_mm"
    ] == pytest.approx([297.0, 210.0], abs=0.01)
    entity = red
    assert entity["source"] == {"format": "pdf", "id": "1:0", "kind": "PATH"}
    assert entity["metadata"]["pdf"]["page"] == 1


def test_bezier_arcs_become_arcs_circles_and_splines(tmp_path: Path) -> None:
    content = (
        b"q " + arc_bezier(200, 200, 100, 0, 90) + b"S Q\n"
        b"q " + arc_bezier(500, 200, 80, 30, 250) + b"S Q\n"
        b"q " + circle_bezier(700, 400, 50) + b"S Q\n"
        b"q " + arc_bezier(200, 450, 60, 90, 0) + b"S Q\n"
        b"q 100 100 m 150 200 250 0 300 100 c S Q\n"
    )
    result = _convert(tmp_path, content)
    arcs = _of(result, "ARC")
    circles = _of(result, "CIRCLE")
    splines = _of(result, "SPLINE")
    assert len(arcs) == 3 and len(circles) == 1 and len(splines) == 1
    quarter, long_arc, clockwise = arcs
    assert quarter["center"] == pytest.approx([200 * MM, 200 * MM], abs=1e-3)
    assert quarter["radius"] == pytest.approx(100 * MM, abs=1e-3)
    assert (quarter["start_angle"], quarter["end_angle"]) == pytest.approx(
        (0.0, 90.0), abs=1e-3
    )
    assert long_arc["radius"] == pytest.approx(80 * MM, abs=1e-3)
    assert (long_arc["start_angle"], long_arc["end_angle"]) == pytest.approx(
        (30.0, 250.0), abs=1e-3
    )
    # A clockwise arc is stored counter-clockwise from its end to its start.
    assert (clockwise["start_angle"], clockwise["end_angle"]) == pytest.approx(
        (0.0, 90.0), abs=1e-3
    )
    assert circles[0]["center"] == pytest.approx([700 * MM, 400 * MM], abs=1e-3)
    assert circles[0]["radius"] == pytest.approx(50 * MM, abs=1e-3)
    spline = splines[0]
    assert spline["degree"] == 3
    assert len(spline["control_points"]) == 4
    assert spline["knots"] == [0.0, 0.0, 0.0, 0.0, 1.0, 1.0, 1.0, 1.0]
    assert result.statistics["bezier_arcs"] == 1 + 3 + 4 + 1
    assert result.statistics["splines"] == 1


def test_polyline_runs_are_refitted_to_arcs(tmp_path: Path) -> None:
    content = (
        b"q " + polyline_arc(200, 200, 100, 0, 90, 24) + b"S Q\n"
        b"q " + polyline_arc(500, 300, 60, 0, 360, 36) + b"h S Q\n"
        b"q 600 100 m 620 140 l 640 100 l 660 140 l 680 100 l 700 140 l 720 100 l S Q\n"
    )
    result = _convert(tmp_path, content)
    arcs, circles, polylines = (
        _of(result, "ARC"),
        _of(result, "CIRCLE"),
        _of(result, "LWPOLYLINE"),
    )
    assert len(arcs) == 1 and len(circles) == 1 and len(polylines) == 1
    assert arcs[0]["center"] == pytest.approx([200 * MM, 200 * MM], abs=0.05)
    assert arcs[0]["radius"] == pytest.approx(100 * MM, abs=0.05)
    assert (arcs[0]["start_angle"], arcs[0]["end_angle"]) == pytest.approx(
        (0.0, 90.0), abs=0.5
    )
    assert circles[0]["radius"] == pytest.approx(60 * MM, abs=0.05)
    assert len(polylines[0]["vertices"]) == 7
    assert "PDF_POLYLINE_ARC_REFITTED" in _codes(result)
    assert result.statistics["polyline_arc_refits"] == 2


def test_tiny_closed_shapes_become_points(tmp_path: Path) -> None:
    # A 0.03 mm circle (a plotted point marker) and a normal 5 mm circle.
    content = (
        b"q " + circle_bezier(100, 100, 0.03 / MM) + b"S Q\n"
        b"q " + circle_bezier(300, 300, 5 / MM) + b"S Q\n"
    )
    result = _convert(tmp_path, content)
    points = _of(result, "POINT")
    assert len(points) == 1 and len(_of(result, "CIRCLE")) == 1
    assert points[0]["position"] == pytest.approx([100 * MM, 100 * MM], abs=1e-3)
    assert "PDF_POINT_FROM_TINY_SHAPE" in _codes(result)
    assert result.statistics["points_from_tiny_shapes"] == 1


def test_fills_become_hatches_or_thick_lines(tmp_path: Path) -> None:
    content = (
        b"q 0 0 1 rg 100 100 200 0.5 re f Q\n"
        b"q 0.5 g 400 100 100 100 re f Q\n"
        b"q 1 0 0 rg " + circle_bezier(700, 300, 30) + b"f Q\n"
        b"q 0 1 0 rg 0 0 0 RG 1 w 100 400 100 50 re B Q\n"
    )
    result = _convert(tmp_path, content)
    lines = _of(result, "LINE")
    hatches = _of(result, "HATCH")
    polylines = _of(result, "LWPOLYLINE")
    assert len(lines) == 1 and len(hatches) == 3 and len(polylines) == 1
    thick = lines[0]
    assert thick["p1"] == pytest.approx([100 * MM, 100.25 * MM], abs=1e-3)
    assert thick["p2"] == pytest.approx([300 * MM, 100.25 * MM], abs=1e-3)
    assert thick["lineweight_mm"] == pytest.approx(0.5 * MM, abs=1e-4)
    assert thick["color"] == "#0000FF"
    square, disc, both = hatches
    assert square["solid"] is True and square["pattern"] == "SOLID"
    assert len(square["loops"]) == 1 and len(square["loops"][0]["vertices"]) == 4
    assert square["color"] == "#808080" or square["color"] == "#7F7F7F"
    assert len(disc["loops"][0]["vertices"]) >= 16
    for x, y in disc["loops"][0]["vertices"]:
        assert math.hypot(x - 700 * MM, y - 300 * MM) == pytest.approx(
            30 * MM, abs=0.06
        )
    assert both["color"] == "#00FF00"
    assert polylines[0]["closed"] is True and polylines[0]["color"] == "#000000"
    assert {"PDF_FILL_AS_LINE", "PDF_HATCH_FLATTENED"} <= _codes(result)


def test_forms_clips_and_optional_content_layers(tmp_path: Path) -> None:
    form = b"0 0 m 100 0 l 100 50 l 0 50 l S\n"
    content = (
        b"q 2 0 0 2 400 300 cm /X1 Do Q\n"
        b"q 100 500 50 50 re W n 90 490 m 160 560 l S Q\n"
        b"q 100 500 50 50 re W n 300 300 m 350 350 l S Q\n"
        b"q /OC /oc1 BDC 600 100 m 700 100 l S EMC Q\n"
        b"q 600 200 m 700 200 l S Q\n"
    )
    result = _convert(
        tmp_path,
        content,
        forms={"X1": (form, [0, 0, 100, 50], [1, 0, 0, 1, 10, 20])},
        ocgs=["oc1"],
    )
    document = result.document
    polylines = _of(result, "LWPOLYLINE")
    assert len(polylines) == 1
    # raw (0,0) -> form matrix (+10,+20) -> page cm (x2, +400,+300) = (420, 340)
    assert polylines[0]["vertices"][0] == pytest.approx([420 * MM, 340 * MM], abs=1e-3)
    assert polylines[0]["vertices"][2] == pytest.approx([620 * MM, 440 * MM], abs=1e-3)
    assert polylines[0]["lineweight_mm"] == pytest.approx(2 * MM, abs=1e-4)
    lines = _of(result, "LINE")
    assert len(lines) == 3
    clipped, ocg_line, plain = lines
    assert clipped["p1"] == pytest.approx([100 * MM, 500 * MM], abs=1e-3)
    assert clipped["p2"] == pytest.approx([150 * MM, 550 * MM], abs=1e-3)
    assert ocg_line["layer"] == "oc1"
    assert plain["layer"] == "0"
    assert set(document["tables"]["layers"]) == {"0", "oc1"}
    assert {"PDF_CLIPPED_OUT", "PDF_LAYERS_FROM_OCG"} <= _codes(result)
    assert result.statistics["skipped_entity_counts"] == {"clipped_out": 1}


def test_clipped_arcs_are_split_at_the_clip_rectangle(tmp_path: Path) -> None:
    # Circle of radius 100 at (300, 300), clipped to x >= 300: the right half remains.
    content = b"q 300 100 400 400 re W n " + circle_bezier(300, 300, 100) + b"S Q\n"
    result = _convert(tmp_path, content)
    arcs = _of(result, "ARC")
    assert len(arcs) == 1 and not _of(result, "CIRCLE")
    arc = arcs[0]
    assert arc["radius"] == pytest.approx(100 * MM, abs=1e-3)
    assert (arc["start_angle"], arc["end_angle"]) == pytest.approx(
        (270.0, 90.0), abs=1e-3
    )


def test_text_runs_heights_rotation_and_invisible_text(tmp_path: Path) -> None:
    content = (
        text(300, 400, 12, "AB")
        + text(500, 100, 12, "R30", angle_deg=30)
        + text(100, 200, 8, "W")
        + text(106, 200, 8, "A")
        + text(112, 200, 8, "L")
        + text(100, 300, 10, "A B")
        + text(300, 300, 10, "far")
        + text(400, 300, 10, "away")
        + text(300, 450, 10, "hidden", mode=3)
        + b"BT /F1 12 Tf 2 0 0 1 100 500 Tm (Wide) Tj ET\n"
        + cid_text(600, 500, 10, "日本")
    )
    result = _convert(tmp_path, content, cid_fonts={"J1": "KozMinPr6N-Regular"})
    texts = {e["text"]: e for e in _of(result, "TEXT")}
    assert set(texts) == {"AB", "R30", "WAL", "A B", "far", "away", "Wide", "日本"}
    ab = texts["AB"]
    assert ab["insert"] == pytest.approx([300 * MM, 400 * MM], abs=1e-3)
    assert ab["height"] == pytest.approx(
        8.62 * MM, abs=0.1
    )  # measured cap height of Helvetica
    assert "rotation" not in ab
    assert ab["style"] == "PDF_Helvetica"
    assert result.document["tables"]["text_styles"]["PDF_Helvetica"] == {
        "font": "Helvetica"
    }
    assert ab["metadata"]["pdf"]["font_size_mm"] == pytest.approx(12 * MM, abs=1e-3)
    r30 = texts["R30"]
    assert r30["rotation"] == pytest.approx(30.0, abs=1e-3)
    assert r30["height"] == pytest.approx(0.72 * 12 * MM, abs=1e-3)
    assert texts["Wide"]["width_factor"] == pytest.approx(2.0, abs=1e-3)
    assert texts["日本"]["height"] == pytest.approx(10 * MM, abs=1e-3)
    assert "PDF_TEXT_INVISIBLE_SKIPPED" in _codes(result)
    assert result.statistics["text_runs"] == 8
    assert result.statistics["source_entity_counts"]["TEXT"] == 11


def test_rotated_page_cropbox_and_multipage_tiling(tmp_path: Path) -> None:
    spec = PdfSpec(
        pages=[
            PdfPage(b"q 10 10 m 100 10 l S Q\n", width=200, height=100),
            PdfPage(
                b"q 20 20 m 100 20 l S Q\n",
                width=200,
                height=300,
                rotate=90,
                cropbox=[10, 10, 150, 250],
            ),
        ]
    )
    result = convert_pdf_file_to_ir(_write(tmp_path, spec), scale="none")
    document = result.document
    pages = document["source"]["metadata"]["pdf"]["pages"]
    assert pages[0]["size_mm"] == pytest.approx([200 * MM, 100 * MM], abs=1e-3)
    assert pages[1]["size_mm"] == pytest.approx([240 * MM, 140 * MM], abs=1e-3)
    # The gap is a tenth of the widest page as displayed (page 2 is 240 pt wide once rotated).
    gap = 240 * MM * pdf_importer.DEFAULT_PAGE_GAP_RATIO
    assert pages[1]["offset_mm"][0] == pytest.approx(200 * MM + gap, abs=1e-3)
    first, second = _of(result, "LINE")
    assert first["p1"] == pytest.approx([10 * MM, 10 * MM], abs=1e-3)
    # Rotate 90 with crop [10,10,150,250]: (x, y) -> (y - 10, 150 - x), then the tiling offset.
    offset = 200 * MM + gap
    assert second["p1"] == pytest.approx([offset + 10 * MM, 130 * MM], abs=1e-3)
    assert second["p2"] == pytest.approx([offset + 10 * MM, 50 * MM], abs=1e-3)
    assert {"PDF_PAGES_TILED", "PDF_PAGE_ROTATED"} <= _codes(result)
    assert document["header"]["bbox"]["max"][0] == pytest.approx(
        offset + 240 * MM, abs=1e-6
    )

    single = convert_pdf_file_to_ir(_write(tmp_path, spec), pages=[2], scale="none")
    assert single.statistics["converted_pages"] == 1
    assert _of(single, "LINE")[0]["p1"] == pytest.approx([10 * MM, 130 * MM], abs=1e-3)
    with pytest.raises(ImporterError):
        convert_pdf_file_to_ir(_write(tmp_path, spec), pages=[3])


def _dimensioned_drawing() -> bytes:
    """Five horizontal dimensions drawn at 1:50 (20 mm of paper per 1000 mm)."""
    parts = []
    for i, (value, span_mm) in enumerate(
        [(1000, 20), (1000, 20), (1500, 30), (500, 10), (2000, 40)]
    ):
        x0 = pt(40 + 50 * (i % 3))
        y = pt(60 + 40 * (i // 3))
        span = pt(span_mm)
        parts.append(
            f"q 0.3 w {x0:.3f} {y:.3f} m {x0 + span:.3f} {y:.3f} l S Q\n".encode()
        )
        for x in (x0, x0 + span):
            parts.append(
                f"q 0.3 w {x:.3f} {y - pt(1.5):.3f} m {x:.3f} {y + pt(4):.3f} l S Q\n".encode()
            )
        # Text centre over the middle of the span: insert = centre - 0.8*h*len/2 with h ~ 2.5 mm
        parts.append(
            text(
                x0 + span / 2 - pt(0.8 * 2.5 * len(str(value)) / 2),
                y + pt(1.0),
                pt(3.5),
                str(value),
            )
        )
    # Some geometry to scale: a wall line 100 mm long on paper (5000 mm real)
    parts.append(b"q 1 w 50 400 m 333.46 400 l S Q\n")
    return b"".join(parts)


def test_scale_is_recovered_from_dimension_texts(tmp_path: Path) -> None:
    spec = PdfSpec(pages=[PdfPage(_dimensioned_drawing())])
    path = _write(tmp_path, spec)
    auto = convert_pdf_file_to_ir(path)
    header = auto.document["header"]
    assert header["metadata"]["pdf"]["coordinate_space"] == "model"
    assert header["metadata"]["pdf"]["paper_scale"]["label"] == "1:50"
    assert header["metadata"]["pdf"]["paper_scale"]["applied"] == 50.0
    assert header["linetype_scale"] == 50.0
    assert "PDF_SCALE_ESTIMATED" in _codes(auto)
    wall = max(_of(auto, "LINE"), key=lambda e: abs(e["p2"][0] - e["p1"][0]))
    assert abs(wall["p2"][0] - wall["p1"][0]) == pytest.approx(5000.0, abs=0.5)
    assert wall["lineweight_mm"] == pytest.approx(1 * MM, abs=1e-4)
    thousand = next(e for e in _of(auto, "TEXT") if e["text"] == "1000")
    assert thousand["height"] == pytest.approx(50 * 2.5, rel=0.1)

    paper = convert_pdf_file_to_ir(path, scale="none")
    assert paper.document["header"]["metadata"]["pdf"]["coordinate_space"] == "paper"
    assert "linetype_scale" not in paper.document["header"]
    assert not {"PDF_SCALE_ESTIMATED", "PDF_SCALE_UNKNOWN"} & _codes(paper)

    explicit = convert_pdf_file_to_ir(path, scale=100)
    assert (
        explicit.document["header"]["metadata"]["pdf"]["paper_scale"]["label"]
        == "1:100"
    )
    assert "PDF_SCALE_APPLIED" in _codes(explicit)
    with pytest.raises(ImporterError):
        convert_pdf_file_to_ir(path, scale="half")


def test_scale_stays_paper_when_dimensions_disagree(tmp_path: Path) -> None:
    content = (
        b"q 100 100 m 300 100 l S Q\n"
        + text(150, 110, 10, "1000")
        + text(400, 110, 10, "7")
    )
    result = convert_pdf_file_to_ir(_write(tmp_path, PdfSpec(pages=[PdfPage(content)])))
    assert result.document["header"]["metadata"]["pdf"]["coordinate_space"] == "paper"
    assert "PDF_SCALE_UNKNOWN" in _codes(result)


def test_images_keep_placement_and_can_embed_pixels(tmp_path: Path) -> None:
    content = b"q 120 0 0 60 700 400 cm /Im1 Do Q\n"
    placeholder = _convert(tmp_path, content, images={"Im1": (1, 1, PNG_RED_1X1)})
    image = _of(placeholder, "IMAGE")[0]
    assert image["insert"] == pytest.approx([700 * MM, 400 * MM], abs=1e-3)
    assert image["width"] == pytest.approx(120 * MM, abs=1e-3)
    assert image["height"] == pytest.approx(60 * MM, abs=1e-3)
    assert "data" not in image
    assert image["metadata"]["pdf"]["pixel_width"] == 1
    assert "PDF_IMAGE_PLACEHOLDER" in _codes(placeholder)

    embedded = _convert(
        tmp_path, content, images={"Im1": (1, 1, PNG_RED_1X1)}, embed_images=True
    )
    image = _of(embedded, "IMAGE")[0]
    assert image["mime_type"] == "image/png"
    assert base64.b64decode(image["data"]).startswith(b"\x89PNG")
    assert "PDF_IMAGE_PLACEHOLDER" not in _codes(embedded)


def test_duplicates_annotations_and_statistics(tmp_path: Path) -> None:
    content = b"q 100 100 m 200 100 l S Q\nq 200 100 m 100 100 l S Q\n"
    result = _convert(tmp_path, content, annots=2)
    assert len(_of(result, "LINE")) == 1
    assert {"PDF_DUPLICATE_REMOVED", "PDF_ANNOTATIONS_SKIPPED"} <= _codes(result)
    assert result.statistics["annotations"] == 2
    assert result.statistics["skipped_entity_counts"] == {"duplicates_removed": 1}
    assert result.statistics["source_format"] == "pdf"


def test_entity_provenance_can_be_omitted(tmp_path: Path) -> None:
    content = b"q 100 100 m 200 100 l S Q\n" + text(100, 200, 10, "x")
    result = convert_pdf_file_to_ir(
        _write(tmp_path, PdfSpec(pages=[PdfPage(content)])),
        options=ImportOptions(entity_provenance=False),
        scale="none",
    )
    for entity in result.document["entities"]:
        assert "source" not in entity and "metadata" not in entity


def test_registry_cli_and_missing_dependency(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert detect_source_format("sheet.PDF") == "pdf"
    path = _write(tmp_path, PdfSpec(pages=[PdfPage(b"q 10 10 m 50 10 l S Q\n")]))
    result = convert_file_to_ir(path)
    assert result.statistics["source_format"] == "pdf"
    assert len(result.document["entities"]) == 1

    output = tmp_path / "out.json"
    assert main(["import", str(path), "-o", str(output)]) == 0
    document = json.loads(output.read_text(encoding="utf-8"))
    assert document["source"]["format"] == "pdf"

    monkeypatch.setitem(sys.modules, "pypdfium2", None)
    with pytest.raises(MissingOptionalDependencyError):
        convert_pdf_file_to_ir(path)


def test_bezier_arc_fit_rejects_non_circular_curves() -> None:
    k = 4.0 / 3.0 * math.tan(math.pi / 8.0)
    arc = pdf_importer._bezier_arc((1.0, 0.0), (1.0, k), (k, 1.0), (0.0, 1.0), 1e-6)
    assert arc is not None
    assert arc.center == pytest.approx((0.0, 0.0), abs=1e-9)
    assert arc.radius == pytest.approx(1.0, abs=1e-9)
    assert arc.sweep == pytest.approx(math.pi / 2.0)
    assert (
        pdf_importer._bezier_arc((0.0, 0.0), (0.5, 1.0), (1.5, -1.0), (2.0, 0.0), 1e-3)
        is None
    )
    assert (
        pdf_importer._bezier_arc((0.0, 0.0), (1.0, 0.0), (2.0, 0.0), (3.0, 0.0), 1e-3)
        is None
    )


def test_paper_scale_helpers_cover_values_and_scaling() -> None:
    assert paper_scale.numeric_value("1,500") == 1500.0
    assert paper_scale.numeric_value("φ20") == 20.0
    assert paper_scale.numeric_value("16.0±0.5") == 16.0
    assert paper_scale.numeric_value("(1500)") == 1500.0
    assert paper_scale.numeric_value("M12") is None
    assert paper_scale.numeric_value("2-φ10") is None
    assert paper_scale.snap_paper_scale(50.4) == "1:50"
    assert paper_scale.snap_paper_scale(0.5) == "2:1"
    assert paper_scale.paper_scale_value("1:50") == 50.0
    hatch = {"kind": "HATCH", "loops": [{"vertices": [[1, 2], [3, 4], [5, 6]]}]}
    assert paper_scale.scale_entity(hatch, 2.0)["loops"][0]["vertices"] == [
        [2, 4],
        [6, 8],
        [10, 12],
    ]
    spline = {"kind": "SPLINE", "control_points": [[1, 1], [2, 2]], "knots": [0, 1]}
    assert paper_scale.scale_entity(spline, 3.0)["control_points"] == [[3, 3], [6, 6]]
    dim = {
        "kind": "DIMENSION",
        "definition": {"points": {"p1": [1, 1], "p2": None}, "measurement": 2.0},
    }
    scaled = paper_scale.scale_entity(dim, 10.0)
    assert scaled["definition"]["points"] == {"p1": [10, 10], "p2": None}
    assert scaled["definition"]["measurement"] == 20.0
    poly = {"kind": "LWPOLYLINE", "vertices": [[1, 1, 0.5], [2, 2]]}
    assert paper_scale.scale_entity(poly, 2.0)["vertices"] == [[2, 2, 0.5], [4, 4]]
