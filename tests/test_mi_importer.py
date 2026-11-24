from __future__ import annotations

import gzip
import hashlib
import json
from collections import Counter
from dataclasses import replace
from pathlib import Path

import pytest

from cad2d_ir import (
    convert_file_to_ir,
    convert_ir_to_dxf_text,
    convert_mi_file_to_ir,
    dxf_to_ir,
    validate_ir,
)
from cad2d_ir.cli import main
from cad2d_ir.importers import ImportOptions, ImporterError
from cad2d_ir.importers.mi import mi_document_to_ir

ezmi2d = pytest.importorskip("ezmi2d")


FIXTURES = Path(__file__).parent / "data" / "mi"


@pytest.mark.parametrize(
    ("name", "expected_sha256"),
    [
        (
            "geometry.mi",
            "d100addad10b18c293d74d55a9bd2d0bd969544f75f06af35d1b9f4b3cec0241",
        ),
        (
            "phase5.mi",
            "4530ce28c5f0718f7cdc291a090540dcce680a287cefa3d83e7d4d10d6060b82",
        ),
        (
            "text-utf8.mi",
            "b0103fb2dbbf468459627379b3a4a6cafc7eb4fe73555364b9c93ff44ad161d4",
        ),
    ],
)
def test_mi_fixtures_are_byte_stable(name: str, expected_sha256: str) -> None:
    assert hashlib.sha256((FIXTURES / name).read_bytes()).hexdigest() == expected_sha256


def test_geometry_mi_maps_directly_with_provenance_and_parser_diagnostics() -> None:
    result = convert_mi_file_to_ir(FIXTURES / "geometry.mi")
    document = result.document

    assert document["source"]["format"] == "mi"
    assert document["source"]["version"] == "2.10"
    assert document["source"]["sha256"] == (
        "d100addad10b18c293d74d55a9bd2d0bd969544f75f06af35d1b9f4b3cec0241"
    )
    assert document["header"]["units"] == "mm"
    assert document["header"]["angle_unit"] == "deg"
    assert document["header"]["metadata"]["mi"]["source_angle_unit"] == "RAD"
    assert document["header"]["bbox"] == {
        "min": pytest.approx([0.0, 0.0]),
        "max": pytest.approx([10.0, 10.0]),
    }

    line, arc, circle = document["entities"]
    assert line["kind"] == "LINE"
    assert line["p1"] == pytest.approx([0.0, 0.0])
    assert line["p2"] == pytest.approx([10.0, 0.0])
    assert line["layer"] == "1"
    assert line["color"] == "#FFFF00"
    assert line["source"]["format"] == "mi"
    assert line["source"]["id"] == "10"
    assert line["source"]["metadata"]["span"]["start_line"] == 118

    assert arc["kind"] == "ARC"
    assert arc["center"] == pytest.approx([5.0, 5.0])
    assert arc["radius"] == pytest.approx(5.0)
    assert arc["start_angle"] == pytest.approx(0.0)
    assert arc["end_angle"] == pytest.approx(90.0)
    assert arc["ccw"] is True

    assert circle["kind"] == "CIRCLE"
    assert circle["center"] == pytest.approx([5.0, 5.0])
    assert circle["radius"] == pytest.approx(3.0)

    parser_diagnostic = next(
        diagnostic
        for diagnostic in result.diagnostics
        if diagnostic.code == "MI_PARSER_DIAGNOSTIC"
    )
    assert parser_diagnostic.details is not None
    assert parser_diagnostic.details["upstream_code"] == "MI_UNSUPPORTED_ENTITY"
    assert any(
        diagnostic.code == "MI_UNSUPPORTED_ENTITY"
        and diagnostic.source_kind == "MYSTERY"
        for diagnostic in result.diagnostics
    )
    assert result.statistics["converted_entity_counts"] == {
        "ARC": 1,
        "CIRCLE": 1,
        "LINE": 1,
    }
    assert result.statistics["skipped_entity_counts"] == {"MYSTERY": 1}
    validate_ir(document, strict_jsonschema=True)


def test_utf8_text_maps_font_alignment_and_degree_rotation() -> None:
    result = convert_mi_file_to_ir(FIXTURES / "text-utf8.mi")
    text = result.document["entities"][0]

    assert text["kind"] == "TEXT"
    assert text["insert"] == pytest.approx([25.0, 12.0])
    assert text["height"] == pytest.approx(3.5)
    assert text["rotation"] == pytest.approx(0.0)
    assert text["text"] == "日本語 café"
    assert text["halign"] == "left"
    assert text["valign"] == "middle"
    assert text["width_factor"] == pytest.approx(1.0)
    assert result.document["tables"]["text_styles"][text["style"]] == {
        "font": "hp_i3098_v"
    }
    encoding = result.document["source"]["metadata"]["mi"]["encoding"]
    assert encoding == {
        "name": "utf-8",
        "source": "mi_version",
        "declared_name": "UTF-8",
    }

    overridden = convert_mi_file_to_ir(FIXTURES / "text-utf8.mi", encoding="utf-8")
    override_metadata = overridden.document["source"]["metadata"]["mi"]
    assert override_metadata["requested_encoding"] == "utf-8"
    assert override_metadata["encoding"]["source"] == "override"


def _ezmi2d_reads_dimension_text() -> bool:
    """ezmi2d 0.2.3 and later name the DTA text attribute of a dimension text."""
    return hasattr(ezmi2d.Dimension, "text_height")


@pytest.mark.skipif(
    not _ezmi2d_reads_dimension_text(), reason="ezmi2d before 0.2.3 does not read DTA sizes"
)
def test_dimension_text_height_and_direction_reach_the_ir() -> None:
    # ``dimension-text.mi`` is ``phase5.mi`` with a DTA text attribute for the main
    # text of the dimension (height 0.2), one for its tolerance (0.6 of the main
    # text) and a stored text box of 0.4 by 0.2 along the X axis.
    result = convert_mi_file_to_ir(FIXTURES / "dimension-text.mi", curve_segments=32)
    blocks = result.document["tables"]["blocks"]
    (dimension,) = [
        entity
        for block in blocks.values()
        for entity in block["entities"]
        if entity["kind"] == "DIMENSION"
    ]
    definition = dimension["definition"]

    assert definition["text"] == "90"
    assert definition["text_height"] == pytest.approx(0.2)
    assert definition["text_rotation"] == pytest.approx(0.0)
    validate_ir(result.document, strict_jsonschema=True)

    # The size reaches the DXF: the text of the dimension is written 0.2 high, not
    # with the height a writer falls back to.
    written = dxf_to_ir(convert_ir_to_dxf_text(result.document).dxf_text)
    heights = [
        entity["height"]
        for block in written["tables"]["blocks"].values()
        for entity in block["entities"]
        if entity["kind"] == "TEXT" and entity["text"] == "90"
    ]
    assert heights == [pytest.approx(0.2)]


needs_dimension_graphics = pytest.mark.skipif(
    not hasattr(ezmi2d, "DimensionGraphics"),
    reason="ezmi2d before 0.2.4 does not read the drawn form of a dimension",
)


def _only_dimension(document: dict) -> dict:
    (dimension,) = [
        entity
        for block in document["tables"]["blocks"].values()
        for entity in block["entities"]
        if entity["kind"] == "DIMENSION"
    ]
    return dimension


@needs_dimension_graphics
def test_dimension_is_drawn_from_the_form_the_file_stores() -> None:
    # ``dimension-graphics.mi``: an angle of 90 degrees about the origin with radius 1.
    # The file stores, relative to the text position (0.5, 0.5), four extension line
    # pieces, the arc in two pieces around the text, two terminators (the second is
    # not drawn) and where the five texts start.
    result = convert_mi_file_to_ir(FIXTURES / "dimension-graphics.mi", curve_segments=32)
    definition = _only_dimension(result.document)["definition"]
    validate_ir(result.document, strict_jsonschema=True)

    paths = definition["rendered_paths"]
    lines = [path["points"] for path in paths if len(path["points"]) == 2]
    assert lines == [
        [pytest.approx([1.0, 0.0]), pytest.approx([1.1, 0.0])],
        [pytest.approx([0.0, 1.0]), pytest.approx([0.0, 1.1])],
        [pytest.approx([0.8, 0.0]), pytest.approx([1.0, 0.0])],
        [pytest.approx([0.0, 0.8]), pytest.approx([0.0, 1.0])],
    ]
    # Each arc piece covers 30 degrees counterclockwise on the circle of radius 1.
    arcs = [path for path in paths if len(path["points"]) > 3]
    assert len(arcs) == 2
    assert all(path["closed"] is False for path in arcs)
    for path in arcs:
        assert all(x * x + y * y == pytest.approx(1.0) for x, y in path["points"])
    assert arcs[0]["points"][0] == pytest.approx([1.0, 0.0])
    assert arcs[0]["points"][-1] == pytest.approx([0.8660254037844386, 0.5])
    assert arcs[1]["points"][-1] == pytest.approx([0.0, 1.0])
    # One arrowhead: 0.2 long and 0.052 wide, its tip on the first reference line
    # and its body along the arc. The terminator that is stored without being drawn
    # leaves no path.
    (arrow,) = [path for path in paths if path["closed"]]
    assert arrow["points"] == [
        pytest.approx([1.0 - 0.026, 0.2]),
        pytest.approx([1.0, 0.0]),
        pytest.approx([1.0 + 0.026, 0.2]),
    ]
    assert len(paths) == 7

    texts = {
        text["text"]: (text["anchor"], text["height"], text["angle_deg"])
        for text in definition["rendered_texts"]
    }
    assert texts == {
        "90": (pytest.approx([0.3, 0.4]), pytest.approx(0.2), 0.0),
        # the tolerance: an upper and a lower text at 0.6 of the main text
        "0.9990": (pytest.approx([0.55, 0.52]), pytest.approx(0.12), 0.0),
        "0.9985": (pytest.approx([0.55, 0.34]), pytest.approx(0.12), 0.0),
        "ca.": (pytest.approx([0.05, 0.4]), pytest.approx(0.2), 0.0),
        "(TYP)": (pytest.approx([0.3, 0.16]), pytest.approx(0.12), 0.0),
    }
    assert definition["text"] == "90"
    assert definition["text_height"] == pytest.approx(0.2)

    # The DXF carries the same pieces: four lines, the two arcs and the arrowhead as
    # polylines and the five texts at their sizes, and no line between the
    # reference points.
    alone = {
        **result.document,
        "tables": {**result.document["tables"], "blocks": {}},
        "entities": [_only_dimension(result.document)],
    }
    drawn = dxf_to_ir(convert_ir_to_dxf_text(alone).dxf_text)["entities"]
    assert Counter(entity["kind"] for entity in drawn) == {"LINE": 4, "LWPOLYLINE": 3, "TEXT": 5}
    heights = {entity["text"]: entity["height"] for entity in drawn if entity["kind"] == "TEXT"}
    assert heights == {
        "90": pytest.approx(0.2),
        "0.9990": pytest.approx(0.12),
        "0.9985": pytest.approx(0.12),
        "ca.": pytest.approx(0.2),
        "(TYP)": pytest.approx(0.12),
    }
    polylines = [entity for entity in drawn if entity["kind"] == "LWPOLYLINE"]
    assert sorted(len(entity["vertices"]) for entity in polylines) == [3, 7, 7]
    assert [
        bool(entity.get("closed")) for entity in polylines if len(entity["vertices"]) == 3
    ] == [True]


@needs_dimension_graphics
def test_dimension_text_shows_the_symbols_of_the_symbol_font(tmp_path: Path) -> None:
    # An older file switches to the symbol font between SO and SI: code 0 is the
    # degree sign, 2 the plus-minus sign. A text that starts with a space is moved
    # by about the width of the space instead of keeping it.
    source = (FIXTURES / "dimension-graphics.mi").read_text(encoding="ascii")
    for old, new in (
        ("\n1.5707963267948966\n90\n", "\n1.5707963267948966\n90\x0e0\x0f\n"),
        ("\n0.9990\n", "\n\x0e2\x0f0.5\x0e0\x0f\n"),
        ("\nca.\n", "\n R\n"),
    ):
        assert source.count(old) == 1
        source = source.replace(old, new)
    path = tmp_path / "symbols.mi"
    path.write_text(source, encoding="ascii")

    definition = _only_dimension(convert_mi_file_to_ir(path, curve_segments=32).document)[
        "definition"
    ]

    assert definition["text"] == "90\u00b0"
    texts = {text["text"]: text["anchor"] for text in definition["rendered_texts"]}
    assert set(texts) == {"90\u00b0", "\u00b10.5\u00b0", "0.9985", "R", "(TYP)"}
    # " R" started at (0.05, 0.4); the letter starts one text height further on
    assert texts["R"] == pytest.approx([0.25, 0.4])


@needs_dimension_graphics
def test_tolerance_limits_are_drawn_in_place_of_the_main_text(tmp_path: Path) -> None:
    # Layout 3 of a tolerance: the two limits stand where the main text would.
    source = (FIXTURES / "dimension-graphics.mi").read_text(encoding="ascii")
    old = "\n0\n2\n30\n0.05\n0.02\n0.05\n-0.16\n"
    assert source.count(old) == 1
    path = tmp_path / "limits.mi"
    path.write_text(source.replace(old, "\n0\n3\n30\n-0.2\n0.02\n-0.2\n-0.16\n"), encoding="ascii")

    definition = _only_dimension(convert_mi_file_to_ir(path, curve_segments=32).document)[
        "definition"
    ]

    assert {text["text"] for text in definition["rendered_texts"]} == {
        "0.9990",
        "0.9985",
        "ca.",
        "(TYP)",
    }
    assert definition["text"] == "90"  # the measured value stays in the definition


@needs_dimension_graphics
def test_dimension_whose_drawn_form_is_not_understood_is_converted_as_before(
    tmp_path: Path,
) -> None:
    source = (FIXTURES / "dimension-graphics.mi").read_text(encoding="ascii")
    old = "(TYP)\n-0.2\n-0.34\n17\n|~"
    assert source.count(old) == 1
    path = tmp_path / "other-layout.mi"
    path.write_text(source.replace(old, "(TYP)\n-0.2\n-0.34\n17\n0\n|~"), encoding="ascii")

    definition = _only_dimension(convert_mi_file_to_ir(path, curve_segments=32).document)[
        "definition"
    ]

    assert "rendered_paths" not in definition
    assert "rendered_texts" not in definition
    assert definition["text"] == "90"
    assert definition["text_height"] == pytest.approx(0.2)
    assert definition["points"]["location"] == pytest.approx([0.5, 0.5])


def test_linear_dimension_states_its_dimension_line() -> None:
    from types import SimpleNamespace

    from cad2d_ir.importers.mi import _linear_dimension_line

    def point(x: float, y: float) -> SimpleNamespace:
        return SimpleNamespace(x=x, y=y)

    def dimension(kind: str, *tips: tuple[float, float]) -> SimpleNamespace:
        arrows = tuple(SimpleNamespace(tip=point(*tip)) for tip in tips)
        return SimpleNamespace(mi_type=kind, graphics=SimpleNamespace(arrows=arrows))

    # A horizontal dimension between two points at different heights: the line
    # through the terminators, not the line between the reference points.
    location, rotation = _linear_dimension_line(dimension("DSGL", (2.0, 5.0), (12.0, 5.0)))
    assert location == [12.0, 5.0]
    assert rotation == pytest.approx(0.0)
    _, rotation = _linear_dimension_line(dimension("DSGL", (3.0, 9.0), (3.0, 1.0)))
    assert rotation == pytest.approx(90.0)
    _, rotation = _linear_dimension_line(dimension("DSGL", (4.0, 4.0), (0.0, 0.0)))
    assert rotation == pytest.approx(45.0)
    # Other kinds, a missing terminator and terminators on one point state nothing.
    assert _linear_dimension_line(dimension("DANG", (0.0, 0.0), (1.0, 0.0))) is None
    assert _linear_dimension_line(dimension("DSGL", (0.0, 0.0))) is None
    assert _linear_dimension_line(dimension("DSGL", (1.0, 1.0), (1.0, 1.0))) is None
    assert _linear_dimension_line(SimpleNamespace(mi_type="DSGL", graphics=None)) is None


def test_dimension_without_a_text_attribute_states_no_text_height() -> None:
    # ``phase5.mi`` names no DTA: the size is left out, not guessed.
    result = convert_mi_file_to_ir(FIXTURES / "phase5.mi", curve_segments=32)
    definitions = [
        entity["definition"]
        for block in result.document["tables"]["blocks"].values()
        for entity in block["entities"]
        if entity["kind"] == "DIMENSION"
    ]
    assert len(definitions) == 1
    assert "text_height" not in definitions[0]


def test_phase5_parts_are_blocks_and_annotations_have_explicit_boundaries() -> None:
    result = convert_mi_file_to_ir(FIXTURES / "phase5.mi", curve_segments=32)
    document = result.document
    blocks = document["tables"]["blocks"]

    assert [entity["kind"] for entity in document["entities"]] == ["INSERT", "INSERT"]
    assert document["entities"][0]["block"] == "MI_PART_1_SheetA"
    assert document["entities"][0]["insert"] == pytest.approx([0.0, 0.0])
    assert document["entities"][0]["metadata"]["mi"]["is_sheet"] is True
    assert document["entities"][1]["block"] == "MI_PART_2_SheetB"
    assert document["entities"][1]["insert"] == pytest.approx([10.0, 0.0])

    leaf = blocks["MI_PART_0_Leaf"]
    assert [entity["kind"] for entity in leaf["entities"]] == ["ARC", "SPLINE"]
    assert leaf["entities"][0]["source"]["kind"] == "FIL"
    spline = leaf["entities"][1]
    assert spline["degree"] == 3
    assert len(spline["control_points"]) == 4
    assert spline["metadata"]["mi"]["closed"] is None

    sheet_a = blocks["MI_PART_1_SheetA"]
    assert [entity["kind"] for entity in sheet_a["entities"]] == [
        "LWPOLYLINE",
        "HATCH",
        "DIMENSION",
        "INSERT",
    ]
    leader = sheet_a["entities"][0]
    assert leader["vertices"][0] == pytest.approx([0.0, 0.0])
    assert leader["vertices"][1] == pytest.approx([1.0, 0.0])
    assert leader["metadata"]["mi"]["arrow_type"] == 1

    hatch = sheet_a["entities"][1]
    assert hatch["solid"] is False
    assert hatch["loops"][0]["is_outer"] is True
    assert len(hatch["loops"][0]["vertices"]) >= 3
    assert hatch["metadata"]["mi"]["pattern_lines"] == [
        {
            "offset": 0.0,
            "distance": 1.0,
            "angle_rad": 0.0,
            "color": 3,
            "linetype": 0,
        }
    ]

    dimension = sheet_a["entities"][2]
    assert dimension["dim_kind"] == "GENERIC"
    assert dimension["definition"]["source_kind"] == "DANG"
    assert dimension["definition"]["measurement"] == pytest.approx(1.5707963267948966)
    assert dimension["definition"]["measurement_unit"] == "rad"
    assert dimension["definition"]["text"] == "90"
    assert dimension["definition"]["points"]["p1"] == pytest.approx([0.0, 0.0])
    assert dimension["definition"]["points"]["p2"] == pytest.approx([1.0, 0.0])

    assert sheet_a["entities"][3]["block"] == "MI_PART_0_Leaf"
    sheet_b = blocks["MI_PART_2_SheetB"]
    assert sheet_b["entities"][0]["block"] == "MI_PART_0_Leaf"
    assert sheet_b["entities"][0]["id"] != sheet_a["entities"][3]["id"]

    codes = [diagnostic.code for diagnostic in result.diagnostics]
    assert "MI_ANNOTATION_FLATTENED" in codes
    assert "MI_CURVE_APPROXIMATED" in codes
    assert "MI_UNSUPPORTED_ANNOTATION" in codes
    assert result.statistics["converted_blocks"] == 3
    assert result.statistics["converted_entity_counts"] == {
        "ARC": 1,
        "DIMENSION": 1,
        "HATCH": 1,
        "INSERT": 4,
        "LWPOLYLINE": 1,
        "SPLINE": 1,
    }
    validate_ir(document, strict_jsonschema=True)


def test_registry_detects_mi_and_gzip_wrapped_bi(tmp_path: Path) -> None:
    mi_result = convert_file_to_ir(FIXTURES / "geometry.mi")
    assert mi_result.statistics["source_format"] == "mi"

    bi_path = tmp_path / "drawing.bi"
    bi_path.write_bytes(gzip.compress((FIXTURES / "geometry.mi").read_bytes()))
    bi_result = convert_file_to_ir(bi_path)

    assert bi_result.document["source"]["format"] == "mi"
    container = bi_result.document["source"]["metadata"]["mi"]["container"]
    assert container["format_kind"] == "mi_text"
    assert container["compression"] == "gzip"
    assert container["container_size"] == bi_path.stat().st_size
    assert container["logical_size"] == (FIXTURES / "geometry.mi").stat().st_size

    output_path = tmp_path / "drawing.json"
    assert (
        main(
            [
                "import",
                str(FIXTURES / "geometry.mi"),
                "--format",
                "mi",
                "--pretty",
                "-o",
                str(output_path),
            ]
        )
        == 0
    )
    assert (
        json.loads(output_path.read_text(encoding="utf-8"))["source"]["format"] == "mi"
    )


def test_document_converter_respects_explicit_parser_model() -> None:
    source = ezmi2d.readfile(FIXTURES / "geometry.mi")
    result = mi_document_to_ir(
        source,
        source_name="memory.mi",
        parser_version="0.2.0-test",
    )

    assert result.document["source"]["name"] == "memory.mi"
    assert result.document["source"]["metadata"]["mi"]["parser_version"] == "0.2.0-test"


def test_multiline_mirrored_text_uses_mtext_without_guessing_a_column_width() -> None:
    source = ezmi2d.readfile(FIXTURES / "text-utf8.mi")
    part = source.parts[0]
    text = part.entities[0]
    multiline = replace(
        text,
        alignment=9,
        mirrored=True,
        line_values=(text.content_value, text.content_value),
    )
    modified_part = replace(part, entities=(multiline,), texts=(multiline,))
    modified = replace(source, parts=(modified_part,))

    result = mi_document_to_ir(modified)
    converted = result.document["entities"][0]

    assert converted["kind"] == "MTEXT"
    assert converted["text"] == "日本語 café\n日本語 café"
    assert converted["attach"] == "top_right"
    assert "width" not in converted
    assert converted["metadata"]["mi"]["mirrored"] is True
    preserved = next(
        diagnostic
        for diagnostic in result.diagnostics
        if diagnostic.code == "MI_SOURCE_SEMANTICS_PRESERVED"
    )
    assert preserved.details is not None
    assert preserved.details["counts"]["mirrored_text"] == 1


def test_assembly_affines_use_insert_fields_or_complete_transform() -> None:
    source = ezmi2d.readfile(FIXTURES / "phase5.mi")
    drawing = source.parts[source.top_part_index]
    assembly = drawing.assembly
    assert assembly is not None

    decomposable_instance = replace(
        assembly.instances[0],
        transform_values=(0.0, 3.0, 4.0, 2.0, 0.0, 5.0, 0.0, 0.0, 1.0),
    )
    decomposable_assembly = replace(
        assembly,
        instances=(decomposable_instance, assembly.instances[1]),
    )
    decomposable_drawing = replace(drawing, assembly=decomposable_assembly)
    decomposable_parts = list(source.parts)
    decomposable_parts[drawing.index] = decomposable_drawing
    decomposable_document = replace(source, parts=tuple(decomposable_parts))

    decomposable = mi_document_to_ir(decomposable_document).document["entities"][0]
    assert decomposable["insert"] == pytest.approx([4.0, 5.0])
    assert decomposable["rotation"] == pytest.approx(90.0)
    assert decomposable["scale"] == pytest.approx([2.0, -3.0])
    assert "transform" not in decomposable

    shear_instance = replace(
        assembly.instances[0],
        transform_values=(1.0, 0.5, 4.0, 0.0, 1.0, 5.0, 0.0, 0.0, 1.0),
    )
    shear_assembly = replace(
        assembly,
        instances=(shear_instance, assembly.instances[1]),
    )
    shear_drawing = replace(drawing, assembly=shear_assembly)
    shear_parts = list(source.parts)
    shear_parts[drawing.index] = shear_drawing
    shear_document = replace(source, parts=tuple(shear_parts))

    shear_result = mi_document_to_ir(shear_document)
    shear = shear_result.document["entities"][0]
    assert shear["insert"] == pytest.approx([4.0, 5.0])
    # Affine2DはSVG順 [a,b,c,d,e,f] (x'=a*x+c*y+e, y'=b*x+d*y+f)
    assert shear["transform"] == pytest.approx([1.0, 0.0, 0.5, 1.0, 4.0, 5.0])
    assert "rotation" not in shear
    assert "scale" not in shear
    validate_ir(shear_result.document, strict_jsonschema=True)


def test_invalid_assembly_cycle_is_strict_or_diagnosed_in_lenient_mode() -> None:
    source = ezmi2d.readfile(FIXTURES / "phase5.mi")
    drawing = source.parts[source.top_part_index]
    assembly = drawing.assembly
    assert assembly is not None
    cyclic_instance = replace(
        assembly.instances[0],
        target_part_index=drawing.index,
    )
    cyclic_assembly = replace(
        assembly,
        instances=(cyclic_instance, assembly.instances[1]),
    )
    cyclic_drawing = replace(drawing, assembly=cyclic_assembly)
    cyclic_parts = list(source.parts)
    cyclic_parts[drawing.index] = cyclic_drawing
    cyclic_document = replace(source, parts=tuple(cyclic_parts))

    with pytest.raises(ImporterError, match="assembly cycle"):
        mi_document_to_ir(cyclic_document)

    result = mi_document_to_ir(
        cyclic_document,
        options=ImportOptions(strict=False),
    )
    assert [entity["block"] for entity in result.document["entities"]] == [
        "MI_PART_2_SheetB"
    ]
    assert result.statistics["skipped_entity_counts"]["ASSE_INSTANCE"] == 1
    assert any(
        diagnostic.code == "MI_INSTANCE_SKIPPED" for diagnostic in result.diagnostics
    )


def _with_wrapper_chain(source: ezmi2d.Document, length: int) -> ezmi2d.Document:
    """``phase5.mi`` with ``length`` empty parts between the drawing and its second child.

    Each added part holds nothing of its own and places exactly one child at the origin,
    the way some drawings nest a part inside dozens of empty ones.
    """
    drawing = source.parts[source.top_part_index]
    sheet_b = source.parts[2]
    assert not sheet_b.entities and not sheet_b.annotations
    assert sheet_b.assembly is not None and len(sheet_b.assembly.instances) == 1
    inner = sheet_b.assembly.instances[0]
    first = len(source.parts)
    wrappers = []
    for offset in range(length):
        index = first + offset
        target = index + 1 if offset + 1 < length else inner.target_part_index
        instance = replace(inner, target_part_index=target, is_sheet=False)
        wrappers.append(
            replace(
                sheet_b,
                index=index,
                assembly=replace(sheet_b.assembly, instances=(instance,)),
                child_part_indices=(target,),
                parent_part_indices=(index - 1 if offset else drawing.index,),
            )
        )
    assembly = drawing.assembly
    assert assembly is not None
    second = replace(assembly.instances[1], target_part_index=first, is_sheet=False)
    parts = list(source.parts) + wrappers
    parts[drawing.index] = replace(
        drawing, assembly=replace(assembly, instances=(assembly.instances[0], second))
    )
    return replace(source, parts=tuple(parts))


def _nesting_depth(document: dict) -> int:
    blocks = document["tables"]["blocks"]

    def depth(entities: list[dict], seen: tuple[str, ...]) -> int:
        deepest = 0
        for entity in entities:
            if entity["kind"] == "INSERT" and entity["block"] not in seen:
                inner = blocks[entity["block"]]["entities"]
                deepest = max(deepest, 1 + depth(inner, (*seen, entity["block"])))
        return deepest

    return depth(document["entities"], ())


def test_empty_parts_that_only_place_one_child_are_passed_through() -> None:
    source = ezmi2d.readfile(FIXTURES / "phase5.mi")
    plain = mi_document_to_ir(source)
    result = mi_document_to_ir(_with_wrapper_chain(source, 40))
    document = result.document
    blocks = document["tables"]["blocks"]

    # The placement of the chain's first part now names the part that holds the geometry,
    # with the same position; the 40 empty parts are not written as blocks. (SheetB is no
    # longer placed, and stays a block because it is a sheet.)
    placed = document["entities"][1]
    assert placed["block"] == "MI_PART_0_Leaf"
    assert placed["insert"] == pytest.approx([10.0, 0.0])
    assert placed["metadata"]["mi"]["target_part_index"] == 0
    assert placed["metadata"]["mi"]["passed_through_part_indices"] == list(range(4, 44))
    assert sorted(blocks) == ["MI_PART_0_Leaf", "MI_PART_1_SheetA", "MI_PART_2_SheetB"]
    assert _nesting_depth(document) == 2
    assert result.statistics["passed_through_parts"] == 40
    assert result.statistics["converted_blocks"] == 3
    # Nothing else moves: the other placement and the blocks' contents are as before.
    assert document["entities"][0] == plain.document["entities"][0]
    assert blocks["MI_PART_0_Leaf"] == plain.document["tables"]["blocks"]["MI_PART_0_Leaf"]
    validate_ir(document, strict_jsonschema=True)


def test_parts_with_content_a_moved_child_or_a_sheet_are_kept_as_blocks() -> None:
    source = ezmi2d.readfile(FIXTURES / "phase5.mi")

    # A sheet stays a block even when it only places one child (phase5.mi: SheetB).
    sheets = mi_document_to_ir(source)
    assert sheets.document["entities"][1]["block"] == "MI_PART_2_SheetB"
    assert sheets.statistics["passed_through_parts"] == 0

    # An empty part that moves its child is kept: skipping it would need the two placements
    # combined.
    chained = _with_wrapper_chain(source, 1)
    wrapper = chained.parts[4]
    assert wrapper.assembly is not None
    moved = replace(
        wrapper.assembly.instances[0],
        transform_values=(1.0, 0.0, 5.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0),
    )
    parts = list(chained.parts)
    parts[4] = replace(wrapper, assembly=replace(wrapper.assembly, instances=(moved,)))
    result = mi_document_to_ir(replace(chained, parts=tuple(parts)))
    assert result.document["entities"][1]["block"].startswith("MI_PART_4_")
    assert result.statistics["passed_through_parts"] == 0

    # A part with geometry of its own is kept (phase5.mi: SheetA holds annotations).
    assert "MI_PART_1_SheetA" in result.document["tables"]["blocks"]


def _ezmi2d_reads_part_scale() -> bool:
    # ezmi2d 0.2.5 states the scale and offset each part is drawn with.
    return hasattr(ezmi2d.Document, "part_placement")


needs_part_scale = pytest.mark.skipif(
    not _ezmi2d_reads_part_scale(),
    reason="ezmi2d does not read the scale and offset of parts",
)


def _with_placements(
    source: ezmi2d.Document, placements: dict[int, tuple[float, tuple[float, float]]]
) -> ezmi2d.Document:
    """``source`` with the scale and offset of the given parts set."""
    parts = list(source.parts)
    for index, (scale, offset) in placements.items():
        part = parts[index]
        assert part.assembly is not None
        parts[index] = replace(
            part,
            assembly=replace(part.assembly, scale=scale, offset=ezmi2d.Vec2(*offset)),
        )
    return replace(source, parts=tuple(parts))


@needs_part_scale
def test_parts_are_placed_with_the_scale_and_offset_they_are_drawn_with() -> None:
    source = ezmi2d.readfile(FIXTURES / "phase5.mi")
    # A point p of a part lies at scale * p + offset on the sheet. The drawing and SheetA
    # are drawn 1:1, SheetB 1:2 moved by (1, 1), the leaf 1:5 moved by (3, -4).
    document = _with_placements(
        source,
        {
            3: (1.0, (0.0, 0.0)),
            1: (1.0, (0.0, 0.0)),
            2: (0.5, (1.0, 1.0)),
            0: (0.2, (3.0, -4.0)),
        },
    )

    result = mi_document_to_ir(document)
    blocks = result.document["tables"]["blocks"]

    # SheetA in the drawing: nothing changes.
    sheet_a = result.document["entities"][0]
    assert sheet_a["insert"] == pytest.approx([0.0, 0.0])
    assert "scale" not in sheet_a
    # SheetB in the drawing: half size and moved by its offset, then placed 10 to the right.
    sheet_b = result.document["entities"][1]
    assert sheet_b["block"] == "MI_PART_2_SheetB"
    assert sheet_b["scale"] == pytest.approx(0.5)
    assert sheet_b["insert"] == pytest.approx([11.0, 1.0])
    # The leaf in SheetA (1:1): a fifth of its size at its offset.
    leaf_in_a = blocks["MI_PART_1_SheetA"]["entities"][3]
    assert leaf_in_a["scale"] == pytest.approx(0.2)
    assert leaf_in_a["insert"] == pytest.approx([3.0, -4.0])
    # The leaf in SheetB (1:2, moved by (1, 1)): measured in SheetB's units, so that
    # 0.5 * (0.4 * p + (4, -10)) + (1, 1) = 0.2 * p + (3, -4).
    leaf_in_b = blocks["MI_PART_2_SheetB"]["entities"][0]
    assert leaf_in_b["scale"] == pytest.approx(0.4)
    assert leaf_in_b["insert"] == pytest.approx([4.0, -10.0])
    assert blocks["MI_PART_0_Leaf"]["metadata"]["mi"]["scale"] == pytest.approx(0.2)
    assert blocks["MI_PART_0_Leaf"]["metadata"]["mi"]["offset"] == pytest.approx([3.0, -4.0])
    assert result.document["header"]["metadata"]["mi"]["top_part_scale"] == pytest.approx(1.0)
    validate_ir(result.document, strict_jsonschema=True)


@needs_part_scale
def test_a_part_behind_pass_through_parts_keeps_its_own_scale() -> None:
    source = ezmi2d.readfile(FIXTURES / "phase5.mi")
    chained = _with_wrapper_chain(source, 3)
    # The empty parts are drawn at other scales than the leaf they lead to; where the leaf
    # lies on the sheet depends on its own scale and offset only.
    document = _with_placements(
        chained,
        {
            3: (1.0, (0.0, 0.0)),
            4: (0.5, (0.0, 0.0)),
            5: (0.5, (0.0, 0.0)),
            6: (0.25, (0.0, 0.0)),
            0: (0.2, (3.0, -4.0)),
        },
    )

    result = mi_document_to_ir(document)

    placed = result.document["entities"][1]
    assert placed["block"] == "MI_PART_0_Leaf"
    assert placed["metadata"]["mi"]["passed_through_part_indices"] == [4, 5, 6]
    assert placed["scale"] == pytest.approx(0.2)
    # Scaled and moved by its offset, then placed 10 to the right like the chain was.
    assert placed["insert"] == pytest.approx([13.0, -4.0])


def test_parts_that_state_no_scale_are_placed_as_before() -> None:
    result = convert_mi_file_to_ir(FIXTURES / "phase5.mi", curve_segments=32)

    for entity in result.document["entities"]:
        assert "scale" not in entity
    assert result.document["header"]["metadata"]["mi"]["top_part_scale"] is None
    for block in result.document["tables"]["blocks"].values():
        assert block["metadata"]["mi"]["scale"] is None
        assert block["metadata"]["mi"]["offset"] is None


def _with_second_sheet_at_the_origin(source: ezmi2d.Document) -> ezmi2d.Document:
    """``phase5.mi`` with SheetB placed where SheetA is, the way sheets of one drawing are."""
    drawing = source.parts[source.top_part_index]
    assembly = drawing.assembly
    assert assembly is not None
    second = replace(
        assembly.instances[1],
        transform_values=(1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0),
    )
    parts = list(source.parts)
    parts[drawing.index] = replace(
        drawing, assembly=replace(assembly, instances=(assembly.instances[0], second))
    )
    return replace(source, parts=tuple(parts))


def _extent_x(document: dict, placed: dict) -> tuple[float, float]:
    """Smallest and largest x of the line ends, centres and vertices a placement draws."""
    blocks = document["tables"]["blocks"]

    def points(entities: list[dict], dx: float) -> list[float]:
        found: list[float] = []
        for entity in entities:
            if entity["kind"] == "INSERT":
                found += points(
                    blocks[entity["block"]]["entities"], dx + entity["insert"][0]
                )
            elif entity["kind"] in {"ARC", "CIRCLE"}:
                found += [
                    dx + entity["center"][0] - entity["radius"],
                    dx + entity["center"][0] + entity["radius"],
                ]
            elif entity["kind"] == "LWPOLYLINE":
                found += [dx + vertex[0] for vertex in entity["vertices"]]
        return found

    xs = points(blocks[placed["block"]]["entities"], placed["insert"][0])
    return min(xs), max(xs)


def test_sheets_drawn_on_top_of_each_other_are_laid_side_by_side() -> None:
    source = ezmi2d.readfile(FIXTURES / "phase5.mi")

    result = mi_document_to_ir(_with_second_sheet_at_the_origin(source))
    document = result.document

    first, second = document["entities"]
    assert first["insert"] == pytest.approx([0.0, 0.0])
    # The second sheet starts a tenth of the widest sheet to the right of the first.
    first_x = _extent_x(document, first)
    second_x = _extent_x(document, second)
    widest = max(first_x[1] - first_x[0], second_x[1] - second_x[0])
    assert second_x[0] == pytest.approx(first_x[1] + 0.1 * widest)
    assert second["insert"][1] == pytest.approx(0.0)
    assert second["metadata"]["mi"]["sheet_offset"] == pytest.approx(
        [second["insert"][0], 0.0]
    )
    assert "sheet_offset" not in first["metadata"]["mi"]
    tiled = [d for d in result.diagnostics if d.code == "MI_MULTISHEET_TILED"]
    assert len(tiled) == 1
    assert tiled[0].details["sheet_count"] == 2
    assert tiled[0].details["offsets"][0] == pytest.approx([0.0, 0.0])
    assert tiled[0].details["offsets"][1] == pytest.approx(second["insert"])
    validate_ir(document, strict_jsonschema=True)


def test_sheets_that_do_not_overlap_stay_where_the_file_puts_them() -> None:
    # phase5.mi places SheetB 10 to the right of SheetA: nothing is on top of anything.
    result = convert_mi_file_to_ir(FIXTURES / "phase5.mi", curve_segments=32)

    assert result.document["entities"][1]["insert"] == pytest.approx([10.0, 0.0])
    assert not [d for d in result.diagnostics if d.code == "MI_MULTISHEET_TILED"]




class _SheetUnitParts:
    """A document whose given parts are kept in sheet units, the way ezmi2d reports it."""

    def __init__(self, document: ezmi2d.Document, part_indices: set[int]) -> None:
        self._document = document
        self._part_indices = part_indices

    def __getattr__(self, name: str):
        return getattr(self._document, name)

    def part_placement(self, part: ezmi2d.Part):
        placement = self._document.part_placement(part)
        if placement is None or part.index not in self._part_indices:
            return placement
        return replace(placement, scale=1.0)


@needs_part_scale
def test_a_part_kept_in_sheet_units_is_not_scaled() -> None:
    source = ezmi2d.readfile(FIXTURES / "phase5.mi")
    # The leaf's record states 1:5, but its geometry is kept in sheet units (a title block
    # in a drawing at another scale): only its offset applies.
    document = _SheetUnitParts(
        _with_placements(
            source,
            {
                3: (1.0, (0.0, 0.0)),
                1: (1.0, (0.0, 0.0)),
                2: (1.0, (0.0, 0.0)),
                0: (0.2, (3.0, -4.0)),
            },
        ),
        {0},
    )

    result = mi_document_to_ir(document)

    leaf_in_a = result.document["tables"]["blocks"]["MI_PART_1_SheetA"]["entities"][3]
    assert "scale" not in leaf_in_a
    assert leaf_in_a["insert"] == pytest.approx([3.0, -4.0])
    leaf = result.document["tables"]["blocks"]["MI_PART_0_Leaf"]["metadata"]["mi"]
    assert leaf["scale"] == pytest.approx(1.0)
