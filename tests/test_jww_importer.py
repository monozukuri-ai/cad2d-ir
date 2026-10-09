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

    assert document["version"] == "0.3.0"
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


def test_jww_internal_setting_text_is_preserved_as_metadata_not_geometry() -> None:
    source = _document()
    setting = {
        "type": "TEXT",
        "base": _base(),
        **_text_payload("Printer_Orientation = 2"),
        "start_x": 0.0,
        "start_y": -1000.0,
        "end_x": 0.0,
        "end_y": -1000.0,
        "size_x": 3.0,
        "size_y": 3.0,
    }
    source["entities"].insert(0, setting)
    source["metadata_settings"] = [
        {
            "entity_index": 0,
            "key": "Printer_Orientation",
            "value": "2",
            "raw": "Printer_Orientation = 2",
        }
    ]
    source["entity_counts"]["TEXT"] = 2

    result = jww_document_to_ir(source)

    texts = [
        entity["text"]
        for entity in result.document["entities"]
        if entity["kind"] == "TEXT"
    ]
    assert texts == ["note"]
    assert result.document["header"]["metadata"]["jww"]["settings"] == [
        {
            "entity_index": 0,
            "key": "Printer_Orientation",
            "value": "2",
            "raw": "Printer_Orientation = 2",
        }
    ]
    assert result.statistics["metadata_settings"] == 1
    assert any(
        diagnostic.code == "JWW_METADATA_SETTING_EXTRACTED"
        and diagnostic.details
        == {
            "count": 1,
            "keys": ["Printer_Orientation"],
        }
        for diagnostic in result.diagnostics
    )


def test_jww_setting_fallback_requires_sentinel_coordinates() -> None:
    source = _document()
    visible = {
        "type": "TEXT",
        "base": _base(),
        **_text_payload("Printer_Orientation = 2"),
    }
    source["entities"] = [visible]
    source["entity_counts"] = {"TEXT": 1}

    result = jww_document_to_ir(source)

    assert result.document["entities"][0]["text"] == "Printer_Orientation = 2"
    assert "settings" not in result.document["header"]["metadata"]["jww"]
    assert result.statistics["metadata_settings"] == 0


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


def test_jww_parser_diagnostics_are_mapped_to_stable_codes() -> None:
    source = _document()
    source["diagnostics"] = [
        {
            "code": "ENTITY_LIST_TRUNCATED",
            "severity": "error",
            "message": "JWW entity list could not be read to its end",
            "action": "skipped",
            "details": {
                "byte_offset": 1234,
                "expected_entities": 500,
                "parsed_entities": 120,
                "error": "unexpected EOF while reading bytes",
            },
        },
        {
            "code": "CP932_DECODE_REPLACED",
            "severity": "warning",
            "message": "CP932 decoding replaced 1 undecodable character sequence(s) in x.",
            "action": "normalized",
            "details": {"field": "entity.text.content", "byte_offset": 10},
        },
        {"code": "SOMETHING_NEW", "severity": "info", "message": "ignored"},
    ]

    result = jww_document_to_ir(source)

    codes = [diagnostic.code for diagnostic in result.diagnostics]
    assert "JWW_ENTITY_LIST_TRUNCATED" in codes
    assert "JWW_DECODE_REPLACED" in codes
    truncated = next(
        diagnostic
        for diagnostic in result.diagnostics
        if diagnostic.code == "JWW_ENTITY_LIST_TRUNCATED"
    )
    assert truncated.severity == "error"
    assert truncated.action == "skipped"
    assert "120 of 500" in truncated.message
    assert truncated.details == {
        "byte_offset": 1234,
        "expected_entities": 500,
        "parsed_entities": 120,
        "error": "unexpected EOF while reading bytes",
    }
    validate_ir(result.document)


def test_entity_provenance_can_be_omitted_for_render_only_imports() -> None:
    full = jww_document_to_ir(
        _document(), source_name="fixture.jww", source_sha256="a" * 64
    )
    slim = jww_document_to_ir(
        _document(),
        source_name="fixture.jww",
        source_sha256="a" * 64,
        options=ImportOptions(entity_provenance=False),
    )
    validate_ir(slim.document)
    assert slim.statistics == full.statistics
    assert slim.diagnostics == full.diagnostics
    assert slim.document["source"] == full.document["source"]
    assert slim.document["header"] == full.document["header"]
    assert len(slim.document["entities"]) == len(full.document["entities"])
    for lean, rich in zip(slim.document["entities"], full.document["entities"]):
        assert "source" not in lean, lean["kind"]
        assert "metadata" not in lean, lean["kind"]
        assert "source" in rich and "metadata" in rich
        # Geometry, styling and dimension definitions are unchanged.
        stripped = {k: v for k, v in rich.items() if k not in {"source", "metadata"}}
        assert lean == stripped, lean["kind"]


def test_file_style_consumption_releases_raw_entities_without_changing_results() -> (
    None
):
    intact = _document()
    reference = jww_document_to_ir(
        intact, source_name="fixture.jww", source_sha256="a" * 64
    )
    assert all(entity is not None for entity in intact["entities"])

    raw = _document()
    consumed = jww_document_to_ir(
        raw, source_name="fixture.jww", source_sha256="a" * 64, consume_source=True
    )
    assert consumed.document == reference.document
    assert consumed.statistics == reference.statistics
    assert consumed.diagnostics == reference.diagnostics
    # Converted raw entities are released; the list keeps its length for statistics.
    assert len(raw["entities"]) == reference.statistics["source_entities"]
    assert raw["entities"].count(None) == reference.statistics[
        "converted_entities"
    ] + sum(reference.statistics["skipped_entity_counts"].values())


def _line_with_pen_style(pen_style: int, y: float) -> dict:
    return {
        "type": "LINE",
        "base": _base(pen_style=pen_style),
        "start_x": 0.0,
        "start_y": y,
        "end_x": 10.0,
        "end_y": y,
    }


def test_jww_line_types_follow_jw_cad_numbering() -> None:
    document = _document()
    pen_styles = [1, 2, 3, 4, 5, 6, 7, 8, 9, 12, 18, 31, 32, 38, 47]
    document["entities"] = [
        _line_with_pen_style(pen_style, float(index))
        for index, pen_style in enumerate(pen_styles)
    ]
    document["block_defs"] = []

    result = jww_document_to_ir(document)
    names = [entity["linetype"] for entity in result.document["entities"]]

    assert names == [
        "CONTINUOUS",
        "JWW_DASHED1",
        "JWW_DASHED2",
        "JWW_DASHED3",
        "JWW_DASHDOT1",
        "JWW_DASHDOT2",
        "JWW_DIVIDE1",
        "JWW_DIVIDE2",
        "JWW_CONSTRUCTION",
        "BYLAYER",  # random (freehand-looking) line: no dash pattern
        "JWW_DASHED_X2",
        "CONTINUOUS",  # SXF line type 1
        "SXF_DASHED",
        "SXF_CHAIN",
        "BYLAYER",  # SXF user-defined line type the file does not define
    ]
    linetypes = result.document["tables"]["linetypes"]
    assert set(names) <= set(linetypes)
    validate_ir(result.document, strict_jsonschema=True)


def test_jww_line_type_patterns_are_paper_millimetres() -> None:
    document = _document()
    document["entities"] = [_line_with_pen_style(32, 0.0)]
    document["block_defs"] = []

    linetypes = jww_document_to_ir(document).document["tables"]["linetypes"]

    # One pattern bit prints as printer pitch / 32 mm (default pitch 10).
    assert linetypes["JWW_DASHED1"]["pattern_mm"] == [0.625, -0.625]
    assert linetypes["JWW_DASHED3"]["pattern_mm"] == [1.875, -0.625]
    assert linetypes["JWW_DASHDOT1"]["pattern_mm"] == [3.125, -0.625, 0.625, -0.625]
    assert linetypes["JWW_DASHDOT2"]["pattern_mm"] == [8.125, -0.625, 0.625, -0.625]
    assert linetypes["JWW_DIVIDE1"]["pattern_mm"] == [
        2.5,
        -0.625,
        0.3125,
        -0.625,
        0.3125,
        -0.625,
    ]
    assert linetypes["JWW_DASHED_X4"]["pattern_mm"] == [37.5, -2.5]
    assert sum(abs(v) for v in linetypes["JWW_DASHDOT_X2"]["pattern_mm"]) == 20.0
    # SXF-compatible line types appear only when the drawing uses them.
    assert linetypes["SXF_DASHED"]["pattern_mm"] == [6.0, -1.5]
    assert "SXF_CHAIN" not in linetypes


def test_jww_construction_line_type_is_marked_as_not_plotted() -> None:
    document = _document()
    document["entities"] = [_line_with_pen_style(9, 0.0)]
    document["block_defs"] = []

    linetypes = jww_document_to_ir(document).document["tables"]["linetypes"]

    assert linetypes["JWW_CONSTRUCTION"]["plot"] is False
    assert all(
        "plot" not in definition
        for name, definition in linetypes.items()
        if name != "JWW_CONSTRUCTION"
    )


def test_jww_linetype_table_is_not_shared_between_documents() -> None:
    first = jww_document_to_ir(_document()).document["tables"]["linetypes"]
    first["JWW_DASHED1"]["pattern_mm"].append(99.0)
    second = jww_document_to_ir(_document()).document["tables"]["linetypes"]

    assert second["JWW_DASHED1"]["pattern_mm"] == [0.625, -0.625]


def _line_types_header(
    *,
    printer_pitch: int = 10,
    dashed1_segments: list[float] | None = None,
    sxf: list[dict] | None = None,
) -> dict:
    """``header["line_types"]`` as ``ezjww`` >= 0.3.3 reports it."""
    unit = printer_pitch / 32.0
    runs = {
        2: [2, 2],
        3: [4, 4],
        4: [6, 2],
        5: [10, 2, 2, 2],
        6: [26, 2, 2, 2],
        7: [8, 2, 1, 2, 1, 2],
        8: [24, 2, 1, 2, 1, 2],
        9: [1, 3],
    }
    standard = [
        {
            "number": number,
            "pattern": 0,
            "unit_dots": sum(run),
            "pitch": 1,
            "printer_pitch": printer_pitch,
            "runs": run,
            "segments_mm": [length * unit for length in run],
        }
        for number, run in runs.items()
    ]
    if dashed1_segments is not None:
        standard[0]["segments_mm"] = dashed1_segments
    return {
        "standard": standard,
        "random": [],
        "double_length": [
            {
                "number": 16,
                "pattern": 0,
                "unit_dots": 32,
                "pitch": 2,
                "printer_pitch": 20,
                "runs": [26, 2, 2, 2],
                "segments_mm": [16.25, 1.25, 1.25, 1.25],
            }
        ],
        "sxf": sxf,
    }


def _line_types_document(pen_styles: list[int], **settings: object) -> dict:
    document = _document()
    document["entities"] = [
        _line_with_pen_style(pen_style, float(index))
        for index, pen_style in enumerate(pen_styles)
    ]
    document["block_defs"] = []
    document["header"]["line_types"] = _line_types_header(**settings)  # type: ignore[arg-type]
    return document


def test_jww_line_type_patterns_follow_the_printer_pitch_of_the_file() -> None:
    # Printer pitch 5 prints every pattern at half the default size.
    result = jww_document_to_ir(_line_types_document([2, 5, 16], printer_pitch=5))
    linetypes = result.document["tables"]["linetypes"]

    assert linetypes["JWW_DASHED1"]["pattern_mm"] == [0.3125, -0.3125]
    assert linetypes["JWW_DASHDOT1"]["pattern_mm"] == [
        1.5625,
        -0.3125,
        0.3125,
        -0.3125,
    ]
    assert linetypes["JWW_DASHDOT_X2"]["pattern_mm"] == [16.25, -1.25, 1.25, -1.25]
    # Line types the file does not report keep the Jw_cad defaults.
    assert linetypes["JWW_DASHED_X4"]["pattern_mm"] == [37.5, -2.5]
    assert linetypes["JWW_CONSTRUCTION"]["plot"] is False
    jww = result.document["header"]["metadata"]["jww"]
    assert jww["line_type_settings"] == "file"
    validate_ir(result.document, strict_jsonschema=True)


def test_jww_line_type_made_solid_in_the_file_has_no_pattern() -> None:
    # A pattern without gaps reports no runs: the user turned this line type solid.
    document = _line_types_document([2], dashed1_segments=[])
    linetypes = jww_document_to_ir(document).document["tables"]["linetypes"]

    assert linetypes["JWW_DASHED1"]["pattern_mm"] == []
    assert linetypes["JWW_DASHED2"]["pattern_mm"] == [1.25, -1.25]


def test_jww_sxf_line_types_use_the_definitions_of_the_file() -> None:
    sxf = [
        {"number": 32, "name": "dashed", "segments_mm": [5.0, 2.0]},
        # Named without lengths (files written by other software): SXF reference.
        {"number": 38, "name": "chain", "segments_mm": []},
        {"number": 47, "name": "境界線", "segments_mm": [10.0, 1.0, 0.5, 1.0]},
        {"number": 48, "name": "", "segments_mm": [3.0, 1.0, 3.0]},
        {"number": 49, "name": "solid", "segments_mm": [8.0]},
        {"number": 50, "name": "", "segments_mm": []},
    ]
    document = _line_types_document([32, 38, 47, 48, 49, 50, 51], sxf=sxf)
    result = jww_document_to_ir(document)

    names = [entity["linetype"] for entity in result.document["entities"]]
    assert names == [
        "SXF_DASHED",
        "SXF_CHAIN",
        "SXF_USER_17",
        "SXF_USER_18",
        "SXF_USER_19",
        "BYLAYER",  # an empty user-defined slot
        "BYLAYER",  # not reported at all
    ]
    linetypes = result.document["tables"]["linetypes"]
    assert linetypes["SXF_DASHED"]["pattern_mm"] == [5.0, -2.0]
    assert linetypes["SXF_CHAIN"]["pattern_mm"] == [12.0, -1.5, 3.5, -1.5]
    assert linetypes["SXF_USER_17"] == {
        "description": "SXF user-defined line type: 境界線",
        "pattern_mm": [10.0, -1.0, 0.5, -1.0],
    }
    # A pattern that ends with a dash joins the first dash when it repeats.
    assert linetypes["SXF_USER_18"] == {
        "description": "SXF user-defined line type",
        "pattern_mm": [3.0, -1.0, 3.0],
    }
    # One segment is a line without gaps.
    assert linetypes["SXF_USER_19"]["pattern_mm"] == []
    validate_ir(result.document, strict_jsonschema=True)


def test_jww_user_defined_line_types_are_listed_only_when_used() -> None:
    sxf = [{"number": 47, "name": "unused", "segments_mm": [10.0, 1.0]}]
    result = jww_document_to_ir(_line_types_document([2], sxf=sxf))

    assert "SXF_USER_17" not in result.document["tables"]["linetypes"]


def test_jww_line_types_fall_back_to_defaults_without_file_settings() -> None:
    # ezjww older than 0.3.3 has no "line_types", and reports None for old files.
    for line_types in ("missing", None, "garbage"):
        document = _line_types_document([2, 47])
        if line_types == "missing":
            del document["header"]["line_types"]
        else:
            document["header"]["line_types"] = line_types
        result = jww_document_to_ir(document)

        linetypes = result.document["tables"]["linetypes"]
        assert linetypes["JWW_DASHED1"]["pattern_mm"] == [0.625, -0.625]
        assert result.document["entities"][1]["linetype"] == "BYLAYER"
        jww = result.document["header"]["metadata"]["jww"]
        assert jww["line_type_settings"] == "default"


def test_jww_unusable_line_type_settings_are_ignored() -> None:
    document = _line_types_document([2, 3, 47], dashed1_segments=[1.0, float("nan")])
    document["header"]["line_types"]["standard"][1]["segments_mm"] = "4,4"
    document["header"]["line_types"]["sxf"] = [
        {"number": 47, "name": "bad", "segments_mm": [1.0, -2.0]},
        {"number": "48", "name": "bad", "segments_mm": [1.0, 2.0]},
        "not a mapping",
    ]
    result = jww_document_to_ir(document)

    linetypes = result.document["tables"]["linetypes"]
    assert linetypes["JWW_DASHED1"]["pattern_mm"] == [0.625, -0.625]
    assert linetypes["JWW_DASHED2"]["pattern_mm"] == [1.25, -1.25]
    assert result.document["entities"][2]["linetype"] == "BYLAYER"


# ---------------------------------------------------------------------------
# Images: ^@BM placements and the version-700 archive


def _image_jww(
    *,
    embedded: bool = True,
    compressed: bool = True,
    broken: bool = False,
    archive: bool = True,
) -> tuple[dict, bytes]:
    import gzip

    document = _document()
    bitmap = b"BM" + bytes(range(40))
    if embedded:
        content = "^@BM%temp%logo.bmp,100,64.5161,0,0,1,0,255,255,255"
    else:
        content = "^@BMC:\\pictures\\logo.bmp,50,25,0,0,1,0,255,255,255"
    document["entities"] = [{"type": "TEXT", "base": _base(), **_text_payload(content)}]
    document["entity_counts"] = {"TEXT": 1}
    if embedded and archive:
        payload = gzip.compress(bitmap, mtime=0) if compressed else bitmap
        if broken:
            payload = b"\x1f\x8b\x08\x00broken"
        document["images"] = [
            {
                "name": "logo.bmp.gz" if compressed else "logo.bmp",
                "data": payload,
                "compressed": compressed,
            }
        ]
    return document, bitmap


def test_jww_image_placement_becomes_image_entity_with_embedded_bytes() -> None:
    import base64

    document, bitmap = _image_jww()
    result = jww_document_to_ir(document, source_name="image.jww")
    (entity,) = result.document["entities"]
    assert entity["kind"] == "IMAGE"
    assert entity["insert"] == [2.0, 3.0]
    assert (entity["width"], entity["height"]) == (100.0, 64.5161)
    assert entity["name"] == "logo.bmp"
    assert entity["mime_type"] == "image/bmp"
    assert base64.b64decode(entity["data"]) == bitmap
    assert "href" not in entity
    assert "rotation" not in entity
    assert entity["source"]["kind"] == "TEXT"
    jww = entity["metadata"]["jww"]
    assert jww["image_params"] == ["0", "0", "1", "0", "255", "255", "255"]
    assert jww["archive_name"] == "logo.bmp.gz"
    assert jww["image_content"].startswith("^@BM%temp%logo.bmp,")
    assert jww["end"] == [7.0, 3.0]
    assert [d.code for d in result.diagnostics if d.code.startswith("JWW_IMAGE")] == [
        "JWW_IMAGE_EMBEDDED"
    ]
    assert result.statistics["embedded_images"] == 1
    assert result.statistics["linked_images"] == 0
    assert result.statistics["converted_entity_counts"]["IMAGE"] == 1
    validate_ir(result.document, strict_jsonschema=True)
    # The image never reaches DXF as text, and the codec reports the skip.
    exported = convert_ir_to_dxf_text(result.document)
    assert "^@BM" not in exported.dxf_text
    assert {d.code for d in exported.diagnostics} >= {"DXF_IMAGE_SKIPPED"}


def test_jww_uncompressed_archive_entry_and_rotation() -> None:
    import base64

    document, bitmap = _image_jww(compressed=False)
    document["entities"][0]["angle"] = 90.0
    result = jww_document_to_ir(document)
    (entity,) = result.document["entities"]
    assert base64.b64decode(entity["data"]) == bitmap
    assert entity["rotation"] == 90.0
    assert entity["metadata"]["jww"]["archive_name"] == "logo.bmp"


def test_jww_external_and_missing_images_keep_the_path() -> None:
    document, _ = _image_jww(embedded=False)
    result = jww_document_to_ir(document)
    (entity,) = result.document["entities"]
    assert entity["kind"] == "IMAGE"
    assert entity["href"] == "C:\\pictures\\logo.bmp"
    assert entity["name"] == "logo.bmp"
    assert (entity["width"], entity["height"]) == (50.0, 25.0)
    assert "data" not in entity
    assert [d.code for d in result.diagnostics if d.code.startswith("JWW_IMAGE")] == [
        "JWW_IMAGE_LINKED"
    ]
    assert result.statistics["linked_images"] == 1

    document, _ = _image_jww(archive=False)
    result = jww_document_to_ir(document)
    (entity,) = result.document["entities"]
    assert entity["href"] == "%temp%logo.bmp"
    linked = next(d for d in result.diagnostics if d.code == "JWW_IMAGE_LINKED")
    assert "not in the drawing's image archive" in linked.message


def test_jww_broken_archive_entry_reports_decode_failure() -> None:
    document, _ = _image_jww(broken=True)
    result = jww_document_to_ir(document)
    (entity,) = result.document["entities"]
    assert entity["href"] == "%temp%logo.bmp"
    assert "data" not in entity
    codes = [d.code for d in result.diagnostics if d.code.startswith("JWW_IMAGE")]
    assert codes == ["JWW_IMAGE_DECODE_FAILED", "JWW_IMAGE_LINKED"]


def test_jww_malformed_image_text_stays_text() -> None:
    document, _ = _image_jww()
    document["entities"][0]["content"] = "^@BMlogo.bmp"
    document.pop("images")
    result = jww_document_to_ir(document)
    (entity,) = result.document["entities"]
    assert entity["kind"] == "TEXT"
    assert entity["text"] == "^@BMlogo.bmp"
    assert not [d for d in result.diagnostics if d.code.startswith("JWW_IMAGE")]
