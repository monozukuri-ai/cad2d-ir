from __future__ import annotations

import gzip
import hashlib
import json
from dataclasses import replace
from pathlib import Path

import pytest

from cad2d_ir import convert_file_to_ir, convert_mi_file_to_ir, validate_ir
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
    assert shear["transform"] == pytest.approx([1.0, 0.5, 4.0, 0.0, 1.0, 5.0])
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
