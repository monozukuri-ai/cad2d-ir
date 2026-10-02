"""Feature control frames, 3D polylines and meshes through the DXF codec and the DWG importer."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from cad2d_ir.codecs.dxf import dxf_to_ir, ir_to_dxf
from cad2d_ir.importers.base import ImportDiagnostic
from cad2d_ir.importers.dwg import dwg_document_to_ir
from cad2d_ir.schema import validate_ir
from cad2d_ir.tolerance import explode_tolerance, tolerance_rows

Pairs = list[tuple[int, Any]]


def _dxf(*records: Pairs, tables: Pairs | None = None) -> str:
    pairs: Pairs = []
    if tables:
        pairs += [(0, "SECTION"), (2, "TABLES"), *tables, (0, "ENDSEC")]
    pairs += [(0, "SECTION"), (2, "ENTITIES")]
    for record in records:
        pairs += record
    pairs += [(0, "ENDSEC"), (0, "EOF")]
    return "\n".join(f"{code}\n{value}" for code, value in pairs) + "\n"


def _polyline(handle: str, flags: int, vertices: list[Pairs]) -> Pairs:
    pairs: Pairs = [(0, "POLYLINE"), (5, handle), (8, "0"), (66, 1), (70, flags)]
    for vertex in vertices:
        pairs += [(0, "VERTEX"), (8, "0"), *vertex]
    return [*pairs, (0, "SEQEND"), (8, "0")]


def _vertex(x: float, y: float, z: float = 0.0, flags: int = 0) -> Pairs:
    return [(10, x), (20, y), (30, z), (70, flags)]


# ------------------------------------------------------------------ tolerance


def test_tolerance_rows_split_compartments_and_map_the_symbols() -> None:
    text = (
        "{\\Fgdt;j}%%v{\\Fgdt;n}0.05{\\Fgdt;m}%%vA%%v%%vC^J{\\Fgdt;u}%%v0.1^J^J%%v%%v"
    )

    assert tolerance_rows(text) == [
        ["⌖", "⌀0.05Ⓜ", "A", "C"],
        ["⏤", "0.1"],
    ]
    assert tolerance_rows("{\\Ftxt;B}%%v{\\H2.5;%%c12}%%v5%%d") == [["B", "⌀12", "5°"]]
    assert tolerance_rows("") == []


def test_explode_tolerance_draws_boxes_and_centered_texts() -> None:
    common = {"id": "E1", "layer": "DIM", "source": {"format": "dxf", "id": "1"}}
    entities = explode_tolerance(
        text="{\\Fgdt;j}%%v0.1^JA",
        insert=[10.0, 20.0],
        height=2.0,
        common=common,
    )

    assert [(entity["kind"], entity["id"]) for entity in entities] == [
        ("LWPOLYLINE", "E1_R1"),
        ("TEXT", "E1_R1T1"),
        ("LINE", "E1_R1D1"),
        ("TEXT", "E1_R1T2"),
        ("LWPOLYLINE", "E1_R2"),
        ("TEXT", "E1_R2T1"),
    ]
    assert all(entity["layer"] == "DIM" for entity in entities)
    # The gap is half the text height: rows are 4 high and the insert point is
    # the middle of the left edge of the first row.
    first_row, symbol, divider, value, second_row, datum = entities
    assert first_row["closed"] is True
    xs = [vertex[0] for vertex in first_row["vertices"]]
    ys = [vertex[1] for vertex in first_row["vertices"]]
    assert (min(xs), min(ys), max(ys)) == (10.0, 18.0, 22.0)
    # A symbol is 1.2 heights wide, a digit 0.8; both get the gap on each side.
    assert divider["p1"] == pytest.approx([10.0 + 1.2 * 2.0 + 2.0, 18.0])
    assert max(xs) == pytest.approx(divider["p1"][0] + 3 * 0.8 * 2.0 + 2.0)
    assert (symbol["halign"], symbol["valign"]) == ("center", "middle")
    assert symbol["insert"] == pytest.approx([10.0 + (1.2 * 2.0 + 2.0) / 2.0, 20.0])
    assert value["text"] == "0.1" and value["height"] == 2.0
    assert [vertex[1] for vertex in second_row["vertices"]] == [14.0, 14.0, 18.0, 18.0]
    assert datum["insert"][1] == 16.0
    # The shared fields are copied, not shared.
    assert symbol["source"] is not value["source"]

    with pytest.raises(ValueError):
        explode_tolerance(text="A", insert=[0.0, 0.0], height=0.0, common=common)
    assert (
        explode_tolerance(text="%%v", insert=[0.0, 0.0], height=1.0, common=common)
        == []
    )


def test_explode_tolerance_follows_the_rotation_and_a_known_gap() -> None:
    entities = explode_tolerance(
        text="A",
        insert=[0.0, 0.0],
        height=2.0,
        rotation_deg=90.0,
        gap=0.5,
        common={"id": "T"},
    )
    box, text = entities
    # Row height 3 (2 + 2 * 0.5); the row runs along +Y.
    assert box["vertices"] == [
        pytest.approx([1.5, 0.0]),
        pytest.approx([1.5, 2.6]),
        pytest.approx([-1.5, 2.6]),
        pytest.approx([-1.5, 0.0]),
    ]
    assert text["rotation"] == 90.0
    assert text["insert"] == pytest.approx([0.0, 1.3])


_DIMSTYLE_TABLE: Pairs = [
    (0, "TABLE"),
    (2, "DIMSTYLE"),
    (0, "DIMSTYLE"),
    (105, "27"),
    (2, "ISO-25"),
    (40, 2.0),
    (140, 2.5),
    (147, 0.625),
    (0, "ENDTAB"),
]


def _tolerance(*extra: tuple[int, Any]) -> Pairs:
    return [
        (0, "TOLERANCE"),
        (5, "2F"),
        (8, "DIM"),
        (3, "ISO-25"),
        (10, 10),
        (20, 20),
        (30, 0),
        (1, "{\\Fgdt;j}%%v0.1%%vA"),
        (11, 1),
        (21, 0),
        (31, 0),
        *extra,
    ]


def test_dxf_tolerance_is_drawn_with_the_size_of_its_dimension_style() -> None:
    diagnostics: list[ImportDiagnostic] = []
    document = dxf_to_ir(
        _dxf(_tolerance(), tables=_DIMSTYLE_TABLE), diagnostics=diagnostics
    )

    entities = document["entities"]
    assert [entity["kind"] for entity in entities] == [
        "LWPOLYLINE",
        "TEXT",
        "LINE",
        "TEXT",
        "LINE",
        "TEXT",
    ]
    assert [entity["id"] for entity in entities][:2] == ["E2F_R1", "E2F_R1T1"]
    texts = [entity for entity in entities if entity["kind"] == "TEXT"]
    assert [text["text"] for text in texts] == ["⌖", "0.1", "A"]
    # DIMTXT 2.5 at DIMSCALE 2; the gap is DIMGAP at the same scale.
    assert {text["height"] for text in texts} == {5.0}
    ys = [vertex[1] for vertex in entities[0]["vertices"]]
    assert (min(ys), max(ys)) == (20.0 - 3.75, 20.0 + 3.75)
    for entity in entities:
        assert entity["layer"] == "DIM"
        assert entity["source"] == {"format": "dxf", "id": "2F", "kind": "TOLERANCE"}
        assert entity["metadata"]["dxf"]["tolerance_text"].startswith("{\\Fgdt;j}")
    exploded = [d for d in diagnostics if d.code == "DXF_TOLERANCE_EXPLODED"]
    assert len(exploded) == 1 and exploded[0].source_id == "2F"
    validate_ir(document, strict_jsonschema=True)
    # The frame is plain geometry from here on.
    assert "TOLERANCE" not in ir_to_dxf(document)


def test_dxf_tolerance_size_can_be_overridden_and_defaults_without_a_style() -> None:
    override = [
        (1001, "ACAD"),
        (1000, "DSTYLE"),
        (1002, "{"),
        (1070, 140),
        (1040, 4.0),
        (1070, 40),
        (1040, 1.0),
        (1002, "}"),
    ]
    document = dxf_to_ir(_dxf(_tolerance(*override), tables=_DIMSTYLE_TABLE))
    heights = {e["height"] for e in document["entities"] if e["kind"] == "TEXT"}
    assert heights == {4.0}

    # Without the style, the default text height of the drawing units.
    document = dxf_to_ir(_dxf(_tolerance()))
    heights = {e["height"] for e in document["entities"] if e["kind"] == "TEXT"}
    assert heights == {2.5}

    # A style record leaves out the variables that have their initial value.
    bare_style = [
        (0, "TABLE"),
        (2, "DIMSTYLE"),
        (0, "DIMSTYLE"),
        (105, "27"),
        (2, "ISO-25"),
        (0, "ENDTAB"),
    ]
    document = dxf_to_ir(_dxf(_tolerance(), tables=bare_style))
    heights = {e["height"] for e in document["entities"] if e["kind"] == "TEXT"}
    assert heights == {0.18}
    ys = [vertex[1] for vertex in document["entities"][0]["vertices"]]
    assert (min(ys), max(ys)) == pytest.approx((20.0 - 0.18, 20.0 + 0.18))


def test_dxf_tolerance_follows_its_direction() -> None:
    document = dxf_to_ir(
        _dxf(
            [
                (0, "TOLERANCE"),
                (5, "30"),
                (8, "0"),
                (3, "ISO-25"),
                (10, 0),
                (20, 0),
                (1, "A"),
                (11, 0),
                (21, 1),
            ],
            tables=_DIMSTYLE_TABLE,
        )
    )
    (text,) = [entity for entity in document["entities"] if entity["kind"] == "TEXT"]
    assert text["rotation"] == pytest.approx(90.0)
    assert text["insert"][0] == pytest.approx(0.0)
    assert text["insert"][1] > 0.0


# ------------------------------------------------------- polylines and meshes


def test_dxf_3d_polyline_is_projected_and_reported_when_it_leaves_the_plane() -> None:
    diagnostics: list[ImportDiagnostic] = []
    document = dxf_to_ir(
        _dxf(
            _polyline(
                "A1",
                8,
                [_vertex(0, 0, 0, 32), _vertex(10, 0, 5, 32), _vertex(10, 10, 0, 32)],
            ),
            _polyline(
                "A2",
                9,
                [_vertex(0, 0, 0, 32), _vertex(5, 0, 0, 32), _vertex(5, 5, 0, 32)],
            ),
        ),
        diagnostics=diagnostics,
    )

    first, second = document["entities"]
    assert first["vertices"] == [[0.0, 0.0], [10.0, 0.0], [10.0, 10.0]]
    assert second["closed"] is True
    projected = [d for d in diagnostics if d.code == "DXF_POLYLINE_3D_PROJECTED"]
    assert [d.source_id for d in projected] == ["EA1"]


def test_dxf_meshes_are_skipped_not_joined_into_a_path() -> None:
    diagnostics: list[ImportDiagnostic] = []
    face = [(10, 0), (20, 0), (30, 0), (70, 128), (71, 1), (72, 2), (73, 3)]
    document = dxf_to_ir(
        _dxf(
            # Polyface mesh: three vertices and one face record.
            _polyline(
                "B1",
                64,
                [
                    _vertex(5, 5, 0, 192),
                    _vertex(9, 5, 0, 192),
                    _vertex(9, 9, 0, 192),
                    face,
                ],
            ),
            # Polygon mesh: a 2 x 2 grid.
            _polyline(
                "B2",
                16,
                [
                    _vertex(0, 0, 0, 64),
                    _vertex(1, 0, 0, 64),
                    _vertex(0, 1, 0, 64),
                    _vertex(1, 1, 1, 64),
                ],
            ),
            _polyline("B3", 0, [_vertex(0, 0), _vertex(1, 0)]),
        ),
        diagnostics=diagnostics,
    )

    assert [entity["id"] for entity in document["entities"]] == ["EB3"]
    skipped = [d for d in diagnostics if d.code == "DXF_MESH_SKIPPED"]
    assert [d.source_id for d in skipped] == ["B1", "B2"]
    assert (
        "polyface mesh" in skipped[0].message and "polygon mesh" in skipped[1].message
    )


def test_dxf_spline_fit_polyline_leaves_out_its_frame() -> None:
    document = dxf_to_ir(
        _dxf(
            _polyline(
                "C1",
                4,
                [
                    # The control points of the frame come first.
                    _vertex(0, 0, 0, 16),
                    _vertex(5, 10, 0, 16),
                    _vertex(10, 0, 0, 16),
                    _vertex(0, 0, 0, 8),
                    _vertex(5, 5, 0, 8),
                    _vertex(10, 0, 0, 8),
                ],
            ),
            # A polyline that holds frame points only keeps them.
            _polyline("C2", 4, [_vertex(0, 0, 0, 16), _vertex(1, 1, 0, 16)]),
        )
    )

    fitted, frame_only = document["entities"]
    assert fitted["vertices"] == [[0.0, 0.0], [5.0, 5.0], [10.0, 0.0]]
    assert frame_only["vertices"] == [[0.0, 0.0], [1.0, 1.0]]


@dataclass
class _Entity:
    dxftype: str
    handle: int
    dxf: dict[str, Any]


class _Layout:
    def __init__(self, entities: list[_Entity]) -> None:
        self._entities = entities

    def query(self) -> list[_Entity]:
        return self._entities


class _Document:
    version = "AC1027"

    def __init__(self, entities: list[_Entity]) -> None:
        self._entities = entities

    def modelspace(self) -> _Layout:
        return _Layout(self._entities)


def _dwg_polyline(
    handle: int, points: list[tuple[float, float, float]], **dxf: Any
) -> _Entity:
    return _Entity(
        "POLYLINE_3D",
        handle,
        {"points": points, "flags": 0, "closed": False, "layer_handle": 16, **dxf},
    )


def test_dwg_3d_polyline_is_read_like_the_dxf_one() -> None:
    result = dwg_document_to_ir(
        _Document(
            [
                _dwg_polyline(
                    1, [(0.0, 0.0, 0.0), (10.0, 0.0, 0.0), (10.0, 10.0, 0.0)]
                ),
                # ezdwg repeats the first point of a closed polyline.
                _dwg_polyline(
                    2,
                    [
                        (0.0, 0.0, 2.0),
                        (5.0, 0.0, 2.0),
                        (5.0, 5.0, 4.0),
                        (0.0, 0.0, 2.0),
                    ],
                    flags=1,
                    closed=True,
                ),
                _dwg_polyline(3, [(1.0, 1.0, 0.0)]),
                _Entity("POLYLINE_MESH", 4, {"points": [], "layer_handle": 16}),
            ]
        ),
        layer_names_by_handle={16: "0"},
        options=None,
    )

    flat, closed = result.document["entities"]
    assert flat["kind"] == "LWPOLYLINE"
    assert flat["vertices"] == [[0.0, 0.0], [10.0, 0.0], [10.0, 10.0]]
    assert flat["source"] == {"format": "dwg", "id": "0x1", "kind": "POLYLINE_3D"}
    assert closed["closed"] is True
    assert closed["vertices"] == [[0.0, 0.0], [5.0, 0.0], [5.0, 5.0]]
    assert result.statistics["projected_entities"] == 1
    codes = {d.code: d for d in result.diagnostics}
    assert (
        "Projected 1 non-planar DWG POLYLINE_3D"
        in codes["DWG_NONPLANAR_PROJECTED"].message
    )
    assert result.statistics["skipped_entity_counts"] == {
        "POLYLINE_3D": 1,
        "POLYLINE_MESH": 1,
    }
    validate_ir(result.document, strict_jsonschema=True)
