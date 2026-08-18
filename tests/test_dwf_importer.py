from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from cad2d_ir.importers.dwf import dwf_drawing_to_ir
from cad2d_ir.schema import validate_ir


def _point(x: float, y: float) -> SimpleNamespace:
    return SimpleNamespace(x=x, y=y)


def _style(*, fill: bool = False, layer: str = "walls") -> SimpleNamespace:
    return SimpleNamespace(
        layer=layer,
        layer_name=layer,
        layer_number=4,
        color=(12, 34, 56, 255),
        color_index=None,
        stroke_color=(12, 34, 56, 255),
        fill_color=(120, 80, 40, 200) if fill else None,
        line_pattern=None,
        stroke_dash_array=(),
        line_weight_logical=20,
        nominal_stroke_width=0.25,
        fill=fill,
        fill_pattern=None,
        font_name="Arial",
        font_canonical_name="Arial",
        font_bold=False,
        font_italic=False,
        font_underlined=False,
        font_height=2.5,
        font_rotation_degrees=10.0,
        visible=True,
        viewport=None,
        opacity=1.0,
    )


def _entity(kind: str, index: int, **values: Any) -> SimpleNamespace:
    defaults = {
        "kind": kind,
        "points": (),
        "center": None,
        "x_axis": None,
        "y_axis": None,
        "start_angle_degrees": None,
        "end_angle_degrees": None,
        "closed": False,
        "text": None,
        "bounds": None,
        "colored_points": (),
        "contours": (),
        "image": None,
        "path": (),
        "style": _style(),
        "source": SimpleNamespace(
            offset=index * 10,
            length=10,
            opcode=kind,
            decoded_offset=None,
            decoded_length=None,
            compression_depth=0,
        ),
        "resource_href": "sheet/main.w2d",
        "resource_role": "2d streaming graphics",
        "is_markup": False,
        "section_index": 0,
        "stream_index": 0,
        "entity_index": index,
        "clips": (),
        "opacity_masks": (),
        "compositing_groups": (),
        "glyph_outline": None,
    }
    defaults.update(values)
    return SimpleNamespace(**defaults)


def _sheet(
    entities: tuple[Any, ...],
    *,
    markup: tuple[Any, ...] = (),
    units: str = "mm",
    name: str = "Fixture Sheet",
) -> SimpleNamespace:
    return SimpleNamespace(
        name=name,
        title=name,
        plot_order=1,
        units=units,
        paper_bounds=(0.0, 0.0, 297.0, 210.0),
        content_bounds=(0.0, 0.0, 100.0, 100.0),
        section_index=0,
        entities=entities,
        markup_entities=markup,
    )


def test_dwf_native_geometry_markup_and_diagnostics_map_to_ir() -> None:
    line = _entity("LINE", 1, points=(_point(0, 0), _point(10, 5)))
    circle = _entity(
        "CIRCLE",
        2,
        center=_point(20, 20),
        x_axis=_point(5, 0),
        y_axis=_point(0, 5),
        closed=True,
    )
    bezier = _entity(
        "POLYBEZIER",
        3,
        points=(
            _point(0, 0),
            _point(1, 2),
            _point(3, 2),
            _point(4, 0),
        ),
    )
    polygon = _entity(
        "POLYGON",
        4,
        points=(_point(0, 0), _point(5, 0), _point(5, 5)),
        closed=True,
        style=_style(fill=True),
    )
    text = _entity(
        "TEXT",
        5,
        points=(_point(2, 3),),
        text="fixture",
        bounds=(_point(2, 3), _point(8, 3), _point(8, 5), _point(2, 5)),
    )
    image = _entity(
        "IMAGE",
        6,
        image=SimpleNamespace(
            format="PNG",
            identifier=9,
            columns=2,
            rows=3,
            min=_point(0, 0),
            max=_point(2, 3),
            color_map=(),
            data=b"png",
        ),
    )
    cubic = SimpleNamespace(
        kind="cubic_bezier",
        end=_point(5, 0),
        control1=_point(1, 2),
        control2=_point(4, 2),
    )
    path = _entity(
        "PATH",
        7,
        is_markup=True,
        path=(
            SimpleNamespace(
                start=_point(0, 0),
                segments=(cubic,),
                closed=False,
                filled=False,
            ),
        ),
    )
    upstream_diagnostic = SimpleNamespace(
        code="W2D_RECOVERED",
        severity="warning",
        message="Recovered a source record",
        action="reported",
        section="Fixture Sheet",
        resource="sheet/main.w2d",
        offset=42,
        details={"reason": "fixture"},
    )
    drawing = SimpleNamespace(
        sheets=(_sheet((line, circle, bezier, polygon, text, image), markup=(path,)),),
        diagnostics=(upstream_diagnostic,),
    )

    result = dwf_drawing_to_ir(
        drawing,
        source_name="fixture.dwf",
        source_sha256="b" * 64,
        source_kind="dwf_package",
        source_version="06.00",
        parser_version="0.0.1",
    )
    document = result.document

    assert document["source"]["format"] == "dwf"
    assert document["source"]["version"] == "06.00"
    assert document["header"]["units"] == "mm"
    assert document["header"]["bbox"] == {
        "min": [0.0, 0.0],
        "max": [100.0, 100.0],
    }
    assert [entity["kind"] for entity in document["entities"]] == [
        "LINE",
        "CIRCLE",
        "SPLINE",
        "HATCH",
        "TEXT",
        "LWPOLYLINE",
    ]
    assert document["entities"][2]["degree"] == 3
    assert document["entities"][3]["color"] == "#785028C8"
    assert document["entities"][-1]["metadata"]["dwf"]["is_markup"] is True
    assert result.statistics["source_markup_entities"] == 1
    assert result.statistics["skipped_entity_counts"] == {"IMAGE": 1}
    assert {diagnostic.code for diagnostic in result.diagnostics} == {
        "DWF_CURVE_APPROXIMATED",
        "DWF_DRAWING_WARNING",
        "DWF_UNSUPPORTED_ENTITY",
    }
    unsupported = next(
        diagnostic
        for diagnostic in result.diagnostics
        if diagnostic.code == "DWF_UNSUPPORTED_ENTITY"
    )
    assert unsupported.details is not None
    assert unsupported.details["sources"][0]["resource_href"] == "sheet/main.w2d"
    assert unsupported.details["sources"][0]["image"]["format"] == "PNG"
    assert unsupported.details["sources"][0]["image"]["data_size"] == 3
    validate_ir(document, strict_jsonschema=True)


def test_dwf_text_with_mtext_formatting_is_emitted_as_mtext() -> None:
    plain = _entity(
        "TEXT",
        1,
        points=(_point(2, 3),),
        text="plain label",
    )
    formatted_text = r"{\LLIVING ROOM\P\H0.6667x;\lHRWD FLOOR}"
    formatted = _entity(
        "TEXT",
        2,
        points=(_point(4, 5),),
        text=formatted_text,
    )
    drawing = SimpleNamespace(
        sheets=(_sheet((plain, formatted)),),
        diagnostics=(),
    )

    result = dwf_drawing_to_ir(drawing)

    assert [entity["kind"] for entity in result.document["entities"]] == [
        "TEXT",
        "MTEXT",
    ]
    mtext = result.document["entities"][1]
    assert mtext["text"] == formatted_text
    assert mtext["source"]["kind"] == "TEXT"
    assert mtext["metadata"]["dwf"]["mtext_formatting_detected"] is True
    validate_ir(result.document, strict_jsonschema=True)


def test_dwfx_dip_units_use_custom_ir_scale() -> None:
    line = _entity("LINE", 1, points=(_point(0, 0), _point(96, 96)))
    line.clips = (object(),)
    line.compositing_groups = (object(), object())
    drawing = SimpleNamespace(
        sheets=(_sheet((line,), units="dip", name="FixedPage"),),
        diagnostics=(),
    )

    result = dwf_drawing_to_ir(
        drawing,
        source_kind="dwfx",
        parser_version="0.0.1",
    )

    assert result.document["header"]["units"] == "custom"
    assert result.document["header"]["unit_scale_to_mm"] == pytest.approx(25.4 / 96.0)
    assert result.document["entities"][0]["p2"] == [96.0, 96.0]
    assert [diagnostic.code for diagnostic in result.diagnostics] == [
        "DWF_APPEARANCE_EFFECTS_FLATTENED"
    ]
    assert result.diagnostics[0].details == {"clips": 1, "compositing_groups": 2}


def test_dwf_mixed_sheet_units_are_preserved_and_diagnosed() -> None:
    first = _entity("LINE", 1, points=(_point(0, 0), _point(1, 0)))
    second = _entity(
        "LINE",
        2,
        points=(_point(0, 0), _point(1, 0)),
        section_index=1,
    )
    drawing = SimpleNamespace(
        sheets=(
            _sheet((first,), units="mm", name="Metric"),
            _sheet((second,), units="in", name="Imperial"),
        ),
        diagnostics=(),
    )

    result = dwf_drawing_to_ir(drawing)

    assert result.document["header"]["units"] == "unknown"
    assert "bbox" not in result.document["header"]
    assert {diagnostic.code for diagnostic in result.diagnostics} == {
        "DWF_MIXED_SHEET_UNITS",
        "DWF_MULTISHEET_FLATTENED",
    }


def test_dwf_triangle_strips_and_vertex_colors_are_preserved() -> None:
    points = tuple(_point(x, y) for x, y in ((0, 0), (2, 0), (0, 2), (2, 2), (0, 4)))
    triangle_strip = _entity("POLYTRIANGLE", 1, points=points, style=_style(fill=True))
    colored_points = tuple(
        SimpleNamespace(point=_point(index, index % 2), color=color)
        for index, color in enumerate(
            ((255, 0, 0, 255), (0, 255, 0, 255), (0, 0, 255, 255))
        )
    )
    gouraud = _entity("GOURAUD_POLYLINE", 2, colored_points=colored_points)
    drawing = SimpleNamespace(
        sheets=(_sheet((triangle_strip, gouraud)),), diagnostics=()
    )

    result = dwf_drawing_to_ir(drawing)

    hatch, polyline = result.document["entities"]
    assert [loop["vertices"] for loop in hatch["loops"]] == [
        [[0.0, 0.0], [2.0, 0.0], [0.0, 2.0]],
        [[2.0, 0.0], [0.0, 2.0], [2.0, 2.0]],
        [[0.0, 2.0], [2.0, 2.0], [0.0, 4.0]],
    ]
    assert polyline["metadata"]["dwf"]["colored_points"] == [
        {"point": [0.0, 0.0], "color": [255, 0, 0, 255]},
        {"point": [1.0, 1.0], "color": [0, 255, 0, 255]},
        {"point": [2.0, 0.0], "color": [0, 0, 255, 255]},
    ]
    assert [diagnostic.code for diagnostic in result.diagnostics] == [
        "DWF_COLOR_GRADIENT_FLATTENED"
    ]
    validate_ir(result.document, strict_jsonschema=True)


def test_dwf_polymarkers_become_point_entities() -> None:
    markers = _entity(
        "POLYMARKER", 1, points=tuple(_point(x, y) for x, y in ((1, 2), (3, 4), (5, 6)))
    )
    drawing = SimpleNamespace(sheets=(_sheet((markers,)),), diagnostics=())

    result = dwf_drawing_to_ir(drawing)

    entities = result.document["entities"]
    assert [entity["kind"] for entity in entities] == ["POINT"] * 3
    assert [entity["position"] for entity in entities] == [
        [1.0, 2.0],
        [3.0, 4.0],
        [5.0, 6.0],
    ]
    assert len({entity["id"] for entity in entities}) == 3
    assert result.statistics["converted_entities"] == 3
    validate_ir(result.document, strict_jsonschema=True)


def test_dwf_zero_radius_circle_becomes_point() -> None:
    dot = _entity(
        "CIRCLE",
        1,
        center=_point(4, 5),
        x_axis=_point(0, 0),
        y_axis=_point(0, 0),
        closed=True,
    )
    drawing = SimpleNamespace(sheets=(_sheet((dot,)),), diagnostics=())

    result = dwf_drawing_to_ir(drawing)

    (entity,) = result.document["entities"]
    assert entity["kind"] == "POINT" and entity["position"] == [4.0, 5.0]
    assert not [d for d in result.diagnostics if d.severity == "error"]
    validate_ir(result.document, strict_jsonschema=True)
