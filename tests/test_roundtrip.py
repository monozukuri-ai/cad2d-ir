from __future__ import annotations

import pytest

from cad2d_ir.codecs.dxf import dxf_to_ir, ir_to_dxf


def _entity_by_kind(document: dict, kind: str) -> dict:
    return next(entity for entity in document["entities"] if entity["kind"] == kind)


def test_dxf_ir_dxf_roundtrip_preserves_core_geometry() -> None:
    original_dxf = "\n".join(
        [
            "0",
            "SECTION",
            "2",
            "ENTITIES",
            "0",
            "LINE",
            "10",
            "1",
            "20",
            "2",
            "11",
            "3",
            "21",
            "4",
            "0",
            "CIRCLE",
            "10",
            "5",
            "20",
            "6",
            "40",
            "2.5",
            "0",
            "ARC",
            "10",
            "7",
            "20",
            "8",
            "40",
            "1.25",
            "50",
            "15",
            "51",
            "75",
            "0",
            "LWPOLYLINE",
            "70",
            "1",
            "10",
            "0",
            "20",
            "0",
            "10",
            "1",
            "20",
            "0",
            "42",
            "0.2",
            "10",
            "1",
            "20",
            "1",
            "0",
            "TEXT",
            "10",
            "9",
            "20",
            "10",
            "40",
            "1",
            "1",
            "ROUNDTRIP",
            "0",
            "ENDSEC",
            "0",
            "EOF",
            "",
        ]
    )

    ir = dxf_to_ir(original_dxf, validate=True)
    rebuilt_dxf = ir_to_dxf(ir, validate=True)
    reparsed = dxf_to_ir(rebuilt_dxf, validate=True)

    line = _entity_by_kind(reparsed, "LINE")
    circle = _entity_by_kind(reparsed, "CIRCLE")
    arc = _entity_by_kind(reparsed, "ARC")
    polyline = _entity_by_kind(reparsed, "LWPOLYLINE")
    text = _entity_by_kind(reparsed, "TEXT")

    assert line["p1"] == pytest.approx([1.0, 2.0])
    assert line["p2"] == pytest.approx([3.0, 4.0])
    assert circle["center"] == pytest.approx([5.0, 6.0])
    assert circle["radius"] == pytest.approx(2.5)
    assert arc["start_angle"] == pytest.approx(15.0)
    assert arc["end_angle"] == pytest.approx(75.0)
    assert polyline["closed"] is True
    assert polyline["vertices"][1][2] == pytest.approx(0.2)
    assert text["text"] == "ROUNDTRIP"
