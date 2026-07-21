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
