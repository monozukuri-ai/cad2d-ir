from __future__ import annotations

from importlib.resources import files

from cad2d_ir.api import convert_dxf_text_to_ir, convert_ir_to_dxf_text
from cad2d_ir.schema import load_schema


def test_public_api_returns_warnings_for_constraints_omission() -> None:
    document = {
        "format": "cad2d-ir",
        "version": "0.1.0",
        "header": {"units": "mm", "angle_unit": "deg", "coord_space": "world"},
        "entities": [{"id": "E1", "kind": "LINE", "p1": [0, 0], "p2": [10, 0]}],
        "constraints": [
            {
                "kind": "HORIZONTAL",
                "refs": [{"entity": "E1", "feature": "p1->p2"}],
            }
        ],
    }

    result = convert_ir_to_dxf_text(document, validate=True)
    assert "LINE" in result.dxf_text
    assert any("constraints" in warning for warning in result.warnings)


def test_public_api_can_parse_dxf_and_expose_result_object() -> None:
    dxf_text = "\n".join(
        [
            "0",
            "SECTION",
            "2",
            "ENTITIES",
            "0",
            "LINE",
            "10",
            "0",
            "20",
            "0",
            "11",
            "1",
            "21",
            "1",
            "0",
            "ENDSEC",
            "0",
            "EOF",
            "",
        ]
    )
    result = convert_dxf_text_to_ir(dxf_text, validate=True)
    assert result.document["entities"][0]["kind"] == "LINE"
    assert result.warnings == []


def test_schema_is_packaged_and_loadable() -> None:
    resource = files("cad2d_ir.data").joinpath("ir_schema.json")
    assert resource.is_file()

    schema = load_schema()
    assert schema["title"] == "CAD-IR (Intermediate Representation for CAD Drawings)"
