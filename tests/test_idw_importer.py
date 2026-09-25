from __future__ import annotations

import json
import math
import os
from pathlib import Path
import sys
from types import SimpleNamespace
from typing import Any

import pytest

from cad2d_ir import convert_file_to_ir, detect_source_format, validate_ir
from cad2d_ir.cli import main
from cad2d_ir.importers import (
    ImporterError,
    ImportOptions,
    MissingOptionalDependencyError,
)
from cad2d_ir.importers.idw import convert_idw_file_to_ir, idw_document_to_ir

IDENTITY = (
    (1.0, 0.0, 0.0, 0.0),
    (0.0, 1.0, 0.0, 0.0),
    (0.0, 0.0, 1.0, 0.0),
    (0.0, 0.0, 0.0, 1.0),
)
A3 = (42.0, 29.7)


def _item(
    index: int,
    kind: str,
    geometry: dict[str, Any],
    *,
    layer: str | None = "Visible (ANSI)",
    width: float | None = 0.05,
    rgba: tuple[float, float, float, float] | None = None,
    dash: list[float] | None = None,
    unresolved: tuple[str, ...] = (),
) -> SimpleNamespace:
    return SimpleNamespace(
        id=f"{'ab' * 32}/sheet-{index}",
        geometry={"kind": kind, **geometry},
        style={
            "visible": True,
            "width": width,
            "rgba": rgba,
            "dash": dash,
            "layer": layer,
            "sources": [],
            "unresolved": list(unresolved),
        },
        segment_id="segment",
        record_ordinal=index,
        placement_record=7,
        group_path=(),
        transform=IDENTITY,
    )


def _font(
    family: str = "Tahoma", height: float = 0.35, **values: Any
) -> dict[str, Any]:
    return {
        "id": 1,
        "family": family,
        "height_candidate": height,
        "weight_candidate": 400,
        "width_factor": 1.0,
        "flags": 0,
        **values,
    }


def _sheet(
    items: list[Any],
    *,
    index: int = 0,
    name: str = "Sheet",
    size: tuple[float, float] | None = A3,
    status: str = "experimental_partial",
    omissions: tuple[dict[str, Any], ...] = (),
    views: tuple[Any, ...] = (),
    diagnostics: tuple[str, ...] = (),
) -> SimpleNamespace:
    return SimpleNamespace(
        id=f"{'ab' * 32}/sheet-{index}",
        index=index,
        name=name,
        status=status,
        size_in_source_units=size,
        space_segment="segment",
        items=tuple(items),
        omissions=tuple(omissions),
        sources=(),
        diagnostics=tuple(diagnostics),
        views=tuple(views),
    )


def _document(
    sheets: list[Any],
    *,
    images: tuple[Any, ...] = (),
    diagnostics: tuple[dict[str, Any], ...] = (),
    majors: tuple[int, ...] = (31,),
    status: str = "experimental_partial",
) -> SimpleNamespace:
    return SimpleNamespace(
        api_version=1,
        source_sha256="ab" * 32,
        status=status,
        sheet_status="stored_order_unverified_state",
        units="source_units_unverified",
        length_unit=None,
        millimeters_per_unit=None,
        qualified=False,
        complete=False,
        current_state="unverified",
        metadata=SimpleNamespace(
            segments=[
                SimpleNamespace(major=major, kind="DlSheetDlSegmentType")
                for major in majors
            ]
        ),
        sheets=tuple(sheets),
        images=tuple(images),
        diagnostics=tuple(diagnostics),
    )


def _by_kind(entities: list[dict[str, Any]], kind: str) -> list[dict[str, Any]]:
    return [entity for entity in entities if entity["kind"] == kind]


def _codes(result: Any) -> set[str]:
    return {diagnostic.code for diagnostic in result.diagnostics}


def _ellipse_point(entity: dict[str, Any], parameter: float) -> tuple[float, float]:
    cx, cy = entity["center"]
    mx, my = entity["major_axis"]
    ratio = entity["ratio"]
    return (
        cx + mx * math.cos(parameter) - my * ratio * math.sin(parameter),
        cy + my * math.cos(parameter) + mx * ratio * math.sin(parameter),
    )


def _curve_point(geometry: dict[str, Any], t: float) -> tuple[float, float]:
    c, u, v = geometry["center"], geometry["u"], geometry["v"]
    return (
        (c[0] + u[0] * math.cos(t) + v[0] * math.sin(t)) * 10.0,
        (c[1] + u[1] * math.cos(t) + v[1] * math.sin(t)) * 10.0,
    )


def _full_document() -> SimpleNamespace:
    skewed = {
        "center": [20.0, 10.0, -5.0],
        "u": [2.0, 0.0, 0.3],
        "v": [1.0, 1.0, 0.0],
        "start": 0.3,
        "end": 2.0,
    }
    items = [
        _item(
            0,
            "polyline",
            {"points": [[0, 0, 0], [0, 29.7, 0], [42, 29.7, 0], [42, 0, 0], [0, 0, 0]]},
            layer=None,
            width=None,
            rgba=(0.12, 0.12, 0.28, 1.0),
        ),
        _item(1, "polyline", {"points": [[1, 1, 0], [5, 3, 0]]}, dash=[0.456, 0.114]),
        _item(
            2,
            "curve",
            {
                "center": [33.35, 1.75, 0.0],
                "u": [0.0, 0.25, 0.0],
                "v": [-0.25, 0.0, 0.0],
                "start": 0.0,
                "end": 2 * math.pi,
            },
        ),
        _item(
            3,
            "curve",
            {
                "center": [10.0, 10.0, 0.0],
                "u": [1.0, 0.0, 0.0],
                "v": [0.0, 1.0, 0.0],
                "start": 0.0,
                "end": math.pi / 2,
            },
        ),
        _item(4, "curve", skewed, unresolved=("layer_dash_pattern_not_decoded",)),
        _item(
            5,
            "curve",
            {
                "center": [30.0, 20.0, -3.0],
                "u": [1.5, 0.0, 0.0],
                "v": [0.0, 0.0, 1.5],
                "start": 0.0,
                "end": math.pi,
            },
        ),
        _item(
            6,
            "curve",
            {
                "center": [5.0, 20.0, 0.0],
                "u": [0.4, 0.0, 0.0],
                "v": [0.0, 0.4, 0.0],
                "start": 0.0,
                "end": math.pi,
                "filled": True,
            },
        ),
        _item(
            7,
            "triangles",
            {
                "vertices": [[1, 1, 0], [1.5, 1, 0], [1.25, 1.5, 0]],
                "indices": [0, 1, 2],
            },
        ),
        _item(
            8,
            "text",
            {
                "text": "図面",
                "position": [10.0, 5.0, 0.0],
                "direction": [math.sqrt(0.5), math.sqrt(0.5), 0.0],
                "up": [-math.sqrt(0.5), math.sqrt(0.5), 0.0],
                "raw_flags": 9,
                "font": _font(),
            },
            width=0.07,
        ),
        _item(
            9,
            "text",
            {
                "text": "n",
                "position": [12.0, 5.0, 0.0],
                "direction": [1.0, 0.0, 0.0],
                "up": [0.0, 1.0, 0.0],
                "raw_flags": 9,
                "font": _font("AIGDT", 0.6096),
            },
        ),
        _item(
            10,
            "text",
            {
                "text": "LINE 1\nLINE 2",
                "position": [20.0, 25.0, 0.0],
                "direction": [1.0, 0.0, 0.0],
                "up": [0.0, 1.0, 0.0],
                "raw_flags": 9,
                "font": _font("Arial", 0.5, width_factor=0.8, flags=1),
            },
        ),
        _item(
            11,
            "text",
            {
                "text": "mirror",
                "position": [30.0, 25.0, 0.0],
                "direction": [1.0, 0.0, 0.0],
                "up": [0.0, -1.0, 0.0],
                "raw_flags": 9,
                "font": _font("Tahoma", 0.3, weight_candidate=700),
            },
        ),
        _item(
            12,
            "image",
            {
                "reference": 2147483648,
                "format": 3,
                "origin": [5.675, 11.325, 0.0],
                "u": [8.65, 0.0, 0.0],
                "v": [0.0, -2.65, 0.0],
            },
            layer=None,
            width=None,
        ),
        _item(13, "polyline", {"points": [[100.0, 100.0, 0.0], [110.0, 100.0, 0.0]]}),
        _item(14, "sketch_marker", {"position": [1.0, 1.0, 0.0]}),
    ]
    sheet = _sheet(
        items,
        name="Blatt",
        omissions=(
            {"reason": "hidden_by_stored_attribute", "segment_id": "segment"},
            {"reason": "display_type_not_decoded", "segment_id": "segment"},
            {"reason": "display_type_not_decoded", "segment_id": "segment"},
        ),
        views=(
            SimpleNamespace(name="View1", image_reference=2147483648, item_ids=()),
            SimpleNamespace(name="View2", image_reference=None, item_ids=("x",)),
        ),
        diagnostics=("stored_display_partial",),
    )
    image = SimpleNamespace(
        reference=2147483648,
        status="decoded_monochrome_view_cache_unqualified",
        mime_type="image/png",
        width=340,
        height=104,
        sha256="cd" * 32,
        data=b"\x89PNG\r\n\x1a\n",
        source=None,
        diagnostic=None,
    )
    return _document(
        [sheet],
        images=(image,),
        diagnostics=(
            {
                "code": "drawing.unverified",
                "severity": "warning",
                "message": "physical_units_and_active_state_unverified",
                "source": None,
            },
        ),
    )


def test_saved_display_maps_to_ir_with_centimeter_scaling() -> None:
    result = idw_document_to_ir(
        _full_document(),
        source_name="sample.idw",
        source_sha256="ef" * 32,
        parser_version="0.6.0",
    )
    document = result.document
    validate_ir(document)

    assert document["source"]["format"] == "idw"
    assert document["source"]["version"] == "segment-major-31"
    assert document["source"]["name"] == "sample.idw"
    assert document["source"]["sha256"] == "ef" * 32
    idw = document["source"]["metadata"]["idw"]
    assert idw["parser_version"] == "0.6.0"
    assert idw["segment_majors"] == [31]
    assert idw["sheets"][0]["paper"] == "A3"
    assert idw["sheets"][0]["size_mm"] == [420.0, 297.0]
    assert idw["sheets"][0]["raster_only_view_count"] == 1
    assert idw["images"][0]["bytes"] == 8
    assert idw["images"][0]["sha256"] == "cd" * 32
    assert document["header"]["units"] == "mm"
    assert document["header"]["bbox"] == {"min": [0.0, 0.0], "max": [420.0, 297.0]}
    assert document["header"]["metadata"]["idw"]["unit_scale_to_mm"] == 10.0

    entities = document["entities"]
    kinds = {entity["kind"] for entity in entities}
    assert kinds == {"LWPOLYLINE", "LINE", "CIRCLE", "ARC", "ELLIPSE", "HATCH", "TEXT"}

    frame = _by_kind(entities, "LWPOLYLINE")[0]
    assert frame["closed"] is True
    assert frame["vertices"] == [[0.0, 0.0], [0.0, 297.0], [420.0, 297.0], [420.0, 0.0]]
    assert frame["layer"] == "0"
    assert frame["color"] == "#1F1F47"
    assert "lineweight_mm" not in frame

    lines = _by_kind(entities, "LINE")
    dashed = [line for line in lines if line["linetype"] != "CONTINUOUS"]
    assert dashed[0]["p1"] == [10.0, 10.0]
    assert dashed[0]["p2"] == [50.0, 30.0]
    assert dashed[0]["lineweight_mm"] == pytest.approx(0.5)
    assert document["tables"]["linetypes"][dashed[0]["linetype"]][
        "pattern_mm"
    ] == pytest.approx([4.56, 1.14])
    assert document["tables"]["layers"]["Visible (ANSI)"] == {
        "linetype": "CONTINUOUS",
        "plot": True,
    }

    circle = _by_kind(entities, "CIRCLE")[0]
    assert circle["center"] == pytest.approx([333.5, 17.5])
    assert circle["radius"] == pytest.approx(2.5)

    arc = _by_kind(entities, "ARC")[0]
    assert arc["center"] == pytest.approx([100.0, 100.0])
    assert arc["radius"] == pytest.approx(10.0)
    assert arc["start_angle"] == pytest.approx(0.0)
    assert arc["end_angle"] == pytest.approx(90.0)

    ellipse = _by_kind(entities, "ELLIPSE")[0]
    skewed = _full_document().sheets[0].items[4].geometry
    # The canonical parametrization must reproduce the source curve endpoints
    # and orientation: start -> mid -> end in CCW parameter order.
    start_param, end_param = ellipse["start_param"], ellipse["end_param"]
    sweep = (end_param - start_param) % (2 * math.pi)
    for fraction, t in ((0.0, 0.3), (0.5, 1.15), (1.0, 2.0)):
        expected = _curve_point(skewed, t)
        found = False
        for candidate in (start_param + sweep * fraction, end_param - sweep * fraction):
            if math.dist(_ellipse_point(ellipse, candidate), expected) < 1e-6:
                found = True
        assert found, f"point at t={t} not on the IR ellipse"
    assert ellipse["ratio"] < 1.0
    assert ellipse["metadata"]["idw"]["unresolved"] == [
        "layer_dash_pattern_not_decoded"
    ]

    edge_on = [line for line in lines if "approximation" in line]
    assert len(edge_on) == 1
    assert edge_on[0]["approximation"]["metadata"]["reason"] == "edge_on_projection"
    assert edge_on[0]["p1"] == pytest.approx([285.0, 200.0])
    assert edge_on[0]["p2"] == pytest.approx([315.0, 200.0])

    hatches = _by_kind(entities, "HATCH")
    assert len(hatches) == 2
    filled = [hatch for hatch in hatches if "approximation" in hatch][0]
    assert filled["solid"] is True
    assert filled["approximation"]["source_kind"] == "filled_curve"
    assert len(filled["loops"][0]["vertices"]) >= 9
    triangle = [hatch for hatch in hatches if "approximation" not in hatch][0]
    assert triangle["loops"][0]["vertices"] == [
        [10.0, 10.0],
        [15.0, 10.0],
        [12.5, 15.0],
    ]

    texts = _by_kind(entities, "TEXT")
    by_text = {text["text"]: text for text in texts}
    assert by_text["図面"]["insert"] == pytest.approx([100.0, 50.0])
    assert by_text["図面"]["height"] == pytest.approx(3.5)
    assert by_text["図面"]["rotation"] == pytest.approx(45.0)
    assert by_text["図面"]["style"] == "IDW_Tahoma"
    assert by_text["図面"]["lineweight_mm"] == pytest.approx(0.7)
    assert by_text["⌀"]["style"] == "IDW_AIGDT"
    assert by_text["⌀"]["height"] == pytest.approx(6.096)
    assert by_text["LINE 1"]["style"] == "IDW_Arial_ITALIC"
    assert by_text["LINE 1"]["width_factor"] == pytest.approx(0.8)
    assert by_text["LINE 1"]["insert"] == pytest.approx([200.0, 250.0])
    assert by_text["LINE 2"]["insert"] == pytest.approx([200.0, 250.0 - 1.2 * 5.0])
    assert by_text["mirror"]["style"] == "IDW_Tahoma_BOLD"
    assert by_text["mirror"]["metadata"]["idw"]["mirrored"] is True
    assert document["tables"]["text_styles"]["IDW_Arial_ITALIC"] == {
        "font": "Arial",
        "oblique_deg": 15.0,
    }
    assert document["tables"]["text_styles"]["IDW_Tahoma_BOLD"] == {"font": "Tahoma"}

    for entity in entities:
        assert entity["source"]["format"] == "idw"
        assert entity["metadata"]["idw"]["sheet_index"] == 0
    assert len({entity["id"] for entity in entities}) == len(entities)

    placements = idw["sheets"][0]["images"]
    assert placements == [
        {
            "reference": 2147483648,
            "origin": [56.75, 113.25],
            "u": [86.5, 0.0],
            "v": [0.0, -26.5],
            "item_id": f"{'ab' * 32}/sheet-12",
        }
    ]

    codes = _codes(result)
    assert codes == {
        "IDW_UNITS_ASSUMED_CM",
        "IDW_VIEW_RASTER_ONLY",
        "IDW_OUT_OF_SHEET_DROPPED",
        "IDW_CURVE_EDGE_ON_LINE",
        "IDW_FILLED_CURVE_APPROXIMATED",
        "IDW_TEXT_SYMBOL_MAPPED",
        "IDW_TEXT_MIRROR_IGNORED",
        "IDW_TEXT_MULTILINE_SPLIT",
        "IDW_IMAGE_NOT_IN_IR",
        "IDW_UNSUPPORTED_ELEMENT",
        "IDW_ELEMENT_OMITTED",
        "IDW_STYLE_UNRESOLVED",
        "IDW_DRAWING_WARNING",
    }
    by_code = {diagnostic.code: diagnostic for diagnostic in result.diagnostics}
    assert by_code["IDW_UNITS_ASSUMED_CM"].severity == "info"
    assert by_code["IDW_OUT_OF_SHEET_DROPPED"].details["count"] == 1
    assert by_code["IDW_UNSUPPORTED_ELEMENT"].source_kind == "sketch_marker"
    assert by_code["IDW_DRAWING_WARNING"].details == {
        "upstream_code": "drawing.unverified",
        "count": 1,
    }
    omitted = [d for d in result.diagnostics if d.code == "IDW_ELEMENT_OMITTED"]
    assert {(d.source_kind, d.severity, d.details["count"]) for d in omitted} == {
        ("hidden_by_stored_attribute", "info", 1),
        ("display_type_not_decoded", "warning", 2),
    }

    statistics = result.statistics
    assert statistics["source_format"] == "idw"
    assert statistics["source_majors"] == [31]
    assert statistics["source_entities"] == 15
    assert statistics["source_entity_counts"] == {
        "curve": 5,
        "image": 1,
        "polyline": 3,
        "sketch_marker": 1,
        "text": 4,
        "triangles": 1,
    }
    assert statistics["converted_entities"] == len(entities)
    assert statistics["converted_entity_counts"]["TEXT"] == 5
    assert statistics["skipped_entity_counts"] == {
        "image": 1,
        "polyline": 1,
        "sketch_marker": 1,
    }
    assert statistics["approximated_entities"] == 2
    assert statistics["out_of_sheet_entities"] == 1
    assert statistics["omitted_elements"] == 3
    assert statistics["images"] == 1
    assert statistics["raster_only_views"] == 1


def test_multiple_sheets_are_tiled_along_x() -> None:
    first = _sheet([_item(0, "polyline", {"points": [[1, 1, 0], [2, 1, 0]]})], index=0)
    second = _sheet(
        [_item(0, "polyline", {"points": [[1, 1, 0], [2, 1, 0]]})],
        index=1,
        size=(29.7, 21.0),
    )
    result = idw_document_to_ir(_document([first, second]))
    validate_ir(result.document)

    lines = _by_kind(result.document["entities"], "LINE")
    assert lines[0]["p1"] == [10.0, 10.0]
    assert lines[1]["p1"] == [420.0 + 42.0 + 10.0, 10.0]
    assert result.document["header"]["bbox"] == {
        "min": [0.0, 0.0],
        "max": [420.0 + 42.0 + 297.0, 297.0],
    }
    tiled = [d for d in result.diagnostics if d.code == "IDW_MULTISHEET_TILED"][0]
    assert tiled.details["offsets_mm"] == [[0.0, 0.0], [462.0, 0.0]]
    assert tiled.details["gap_mm"] == pytest.approx(42.0)
    sheets = result.document["source"]["metadata"]["idw"]["sheets"]
    assert [sheet["paper"] for sheet in sheets] == ["A3", "A4"]
    assert [sheet["offset_mm"] for sheet in sheets] == [[0.0, 0.0], [462.0, 0.0]]
    assert result.statistics["converted_sheets"] == 2

    packed = idw_document_to_ir(_document([first, second]), sheet_gap_ratio=0.0)
    assert _by_kind(packed.document["entities"], "LINE")[1]["p1"] == [430.0, 10.0]


def test_unsupported_profile_raises_with_segment_major() -> None:
    document = _document(
        [_sheet([], size=None, status="unavailable")],
        majors=(21,),
        status="unavailable",
        diagnostics=(
            {
                "code": "drawing.unsupported_profile",
                "severity": "warning",
                "message": "no framing profile for DlSheetDlSegmentType / major 21",
                "source": None,
            },
        ),
    )
    with pytest.raises(
        ImporterError, match=r"unsupported IDW profile: segment major 21"
    ):
        idw_document_to_ir(document)


def test_unavailable_sheet_is_reported_while_others_convert() -> None:
    available = _sheet(
        [_item(0, "polyline", {"points": [[1, 1, 0], [2, 1, 0]]})], index=0
    )
    missing = _sheet(
        [],
        index=1,
        size=None,
        status="unavailable",
        diagnostics=("stored_display_unavailable",),
    )
    result = idw_document_to_ir(_document([available, missing]))

    assert len(result.document["entities"]) == 1
    unavailable = [d for d in result.diagnostics if d.code == "IDW_SHEET_UNAVAILABLE"]
    assert len(unavailable) == 1
    assert unavailable[0].details["sheet_index"] == 1
    assert unavailable[0].details["diagnostics"] == ["stored_display_unavailable"]
    assert "IDW_MULTISHEET_TILED" not in _codes(result)
    assert result.statistics["source_sheets"] == 2
    assert result.statistics["converted_sheets"] == 1


def test_descending_parameter_range_keeps_the_arc_point_set() -> None:
    geometry = {
        "center": [10.0, 10.0, 0.0],
        "u": [1.0, 0.0, 0.0],
        "v": [0.0, 1.0, 0.0],
        "start": math.pi / 2,
        "end": 0.0,
    }
    result = idw_document_to_ir(_document([_sheet([_item(0, "curve", geometry)])]))
    arc = _by_kind(result.document["entities"], "ARC")[0]
    assert arc["start_angle"] == pytest.approx(0.0)
    assert arc["end_angle"] == pytest.approx(90.0)


def test_strict_mode_raises_and_lenient_mode_skips_malformed_items() -> None:
    broken = _item(
        0,
        "curve",
        {"center": [1, 1, 0], "u": None, "v": [0, 1, 0], "start": 0, "end": 1},
    )
    good = _item(1, "polyline", {"points": [[1, 1, 0], [2, 1, 0]]})
    document = _document([_sheet([broken, good])])

    with pytest.raises(ImporterError, match=r"Failed to convert IDW curve"):
        idw_document_to_ir(document)

    result = idw_document_to_ir(document, options=ImportOptions(strict=False))
    assert [entity["kind"] for entity in result.document["entities"]] == ["LINE"]
    failed = [d for d in result.diagnostics if d.code == "IDW_ENTITY_CONVERSION_FAILED"]
    assert len(failed) == 1
    assert failed[0].source_kind == "curve"
    assert result.statistics["skipped_entity_counts"] == {"curve": 1}


def test_entity_provenance_can_be_omitted() -> None:
    document = _document(
        [_sheet([_item(0, "polyline", {"points": [[1, 1, 0], [2, 1, 0]]})])]
    )
    result = idw_document_to_ir(
        document, options=ImportOptions(entity_provenance=False)
    )
    entity = result.document["entities"][0]
    assert "source" not in entity
    assert "metadata" not in entity
    validate_ir(result.document)


def test_unit_scale_override_and_nonstandard_paper_warning() -> None:
    sheet = _sheet(
        [_item(0, "polyline", {"points": [[1, 1, 0], [2, 1, 0]]})], size=(10.0, 7.0)
    )
    result = idw_document_to_ir(_document([sheet]), unit_scale=1.0)
    assert result.document["entities"][0]["p1"] == [1.0, 1.0]
    assert result.document["header"]["bbox"]["max"] == [10.0, 7.0]
    units = [d for d in result.diagnostics if d.code == "IDW_UNITS_ASSUMED_CM"][0]
    assert units.severity == "warning"
    assert units.details["sheets"][0]["paper"] is None

    with pytest.raises(ValueError):
        idw_document_to_ir(_document([sheet]), unit_scale=0.0)
    with pytest.raises(ValueError):
        idw_document_to_ir(_document([sheet]), sheet_gap_ratio=-1.0)


def test_file_import_requires_inventor_kit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(sys.modules, "inventor_kit", None)
    source = tmp_path / "drawing.idw"
    source.write_bytes(b"\xd0\xcf\x11\xe0" + b"\x00" * 32)
    with pytest.raises(MissingOptionalDependencyError, match=r"cad2d-ir\[idw\]"):
        convert_idw_file_to_ir(source)


def test_registry_detects_idw_suffix() -> None:
    assert detect_source_format("drawing.IDW") == "idw"


# ---------------------------------------------------------------------------
# Real-file validation gate (opt-in): CAD2D_IR_IDW_FIXTURES points at
# inventor-kit's fixtures/public directory.

_FIXTURES = os.environ.get("CAD2D_IR_IDW_FIXTURES")
needs_fixtures = pytest.mark.skipif(
    not _FIXTURES or not Path(_FIXTURES).is_dir(),
    reason="set CAD2D_IR_IDW_FIXTURES to inventor-kit's fixtures/public directory",
)


@needs_fixtures
def test_real_major31_drawing_converts_and_validates(tmp_path: Path) -> None:
    pytest.importorskip("inventor_kit")
    source = Path(_FIXTURES or "") / "SampleBg.idw"
    if not source.is_file():
        pytest.skip("SampleBg.idw is not downloaded")

    result = convert_idw_file_to_ir(source)
    document = result.document
    validate_ir(document)
    assert document["source"]["format"] == "idw"
    assert document["source"]["name"] == "SampleBg.idw"
    assert document["source"]["metadata"]["idw"]["segment_majors"] == [31]
    assert document["source"]["metadata"]["idw"]["sheets"][0]["paper"] == "A3"
    assert result.statistics["converted_entities"] > 100
    assert result.statistics["converted_entity_counts"]["TEXT"] > 40
    assert "IDW_IMAGE_NOT_IN_IR" in _codes(result)

    auto = convert_file_to_ir(source)
    assert (
        auto.statistics["converted_entities"] == result.statistics["converted_entities"]
    )

    output = tmp_path / "sample.json"
    assert main(["import", str(source), "--format", "idw", "-o", str(output)]) == 0
    written = json.loads(output.read_text(encoding="utf-8"))
    assert written["source"]["format"] == "idw"


@needs_fixtures
def test_real_major26_drawing_keeps_arrowheads_and_view_metadata() -> None:
    pytest.importorskip("inventor_kit")
    source = (
        Path(_FIXTURES or "")
        / "drawings"
        / "versions"
        / "RespiraWorks-bottom-assembly.idw"
    )
    if not source.is_file():
        pytest.skip("RespiraWorks-bottom-assembly.idw is not downloaded")

    result = convert_idw_file_to_ir(source)
    validate_ir(result.document)
    assert result.document["source"]["metadata"]["idw"]["segment_majors"] == [26]
    assert result.statistics["converted_entity_counts"]["HATCH"] >= 3
    assert result.statistics["approximated_entity_counts"].get("curve", 0) == 0
