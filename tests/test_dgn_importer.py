from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from cad2d_ir.importers.base import ImporterError, ImportOptions
from cad2d_ir.importers.dgn import dgn_drawing_to_ir
from cad2d_ir.schema import validate_ir


def _record(index: int, element_type: int, *, level: int = 2) -> SimpleNamespace:
    return SimpleNamespace(
        index=index,
        offset=index * 100,
        element_type=element_type,
        level=level,
        deleted=False,
    )


def _style(*, fill: bool = False) -> SimpleNamespace:
    return SimpleNamespace(
        color_index=3,
        line_style=0,
        line_weight=2,
        rgb=(255, 0, 0),
        fill_color_index=6 if fill else None,
        fill_rgb=(0, 255, 0) if fill else None,
    )


def _entity(kind: str, index: int, element_type: int, **values: Any) -> SimpleNamespace:
    defaults = {
        "kind": kind,
        "record": _record(index, element_type),
        "level": 2,
        "style": _style(),
        "parent_index": None,
        "association_ids": (),
        "linkages": (),
    }
    defaults.update(values)
    return SimpleNamespace(**defaults)


def _text_entity(
    index: int,
    text_bytes: bytes,
    *,
    justification: int = 0,
) -> SimpleNamespace:
    return _entity(
        "TEXT",
        index,
        17,
        text_bytes=text_bytes,
        origin_master=(0.0, 0.0),
        height_multiplier_master=2.0,
        length_multiplier_master=1.5,
        rotation_degrees=0.0,
        font_id=0,
        justification=justification,
        editable_fields=0,
    )


class _Drawing:
    def __init__(
        self,
        entities: tuple[Any, ...],
        children: dict[int, tuple[Any, ...]],
    ) -> None:
        self.entities = entities
        self._children = children
        descendants = tuple(child for values in children.values() for child in values)
        self.all_entities = (*entities, *descendants)
        self.elements = self.all_entities
        self.unsupported_elements: tuple[Any, ...] = ()
        self.active_color_table_index = None
        self.design_settings = SimpleNamespace(
            master_unit_name="mm",
            sub_unit_name="um",
            uor_per_master=1000,
            subunits_per_master=1000,
            uor_per_subunit=1,
            scale=0.001,
        )
        self.raw_scan = SimpleNamespace(
            format=SimpleNamespace(kind="V7", dimension=2),
            records=tuple(range(len(self.all_entities))),
            termination="end_marker",
            trailing_bytes=0,
        )

    def children(self, entity: Any) -> tuple[Any, ...]:
        return self._children.get(entity.record.index, ())


def test_dgn_native_entities_hierarchy_and_styles_map_to_ir() -> None:
    line = _entity(
        "LINE",
        1,
        3,
        start_master=(0.0, 0.0),
        end_master=(10.0, 5.0),
    )
    ellipse = _entity(
        "ELLIPSE",
        2,
        15,
        center_master=(5.0, 5.0),
        primary_axis_master=4.0,
        secondary_axis_master=2.0,
        rotation_degrees=30.0,
    )
    shape = _entity(
        "SHAPE",
        3,
        6,
        style=_style(fill=True),
        vertices_master=((0.0, 0.0), (4.0, 0.0), (4.0, 3.0), (0.0, 0.0)),
    )
    text = _entity(
        "TEXT",
        4,
        17,
        text_bytes="寸法".encode("cp932"),
        origin_master=(1.0, 2.0),
        height_multiplier_master=2.0,
        length_multiplier_master=1.5,
        rotation_degrees=15.0,
        font_id=3,
        justification=7,
        editable_fields=0,
    )
    cell_child = _entity(
        "LINE",
        6,
        3,
        parent_index=5,
        start_master=(20.0, 10.0),
        end_master=(21.0, 10.0),
    )
    cell = _entity(
        "CELL",
        5,
        2,
        name="DETAIL",
        cell_class=1,
        origin_master=(20.0, 10.0),
        transform=((1.0, 0.0), (0.0, 1.0)),
    )
    pole = _entity(
        "BSPLINE_POLE",
        8,
        21,
        parent_index=7,
        vertices_master=((0.0, 0.0), (1.0, 2.0), (3.0, 2.0), (4.0, 0.0)),
    )
    knot = _entity(
        "BSPLINE_KNOT",
        9,
        26,
        parent_index=7,
        values=(0.5,),
    )
    weight = _entity(
        "BSPLINE_WEIGHT",
        10,
        28,
        parent_index=7,
        values=(1.0, 1.0, 1.0, 1.0),
    )
    spline = _entity(
        "BSPLINE_CURVE",
        7,
        27,
        order=3,
        curve_type=2,
        properties=0x40,
        is_closed=False,
    )
    curve = _entity(
        "CURVE",
        11,
        11,
        vertices_master=((0.0, 0.0), (1.0, 1.0), (2.0, 0.0)),
    )
    complex_child = _entity(
        "LINE",
        13,
        3,
        parent_index=12,
        start_master=(30.0, 0.0),
        end_master=(31.0, 0.0),
    )
    complex_chain = _entity("COMPLEX_CHAIN", 12, 12)
    drawing = _Drawing(
        (line, ellipse, shape, text, cell, spline, curve, complex_chain),
        {
            5: (cell_child,),
            7: (knot, pole, weight),
            12: (complex_child,),
        },
    )

    result = dgn_drawing_to_ir(
        drawing,
        source_name="fixture.dgn",
        source_sha256="a" * 64,
        parser_version="0.1.2",
        options=ImportOptions(encoding="cp932"),
    )
    document = result.document

    assert document["source"]["format"] == "dgn"
    assert document["source"]["version"] == "V7"
    assert document["header"]["units"] == "mm"
    assert [entity["kind"] for entity in document["entities"]] == [
        "LINE",
        "ELLIPSE",
        "HATCH",
        "TEXT",
        "INSERT",
        "SPLINE",
        "LWPOLYLINE",
        "LINE",
    ]
    assert document["entities"][3]["text"] == "寸法"
    assert document["entities"][3]["halign"] == "left"
    assert document["entities"][3]["width_factor"] == pytest.approx(0.75)
    assert document["source"]["metadata"]["encoding"] == "cp932"
    assert document["source"]["metadata"]["encoding_source"] == "explicit"
    insert = document["entities"][4]
    assert insert["block"].startswith("DGN_DETAIL_5")
    assert insert["insert"] == [20.0, 10.0]
    assert "transform" not in insert
    block = document["tables"]["blocks"][insert["block"]]
    assert block["base_point"] == [20.0, 10.0]
    assert block["entities"][0]["kind"] == "LINE"
    assert block["entities"][0]["p1"] == [20.0, 10.0]
    assert block["entities"][0]["p2"] == [21.0, 10.0]
    assert block["metadata"]["dgn"] == {
        "cell_name": "DETAIL",
        "cell_class": 1,
        "source_record": "5",
        "placement_transform": [[1.0, 0.0], [0.0, 1.0]],
        "component_coordinate_space": "design",
    }
    spline_entity = document["entities"][5]
    assert spline_entity["degree"] == 2
    assert spline_entity["knots"] == [0.0, 0.0, 0.0, 0.5, 1.0, 1.0, 1.0]
    assert spline_entity["weights"] == [1.0, 1.0, 1.0, 1.0]
    assert result.statistics["converted_blocks"] == 1
    assert {diagnostic.code for diagnostic in result.diagnostics} == {
        "DGN_CURVE_APPROXIMATED",
        "DGN_COMPLEX_FLATTENED",
    }
    validate_ir(document, strict_jsonschema=True)


def test_dgn_auto_text_encoding_is_probed_once_for_the_whole_file() -> None:
    drawing = _Drawing(
        (
            _text_entity(1, b"ASCII"),
            _text_entity(2, "寸法".encode("cp932")),
        ),
        {},
    )

    result = dgn_drawing_to_ir(drawing)

    assert [entity["text"] for entity in result.document["entities"]] == [
        "ASCII",
        "寸法",
    ]
    assert result.document["source"]["metadata"]["encoding"] == "cp932"
    assert result.document["source"]["metadata"]["encoding_source"] == "cp932-probe"
    assert result.statistics["encoding"] == "cp932"
    assert result.statistics["encoding_source"] == "cp932-probe"
    assert [diagnostic.code for diagnostic in result.diagnostics] == [
        "DGN_ENCODING_DETECTED"
    ]
    assert result.diagnostics[0].details == {
        "encoding": "cp932",
        "source": "cp932-probe",
        "text_elements_probed": 2,
    }


def test_dgn_auto_text_encoding_falls_back_to_latin1() -> None:
    drawing = _Drawing((_text_entity(1, b"\x82"),), {})
    drawing.design_settings.master_unit_name = "mu"

    result = dgn_drawing_to_ir(drawing)

    assert result.document["header"]["units"] == "unknown"
    assert result.document["entities"][0]["text"] == "\x82"
    assert result.document["source"]["metadata"]["encoding"] == "latin-1"
    assert result.statistics["encoding_source"] == "latin-1-fallback"
    assert {diagnostic.code for diagnostic in result.diagnostics} == {
        "DGN_ENCODING_DETECTED",
        "DGN_UNKNOWN_UNITS",
    }


def test_dgn_explicit_decode_keeps_strict_and_replacement_behavior() -> None:
    drawing = _Drawing((_text_entity(1, "寸法".encode("cp932")),), {})

    with pytest.raises(ImporterError, match="cannot be decoded as ascii"):
        dgn_drawing_to_ir(drawing, options=ImportOptions(encoding="ascii"))

    result = dgn_drawing_to_ir(
        drawing,
        options=ImportOptions(encoding="ascii", strict=False),
    )

    assert "�" in result.document["entities"][0]["text"]
    assert [diagnostic.code for diagnostic in result.diagnostics] == [
        "DGN_TEXT_DECODE_REPLACED"
    ]


@pytest.mark.parametrize("justification", [0, 3, 6, 8, 9, 14])
def test_dgn_text_halign_stays_left_for_all_justifications(
    justification: int,
) -> None:
    # The stored V7 origin is the bottom-left corner of the string regardless
    # of the justification code, so re-anchoring by justification would shift
    # the text; the code is only preserved in metadata.
    drawing = _Drawing(
        (_text_entity(1, b"alignment", justification=justification),), {}
    )

    result = dgn_drawing_to_ir(drawing)

    entity = result.document["entities"][0]
    assert entity["halign"] == "left"
    assert entity["metadata"]["dgn"]["justification"] == justification
    assert entity["width_factor"] == pytest.approx(0.75)


def test_dgn_3d_design_is_projected_with_a_loss_diagnostic() -> None:
    line = _entity(
        "LINE",
        1,
        3,
        start_master=(1.0, 2.0, 3.0),
        end_master=(4.0, 5.0, 6.0),
    )
    drawing = _Drawing((line,), {})
    drawing.raw_scan.format.dimension = 3

    result = dgn_drawing_to_ir(drawing)

    assert result.document["entities"][0]["p1"] == [1.0, 2.0]
    assert result.document["entities"][0]["p2"] == [4.0, 5.0]
    assert result.document["source"]["metadata"]["dgn"]["dimension"] == 3
    assert [diagnostic.code for diagnostic in result.diagnostics] == [
        "DGN_3D_FLATTENED"
    ]
    assert result.diagnostics[0].action == "projected"
    assert result.diagnostics[0].details == {
        "source_dimension": 3,
        "target_dimension": 2,
        "projection_plane": "xy",
    }
    validate_ir(result.document, strict_jsonschema=True)


def test_dgn_control_records_are_not_reported_as_unsupported_graphics() -> None:
    drawing = _Drawing((), {})
    drawing.unsupported_elements = tuple(
        SimpleNamespace(record=_record(index, element_type), common_header=object())
        for index, element_type in enumerate((5, 8, 9, 10, 37, 66), start=1)
    ) + (SimpleNamespace(record=_record(7, 33), common_header=object()),)

    result = dgn_drawing_to_ir(drawing)

    assert [
        (diagnostic.code, diagnostic.source_kind) for diagnostic in result.diagnostics
    ] == [("DGN_UNSUPPORTED_ENTITY", "TYPE_33")]
    assert result.statistics["skipped_entities"] == 1
    assert result.statistics["skipped_entity_counts"] == {"TYPE_33": 1}


@pytest.mark.parametrize(("label", "expected"), [("'", "ft"), ('"', "inch")])
def test_dgn_symbol_unit_labels_map_to_ir(label: str, expected: str) -> None:
    drawing = _Drawing((), {})
    drawing.design_settings.master_unit_name = label

    result = dgn_drawing_to_ir(drawing)

    assert result.document["header"]["units"] == expected
    assert result.diagnostics == []
