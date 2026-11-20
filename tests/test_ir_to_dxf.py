from __future__ import annotations

from cad2d_ir.codecs.dxf import dxf_to_ir, ir_to_dxf


def test_ir_to_dxf_writes_mvp_entities_and_roundtrips() -> None:
    ir_document = {
        "format": "cad2d-ir",
        "version": "0.1.0",
        "header": {
            "units": "inch",
            "angle_unit": "deg",
            "coord_space": "world",
        },
        "entities": [
            {"id": "E1", "kind": "LINE", "p1": [0, 0], "p2": [5, 0]},
            {"id": "E2", "kind": "CIRCLE", "center": [3, 3], "radius": 2},
            {
                "id": "E3",
                "kind": "ARC",
                "center": [1, 1],
                "radius": 1,
                "start_angle": 0,
                "end_angle": 180,
            },
            {
                "id": "E4",
                "kind": "LWPOLYLINE",
                "vertices": [[0, 0], [1, 0, 0.1], [1, 1]],
                "closed": True,
            },
            {
                "id": "E5",
                "kind": "TEXT",
                "insert": [2, 2],
                "height": 0.2,
                "text": "ABC",
                "rotation": 15,
                "halign": "center",
                "valign": "top",
            },
        ],
    }

    dxf_text = ir_to_dxf(ir_document, validate=True)
    reparsed = dxf_to_ir(dxf_text, validate=True)

    assert "LWPOLYLINE" in dxf_text
    assert [entity["kind"] for entity in reparsed["entities"]] == [
        "LINE",
        "CIRCLE",
        "ARC",
        "LWPOLYLINE",
        "TEXT",
    ]
    assert reparsed["header"]["units"] == "inch"

    line = reparsed["entities"][0]
    assert line["p1"] == [0.0, 0.0]
    assert line["p2"] == [5.0, 0.0]


def _arc_document(**arc: object) -> dict:
    return {
        "format": "cad2d-ir",
        "version": "0.1.0",
        "header": {"units": "mm", "angle_unit": "deg", "coord_space": "world"},
        "entities": [{"id": "A1", "kind": "ARC", "center": [10, 20], "radius": 5, **arc}],
    }


def _written_arc(document: dict, target_version: str = "AC1024") -> dict:
    written = dxf_to_ir(ir_to_dxf(document, target_version=target_version))
    (arc,) = [entity for entity in written["entities"] if entity["kind"] == "ARC"]
    return arc


def test_a_clockwise_arc_is_written_as_the_same_curve() -> None:
    # A DXF ARC runs counter-clockwise from its start to its end. A clockwise arc
    # of 26 degrees written with its angles as they are is read as the other 334.
    for target_version in ("AC1024", "AC1009"):
        arc = _written_arc(
            _arc_document(start_angle=0.0, end_angle=-26.0, ccw=False), target_version
        )
        assert arc.get("ccw", True) is True
        assert (arc["start_angle"], arc["end_angle"]) == (334.0, 0.0)


def test_arc_angles_outside_one_turn_are_written_inside_it() -> None:
    arc = _written_arc(_arc_document(start_angle=448.5, end_angle=472.5, ccw=True))
    assert (arc["start_angle"], arc["end_angle"]) == (88.5, 112.5)

    untouched = _written_arc(_arc_document(start_angle=350.0, end_angle=10.0))
    assert (untouched["start_angle"], untouched["end_angle"]) == (350.0, 10.0)


def _generic_dimension(**definition: object) -> dict:
    return {
        "format": "cad2d-ir",
        "version": "0.1.0",
        "header": {"units": "mm", "angle_unit": "deg", "coord_space": "world"},
        "entities": [
            {
                "id": "D1",
                "kind": "DIMENSION",
                "dim_kind": "GENERIC",
                "definition": {
                    "points": {
                        "p1": [0.0, 0.0],
                        "p2": [0.0, 40.0],
                        "location": [-5.0, 20.0],
                        "text_midpoint": [-5.0, 20.0],
                    },
                    "text": "40",
                    **definition,
                },
            }
        ],
    }


def _dimension_text(document: dict) -> dict:
    written = dxf_to_ir(ir_to_dxf(document))
    texts = [
        entity
        for scope in (
            written["entities"],
            *(block["entities"] for block in written["tables"].get("blocks", {}).values()),
        )
        for entity in scope
        if entity["kind"] == "TEXT" and entity["text"] == "40"
    ]
    (text,) = texts
    return text


def test_generic_dimension_with_a_text_height_is_centred_and_turned() -> None:
    # A dimension that states the height of its text (an MI dimension with its text
    # attribute) describes the text itself: 1.1 high, its middle at the text
    # midpoint, along the direction it states.
    text = _dimension_text(_generic_dimension(text_height=1.1, text_rotation=90.0))

    assert text["height"] == 1.1
    assert text["rotation"] == 90.0
    assert (text["halign"], text["valign"]) == ("center", "middle")
    assert text["insert"] == [-5.0, 20.0]


def test_generic_dimension_without_a_text_height_is_written_as_before() -> None:
    text = _dimension_text(_generic_dimension(text_rotation=90.0))

    assert text["height"] == 2.5
    assert text.get("rotation", 0.0) == 0.0
    assert (text.get("halign", "left"), text.get("valign", "baseline")) == ("left", "baseline")
    assert text["insert"] == [-5.0, 20.0]
