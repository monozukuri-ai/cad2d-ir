from __future__ import annotations

import builtins
import io
import json
import math
from pathlib import Path

import pytest

ezhpgl = pytest.importorskip("ezhpgl")

from cad2d_ir import (  # noqa: E402
    ImporterError,
    ImportOptions,
    convert_file_to_ir,
    convert_hpgl_file_to_dxf,
    convert_hpgl_file_to_ir,
    detect_source_format,
    validate_ir,
)
from cad2d_ir.cli import main  # noqa: E402
from cad2d_ir.importers.base import MissingOptionalDependencyError  # noqa: E402
from cad2d_ir.importers.hpgl import hpgl_document_to_ir  # noqa: E402


def convert(data: bytes, **kwargs):
    return hpgl_document_to_ir(ezhpgl.parse(data), **kwargs)


def entities(result, page=0):
    return result.document["layouts"][page]["entities"]


def codes(result):
    return {d.code for d in result.diagnostics}


def save(tmp_path: Path, data: bytes) -> Path:
    path = tmp_path / "drawing.plt"
    path.write_bytes(data)
    return path


def test_layouts_preserve_pages_mm_order_pen_and_source():
    data = b"SP2;PA40,80;PD120,160;PG;SP3;PA400,800;PD1200,1600;PG1;"
    plot = ezhpgl.parse(data, profile=ezhpgl.DeviceProfile(paper_size_mm=(210, 297)))
    result = hpgl_document_to_ir(plot)
    doc = result.document
    assert doc["entities"] == [] and doc["header"]["units"] == "mm"
    assert [p["name"] for p in doc["layouts"]] == ["Page1", "Page2"]
    assert doc["layouts"][0]["paper"]["size_mm"] == [210, 297]
    line = next(e for e in entities(result) if e["kind"] == "LINE")
    assert line["p1"] == [1, 2] and line["p2"] == [3, 4]
    assert line["layer"] == "HPGL_PEN_2" and line["lineweight_mm"] == 0.35
    source = line["source"]["metadata"]
    assert data[slice(*source["span"])] == b"PD120,160;"
    assert source["page_index"] == 0 and source["command_index"] == 2
    assert entities(result, 1)[-1]["p1"] == [10, 20]  # no tiling/model scaling
    validate_ir(doc, strict_jsonschema=True)


def test_unknown_paper_and_blank_pages_are_not_fabricated():
    result = convert(b"PG1;PG1;")
    assert len(result.document["layouts"]) == 2
    assert all(
        p["paper"] == {"units": "mm"} and p["entities"] == []
        for p in result.document["layouts"]
    )
    assert result.statistics["completeness"] == "complete"


def test_analytic_circle_arc_ellipse_and_cubic_keep_geometry():
    result = convert(
        b"SP1;PA400,0;CI40;AA0,0,-90;IP0,0,400,800;SC0,10,0,10;PA0,0;CI2;PD;BZ0,1,1,1,1,0;"
    )
    kinds = [e["kind"] for e in entities(result)]
    assert "CIRCLE" in kinds and "ELLIPSE" in kinds and "SPLINE" in kinds
    circle = next(e for e in entities(result) if e["kind"] == "CIRCLE")
    assert circle["center"] == [10, 0] and circle["radius"] == 1
    ellipse = next(e for e in entities(result) if e["kind"] == "ELLIPSE")
    assert ellipse["major_axis"] == [0, 4] and ellipse["ratio"] == 0.5
    spline = next(e for e in entities(result) if e["kind"] == "SPLINE")
    assert spline["control_points"] == [[0, 0], [0, 2], [1, 2], [1, 0]]
    assert spline["knots"] == [0] * 4 + [1] * 4
    assert "HPGL_CURVE_APPROXIMATED" not in codes(result)
    validate_ir(result.document, strict_jsonschema=True)


@pytest.mark.parametrize("sc", ["SC0,10,0,10", "SC10,0,0,10", "SC0,10,10,0"])
@pytest.mark.parametrize("sweep", [-90, 90, -360, 360])
def test_ellipse_native_dxf_endpoints_and_closed_sweep(tmp_path, sc, sweep):
    ezdxf = pytest.importorskip("ezdxf")
    path = save(tmp_path, f"SP1;IP0,0,400,800;{sc};PA2,0;PD;AA0,0,{sweep};".encode())
    result = convert_hpgl_file_to_dxf(path)
    native = ezdxf.read(io.StringIO(result.exported.dxf_text))
    ellipse = next(iter(native.paperspace().query("ELLIPSE")))
    if abs(sweep) == 360:
        assert math.isclose(ellipse.dxf.end_param - ellipse.dxf.start_param, math.tau)
    else:
        source = ezhpgl.readfile(path).pages[0].operations[-1].geometry.points
        expected = sorted([(source[0].x, source[0].y), (source[-1].x, source[-1].y)])
        found = sorted(
            [
                (ellipse.start_point.x, ellipse.start_point.y),
                (ellipse.end_point.x, ellipse.end_point.y),
            ]
        )
        for a, b in zip(found, expected):
            assert a == pytest.approx(b, abs=1e-9)
    assert not native.audit().errors


def test_rectangle_clip_splits_reentries_and_does_not_bridge():
    result = convert(b"SP1;IW0,0,80,80;PU-40,40;PD120,40,120,120,40,120,40,-40;")
    lines = [e for e in entities(result) if e["kind"] == "LINE"]
    assert [(e["p1"], e["p2"]) for e in lines] == [([0, 1], [2, 1]), ([1, 2], [1, 0])]
    assert "HPGL_CLIP_APPROXIMATED" in codes(result)
    result = convert(b"SP1;IW0,0,0,0;PD40,0;")
    assert entities(result) == []


def test_clipped_curves_use_preview_and_mark_approximation():
    result = convert(b"SP1;IW0,0,400,400;CI400;")
    assert "CIRCLE" not in [e["kind"] for e in entities(result)]
    assert "HPGL_CURVE_APPROXIMATED" in codes(result)
    for e in entities(result):
        assert "approximation" in e


def test_evenodd_holes_hatches_and_pen_up_polygon_edges():
    result = convert(
        b"SP1;PM0;PD400,0,400,400,0,400,0,0;PM1;PU100,100;PD300,100,300,300,100,300,100,100;PM2;FP;FT3,40;FP;EP;"
    )
    hatch = next(e for e in entities(result) if e["kind"] == "HATCH")
    assert len(hatch["loops"]) == 2
    assert "HPGL_HATCH_EXPLODED" in codes(result)
    assert len([e for e in entities(result) if e["kind"] == "LINE"]) > 1
    validate_ir(result.document, strict_jsonschema=True)


@pytest.mark.parametrize(
    "command", [b"IW0,0,80,80;RA400,400;", b"PM0;PD400,0,400,400,0,0;PM2;FP1;"]
)
def test_unrepresentable_fill_is_reported_and_omitted(command):
    result = convert(b"SP1;" + command)
    assert entities(result) == []
    assert "HPGL_FILL_OMITTED" in codes(result)
    assert result.statistics["completeness"] == "partial"


def test_text_raw_bytes_attributes_reflection_and_clip():
    result = convert(b"SP1;SD1,14;SI-.2,.3;LBA\xe9\x03")
    label = result.document["layouts"][0]["metadata"]["hpgl"]["labels"][0]
    assert (
        label["data_hex"] == "41e9" and label["state"]["standard"]["symbol_set"] == 14
    )
    assert [e["kind"] for e in entities(result)] == ["INSERT", "INSERT"]
    assert all(e["scale"][1] == -1 for e in entities(result))
    text = next(iter(result.document["tables"]["blocks"].values()))["entities"][0]
    assert (
        text["text"] == "A" and text["source"]["metadata"]["font"]["symbol_set"] == 14
    )
    validate_ir(result.document, strict_jsonschema=True)
    result = convert(b"SP1;SI.2,.3;IW0,0,40,40;LBA\x03")
    assert entities(result) == [] and "HPGL_TEXT_CLIP_OMITTED" in codes(result)


def test_source_diagnostics_partial_prefix_and_container_omissions(tmp_path):
    path = save(tmp_path, b"\x1bE\x1b*b3Wabc\x0c\x1b%0BSP1;PD40,0;ZZ;PD80,0;")
    result = convert_hpgl_file_to_ir(path, strict=False)
    ds = [d for d in result.diagnostics if d.code == "HPGL_SOURCE_DIAGNOSTIC"]
    assert [d.details["code"] for d in ds] == ["OMITTED_RASTER", "UNSUPPORTED_SYNTAX"]
    assert ds[-1].action == "stop_interpretation" and ds[-1].severity == "error"
    assert result.document["layouts"][0]["metadata"]["hpgl"]["omitted_content"]
    assert entities(result) == [] and entities(result, 1)[-1]["p2"] == [1, 0]
    assert result.document["source"]["sha256"]
    with pytest.raises(ImporterError):
        convert_hpgl_file_to_ir(path)


def test_adapter_caps_and_missing_dependency_are_explicit(monkeypatch, tmp_path):
    with pytest.raises(ImporterError, match="entity limit"):
        convert(b"SP1;SI-.2,.3;LBA\x03", max_entities=1)
    with pytest.raises(ValueError):
        convert(b"", max_entities=-1)
    path = save(tmp_path, b"SP1;PD40,0;")
    with pytest.raises(ImporterError, match="LIMIT_EXCEEDED"):
        convert_hpgl_file_to_ir(
            path, strict=False, limits=ezhpgl.Limits(max_input_bytes=1)
        )
    original = builtins.__import__

    def missing(name, *args, **kwargs):
        if name == "ezhpgl":
            raise ImportError("unavailable")
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", missing)
    with pytest.raises(MissingOptionalDependencyError, match=r"cad2d-ir\[hpgl\]"):
        convert_hpgl_file_to_ir(path)


def test_registry_does_not_certify_plt_contents(tmp_path):
    path = save(tmp_path, b"%PDF-1.7\n")
    assert detect_source_format(path) == "hpgl"
    with pytest.raises(ImporterError, match="UNSUPPORTED_CONTAINER"):
        convert_file_to_ir(path)
    path.write_bytes(b"SP1;PD40,0;")
    result = convert_file_to_ir(path)
    assert result.statistics["source_format"] == "hpgl"
    no_source = convert(
        path.read_bytes(), options=ImportOptions(entity_provenance=False)
    )
    assert all("source" not in e for e in entities(no_source))


def test_dxf_page_selection_width_mapping_and_unused_blocks(tmp_path):
    ezdxf = pytest.importorskip("ezdxf")
    path = save(tmp_path, b"SP1;SI-.2,.3;LBSECRET\x03PG;PW.123;PA0,0;PD40,0;")
    with pytest.raises(ValueError, match="Multi-page"):
        convert_hpgl_file_to_dxf(path)
    result = convert_hpgl_file_to_dxf(path, page=1)
    assert len(result.imported.document["layouts"]) == 2
    assert (
        next(e for e in entities(result.imported, 1) if e["kind"] == "LINE")[
            "lineweight_mm"
        ]
        == 0.123
    )
    assert "HPGL_DXF_WIDTH_APPROXIMATED" in {
        d.code for d in result.exported.diagnostics
    }
    assert "DXF_LAYOUT_OMITTED" not in {d.code for d in result.exported.diagnostics}
    native = ezdxf.read(io.StringIO(result.exported.dxf_text))
    assert not list(native.query("TEXT")) and not list(native.modelspace())
    assert next(iter(native.paperspace().query("LINE"))).dxf.lineweight == 13
    assert not native.audit().errors
    for page in [-1, 2, True]:
        with pytest.raises(ValueError):
            convert_hpgl_file_to_dxf(path, page=page)


def test_cli_requires_page_preserves_output_on_failure_and_reports_partial(
    tmp_path, capsys
):
    path = save(tmp_path, b"SP1;LA1,4;PD40,0;PG;PD80,0;")  # LA: caps approximated, so partial
    out, report, ir = [tmp_path / n for n in ("out.dxf", "report.json", "all.json")]
    out.write_text("keep")
    assert main(["hpgl2dxf", str(path), "-o", str(out)]) == 2
    assert out.read_text() == "keep"
    assert (
        main(
            [
                "hpgl2dxf",
                str(path),
                "--page",
                "2",
                "-o",
                str(out),
                "--report",
                str(report),
                "--ir-output",
                str(ir),
                "--plot-size",
                "100",
                "100",
            ]
        )
        == 1
    )
    assert json.loads(report.read_text())["page"] == 2 and "SECTION" in out.read_text()
    assert len(json.loads(ir.read_text())["layouts"]) == 2
    assert main(["hpgl2dxf", str(path), "--page", "1", "-o", str(path)]) == 2
    assert path.read_bytes().startswith(b"SP1;")
    assert main(["import", str(path), "--format", "hpgl", "-o", str(ir)]) == 1
    assert json.loads(ir.read_text())["source"]["format"] == "hpgl"
    capsys.readouterr()


def test_closed_edges_dot_mode_and_collinear_fill():
    result = convert(b"SP1;EA400,400;")
    edge = next(e for e in entities(result) if e["kind"] == "LWPOLYLINE")
    assert edge["closed"] and len(edge["vertices"]) == 4
    result = convert(b"SP1;LT0;EA400,400;FT3,40;RA400,400;")
    assert entities(result) and all(e["kind"] == "POINT" for e in entities(result))
    assert "HPGL_DOT_APPROXIMATED" in codes(result)
    result = convert(b"SP1;RA400,0;")
    assert not entities(result) and "HPGL_FILL_OMITTED" in codes(result)


def test_start_dots_of_drawn_vectors_are_not_repeated_as_points():
    result = convert(b"SP1;PA0,0;PD100,0;PU;PA50,50;PD;PU;PA10,10;PD;PA10,10;")
    kinds = [e["kind"] for e in entities(result)]
    assert kinds.count("LINE") == 1 and kinds.count("POINT") == 2
    assert "HPGL_DOT_APPROXIMATED" in codes(result)
    result = convert(b"SP1;PA0,0;PD100,0;")
    assert [e["kind"] for e in entities(result)] == ["LINE"]
    assert "HPGL_DOT_APPROXIMATED" not in codes(result)


def test_label_mode_label_emits_one_text_per_line_and_guesses_cp932():
    data = b"IN;SP1;PA1000,2000;SI0.5,0.7;LBAb\r\ncd\x03"
    result = convert(data, label_mode="label")
    texts = [e for e in entities(result) if e["kind"] == "TEXT"]
    assert [t["text"] for t in texts] == ["Ab", "cd"]
    assert texts[0]["insert"] == pytest.approx([25.0, 50.0])
    assert texts[1]["insert"] == pytest.approx([25.0, 36.0])
    assert texts[0]["height"] == pytest.approx(7.0)
    assert texts[0]["source"]["metadata"]["line"] == 0
    assert "HPGL_TEXT_ENCODING_GUESSED" not in codes(result)
    assert len([e for e in entities(convert(data)) if e["kind"] == "TEXT"]) == 4
    result = convert(b"IN;SP1;PA0,0;LB\x82\xa0\x82\xa2x\x03", label_mode="label")
    (text,) = [e for e in entities(result) if e["kind"] == "TEXT"]
    assert text["text"] == "あいx"
    assert text["source"]["metadata"]["encoding"] == "cp932"
    assert "HPGL_TEXT_ENCODING_GUESSED" in codes(result)
    # Roman-8 accents (0xA0-0xFF) are not mistaken for Shift_JIS
    result = convert(b"IN;SP1;PA0,0;LBStra\xdee\x03", label_mode="label")
    (text,) = [e for e in entities(result) if e["kind"] == "TEXT"]
    assert text["text"].startswith("Stra")
    assert "HPGL_TEXT_ENCODING_GUESSED" not in codes(result)
    with pytest.raises(ValueError):
        convert(data, label_mode="words")


def test_model_page_promotes_one_page_and_operation_metadata_is_optional():
    data = b"SP1;PA0,0;PD100,0;PG;SP1;PA0,0;PD100,100;"
    result = convert(data, model_page=0, operation_metadata=False)
    doc = result.document
    assert [e["kind"] for e in doc["entities"]] == ["LINE"]
    assert [layout["name"] for layout in doc["layouts"]] == ["Page2"]
    assert "operations" not in doc["layouts"][0]["metadata"]["hpgl"]
    hpgl = doc["header"]["metadata"]["hpgl"]
    assert hpgl["model_page"] == 0 and hpgl["paper"] == {"units": "mm"}
    assert "operations" not in hpgl
    assert "HPGL_PAGE_PROMOTED" in codes(result)
    assert result.statistics["completeness"] == convert(data).statistics["completeness"]
    result = convert(data, model_page=0)
    assert "operations" in result.document["header"]["metadata"]["hpgl"]
    assert not convert(data, model_page=5).document["entities"]
    with pytest.raises(ValueError):
        convert(data, model_page=-1)
