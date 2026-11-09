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
            # A DIMSTYLE record carries its handle in group 105.
            assert pairs[handle_index][0] == (105 if pair[1] == "DIMSTYLE" else 5)
            record_handles.append(pairs[handle_index][1])
            # Every record names its owner right behind its handle.
            assert pairs[handle_index + 1][0] == 330
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
    # The face name is written as the file AutoCAD looks for, with the family
    # in the style's xdata.
    assert tables["text_styles"]["NOTES"]["font"] == "arial.ttf"
    assert tables["text_styles"]["NOTES"]["family"] == "Arial"
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


@pytest.mark.parametrize("target_version", ["AC1024", "AC1009"])
def test_line_breaks_in_text_keep_one_value_per_line(target_version: str) -> None:
    # Importers keep a multi-line MTEXT as raw line breaks (DWG, MI). A raw break
    # in a DXF value would shift every following group code.
    document = {
        "format": "cad2d-ir",
        "version": "0.2.0",
        "header": {"units": "mm", "angle_unit": "deg", "coord_space": "world"},
        "tables": {"layers": {"0": {}}},
        "entities": [
            {
                "id": "E1",
                "kind": "MTEXT",
                "insert": [0, 0],
                "height": 2.5,
                "text": "first\nsecond\r\nthird",
            },
            {
                "id": "E2",
                "kind": "TEXT",
                "insert": [0, 10],
                "height": 2.5,
                "text": "a\nb",
            },
            {"id": "E3", "kind": "LINE", "p1": [0, 0], "p2": [1, 0]},
        ],
    }
    text = ir_to_dxf(document, target_version=target_version)  # type: ignore[arg-type]

    lines = text.splitlines()
    assert len(lines) % 2 == 0
    assert all(code.strip().lstrip("-").isdigit() for code in lines[0::2])
    if target_version == "AC1024":
        assert "first\\Psecond\\Pthird" in lines
    assert "a^Jb" in lines
    restored = dxf_to_ir(text)
    assert [entity["kind"] for entity in restored["entities"]][-1] == "LINE"


def test_mtext_width_factor_is_written_in_front_of_the_text() -> None:
    # A DXF MTEXT has no width factor of its own: it is drawn with the one of
    # its text style unless the text sets one. Importers of formats that store
    # a factor per text (EXB, SXF, MI) put it on the entity.
    def mtext(index: int, text: str, **extra: float) -> dict:
        return {
            "id": f"M{index}",
            "kind": "MTEXT",
            "insert": [0, 10 * index],
            "height": 2.5,
            "text": text,
            **extra,
        }

    document = {
        "format": "cad2d-ir",
        "version": "0.2.0",
        "header": {"units": "mm", "angle_unit": "deg", "coord_space": "world"},
        "tables": {"layers": {"0": {}}},
        "entities": [
            mtext(0, "narrow", width_factor=0.667),
            mtext(1, "plain"),
            mtext(2, "one", width_factor=1.0),
            # Outside the 0.1 to 10 an MTEXT accepts
            mtext(3, "wide", width_factor=25.0),
            mtext(4, "{\\W0.5;a}b\nc", width_factor=1.5),
        ],
    }

    lines = ir_to_dxf(document).splitlines()
    assert "\\W0.667;narrow" in lines
    assert "\\W1.5;{\\W0.5;a}b\\Pc" in lines
    assert {"plain", "one", "wide"} <= set(lines)

    # R12 has no MTEXT: every TEXT it becomes carries the factor of its line.
    restored = dxf_to_ir(ir_to_dxf(document, target_version="AC1009"))
    factors = {
        entity["text"]: entity.get("width_factor")
        for entity in restored["entities"]
        if entity["kind"] == "TEXT"
    }
    assert factors == {
        "narrow": 0.667,
        "plain": None,
        "one": None,
        "wide": None,
        # "a" at 0.5 and "b" at 1.5: the line is as long as at 1.0
        "ab": None,
        "c": 1.5,
    }


def _mtext_document(entities: list[dict], **tables: dict) -> dict:
    return {
        "format": "cad2d-ir",
        "version": "0.2.0",
        "header": {"units": "mm", "angle_unit": "deg", "coord_space": "world"},
        "tables": {"layers": {"0": {}}, **tables},
        "entities": entities,
    }


def test_r12_mtext_lines_keep_their_place_width_and_height() -> None:
    def mtext(index: int, text: str, **extra: object) -> dict:
        return {
            "id": f"M{index}",
            "kind": "MTEXT",
            "insert": [100.0 * index, 100.0],
            "height": 5.0,
            "text": text,
            **extra,
        }

    document = _mtext_document(
        [
            # Top left (the default): the first baseline is one height below
            # the insertion point, the next one a line further down. An MTEXT
            # is drawn with the width factor of its text style.
            mtext(0, "one\\Ptwo", style="NARROW"),
            # Middle center: the lines are centered on the insertion point, and
            # a line advances by its own height.
            mtext(1, "{\\H2x;BIG}\\Psmall", attach="middle_center"),
            # Bottom right, upright: the last baseline is at the insertion point.
            mtext(2, "a\\Pb", attach="bottom_right", rotation=90.0),
            # An inline width, and a height given in drawing units.
            mtext(3, "\\W0.5;\\H2.5;half", style="NARROW"),
            # An empty line keeps its room.
            mtext(4, "first\\P\\Pthird"),
        ],
        text_styles={"NARROW": {"font": "arial.ttf", "width_factor": 0.8}},
    )

    restored = dxf_to_ir(ir_to_dxf(document, target_version="AC1009"))
    texts = {
        entity["text"]: (
            [round(value, 6) for value in entity["insert"]],
            entity["height"],
            entity.get("width_factor"),
            entity.get("halign", "left"),
            entity.get("rotation", 0.0),
        )
        for entity in restored["entities"]
    }
    assert texts == {
        "one": ([0.0, 95.0], 5.0, 0.8, "left", 0.0),
        "two": ([0.0, 87.0], 5.0, 0.8, "left", 0.0),
        "BIG": ([100.0, 99.0], 10.0, None, "center", 0.0),
        "small": ([100.0, 91.0], 5.0, None, "center", 0.0),
        "a": ([192.0, 100.0], 5.0, None, "right", 90.0),
        "b": ([200.0, 100.0], 5.0, None, "right", 90.0),
        "half": ([300.0, 97.5], 2.5, 0.5, "left", 0.0),
        "first": ([400.0, 95.0], 5.0, None, "left", 0.0),
        "third": ([400.0, 79.0], 5.0, None, "left", 0.0),
    }


def test_dwg_mtext_is_written_with_its_stored_inline_codes() -> None:
    # The DWG importer keeps the stored text beside the text without codes.
    def mtext(index: int, text: str, stored: str) -> dict:
        return {
            "id": f"M{index}",
            "kind": "MTEXT",
            "insert": [0.0, 10.0 * index],
            "height": 2.5,
            "text": text,
            "metadata": {"dwg": {"handle": index, "raw_text": stored}},
        }

    document = _mtext_document(
        [
            mtext(0, "HALF\nPLAIN", "{\\W0.5;HALF}\\PPLAIN"),
            # Edited since the import: the stored text no longer applies.
            mtext(1, "EDITED", "{\\W0.5;OLD}"),
        ]
    )

    lines = ir_to_dxf(document).splitlines()
    assert "{\\W0.5;HALF}\\PPLAIN" in lines
    assert "EDITED" in lines and "{\\W0.5;OLD}" not in lines

    restored = dxf_to_ir(ir_to_dxf(document, target_version="AC1009"))
    assert {
        entity["text"]: entity.get("width_factor") for entity in restored["entities"]
    } == {"HALF": 0.5, "PLAIN": None, "EDITED": None}


def test_long_mtext_is_written_in_chunks() -> None:
    text = "\\fArial|b0|i0;" + "0123456789" * 60
    document = _mtext_document(
        [{"id": "M", "kind": "MTEXT", "insert": [0, 0], "height": 2.5, "text": text}]
    )

    dxf = ir_to_dxf(document)
    lines = dxf.splitlines()
    codes, values = lines[0::2], lines[1::2]
    start = values.index("MTEXT")
    end = next(
        index for index in range(start + 1, len(codes)) if codes[index].strip() == "0"
    )
    chunks = [
        (int(code), value)
        for code, value in zip(codes[start:end], values[start:end])
        if int(code) in (1, 3)
    ]
    # 250 characters in every group 3, the rest in group 1
    assert [code for code, _ in chunks] == [3, 3, 1]
    assert [len(value) for _, value in chunks] == [250, 250, len(text) - 500]
    assert dxf_to_ir(dxf)["entities"][0]["text"] == text


@pytest.mark.parametrize("target_version", ["AC1024", "AC1009"])
def test_generic_dimension_is_written_as_the_geometry_of_its_block(
    target_version: str,
) -> None:
    # A dimension whose kind the importer could not tell carries what the source
    # drew for it. That is written, not a bare line and a text of default size.
    def dimension(index: int, block: str | None) -> dict:
        definition: dict = {
            "points": {"location": [0.0, 0.0], "text_midpoint": [5.0, 1.0]},
            "text": "R5",
            "measurement": 5.0,
        }
        if block is not None:
            definition["block"] = block
        return {
            "id": f"D{index}",
            "kind": "DIMENSION",
            "dim_kind": "GENERIC",
            "layer": "DIM",
            "definition": definition,
        }

    def geometry(prefix: str) -> list[dict]:
        return [
            {"id": f"{prefix}1", "kind": "LINE", "layer": "0", "p1": [0, 0], "p2": [10, 0]},
            {"id": f"{prefix}2", "kind": "LINE", "layer": "0", "p1": [10, 0], "p2": [9, 1]},
            {
                "id": f"{prefix}3",
                "kind": "TEXT",
                "layer": "NOTES",
                "insert": [5.0, 1.0],
                "height": 3.5,
                "text": "R5",
                "width_factor": 0.667,
                "halign": "center",
            },
        ]

    document = _mtext_document(
        [dimension(0, "*D0"), dimension(1, None), dimension(2, "*D2")],
        layers={"0": {}, "DIM": {}, "NOTES": {}},
        blocks={
            "*D0": {"base_point": [0.0, 0.0], "entities": geometry("A")},
            # A block that is not placed at the origin is not used.
            "*D2": {"base_point": [1.0, 0.0], "entities": geometry("B")},
        },
    )

    result = convert_ir_to_dxf_text(document, target_version=target_version)  # type: ignore[arg-type]
    restored = dxf_to_ir(result.dxf_text)
    kinds = [entity["kind"] for entity in restored["entities"]]
    # The block of D0, then one text each for D1 and D2 (no line: they have no
    # measured points).
    assert kinds == ["LINE", "LINE", "TEXT", "TEXT", "TEXT"]
    first, second, text = restored["entities"][:3]
    assert (first["p1"], first["p2"]) == ([0.0, 0.0], [10.0, 0.0])
    assert (second["p1"], second["p2"]) == ([10.0, 0.0], [9.0, 1.0])
    # Geometry on layer 0 follows the dimension; other layers stay.
    assert [first["layer"], second["layer"], text["layer"]] == ["DIM", "DIM", "NOTES"]
    assert (text["text"], text["height"], text["width_factor"], text["halign"]) == (
        "R5",
        3.5,
        0.667,
        "center",
    )
    # Without a block the text has the default height.
    assert [entity["height"] for entity in restored["entities"][3:]] == [2.5, 2.5]
    # Every primitive is mapped to its dimension.
    assert [entry["ir_id"] for entry in result.entity_map if entry["scope"] == "modelspace"] == [
        "D0",
        "D0",
        "D0",
        "D1",
        "D2",
    ]


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


def test_image_entities_are_skipped_for_r12_with_a_dedicated_diagnostic() -> None:
    # R12 has no raster images; R2010 output writes them (tests/test_dxf_images.py).
    document = {
        "format": "cad2d-ir",
        "version": "0.3.0",
        "header": {"units": "mm", "angle_unit": "deg", "coord_space": "world"},
        "entities": [
            {
                "id": "I1",
                "kind": "IMAGE",
                "insert": [0.0, 0.0],
                "width": 10.0,
                "height": 5.0,
                "name": "logo.bmp",
                "data": "Qk0=",
                "mime_type": "image/bmp",
            },
            {"id": "L1", "kind": "LINE", "p1": [0.0, 0.0], "p2": [1.0, 0.0]},
        ],
    }

    result = convert_ir_to_dxf_text(document, target_version="AC1009")

    skipped = [d for d in result.diagnostics if d.code == "DXF_IMAGE_SKIPPED"]
    assert len(skipped) == 1
    assert skipped[0].entity_id == "I1"
    assert skipped[0].action == "skipped"
    assert "DXF_UNKNOWN_ENTITY_SKIPPED" not in {d.code for d in result.diagnostics}
    assert "\nIMAGE\n" not in result.dxf_text
    assert "\nLINE\n" in result.dxf_text
    image_entry = next(entry for entry in result.entity_map if entry["ir_id"] == "I1")
    assert image_entry["reason_code"] == "DXF_IMAGE_SKIPPED"


def _jww_dimension_document(sxf_mode: int | None, *, angle: float = 0.0) -> dict:
    geometry = {
        "line": {"start_x": 0, "start_y": 5, "end_x": 10, "end_y": 5},
        "text": {
            "start_x": 4,
            "start_y": 6,
            "size_y": 2.5,
            "angle": angle,
            "content": "10",
        },
        # Saved stale coordinates, far outside the drawing
        "aux_lines": [
            {"start_x": -600, "start_y": 1000, "end_x": -600, "end_y": 1000},
            {"start_x": -600, "start_y": 1000, "end_x": -600, "end_y": 1000},
        ],
        "aux_points": [{"x": -600, "y": 1000}] * 4,
    }
    if sxf_mode is not None:
        geometry["sxf_mode"] = sxf_mode
    return {
        "format": "cad2d-ir",
        "version": "0.2.0",
        "header": {"units": "mm", "angle_unit": "deg", "coord_space": "world"},
        "entities": [
            {
                "id": "D1",
                "kind": "DIMENSION",
                "dim_kind": "GENERIC",
                "definition": {"text": "10", "source_geometry": geometry},
            }
        ],
    }


def test_auxiliaries_of_a_non_sxf_jww_dimension_are_not_written() -> None:
    """Mode 0 keeps zeros or stale coordinates in the auxiliary slots."""
    result = convert_ir_to_dxf_text(_jww_dimension_document(0))
    reparsed = dxf_to_ir(result.dxf_text, validate=True)
    assert [entity["kind"] for entity in reparsed["entities"]] == ["LINE", "TEXT"]
    assert reparsed["entities"][0]["p1"] == pytest.approx([0.0, 5.0])
    assert "-600.0" not in result.dxf_text


@pytest.mark.parametrize("sxf_mode", [1, None])
def test_auxiliaries_of_other_jww_dimensions_are_written(sxf_mode: int | None) -> None:
    """SXF mode uses them; a file older than version 4.20 has no mode to go by."""
    result = convert_ir_to_dxf_text(_jww_dimension_document(sxf_mode))
    kinds = [entity["kind"] for entity in dxf_to_ir(result.dxf_text)["entities"]]
    assert kinds.count("POINT") == 4
    assert kinds.count("LINE") == 2


@pytest.mark.parametrize("angle", [90.0, 45.0, 0.0])
def test_generic_dimension_text_angle_is_in_degrees(angle: float) -> None:
    result = convert_ir_to_dxf_text(_jww_dimension_document(0, angle=angle))
    text = next(
        e for e in dxf_to_ir(result.dxf_text)["entities"] if e["kind"] == "TEXT"
    )
    assert text.get("rotation", 0.0) == pytest.approx(angle)
