from __future__ import annotations

import pytest

from cad2d_ir.codecs.dxf import dxf_to_ir, ir_to_dxf


def _entity_by_kind(document: dict, kind: str) -> dict:
    return next(entity for entity in document["entities"] if entity["kind"] == kind)


def test_dxf_to_ir_parses_spline_and_dimension() -> None:
    dxf_text = "\n".join(
        [
            "0",
            "SECTION",
            "2",
            "ENTITIES",
            "0",
            "SPLINE",
            "70",
            "1",
            "71",
            "3",
            "72",
            "8",
            "73",
            "4",
            "74",
            "0",
            "40",
            "0",
            "40",
            "0",
            "40",
            "0",
            "40",
            "1",
            "40",
            "2",
            "40",
            "3",
            "40",
            "3",
            "40",
            "3",
            "10",
            "0",
            "20",
            "0",
            "10",
            "1",
            "20",
            "0",
            "10",
            "2",
            "20",
            "1",
            "10",
            "3",
            "20",
            "0",
            "41",
            "1",
            "41",
            "1",
            "41",
            "1",
            "41",
            "1",
            "0",
            "DIMENSION",
            "2",
            "*D1",
            "3",
            "Standard",
            "70",
            "0",
            "10",
            "5",
            "20",
            "5",
            "13",
            "0",
            "23",
            "0",
            "14",
            "10",
            "24",
            "0",
            "1",
            "<>",
            "42",
            "10",
            "0",
            "ENDSEC",
            "0",
            "EOF",
            "",
        ]
    )

    warnings: list[str] = []
    ir = dxf_to_ir(dxf_text, validate=True, warnings=warnings)

    kinds = [entity["kind"] for entity in ir["entities"]]
    assert kinds == ["SPLINE", "DIMENSION"]
    assert warnings == []

    spline = _entity_by_kind(ir, "SPLINE")
    assert spline["degree"] == 3
    assert spline["closed"] is True
    assert len(spline["control_points"]) == 4
    assert spline["weights"] == pytest.approx([1, 1, 1, 1])

    dimension = _entity_by_kind(ir, "DIMENSION")
    assert dimension["dim_kind"] == "LINEAR"
    assert dimension["style"] == "Standard"
    assert dimension["definition"]["measurement"] == pytest.approx(10.0)


def test_ir_to_dxf_roundtrip_with_spline_dimension_and_constraints_warning() -> None:
    ir_document = {
        "format": "cad2d-ir",
        "version": "0.1.0",
        "header": {"units": "mm", "angle_unit": "deg", "coord_space": "world"},
        "entities": [
            {
                "id": "E1",
                "kind": "SPLINE",
                "degree": 2,
                "control_points": [[0, 0], [2, 1], [4, 0]],
                "weights": [1, 1, 1],
                "closed": False,
            },
            {
                "id": "E2",
                "kind": "DIMENSION",
                "dim_kind": "ALIGNED",
                "style": "Standard",
                "definition": {
                    "points": {"location": [2, 2], "p1": [0, 0], "p2": [4, 0]},
                    "text": "<>",
                    "measurement": 4.0,
                },
            },
        ],
        "constraints": [
            {
                "kind": "COINCIDENT",
                "refs": [
                    {"entity": "E1", "feature": "v[0]"},
                    {"entity": "E2", "feature": "p1"},
                ],
                "tolerance": 0.01,
            }
        ],
    }

    warnings: list[str] = []
    dxf_text = ir_to_dxf(ir_document, validate=True, warnings=warnings)
    reparsed = dxf_to_ir(dxf_text, validate=True)

    assert "SPLINE" in dxf_text
    assert "DIMENSION" in dxf_text
    assert any("constraints" in warning for warning in warnings)

    kinds = [entity["kind"] for entity in reparsed["entities"]]
    assert kinds == ["SPLINE", "DIMENSION"]

    spline = _entity_by_kind(reparsed, "SPLINE")
    assert spline["degree"] == 2
    assert len(spline["control_points"]) == 3

    dimension = _entity_by_kind(reparsed, "DIMENSION")
    assert dimension["dim_kind"] == "ALIGNED"
