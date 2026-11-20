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
