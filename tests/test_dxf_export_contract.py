from __future__ import annotations

import math

import pytest

from cad2d_ir import convert_ir_to_dxf_text
from cad2d_ir.codecs.dxf import dxf_to_ir, ir_to_dxf
from cad2d_ir.schema import validate_ir


def _document() -> dict:
    return {
        "format": "cad2d-ir",
        "version": "0.2.0",
        "header": {"units": "mm", "angle_unit": "deg", "coord_space": "world"},
        "tables": {
            "layers": {
                "Geometry": {
                    "color": "#FF0000",
                    "linetype": "DASHED",
                    "lineweight_mm": 0.25,
                    "plot": True,
                }
            },
            "linetypes": {
                "DASHED": {
                    "description": "Dashed",
                    "pattern_mm": [1.0, -0.5],
                }
            },
            "text_styles": {
                "NOTES": {
                    "font": "Arial",
                    "height": 0.0,
                    "width_factor": 0.8,
                    "oblique_deg": 5.0,
                }
            },
            "blocks": {
                "SYMBOL": {
                    "base_point": [0, 0],
                    "entities": [
                        {
                            "id": "B1",
                            "kind": "LINE",
                            "p1": [0, 0],
                            "p2": [1, 0],
                        }
                    ],
                }
            },
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
            {"id": "E4", "kind": "POINT", "position": [2, 2]},
            {
                "id": "E5",
                "kind": "ELLIPSE",
                "center": [3, 4],
                "major_axis": [2, 0],
                "ratio": 0.5,
                "start_param": 0,
                "end_param": math.tau,
            },
            {
                "id": "E6",
                "kind": "LWPOLYLINE",
                "vertices": [[0, 0], [1, 0, 0.1], [1, 1]],
                "closed": True,
            },
            {
                "id": "E7",
                "kind": "TEXT",
                "insert": [2, 2],
                "height": 0.5,
                "text": "ABC",
                "style": "NOTES",
            },
            {
                "id": "E8",
                "kind": "MTEXT",
                "insert": [2, 4],
                "height": 0.5,
                "text": "A\\PB",
                "style": "NOTES",
            },
            {
                "id": "E9",
                "kind": "INSERT",
                "block": "SYMBOL",
                "insert": [5, 6],
                "attributes": {"TAG": "VALUE"},
            },
            {
                "id": "E10",
                "kind": "HATCH",
                "loops": [{"vertices": [[0, 0], [2, 0], [2, 1], [0, 1]]}],
            },
            {
                "id": "E11",
                "kind": "SPLINE",
                "degree": 2,
                "control_points": [[0, 0], [1, 1], [2, 0]],
            },
            {
                "id": "E12",
                "kind": "DIMENSION",
                "dim_kind": "ALIGNED",
                "definition": {
                    "points": {
                        "location": [2, 2],
                        "p1": [0, 0],
                        "p2": [4, 0],
                    },
                    "text": "<>",
                },
            },
        ],
    }


def _handles_by_scope(document: dict) -> dict[str, set[str]]:
    result = {
        "modelspace": {
            entity["source"]["id"]
            for entity in document["entities"]
            if entity.get("source", {}).get("id")
        }
    }
    for name, definition in document.get("tables", {}).get("blocks", {}).items():
        result[f"block:{name}"] = {
            entity["source"]["id"]
            for entity in definition["entities"]
            if entity.get("source", {}).get("id")
        }
    return result


def test_r2010_handles_and_entity_map_roundtrip_for_supported_entities() -> None:
    result = convert_ir_to_dxf_text(_document(), target_version="AC1024")
    reparsed = dxf_to_ir(result.dxf_text, validate=True)
    handles_by_scope = _handles_by_scope(reparsed)

    assert "$HANDSEED" in result.dxf_text
    for subclass in (
        "AcDbSymbolTable",
        "AcDbLayerTableRecord",
        "AcDbPolyline",
        "AcDbMText",
        "AcDbHatch",
        "AcDbSpline",
        "AcDbDimension",
    ):
        assert f"100\n{subclass}\n" in result.dxf_text

    lines = result.dxf_text.splitlines()
    pairs = [(int(lines[index]), lines[index + 1]) for index in range(0, len(lines), 2)]
    record_handles: list[str] = []
    structural = {"SECTION", "ENDSEC", "ENDTAB", "EOF"}
    for index, pair in enumerate(pairs):
        if pair[0] == 0 and pair[1] not in structural:
            handle_index = index + 2 if pair[1] == "TABLE" else index + 1
            assert pairs[handle_index][0] == 5
            record_handles.append(pairs[handle_index][1])
    handseed_index = pairs.index((9, "$HANDSEED"))
    handseed = pairs[handseed_index + 1][1]
    assert len(record_handles) == len(set(record_handles))
    assert int(handseed, 16) > max(int(handle, 16) for handle in record_handles)

    assert len(result.entity_map) == 15
    assert all(entry["handle"] is not None for entry in result.entity_map)
    assert len({entry["handle"] for entry in result.entity_map}) == 15
    assert [entry["index"] for entry in result.entity_map] == list(range(15))
    assert [
        entry["dxf_type"] for entry in result.entity_map if entry["ir_id"] == "E12"
    ] == ["LINE", "TEXT", "DIMENSION"]
    assert any(
        diagnostic.code == "DXF_DIMENSION_BLOCK_GENERATED"
        and diagnostic.entity_id == "E12"
        for diagnostic in result.diagnostics
    )
    for entry in result.entity_map:
        assert entry["handle"] in handles_by_scope[entry["scope"]]


def test_r2010_output_and_mapping_are_deterministic() -> None:
    first = convert_ir_to_dxf_text(_document(), target_version="AC1024")
    second = convert_ir_to_dxf_text(_document(), target_version="AC1024")

    assert first.dxf_text == second.dxf_text
    assert first.entity_map == second.entity_map
    assert [item.as_dict() for item in first.diagnostics] == [
        item.as_dict() for item in second.diagnostics
    ]


def test_generic_dimension_explodes_to_mapped_primitives_and_can_be_skipped() -> None:
    document = {
        "format": "cad2d-ir",
        "version": "0.2.0",
        "header": {"units": "mm", "angle_unit": "deg", "coord_space": "world"},
        "entities": [
            {
                "id": "D1",
                "kind": "DIMENSION",
                "dim_kind": "GENERIC",
                "definition": {
                    "text": "100",
                    "source_geometry": {
                        "line": {
                            "start_x": 0,
                            "start_y": 5,
                            "end_x": 10,
                            "end_y": 5,
                        },
                        "text": {
                            "start_x": 4,
                            "start_y": 6,
                            "size_x": 2.5,
                            "size_y": 2.5,
                            "angle": 0,
                            "font_name": "STANDARD",
                            "content": "100",
                        },
                        "aux_lines": [
                            {
                                "start_x": 0,
                                "start_y": 0,
                                "end_x": 0,
                                "end_y": 5,
                            }
                        ],
                        "aux_points": [{"x": 10, "y": 5}],
                    },
                },
            }
        ],
    }

    exploded = convert_ir_to_dxf_text(document)
    reparsed = dxf_to_ir(exploded.dxf_text, validate=True)

    assert [entity["kind"] for entity in reparsed["entities"]] == [
        "LINE",
        "LINE",
        "TEXT",
        "POINT",
    ]
    assert len(exploded.entity_map) == 4
    assert {entry["ir_id"] for entry in exploded.entity_map} == {"D1"}
    assert {diagnostic.code for diagnostic in exploded.diagnostics} >= {
        "DXF_GENERIC_DIMENSION_EXPLODED"
    }

    skipped = convert_ir_to_dxf_text(document, generic_dimensions="skip")
    assert skipped.entity_map == [
        {
            "ir_id": "D1",
            "handle": None,
            "dxf_type": None,
            "index": 0,
            "scope": "modelspace",
            "reason_code": "DXF_GENERIC_DIMENSION_SKIPPED",
        }
    ]
    assert dxf_to_ir(skipped.dxf_text)["entities"] == []


def test_r12_downgrades_newer_entities_and_keeps_index_mapping() -> None:
    document = _document()
    document["entities"] = [
        entity
        for entity in document["entities"]
        if entity["kind"]
        in {
            "LWPOLYLINE",
            "MTEXT",
            "ELLIPSE",
            "SPLINE",
            "HATCH",
        }
    ]

    result = convert_ir_to_dxf_text(
        document,
        target_version="AC1009",
        curve_segments=16,
    )
    reparsed = dxf_to_ir(result.dxf_text, validate=True)

    assert "$ACADVER\n1\nAC1009" in result.dxf_text
    for unsupported in ("LWPOLYLINE", "MTEXT", "ELLIPSE", "SPLINE", "HATCH"):
        assert f"\n{unsupported}\n" not in result.dxf_text
    assert all(entry["handle"] is None for entry in result.entity_map)
    assert [entry["index"] for entry in result.entity_map] == list(
        range(len(result.entity_map))
    )
    assert [entity["kind"] for entity in reparsed["entities"]] == [
        "LWPOLYLINE",
        "LWPOLYLINE",
        "TEXT",
        "TEXT",
        "LWPOLYLINE",
        "LWPOLYLINE",
    ]
    assert {diagnostic.code for diagnostic in result.diagnostics} >= {
        "DXF_R12_LWPOLYLINE_EXPLODED",
        "DXF_R12_MTEXT_EXPLODED",
        "DXF_R12_ELLIPSE_APPROXIMATED",
        "DXF_R12_SPLINE_APPROXIMATED",
        "DXF_R12_HATCH_EXPLODED",
        "DXF_R12_TRUE_COLOR_APPROXIMATED",
        "DXF_R12_LINEWEIGHT_OMITTED",
    }


def test_tables_roundtrip_layer_linetype_and_text_style() -> None:
    result = convert_ir_to_dxf_text(_document())
    reparsed = dxf_to_ir(result.dxf_text, validate=True)
    tables = reparsed["tables"]
    validate_ir(reparsed, strict_jsonschema=True)

    assert "SECTION\n2\nTABLES" in result.dxf_text
    assert tables["layers"]["Geometry"] == {
        "color": "#FF0000",
        "linetype": "DASHED",
        "lineweight_mm": 0.25,
        "plot": True,
    }
    assert tables["linetypes"]["DASHED"]["pattern_mm"] == pytest.approx([1.0, -0.5])
    assert tables["text_styles"]["NOTES"]["font"] == "Arial"
    assert tables["text_styles"]["NOTES"]["width_factor"] == pytest.approx(0.8)


def test_invalid_writer_options_are_rejected() -> None:
    with pytest.raises(ValueError, match="target_version"):
        ir_to_dxf(_document(), target_version="AC1015")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="curve_segments"):
        ir_to_dxf(_document(), curve_segments=4)
    with pytest.raises(ValueError, match="generic_dimensions"):
        ir_to_dxf(
            _document(),
            generic_dimensions="invalid",  # type: ignore[arg-type]
        )


def test_unknown_entity_and_constraints_have_structured_export_diagnostics() -> None:
    document = {
        "format": "cad2d-ir",
        "version": "0.2.0",
        "header": {"units": "mm", "angle_unit": "deg", "coord_space": "world"},
        "entities": [{"id": "U1", "kind": "FUTURE_ENTITY"}],
        "constraints": [
            {
                "kind": "HORIZONTAL",
                "refs": [{"entity": "U1", "feature": "axis"}],
            }
        ],
    }

    result = convert_ir_to_dxf_text(document, validate=False)

    assert {diagnostic.code for diagnostic in result.diagnostics} >= {
        "DXF_UNKNOWN_ENTITY_SKIPPED",
        "DXF_CONSTRAINTS_OMITTED",
    }
    unknown = next(
        diagnostic
        for diagnostic in result.diagnostics
        if diagnostic.code == "DXF_UNKNOWN_ENTITY_SKIPPED"
    )
    assert unknown.entity_id == "U1"
    assert unknown.action == "skipped"
    assert result.entity_map[-1]["reason_code"] == "DXF_UNKNOWN_ENTITY_SKIPPED"
