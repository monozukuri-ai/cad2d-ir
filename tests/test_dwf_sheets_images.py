"""DWF import: several sheets land on a grid, raster images become IMAGE entities."""

from __future__ import annotations

import base64
from types import SimpleNamespace
from typing import Any

import pytest

from cad2d_ir.importers.base import ImporterError, ImportOptions
from cad2d_ir.importers.dwf import dwf_drawing_to_ir
from cad2d_ir.schema import validate_ir

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 16
TIFF = b"II*\x00" + b"\x00" * 16


def _point(x: float, y: float) -> SimpleNamespace:
    return SimpleNamespace(x=x, y=y)


def _style() -> SimpleNamespace:
    return SimpleNamespace(
        layer="walls",
        layer_number=1,
        color=(0, 0, 0, 255),
        stroke_color=(0, 0, 0, 255),
        fill_color=None,
        line_pattern=None,
        stroke_dash_array=(),
        nominal_stroke_width=0.25,
        fill=False,
        font_name="Arial",
        font_canonical_name="Arial",
        font_height=2.5,
        font_rotation_degrees=0.0,
        visible=True,
        opacity=1.0,
    )


def _entity(kind: str, index: int, section: int = 0, **values: Any) -> SimpleNamespace:
    defaults: dict[str, Any] = {
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
        "source": SimpleNamespace(offset=index * 10, length=10, opcode=kind),
        "resource_href": f"sheet{section}/main.w2d",
        "resource_role": "2d streaming graphics",
        "is_markup": False,
        "section_index": section,
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
    section: int,
    entities: tuple[Any, ...],
    *,
    paper: tuple[float, float, float, float] | None = (0.0, 0.0, 420.0, 297.0),
    content: tuple[float, float, float, float] | None = (10.0, 10.0, 110.0, 60.0),
    units: str = "mm",
) -> SimpleNamespace:
    return SimpleNamespace(
        name=f"Sheet {section + 1}",
        title=f"Sheet {section + 1}",
        plot_order=section + 1,
        units=units,
        paper_bounds=paper,
        content_bounds=content,
        section_index=section,
        entities=entities,
        markup_entities=(),
    )


def _image(
    index: int,
    *,
    section: int = 0,
    format_name: str = "png",
    data: bytes = PNG,
    low: tuple[float, float] = (10.0, 20.0),
    high: tuple[float, float] = (50.0, 40.0),
    color_map: tuple[tuple[int, int, int, int], ...] = (),
    **extra: Any,
) -> SimpleNamespace:
    record = SimpleNamespace(
        format=format_name,
        identifier=index,
        columns=40,
        rows=20,
        min=_point(*low),
        max=_point(*high),
        color_map=color_map,
        data=data,
        **extra,
    )
    return _entity("IMAGE", index, section, image=record)


def _line_sheet(section: int, **kwargs: Any) -> SimpleNamespace:
    line = _entity("LINE", 1, section, points=(_point(10, 10), _point(110, 60)))
    return _sheet(section, (line,), **kwargs)


def _codes(result: Any) -> set[str]:
    return {diagnostic.code for diagnostic in result.diagnostics}


def test_five_sheets_form_a_grid_of_three_columns() -> None:
    drawing = SimpleNamespace(
        sheets=tuple(_line_sheet(section) for section in range(5)), diagnostics=()
    )
    result = dwf_drawing_to_ir(drawing)
    document = result.document

    step_x, step_y = 420.0 + 42.0, 297.0 + 42.0
    starts = [entity["p1"] for entity in document["entities"]]
    assert starts == [
        [10.0, 10.0],
        [10.0 + step_x, 10.0],
        [10.0 + 2 * step_x, 10.0],
        [10.0, 10.0 - step_y],
        [10.0 + step_x, 10.0 - step_y],
    ]
    assert "DWF_MULTISHEET_TILED" in _codes(result)
    assert "DWF_MULTISHEET_FLATTENED" not in _codes(result)
    tiled = next(d for d in result.diagnostics if d.code == "DWF_MULTISHEET_TILED")
    assert tiled.details == {
        "sheet_count": 5,
        "columns": 3,
        "rows": 2,
        "cell": [420.0, 297.0],
        "gap": 42.0,
        "offsets": [
            [0.0, 0.0],
            [step_x, 0.0],
            [2 * step_x, 0.0],
            [0.0, -step_y],
            [step_x, -step_y],
        ],
    }
    sheets = document["source"]["metadata"]["dwf"]["sheets"]
    assert sheets[4]["offset"] == [step_x, -step_y]
    assert sheets[4]["cell"] == [1, 1]
    assert sheets[4]["placed_bounds"] == [step_x, -step_y, 420.0 + step_x, 297.0 - step_y]
    assert document["header"]["metadata"]["dwf"]["tiled_sheets"] is True
    assert document["header"]["metadata"]["dwf"]["sheet_grid"] == {
        "columns": 3,
        "rows": 2,
        "cell": [420.0, 297.0],
        "gap": 42.0,
    }
    assert document["header"]["bbox"] == {
        "min": [10.0, 10.0 - step_y],
        "max": [110.0 + 2 * step_x, 60.0],
    }
    validate_ir(document, strict_jsonschema=True)


def test_up_to_four_sheets_sit_in_one_row() -> None:
    drawing = SimpleNamespace(
        sheets=tuple(_line_sheet(section) for section in range(4)), diagnostics=()
    )
    result = dwf_drawing_to_ir(drawing)
    offsets = [s["offset"] for s in result.document["source"]["metadata"]["dwf"]["sheets"]]
    assert offsets == [[0.0, 0.0], [462.0, 0.0], [924.0, 0.0], [1386.0, 0.0]]


def test_sheets_of_different_papers_share_the_largest_cell() -> None:
    small = _line_sheet(1, paper=(0.0, 0.0, 297.0, 210.0))
    drawing = SimpleNamespace(sheets=(_line_sheet(0), small), diagnostics=())
    result = dwf_drawing_to_ir(drawing)
    sheets = result.document["source"]["metadata"]["dwf"]["sheets"]
    # The small sheet is aligned with the top left of its cell.
    assert sheets[1]["offset"] == [462.0, 297.0 - 210.0]
    assert sheets[1]["placed_bounds"] == [462.0, 87.0, 462.0 + 297.0, 297.0]


def test_sheet_without_paper_uses_its_content_and_an_empty_sheet_stays() -> None:
    bare = _line_sheet(1, paper=None, content=(10.0, 10.0, 110.0, 60.0))
    empty = _sheet(2, (), paper=None, content=None)
    drawing = SimpleNamespace(sheets=(_line_sheet(0), bare, empty), diagnostics=())
    result = dwf_drawing_to_ir(drawing)
    sheets = result.document["source"]["metadata"]["dwf"]["sheets"]
    # Two sheets have an extent, so they form one row of two; the bare sheet's
    # content box (100 x 50) is placed in the second 420 x 297 cell.
    assert sheets[1]["offset"] == [462.0 - 10.0, 297.0 - 60.0]
    assert sheets[2]["offset"] == [0.0, 0.0]
    assert sheets[2]["cell"] is None
    tiled = next(d for d in result.diagnostics if d.code == "DWF_MULTISHEET_TILED")
    assert tiled.details["columns"] == 2


def test_tiling_moves_every_kind_of_geometry() -> None:
    circle = _entity(
        "CIRCLE", 2, 1, center=_point(20, 20), x_axis=_point(5, 0), y_axis=_point(0, 5)
    )
    polygon = _entity(
        "POLYGON",
        3,
        1,
        points=(_point(0, 0), _point(5, 0), _point(5, 5)),
        closed=True,
    )
    text = _entity("TEXT", 4, 1, points=(_point(2, 3),), text="fixture")
    bezier = _entity(
        "POLYBEZIER",
        5,
        1,
        points=(_point(0, 0), _point(1, 2), _point(3, 2), _point(4, 0)),
    )
    strip = _entity(
        "GOURAUD_POLYTRIANGLE",
        6,
        1,
        colored_points=(
            SimpleNamespace(point=_point(0, 0), color=(255, 0, 0, 255)),
            SimpleNamespace(point=_point(10, 0), color=(0, 255, 0, 255)),
            SimpleNamespace(point=_point(0, 10), color=(0, 0, 255, 255)),
        ),
    )
    picture = _image(7, section=1)
    second = _sheet(1, (circle, polygon, text, bezier, strip, picture))
    drawing = SimpleNamespace(sheets=(_line_sheet(0), second), diagnostics=())
    result = dwf_drawing_to_ir(drawing)
    kinds = {entity["kind"]: entity for entity in result.document["entities"][1:]}

    assert kinds["CIRCLE"]["center"] == [482.0, 20.0]
    assert kinds["LWPOLYLINE"]["vertices"] == [[462.0, 0.0], [467.0, 0.0], [467.0, 5.0]]
    assert kinds["TEXT"]["insert"] == [464.0, 3.0]
    assert kinds["SPLINE"]["control_points"][0] == [462.0, 0.0]
    assert kinds["HATCH"]["loops"][0]["vertices"][1] == [472.0, 0.0]
    assert kinds["HATCH"]["metadata"]["dwf"]["colored_points"][1]["point"] == [472.0, 0.0]
    assert kinds["IMAGE"]["insert"] == [472.0, 20.0]
    validate_ir(result.document, strict_jsonschema=True)


def test_mixed_units_keep_the_sheets_flattened() -> None:
    drawing = SimpleNamespace(
        sheets=(_line_sheet(0, units="mm"), _line_sheet(1, units="in")),
        diagnostics=(),
    )
    result = dwf_drawing_to_ir(drawing)
    assert "DWF_MULTISHEET_FLATTENED" in _codes(result)
    assert "DWF_MULTISHEET_TILED" not in _codes(result)
    assert [e["p1"] for e in result.document["entities"]] == [[10.0, 10.0], [10.0, 10.0]]


def test_single_sheet_is_not_moved() -> None:
    drawing = SimpleNamespace(sheets=(_line_sheet(0),), diagnostics=())
    result = dwf_drawing_to_ir(drawing)
    assert result.document["header"]["metadata"]["dwf"]["tiled_sheets"] is False
    assert "offset" not in result.document["source"]["metadata"]["dwf"]["sheets"][0]
    assert not _codes(result) & {"DWF_MULTISHEET_TILED", "DWF_MULTISHEET_FLATTENED"}


@pytest.mark.parametrize(
    ("format_name", "data", "mime", "extension"),
    [("png", PNG, "image/png", "png"), ("jpeg", JPEG, "image/jpeg", "jpg")],
)
def test_png_and_jpeg_images_are_embedded_as_they_are(
    format_name: str, data: bytes, mime: str, extension: str
) -> None:
    drawing = SimpleNamespace(
        sheets=(_sheet(0, (_image(3, format_name=format_name, data=data),)),),
        diagnostics=(),
    )
    result = dwf_drawing_to_ir(drawing)
    (image,) = result.document["entities"]
    assert image["kind"] == "IMAGE"
    assert image["insert"] == [10.0, 20.0]
    assert image["width"] == 40.0
    assert image["height"] == 20.0
    assert image["mime_type"] == mime
    assert image["name"] == f"dwf_image_3.{extension}"
    assert base64.b64decode(image["data"]) == data
    assert image["metadata"]["dwf"]["image"] == {
        "format": format_name,
        "identifier": 3,
        "columns": 40,
        "rows": 20,
        "color_map_size": 0,
        "transparent": False,
    }
    assert result.statistics["embedded_images"] == 1
    assert result.statistics["skipped_entity_counts"] == {}
    embedded = next(d for d in result.diagnostics if d.code == "DWF_IMAGE_EMBEDDED")
    assert embedded.details == {"count": 1}
    validate_ir(result.document, strict_jsonschema=True)


def test_group4_image_is_decoded_by_ezdwf() -> None:
    picture = _image(
        4,
        format_name="group4x_mapped",
        data=TIFF,
        color_map=((255, 255, 255, 0), (0, 0, 0, 255)),
        to_png=lambda: PNG,
    )
    drawing = SimpleNamespace(sheets=(_sheet(0, (picture,)),), diagnostics=())
    result = dwf_drawing_to_ir(drawing)
    (image,) = result.document["entities"]
    assert image["mime_type"] == "image/png"
    assert base64.b64decode(image["data"]) == PNG
    assert image["metadata"]["dwf"]["image"]["format"] == "group4x_mapped"
    assert image["metadata"]["dwf"]["image"]["transparent"] is True
    assert image["metadata"]["dwf"]["image"]["color_map_size"] == 2


def test_image_the_installed_ezdwf_cannot_decode_stays_unsupported() -> None:
    picture = _image(5, format_name="group4x_mapped", data=TIFF)
    drawing = SimpleNamespace(sheets=(_sheet(0, (picture,)),), diagnostics=())
    result = dwf_drawing_to_ir(drawing)
    assert result.document["entities"] == []
    assert result.statistics["skipped_entity_counts"] == {"IMAGE": 1}
    assert result.statistics["embedded_images"] == 0
    assert _codes(result) == {"DWF_UNSUPPORTED_ENTITY"}


def test_image_that_fails_to_decode_is_diagnosed_and_skipped() -> None:
    def explode() -> bytes:
        raise RuntimeError("Group 4 data ends after 3 of 20 rows")

    picture = _image(6, format_name="group4", data=b"\x00\x01", to_png=explode)
    drawing = SimpleNamespace(sheets=(_sheet(0, (picture,)),), diagnostics=())
    result = dwf_drawing_to_ir(drawing)
    assert result.document["entities"] == []
    failed = next(d for d in result.diagnostics if d.code == "DWF_IMAGE_DECODE_FAILED")
    assert failed.action == "skipped"
    assert "3 of 20 rows" in failed.message
    assert failed.details["format"] == "group4"
    assert result.statistics["skipped_entity_counts"] == {"IMAGE": 1}


def test_image_without_area_is_a_conversion_failure() -> None:
    picture = _image(8, low=(10.0, 20.0), high=(10.0, 40.0))
    drawing = SimpleNamespace(sheets=(_sheet(0, (picture,)),), diagnostics=())
    with pytest.raises(ImporterError, match="IMAGE has no area"):
        dwf_drawing_to_ir(drawing)
    result = dwf_drawing_to_ir(drawing, options=ImportOptions(strict=False))
    assert result.document["entities"] == []
    assert "DWF_ENTITY_CONVERSION_FAILED" in _codes(result)
