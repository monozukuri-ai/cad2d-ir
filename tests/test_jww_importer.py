from __future__ import annotations

import math

import pytest

from cad2d_ir import convert_ir_to_dxf_text
from cad2d_ir.importers import ImportOptions
from cad2d_ir.importers.jww import jww_document_to_ir
from cad2d_ir.schema import validate_ir


def _base(*, pen_style: int = 1) -> dict:
    return {
        "group": 0,
        "pen_style": pen_style,
        "pen_color": 2,
        "pen_width": 20,
        "layer": 0,
        "layer_group": 0,
        "flag": 0,
    }


def _text_payload(content: str = "100") -> dict:
    return {
        "start_x": 2.0,
        "start_y": 3.0,
        "end_x": 7.0,
        "end_y": 3.0,
        "text_type": 0,
        "size_x": 2.5,
        "size_y": 2.5,
        "spacing": 0.0,
        "angle": 0.0,
        "font_name": "Test Font",
        "content": content,
    }


def _document() -> dict:
    line = {
        "type": "LINE",
        "base": _base(),
        "start_x": 0.0,
        "start_y": 0.0,
        "end_x": 10.0,
        "end_y": 0.0,
    }
    dimension_line = {
        "start_x": 0.0,
        "start_y": 5.0,
        "end_x": 10.0,
        "end_y": 5.0,
    }
    return {
        "header": {
            "version": 600,
            "memo": "fixture",
            "paper_size": 3,
            "write_layer_group": 0,
            "layer_groups": [
                {
                    "state": 3,
                    "write_layer": 0,
                    "scale": 100.0,
                    "protect": 0,
                    "name": "Group 0",
                    "layers": [
                        {"state": 2, "protect": 0, "name": "Geometry"},
                        {"state": 2, "protect": 0, "name": "Geometry"},
                    ],
                }
            ],
        },
        "entities": [
            line,
            {
                "type": "CIRCLE",
                "base": _base(),
                "center_x": 2.0,
                "center_y": 2.0,
                "radius": 1.0,
                "start_angle": 0.0,
                "arc_angle": 2.0 * math.pi,
                "tilt_angle": 0.0,
                "flatness": 1.0,
                "is_full_circle": True,
            },
            {
                "type": "ARC",
                "base": _base(pen_style=2),
                "center_x": 5.0,
                "center_y": 5.0,
                "radius": 4.0,
                "start_angle": 0.0,
                "arc_angle": math.pi,
                "tilt_angle": math.pi / 4.0,
                "flatness": 0.5,
                "is_full_circle": False,
            },
            {
                "type": "POINT",
                "base": _base(),
                "x": 1.0,
                "y": 2.0,
                "is_temporary": True,
                "code": 3,
                "angle": 0.0,
                "scale": 0.0,
            },
            {"type": "TEXT", "base": _base(), **_text_payload("note")},
            {
                "type": "SOLID",
                "base": _base(),
                "point1_x": 0.0,
                "point1_y": 0.0,
                "point2_x": 1.0,
                "point2_y": 1.0,
                "point3_x": 0.0,
                "point3_y": 1.0,
                "point4_x": 1.0,
                "point4_y": 0.0,
                "color": None,
            },
            {
                "type": "CIRCLE_SOLID",
                "base": _base(pen_style=101),
                "center_x": 0.0,
                "center_y": 0.0,
                "radius": 3.0,
                "flatness": 1.0,
                "tilt_angle": 0.0,
                "start_angle": 0.0,
                "arc_angle": 2.0 * math.pi,
                "solid_mode": 100.0,
                "color": None,
            },
            {
                "type": "BLOCK",
                "base": _base(),
                "ref_x": 20.0,
                "ref_y": 30.0,
                "scale_x": -1.0,
                "scale_y": 2.0,
                "rotation": math.pi / 2.0,
                "def_number": 7,
                "block_name": "SYMBOL",
            },
            {
                "type": "DIMENSION",
                "base": _base(),
                "line": dimension_line,
                "text": _text_payload("100"),
                "sxf_mode": 0,
                "aux_lines": [dimension_line],
                "aux_points": [
                    {
                        "x": 0.0,
                        "y": 0.0,
                        "is_temporary": False,
                        "code": 0,
                        "angle": 0.0,
                        "scale": 0.0,
                    }
                ],
            },
        ],
        "block_defs": [
            {
                "number": 7,
                "is_referenced": True,
                "name": "SYMBOL",
                "base": _base(),
                "entities": [line],
            }
        ],
        "block_def_names": {7: "SYMBOL"},
        "entity_counts": {
            "LINE": 1,
            "CIRCLE": 1,
            "ARC": 1,
            "POINT": 1,
            "TEXT": 1,
            "SOLID": 1,
            "CIRCLE_SOLID": 1,
            "BLOCK": 1,
            "DIMENSION": 1,
        },
        "validation": {
            "total_references": 1,
            "resolved_references": 1,
            "unresolved_def_numbers": [],
            "has_unresolved": False,
        },
    }


def test_jww_document_to_ir_preserves_semantics_and_reports_approximation() -> None:
    result = jww_document_to_ir(
        _document(),
        source_name="fixture.jww",
        source_sha256="a" * 64,
    )
    document = result.document

    assert document["version"] == "0.2.0"
    assert document["source"] == {
        "format": "jww",
        "version": "600",
        "name": "fixture.jww",
        "sha256": "a" * 64,
    }
    assert document["header"]["units"] == "mm"
    assert set(document["tables"]["layers"]) == {"Geometry", "Geometry [0-1]"}
    assert "SYMBOL" in document["tables"]["blocks"]

    kinds = [entity["kind"] for entity in document["entities"]]
    assert kinds == [
        "LINE",
        "CIRCLE",
        "ELLIPSE",
        "POINT",
        "TEXT",
        "HATCH",
        "HATCH",
        "INSERT",
        "DIMENSION",
    ]

    point = document["entities"][3]
    assert point["temporary"] is True
    assert "scale" not in point

    approximated_hatch = document["entities"][6]
    assert approximated_hatch["approximation"]["source_kind"] == "CIRCLE_SOLID"
    assert len(approximated_hatch["loops"][0]["vertices"]) == 96

    insert = document["entities"][7]
    assert insert["scale"] == pytest.approx([-1.0, 2.0])
    assert insert["rotation"] == pytest.approx(90.0)

    dimension = document["entities"][8]
    assert dimension["dim_kind"] == "GENERIC"
    assert dimension["definition"]["text"] == "100"
    assert dimension["definition"]["source_geometry"]["aux_lines"]

    assert result.statistics["preserved_dimensions"] == 1
    assert result.statistics["approximated_entities"] == 1
    assert [diagnostic.code for diagnostic in result.diagnostics] == [
        "JWW_CURVE_APPROXIMATED"
    ]
    validate_ir(document, strict_jsonschema=True)


def test_jww_document_to_ir_leniently_skips_unknown_entities() -> None:
    source = _document()
    source["entities"] = [{"type": "FUTURE_ENTITY", "base": _base()}]
    source["entity_counts"] = {"FUTURE_ENTITY": 1}

    result = jww_document_to_ir(source, options=ImportOptions(strict=False))

    assert result.document["entities"] == []
    assert result.statistics["skipped_entity_counts"] == {"FUTURE_ENTITY": 1}
    assert result.diagnostics[0].code == "JWW_UNSUPPORTED_ENTITY"


def test_jww_generic_dimension_is_visible_in_dxf_and_mapped_one_to_many() -> None:
    imported = jww_document_to_ir(_document())
    dimension = next(
        entity
        for entity in imported.document["entities"]
        if entity["kind"] == "DIMENSION"
    )

    exported = convert_ir_to_dxf_text(imported.document)
    mapped = [
        entry for entry in exported.entity_map if entry["ir_id"] == dimension["id"]
    ]

    assert [entry["dxf_type"] for entry in mapped] == ["LINE", "TEXT", "POINT"]
    assert all(entry["handle"] is not None for entry in mapped)
    assert any(
        diagnostic.code == "DXF_GENERIC_DIMENSION_EXPLODED"
        and diagnostic.entity_id == dimension["id"]
        for diagnostic in exported.diagnostics
    )
