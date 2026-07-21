from __future__ import annotations

import pytest

from cad2d_ir.codecs.dxf import dxf_to_ir, ir_to_dxf


def test_point_and_ellipse_roundtrip_through_dxf() -> None:
    document = {
        "format": "cad2d-ir",
        "version": "0.2.0",
        "header": {"units": "mm", "angle_unit": "deg", "coord_space": "world"},
        "entities": [
            {"id": "P1", "kind": "POINT", "position": [1.0, 2.0]},
            {
                "id": "EL1",
                "kind": "ELLIPSE",
                "center": [3.0, 4.0],
                "major_axis": [5.0, 1.0],
                "ratio": 0.4,
                "start_param": 0.25,
                "end_param": 2.5,
            },
        ],
    }

    dxf = ir_to_dxf(document, validate=True)
    reparsed = dxf_to_ir(dxf, validate=True)

    assert [entity["kind"] for entity in reparsed["entities"]] == ["POINT", "ELLIPSE"]
    assert reparsed["entities"][0]["position"] == pytest.approx([1.0, 2.0])
    ellipse = reparsed["entities"][1]
    assert ellipse["major_axis"] == pytest.approx([5.0, 1.0])
    assert ellipse["ratio"] == pytest.approx(0.4)
    assert ellipse["start_param"] == pytest.approx(0.25)
    assert ellipse["end_param"] == pytest.approx(2.5)


def test_generic_dimension_is_not_misrepresented_as_linear_dxf() -> None:
    document = {
        "format": "cad2d-ir",
        "version": "0.2.0",
        "header": {"units": "mm", "angle_unit": "deg", "coord_space": "world"},
        "entities": [
            {
                "id": "D1",
                "kind": "DIMENSION",
                "dim_kind": "GENERIC",
                "definition": {"text": "100"},
            }
        ],
    }
    warnings: list[str] = []

    dxf = ir_to_dxf(document, validate=True, warnings=warnings)

    assert "\nDIMENSION\n" not in dxf
    assert any("GENERIC dimension" in warning for warning in warnings)


def test_unitless_units_roundtrip_through_dxf_insunits_zero() -> None:
    document = {
        "format": "cad2d-ir",
        "version": "0.2.0",
        "header": {
            "units": "unitless",
            "angle_unit": "deg",
            "coord_space": "world",
        },
        "entities": [],
    }

    reparsed = dxf_to_ir(ir_to_dxf(document, validate=True), validate=True)

    assert reparsed["header"]["units"] == "unitless"
