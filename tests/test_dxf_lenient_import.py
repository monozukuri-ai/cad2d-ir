"""Best-effort DXF import: malformed files/entities degrade with diagnostics."""

from __future__ import annotations

import math
from pathlib import Path

import pytest

from cad2d_ir.codecs.dxf import dxf_to_ir
from cad2d_ir.importers import import_file
from cad2d_ir.importers.base import ImportDiagnostic


def _dxf(entity_lines: list[str], *, tail: tuple[str, ...] | None = None) -> str:
    tail = tail if tail is not None else ("0", "ENDSEC", "0", "EOF", "")
    return "\n".join(["0", "SECTION", "2", "ENTITIES", *entity_lines, *tail])


def _line(handle: str) -> list[str]:
    return [
        "0",
        "LINE",
        "5",
        handle,
        "8",
        "0",
        "10",
        "0",
        "20",
        "0",
        "11",
        "1",
        "21",
        "1",
    ]


def _codes(diagnostics: list[ImportDiagnostic]) -> list[str]:
    return [diagnostic.code for diagnostic in diagnostics]


def _assert_vertices(actual: list[list[float]], expected: list[list[float]]) -> None:
    assert len(actual) == len(expected), (actual, expected)
    for got, want in zip(actual, expected, strict=True):
        assert got == pytest.approx(want, abs=1e-9), (actual, expected)


def test_trailing_blank_lines_and_data_after_eof_are_ignored() -> None:
    text = _dxf(_line("A"), tail=("0", "ENDSEC", "0", "EOF", "stray", "", "  ", ""))
    diagnostics: list[ImportDiagnostic] = []
    warnings: list[str] = []

    ir = dxf_to_ir(text, warnings=warnings, diagnostics=diagnostics)

    assert [entity["kind"] for entity in ir["entities"]] == ["LINE"]
    assert _codes(diagnostics) == ["DXF_TRAILING_DATA_IGNORED"]
    assert diagnostics[0].severity == "info"
    assert warnings == []


def test_odd_line_count_from_extra_trailing_newline_is_accepted() -> None:
    text = _dxf(_line("A")) + "\n"  # was rejected as "even number of lines" before
    ir = dxf_to_ir(text)
    assert [entity["kind"] for entity in ir["entities"]] == ["LINE"]


def test_truncated_file_keeps_complete_entities() -> None:
    complete = _line("A")
    partial = ["0", "LINE", "5", "B", "8", "0", "10", "5", "20"]  # dangling code line
    text = _dxf(complete + partial, tail=())
    diagnostics: list[ImportDiagnostic] = []

    ir = dxf_to_ir(text, diagnostics=diagnostics)

    assert [entity["kind"] for entity in ir["entities"]] == ["LINE"]
    assert ir["entities"][0]["id"] == "EA"
    assert _codes(diagnostics) == [
        "DXF_TRAILING_LINE_IGNORED",
        "DXF_ENTITY_CONVERSION_FAILED",
    ]
    assert diagnostics[1].source_kind == "LINE"
    assert diagnostics[1].source_id == "B"
    assert diagnostics[1].action == "skipped"


def test_leading_blank_lines_crlf_and_form_feed_inside_values() -> None:
    text = (
        "\r\n\r\n  0\r\nSECTION\r\n  2\r\nENTITIES\r\n  0\r\nTEXT\r\n  8\r\n0\r\n"
        " 10\r\n0\r\n 20\r\n0\r\n 40\r\n2.5\r\n  1\r\nab\x0ccd\r\n"
        "  0\r\nENDSEC\r\n  0\r\nEOF\r\n"
    )
    ir = dxf_to_ir(text)
    assert ir["entities"][0]["text"] == "ab\x0ccd"


def test_malformed_entities_are_skipped_not_fatal() -> None:
    zero_circle = ["0", "CIRCLE", "5", "C1", "8", "0", "10", "0", "20", "0", "40", "0"]
    empty_hatch = [
        "0",
        "HATCH",
        "5",
        "H1",
        "8",
        "0",
        "2",
        "SOLID",
        "70",
        "1",
        "91",
        "0",
    ]
    bad_number = ["0", "ARC", "5", "R1", "8", "0", "10", "x", "20", "0", "40", "1"]
    text = _dxf(zero_circle + empty_hatch + _line("L1") + bad_number)
    diagnostics: list[ImportDiagnostic] = []
    warnings: list[str] = []

    ir = dxf_to_ir(text, validate=True, warnings=warnings, diagnostics=diagnostics)

    assert [entity["kind"] for entity in ir["entities"]] == ["LINE"]
    assert _codes(diagnostics) == ["DXF_ENTITY_CONVERSION_FAILED"] * 3
    assert [diagnostic.source_kind for diagnostic in diagnostics] == [
        "CIRCLE",
        "HATCH",
        "ARC",
    ]
    assert all(diagnostic.severity == "error" for diagnostic in diagnostics)
    assert len(warnings) == 3
    assert "radius must be > 0" in warnings[0]


def test_malformed_entity_inside_block_is_skipped() -> None:
    text = "\n".join(
        [
            "0", "SECTION", "2", "BLOCKS",
            "0", "BLOCK", "2", "B1", "3", "B1", "8", "0", "10", "0", "20", "0", "70", "0",
            "0", "CIRCLE", "8", "0", "10", "0", "20", "0", "40", "-1",
            "0", "LINE", "8", "0", "10", "0", "20", "0", "11", "1", "21", "0",
            "0", "ENDBLK", "8", "0",
            "0", "ENDSEC",
            "0", "SECTION", "2", "ENTITIES",
            "0", "INSERT", "8", "0", "2", "B1", "10", "0", "20", "0",
            "0", "ENDSEC", "0", "EOF", "",
        ]
    )  # fmt: skip
    diagnostics: list[ImportDiagnostic] = []

    ir = dxf_to_ir(text, diagnostics=diagnostics)

    block = ir["tables"]["blocks"]["B1"]
    assert [entity["kind"] for entity in block["entities"]] == ["LINE"]
    assert _codes(diagnostics) == ["DXF_ENTITY_CONVERSION_FAILED"]
    assert diagnostics[0].message.startswith("[BLOCK:B1]")


@pytest.mark.parametrize(
    ("insunits", "expected_height"),
    [(None, 2.5), (4, 2.5), (1, 0.1), (6, 0.0025)],
)
def test_zero_text_height_is_defaulted_by_unit(
    insunits: int | None, expected_height: float
) -> None:
    header = (
        [
            "0",
            "SECTION",
            "2",
            "HEADER",
            "9",
            "$INSUNITS",
            "70",
            str(insunits),
            "0",
            "ENDSEC",
        ]
        if insunits is not None
        else []
    )
    text_entity = [
        "0",
        "TEXT",
        "5",
        "T1",
        "8",
        "0",
        "10",
        "0",
        "20",
        "0",
        "40",
        "0",
        "1",
        "abc",
    ]
    mtext_entity = [
        "0",
        "MTEXT",
        "5",
        "M1",
        "8",
        "0",
        "10",
        "0",
        "20",
        "0",
        "40",
        "-1",
        "1",
        "def",
    ]
    text = "\n".join(header + [_dxf(text_entity + mtext_entity)])
    diagnostics: list[ImportDiagnostic] = []

    ir = dxf_to_ir(text, diagnostics=diagnostics)

    heights = [entity["height"] for entity in ir["entities"]]
    assert heights == pytest.approx([expected_height, expected_height])
    assert _codes(diagnostics) == ["DXF_TEXT_HEIGHT_DEFAULTED"] * 2
    assert diagnostics[0].action == "normalized"


def _hatch(handle: str, boundary: list[str], *, pattern: str = "SOLID") -> list[str]:
    solid = "1" if pattern == "SOLID" else "0"
    return [
        "0", "HATCH", "5", handle, "8", "0", "2", pattern, "70", solid, "71", "0",
        *boundary,
        "75", "0", "76", "1", "98", "1", "10", "1", "20", "1",
    ]  # fmt: skip


def test_hatch_edge_path_with_lines_and_arcs_keeps_exact_geometry() -> None:
    quarter_bulge = math.tan(math.radians(90.0) / 4.0)
    boundary = [
        "91", "2",
        # Outer loop: edge path, square with the top-right corner rounded.
        "92", "1", "93", "4",
        "72", "1", "10", "0", "20", "0", "11", "10", "21", "0",
        # This edge is stored reversed relative to the chain; it must be flipped.
        "72", "1", "10", "10", "20", "5", "11", "10", "21", "0",
        # Clockwise arc (ccw=0): DXF stores complementary angles. Real geometry:
        # from (5,10) clockwise to (10,5) -> reversed into the ccw chain direction.
        "72", "2", "10", "5", "20", "5", "40", "5", "50", "270", "51", "360", "73", "0",
        "72", "1", "10", "5", "20", "10", "11", "0", "21", "10",
        "97", "0",
        # Inner loop: full circle given as a clockwise arc edge.
        "92", "16", "93", "1",
        "72", "2", "10", "3", "20", "3", "40", "1", "50", "0", "51", "360", "73", "0",
        "97", "0",
    ]  # fmt: skip
    diagnostics: list[ImportDiagnostic] = []

    ir = dxf_to_ir(_dxf(_hatch("H1", boundary)), validate=True, diagnostics=diagnostics)

    hatch = ir["entities"][0]
    assert hatch["kind"] == "HATCH"
    outer, inner = hatch["loops"]
    assert outer["is_outer"] is True and inner["is_outer"] is False
    _assert_vertices(
        outer["vertices"], [[0, 0], [10, 0], [10, 5, quarter_bulge], [5, 10], [0, 10]]
    )
    _assert_vertices(
        inner["vertices"],
        [
            [4, 3, -quarter_bulge],
            [3, 2, -quarter_bulge],
            [2, 3, -quarter_bulge],
            [3, 4, -quarter_bulge],
        ],
    )
    assert diagnostics == []


def test_hatch_ellipse_and_spline_edges_are_approximated_with_diagnostic() -> None:
    ellipse_boundary = [
        "91", "1", "92", "1", "93", "2",
        "72", "1", "10", "-5", "20", "10", "11", "5", "21", "10",
        # Half ellipse from (5,10) over the top back to (-5,10).
        "72", "3", "10", "0", "20", "10", "11", "5", "21", "0", "40", "0.5",
        "50", "0", "51", "180", "73", "1",
        "97", "0",
    ]  # fmt: skip
    spline_boundary = [
        "91", "1", "92", "1", "93", "2",
        "72", "4", "94", "3", "73", "0", "74", "0", "95", "0", "96", "4",
        "10", "0", "20", "0", "10", "3", "20", "5", "10", "7", "20", "-5", "10", "10", "20", "0",
        "97", "0",
        "72", "1", "10", "10", "20", "0", "11", "0", "21", "0",
        "97", "0",
    ]  # fmt: skip
    diagnostics: list[ImportDiagnostic] = []

    ir = dxf_to_ir(
        _dxf(
            _hatch("H1", ellipse_boundary)
            + _hatch("H2", spline_boundary, pattern="ANSI31")
        ),
        validate=True,
        diagnostics=diagnostics,
    )

    ellipse_hatch, spline_hatch = ir["entities"]
    ellipse_vertices = ellipse_hatch["loops"][0]["vertices"]
    assert ellipse_vertices[0] == pytest.approx([-5, 10])
    assert ellipse_vertices[1] == pytest.approx([5, 10])
    assert max(vertex[1] for vertex in ellipse_vertices) == pytest.approx(12.5)
    assert len(ellipse_vertices) > 10

    spline_vertices = spline_hatch["loops"][0]["vertices"]
    assert spline_vertices[0] == pytest.approx([0, 0])
    assert spline_vertices[-1] == pytest.approx([10, 0])
    assert len(spline_vertices) > 10
    assert spline_hatch["pattern"] == "ANSI31"
    assert spline_hatch["solid"] is False

    assert _codes(diagnostics) == ["DXF_HATCH_EDGE_APPROXIMATED"] * 2
    assert diagnostics[0].source_id == "EH1"
    assert diagnostics[0].action == "approximated"


def test_hatch_with_unusable_loop_is_skipped_but_other_loops_survive() -> None:
    boundary = [
        "91", "2",
        # Unknown edge type: loop is dropped.
        "92", "1", "93", "1", "72", "9", "10", "0", "20", "0", "97", "0",
        # Valid polyline loop.
        "92", "2", "72", "0", "73", "1", "93", "3",
        "10", "0", "20", "0", "10", "1", "20", "0", "10", "1", "20", "1",
        "97", "0",
    ]  # fmt: skip
    diagnostics: list[ImportDiagnostic] = []

    ir = dxf_to_ir(_dxf(_hatch("H1", boundary)), validate=True, diagnostics=diagnostics)

    hatch = ir["entities"][0]
    assert len(hatch["loops"]) == 1
    assert hatch["loops"][0]["is_outer"] is True
    assert _codes(diagnostics) == ["DXF_HATCH_LOOP_SKIPPED"]


def test_import_file_surfaces_structured_codes(tmp_path: Path) -> None:
    zero_circle = ["0", "CIRCLE", "5", "C1", "8", "0", "10", "0", "20", "0", "40", "0"]
    path = tmp_path / "lenient.dxf"
    path.write_text(_dxf(zero_circle + _line("L1")) + "\n", encoding="utf-8")

    result = import_file(path)

    codes = _codes(result.diagnostics)
    assert "DXF_ENTITY_CONVERSION_FAILED" in codes
    assert "DXF_IMPORT_WARNING" not in codes  # structured code is not duplicated
    assert result.statistics["converted_entities"] == 1
    assert result.statistics["skipped_entities"] == 1


@pytest.mark.parametrize(
    "text",
    [
        "",
        "\x00\x01 not a drawing",
        "hello\nworld\n",
        "0\nLINE\n10\n0\n20\n0\n11\n1\n21\n1\n",
    ],
)
def test_text_without_any_section_is_rejected(text: str) -> None:
    with pytest.raises(ValueError, match="no SECTION|Invalid DXF group code"):
        dxf_to_ir(text)


def test_solid_and_trace_become_solid_hatch_loops_in_outline_order() -> None:
    solid = [
        "0", "SOLID", "5", "S1", "8", "0",
        "10", "0", "20", "0", "11", "10", "21", "0", "12", "0", "22", "10", "13", "10", "23", "10",
    ]  # fmt: skip
    triangle = [
        "0", "TRACE", "5", "T1", "8", "0",
        "10", "0", "20", "0", "11", "4", "21", "0", "12", "2", "22", "3", "13", "2", "23", "3",
    ]  # fmt: skip
    ir = dxf_to_ir(_dxf(solid + triangle), validate=True)

    quad, tri = ir["entities"]
    assert quad["kind"] == "HATCH" and quad["source"]["kind"] == "SOLID"
    assert quad["loops"][0]["vertices"] == [[0, 0], [10, 0], [10, 10], [0, 10]]
    assert "solid" not in quad  # solid fill is the default
    assert tri["loops"][0]["vertices"] == [[0, 0], [4, 0], [2, 3]]


def test_leader_becomes_polyline_with_diagnostic() -> None:
    leader = [
        "0", "LEADER", "5", "L1", "8", "0", "3", "STANDARD", "71", "1", "72", "0", "73", "3",
        "76", "3", "10", "0", "20", "0", "10", "5", "20", "5", "10", "12", "20", "5",
    ]  # fmt: skip
    diagnostics: list[ImportDiagnostic] = []
    ir = dxf_to_ir(_dxf(leader), validate=True, diagnostics=diagnostics)

    entity = ir["entities"][0]
    assert entity["kind"] == "LWPOLYLINE" and entity["source"]["kind"] == "LEADER"
    assert entity["vertices"] == [[0, 0], [5, 5], [12, 5]]
    assert _codes(diagnostics) == ["DXF_LEADER_APPROXIMATED"]


def test_two_vertex_arc_loops_are_subdivided() -> None:
    circle_hatch = [
        "0", "HATCH", "5", "H1", "8", "0", "2", "SOLID", "70", "1", "71", "0", "91", "1",
        "92", "2", "72", "1", "73", "1", "93", "2",
        "10", "0", "20", "0", "42", "1.0", "10", "10", "20", "0", "42", "1.0",
        "97", "0", "75", "0", "76", "1",
    ]  # fmt: skip
    ir = dxf_to_ir(_dxf(circle_hatch), validate=True)
    vertices = ir["entities"][0]["loops"][0]["vertices"]
    half = math.tan(math.radians(90.0) / 4.0)
    _assert_vertices(
        vertices, [[0, 0, half], [5, 5, half], [10, 0, half], [5, -5, half]]
    )


def test_merged_line_break_is_resynced_at_next_record() -> None:
    good = _line("A")
    # "LINE" and its "5" handle line collapsed into one line -> the record is unreadable
    broken = [
        "0",
        "LINE 5",
        "55F",
        "330",
        "4B3",
        "8",
        "0",
        "10",
        "0",
        "20",
        "0",
        "11",
        "1",
        "21",
        "1",
    ]
    tail = _line("C")
    diagnostics: list[ImportDiagnostic] = []

    ir = dxf_to_ir(_dxf(good + broken + tail), diagnostics=diagnostics)

    assert [entity["id"] for entity in ir["entities"]] == ["EA", "EC"]
    assert "DXF_STREAM_RESYNCED" in _codes(diagnostics)


def _binary_dxf(entity_records: list[tuple[int, object]]) -> bytes:
    import struct

    def encode(code: int, value: object) -> bytes:
        payload = struct.pack("<H", code)
        if isinstance(value, float):
            return payload + struct.pack("<d", value)
        if isinstance(value, bool):
            return payload + bytes([int(value)])
        if isinstance(value, int):
            if 60 <= code <= 79 or 170 <= code <= 179 or 270 <= code <= 289:
                return payload + struct.pack("<h", value)
            return payload + struct.pack("<i", value)
        return payload + str(value).encode("cp932") + b"\x00"

    records: list[tuple[int, object]] = [
        (0, "SECTION"), (2, "HEADER"), (9, "$DWGCODEPAGE"), (3, "ANSI_932"), (0, "ENDSEC"),
        (0, "SECTION"), (2, "ENTITIES"), *entity_records, (0, "ENDSEC"), (0, "EOF"),
    ]  # fmt: skip
    return b"AutoCAD Binary DXF\r\n\x1a\x00" + b"".join(
        encode(c, v) for c, v in records
    )


def test_binary_dxf_is_imported(tmp_path: Path) -> None:
    data = _binary_dxf(
        [
            (0, "LINE"),
            (5, "A1"),
            (8, "壁"),
            (62, 1),
            (10, 1.5),
            (20, 2.5),
            (11, 3.0),
            (21, 4.0),
            (0, "TEXT"),
            (5, "A2"),
            (8, "0"),
            (10, 0.0),
            (20, 0.0),
            (40, 2.5),
            (1, "図面"),
        ]  # fmt: skip
    )
    path = tmp_path / "binary.dxf"
    path.write_bytes(data)

    result = import_file(path)

    codes = _codes(result.diagnostics)
    assert "DXF_BINARY_DETECTED" in codes
    line, text = result.document["entities"]
    assert (
        line["kind"] == "LINE" and line["p1"] == [1.5, 2.5] and line["p2"] == [3.0, 4.0]
    )
    assert line["layer"] == "壁"
    assert text["text"] == "図面" and text["height"] == 2.5
    assert result.document["source"]["metadata"]["binary"] is True
    assert result.statistics["encoding"] == "cp932"
