from __future__ import annotations

import sys
from pathlib import Path

import pytest

from cad2d_ir import paper_scale
from cad2d_ir.importers import pdf_structure
from cad2d_ir.importers.pdf import convert_pdf_file_to_ir

sys.path.insert(0, str(Path(__file__).resolve().parent))
from pdf_builder import PdfPage, PdfSpec, build_pdf, text  # noqa: E402

pytest.importorskip("pypdfium2")

MM = 25.4 / 72.0


def _line(x1: float, y1: float, x2: float, y2: float, **fields) -> dict:
    return {
        "kind": "LINE",
        "layer": "0",
        "linetype": "CONTINUOUS",
        "color": "#000000",
        "lineweight_mm": 0.25,
        "p1": [x1, y1],
        "p2": [x2, y2],
        **fields,
    }


def _dashes(x: float, y: float, lengths: list[float], gap: float) -> list[dict]:
    """Horizontal strokes of the given lengths with ``gap`` between them."""
    result = []
    for length in lengths:
        result.append(_line(x, y, x + length, y))
        x += length + gap
    return result


def _rebuild(entities: list[dict]) -> tuple[list[dict], dict, list[list[float]]]:
    patterns: list[list[float]] = []

    def linetype_for(pattern) -> str:
        patterns.append(list(pattern))
        return f"LT{len(patterns)}"

    result, stats = pdf_structure.rebuild_dashed_lines(entities, linetype_for)
    return result, stats, patterns


def test_strokes_of_a_dashed_line_become_one_line() -> None:
    entities = _dashes(10.0, 20.0, [2.0, 1.0, 1.0, 1.0, 1.0, 0.6], 1.0)
    entities.append(_line(0.0, 0.0, 50.0, 0.0))  # an ordinary line stays
    result, stats, patterns = _rebuild(entities)
    assert stats == {"lines": 1, "pieces": 6}
    assert patterns == [pytest.approx([1.0, -1.0])]
    dashed = [e for e in result if e["linetype"] != "CONTINUOUS"]
    assert len(dashed) == 1 and len(result) == 2
    assert dashed[0]["p1"] == pytest.approx([10.0, 20.0])
    assert dashed[0]["p2"] == pytest.approx([21.6, 20.0])
    assert dashed[0]["linetype"] == "LT1"


def test_a_slanted_dashed_line_snapped_to_the_printer_grid_is_joined() -> None:
    entities = []
    for i in range(6):
        t0, t1 = i * 2.0, i * 2.0 + 1.0
        # 600 dpi: coordinates land on a 0.042 mm grid
        snap = lambda v: round(v / 0.0423) * 0.0423  # noqa: E731
        entities.append(
            _line(
                snap(5 + 0.8 * t0),
                snap(5 + 0.6 * t0),
                snap(5 + 0.8 * t1),
                snap(5 + 0.6 * t1),
            )
        )
    result, stats, patterns = _rebuild(entities)
    assert stats["lines"] == 1
    assert patterns[0] == pytest.approx([1.0, -1.0], abs=0.08)
    assert len(result) == 1


def test_chain_lines_keep_their_short_dashes_in_the_pattern() -> None:
    # a centre line: long, short, long, short, long with long dashes of different length
    centre = _dashes(0.0, 0.0, [7.0, 1.0, 9.5, 1.0, 2.5], 1.0)
    # a double-chain line
    phantom = _dashes(0.0, 10.0, [8.0, 1.0, 1.0, 8.0, 1.0, 1.0, 8.0], 1.0)
    # a centre mark across a hole: long, short, long
    mark = _dashes(0.0, 20.0, [3.5, 1.0, 3.5], 1.0)
    result, stats, patterns = _rebuild(centre + phantom + mark)
    assert stats["lines"] == 3
    assert len(result) == 3
    by_y = {e["p1"][1]: e["linetype"] for e in result}
    # the centre line and the centre mark are the same linetype; the long dash
    # of a chain line is not compared
    assert by_y[0.0] == by_y[20.0] != by_y[10.0]
    assert sorted(len(p) for p in patterns) == [4, 6]


def test_a_centre_line_and_the_mark_next_to_it_are_two_lines() -> None:
    # long line, then a centre mark (long, short, long) on the same line, same gap
    entities = _dashes(0.0, 0.0, [19.6, 2.5, 1.0, 2.5], 1.0)
    result, stats, _patterns = _rebuild(entities)
    assert stats == {"lines": 1, "pieces": 3}
    assert sorted(e["linetype"] != "CONTINUOUS" for e in result) == [False, True]
    assert next(e for e in result if e["linetype"] == "CONTINUOUS")["p2"][
        0
    ] == pytest.approx(19.6)


def test_a_dash_drawn_in_two_strokes_counts_once() -> None:
    entities = _dashes(0.0, 0.0, [1.0, 1.0, 1.0, 1.0, 1.0], 1.0)
    # the third dash is split in the middle (the line crosses another there)
    third = entities.pop(2)
    entities += [_line(4.0, 0.0, 4.5, 0.0), _line(4.5, 0.0, 5.0, 0.0)]
    assert third["p1"] == [4.0, 0.0]
    result, stats, _patterns = _rebuild(entities)
    assert stats == {"lines": 1, "pieces": 5}
    assert len(result) == 1


def test_dashes_merged_where_dashed_lines_cross_are_tolerated() -> None:
    entities = _dashes(0.0, 0.0, [2.2, 1.0, 2.2, 1.0, 1.0, 0.6, 1.0, 1.0], 1.0)
    result, stats, patterns = _rebuild(entities)
    assert stats["lines"] == 1 and len(result) == 1
    assert patterns == [pytest.approx([1.0, -1.0])]


def test_lines_that_only_look_dashed_are_left_alone() -> None:
    # two long lines with one break
    broken = _dashes(0.0, 0.0, [30.0, 30.0], 1.0)
    # a hatch line cut twice by labels, on a slant: long pieces, equal gaps
    hatch = [
        _line(0.0, 10.0, 7.0, 17.0),
        _line(8.4, 18.4, 9.6, 19.6),
        _line(11.0, 21.0, 20.0, 30.0),
    ]
    # the top bars of the letters E E E: other strokes start at their ends
    bars = _dashes(0.0, 40.0, [1.5, 1.5, 1.5], 0.8)
    stems = [_line(b["p1"][0], 40.0, b["p1"][0], 37.0) for b in bars]
    # a shape filled with scan strokes: rows of collinear pieces right below each other
    rows = [
        e
        for i in range(4)
        for e in _dashes(0.0 + 0.02 * i, 50.0 + 0.1 * i, [0.55, 0.55, 0.55], 0.6)
    ]
    # uneven gaps
    uneven = [
        _line(0, 60, 1, 60),
        _line(1.5, 60, 2.5, 60),
        _line(4.5, 60, 5.5, 60),
        _line(9, 60, 10, 60),
    ]
    entities = broken + hatch + bars + stems + rows + uneven
    result, stats, _patterns = _rebuild(entities)
    assert stats == {"lines": 0, "pieces": 0}
    assert result is entities


def test_only_strokes_of_one_pen_are_joined() -> None:
    entities = _dashes(0.0, 0.0, [1.0, 1.0, 1.0, 1.0], 1.0)
    entities[1]["lineweight_mm"] = 0.5
    entities[2]["color"] = "#FF0000"
    _result, stats, _patterns = _rebuild(entities)
    assert stats["lines"] == 0


def test_layers_are_inferred_from_kind_linetype_and_width() -> None:
    entities = [
        _line(0, 0, 1, 0),
        _line(0, 1, 1, 1, lineweight_mm=0.5),
        _line(0, 2, 1, 2, linetype="DASHED_2"),
        _line(0, 3, 1, 3, linetype="CENTER", lineweight_mm=None),
        {"kind": "TEXT", "layer": "0", "text": "A"},
        {"kind": "HATCH", "layer": "0"},
        {"kind": "IMAGE", "layer": "0"},
        _line(0, 4, 1, 4, layer="Walls"),
    ]
    layers: dict[str, dict] = {"0": {}, "Walls": {}}
    counts = pdf_structure.infer_layers(entities, layers)
    assert counts == {
        "CENTER_0": 1,
        "DASHED_0.25": 1,
        "FILL": 1,
        "IMAGE": 1,
        "LINE_0.25": 1,
        "LINE_0.50": 1,
        "TEXT": 1,
    }
    assert [e["layer"] for e in entities] == [
        "LINE_0.25",
        "LINE_0.50",
        "DASHED_0.25",
        "CENTER_0",
        "TEXT",
        "FILL",
        "IMAGE",
        "Walls",
    ]
    assert set(layers) == set(counts) | {"Walls"}


def test_inferred_layers_name_the_colour_only_when_there_are_several() -> None:
    entities = [_line(0, 0, 1, 0), _line(0, 1, 1, 1, color="#FF0000")]
    counts = pdf_structure.infer_layers(entities, {"0": {}})
    assert counts == {"LINE_0.25_000000": 1, "LINE_0.25_FF0000": 1}


def test_inferred_layers_drop_colour_then_width_when_there_are_too_many() -> None:
    entities = [
        _line(0, i, 1, i, color=f"#{i:02X}0000", lineweight_mm=0.1 * (1 + i % 3))
        for i in range(pdf_structure.MAX_INFERRED_LAYERS + 6)
    ]
    counts = pdf_structure.infer_layers(entities, {"0": {}})
    assert sorted(counts) == ["LINE_0.10", "LINE_0.20", "LINE_0.30"]
    many = [
        _line(0, i, 1, i, lineweight_mm=0.01 * (1 + i))
        for i in range(pdf_structure.MAX_INFERRED_LAYERS + 6)
    ]
    assert list(pdf_structure.infer_layers(many, {"0": {}})) == ["LINE"]


def _texts(*items: tuple[str, float, float]) -> list[dict]:
    return [
        {"kind": "TEXT", "text": s, "insert": [x, y], "height": 2.5}
        for s, x, y in items
    ]


def test_labelled_scale_reads_the_title_block() -> None:
    assert paper_scale.labelled_scale(_texts(("SCALE 1:50", 0, 0))) == (50.0, 1)
    assert paper_scale.labelled_scale(_texts(("縮尺 1/100", 0, 0))) == (100.0, 1)
    assert paper_scale.labelled_scale(_texts(("尺度 2:1", 0, 0))) == (0.5, 1)
    # the word and the ratio in neighbouring cells
    cells = _texts(("DRAWING SCALE", 100, 10), ("1:2", 130, 10))
    assert paper_scale.labelled_scale(cells) == (2.0, 1)
    # a bare ratio far from any such word, and the scale of a single view
    assert (
        paper_scale.labelled_scale(_texts(("SCALE", 0, 0), ("1:2", 300, 200))) is None
    )
    assert (
        paper_scale.labelled_scale(_texts(("(S=1:2)", 0, 0), ("SECTION", 0, 5))) is None
    )
    assert paper_scale.labelled_scale(_texts(("1:50", 0, 0))) is None
    # two title blocks that disagree, and ratios that are not scales
    assert (
        paper_scale.labelled_scale(_texts(("SCALE 1:50", 0, 0), ("SCALE 1:20", 0, 9)))
        is None
    )
    assert paper_scale.labelled_scale(_texts(("SCALE 3:4", 0, 0))) is None
    assert paper_scale.labelled_scale(
        _texts(("SCALE 1:50", 0, 0), ("縮尺 1/50", 0, 9))
    ) == (50.0, 2)


def _convert(tmp_path: Path, content: bytes, **kwargs):
    path = tmp_path / "drawing.pdf"
    path.write_bytes(
        build_pdf(PdfSpec(pages=[PdfPage(content)], **kwargs.pop("spec", {})))
    )
    return convert_pdf_file_to_ir(path, **kwargs)


def _codes(result) -> set[str]:
    return {d.code for d in result.diagnostics}


def _dashed_content(y: float) -> bytes:
    # six dashes of 3 pt with 3 pt gaps, each its own stroke, as a printer driver draws them
    return b"".join(
        f"q 0.5 w {100 + 6 * i} {y} m {103 + 6 * i} {y} l S Q\n".encode()
        for i in range(6)
    )


def test_importer_rebuilds_dashes_and_infers_layers(tmp_path: Path) -> None:
    content = (
        _dashed_content(100)
        + b"q 2 w 50 50 m 400 50 l S Q\n"
        + b"q 0 0 1 rg 300 300 80 40 re f Q\n"
        + text(100, 200, 10, "NOTE")
    )
    result = _convert(tmp_path, content, scale="none")
    document = result.document
    lines = [e for e in document["entities"] if e["kind"] == "LINE"]
    assert len(lines) == 2
    dashed = next(e for e in lines if e["linetype"] != "CONTINUOUS")
    assert dashed["p1"] == pytest.approx([100 * MM, 100 * MM], abs=1e-3)
    assert dashed["p2"] == pytest.approx([133 * MM, 100 * MM], abs=1e-3)
    assert dashed["metadata"]["pdf"]["dash_pieces"] == 6
    assert document["tables"]["linetypes"][dashed["linetype"]][
        "pattern_mm"
    ] == pytest.approx([3 * MM, -3 * MM], abs=0.01)
    assert {e["layer"] for e in document["entities"]} == {
        "DASHED_0.18",
        "LINE_0.71",
        "FILL",
        "TEXT",
    }
    assert set(document["tables"]["layers"]) == {
        "DASHED_0.18",
        "LINE_0.71",
        "FILL",
        "TEXT",
    }
    assert {"PDF_DASHED_LINES_REBUILT", "PDF_LAYERS_INFERRED"} <= _codes(result)
    assert "PDF_LAYERS_FROM_OCG" not in _codes(result)
    stats = result.statistics
    assert stats["dashed_lines_rebuilt"] == 1
    assert stats["dash_pieces_joined"] == 6
    assert stats["inferred_layers"]["TEXT"] == 1
    assert stats["strokes"] == 7 and stats["short_strokes"] == 0

    plain = _convert(
        tmp_path, content, scale="none", rebuild_dashes=False, infer_layers=False
    )
    assert len([e for e in plain.document["entities"] if e["kind"] == "LINE"]) == 7
    assert {e["layer"] for e in plain.document["entities"]} == {"0"}
    assert not {"PDF_DASHED_LINES_REBUILT", "PDF_LAYERS_INFERRED"} & _codes(plain)
    assert plain.statistics["inferred_layers"] == {}


def test_importer_keeps_optional_content_layers(tmp_path: Path) -> None:
    content = b"q /OC /Walls BDC 50 50 m 200 50 l S EMC Q\nq 50 80 m 200 80 l S Q\n"
    result = _convert(tmp_path, content, scale="none", spec={"ocgs": ["Walls"]})
    assert {e["layer"] for e in result.document["entities"]} == {"Walls", "0"}
    assert "PDF_LAYERS_FROM_OCG" in _codes(result)
    assert "PDF_LAYERS_INFERRED" not in _codes(result)


def test_importer_counts_short_strokes(tmp_path: Path) -> None:
    # lettering drawn as lines: many strokes under a millimetre
    content = b"".join(
        f"q 0.3 w {100 + 3 * i} 100 m {100 + 3 * i} 102 l S Q\n".encode()
        for i in range(10)
    )
    result = _convert(tmp_path, content, scale="none")
    assert result.statistics["short_strokes"] == 10
    assert result.statistics["strokes"] == 10


def test_importer_takes_the_scale_from_the_title_block(tmp_path: Path) -> None:
    content = (
        b"q 100 100 m 300 100 l S Q\n"
        + text(500, 40, 8, "SCALE")
        + text(560, 40, 8, "1:50")
    )
    result = _convert(tmp_path, content)
    meta = result.document["header"]["metadata"]["pdf"]
    assert meta["coordinate_space"] == "model"
    assert meta["paper_scale"]["label"] == "1:50"
    assert meta["paper_scale"]["method"] == "title_block"
    assert meta["paper_scale"]["known"] is True
    assert "PDF_SCALE_FROM_TITLE_BLOCK" in _codes(result)
    assert "PDF_SCALE_UNKNOWN" not in _codes(result)
    line = next(e for e in result.document["entities"] if e["kind"] == "LINE")
    assert abs(line["p2"][0] - line["p1"][0]) == pytest.approx(200 * MM * 50, rel=1e-6)

    one_to_one = _convert(
        tmp_path,
        b"q 100 100 m 300 100 l S Q\n" + text(500, 40, 8, "SCALE 1:1"),
    )
    meta = one_to_one.document["header"]["metadata"]["pdf"]
    assert meta["coordinate_space"] == "paper"
    assert meta["paper_scale"]["known"] is True
    assert not {"PDF_SCALE_UNKNOWN", "PDF_SCALE_FROM_TITLE_BLOCK"} & _codes(one_to_one)

    unknown = _convert(tmp_path, b"q 100 100 m 300 100 l S Q\n")
    assert (
        unknown.document["header"]["metadata"]["pdf"]["paper_scale"]["known"] is False
    )
    assert "PDF_SCALE_UNKNOWN" in _codes(unknown)

    explicit = _convert(tmp_path, b"q 100 100 m 300 100 l S Q\n", scale=1)
    assert (
        explicit.document["header"]["metadata"]["pdf"]["paper_scale"]["known"] is True
    )
