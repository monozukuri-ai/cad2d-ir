from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import pytest

from cad2d_ir.importers import ImportOptions
from cad2d_ir.importers.dwg import dwg_document_to_ir
from cad2d_ir.schema import validate_ir


@dataclass(frozen=True)
class _Entity:
    dxftype: str
    handle: int
    dxf: dict[str, Any]


class _Layout:
    def __init__(self, entities: list[_Entity]) -> None:
        self.entities = entities

    def query(self) -> list[_Entity]:
        return self.entities


class _Document:
    version = "AC1027"

    def __init__(self, entities: list[_Entity]) -> None:
        self.entities = entities

    def modelspace(self) -> _Layout:
        return _Layout(self.entities)


def _style(*, layer_handle: int = 16, owner_handle: int | None = None) -> dict:
    return {
        "layer_handle": layer_handle,
        "owner_handle": owner_handle,
        "resolved_color_index": 7,
        "resolved_true_color": None,
    }


def _document() -> _Document:
    return _Document(
        [
            _Entity(
                "LINE",
                1,
                {"start": (0.0, 0.0, 0.0), "end": (10.0, 0.0, 0.0), **_style()},
            ),
            _Entity(
                "CIRCLE",
                2,
                {"center": (2.0, 2.0, 0.0), "radius": 1.0, **_style()},
            ),
            _Entity(
                "ELLIPSE",
                3,
                {
                    "center": (5.0, 5.0, 0.0),
                    "major_axis": (3.0, 0.0, 0.0),
                    "axis_ratio": 0.5,
                    "start_angle": 0.0,
                    "end_angle": 6.283185307179586,
                    "extrusion": (0.0, 0.0, 1.0),
                    **_style(),
                },
            ),
            _Entity(
                "LWPOLYLINE",
                4,
                {
                    "points": [(0.0, 0.0, 0.0), (1.0, 1.0, 0.0), (2.0, 0.0, 0.0)],
                    "bulges": [0.0, 0.25, 0.0],
                    "widths": [(0.0, 0.0)] * 3,
                    "flags": 0,
                    "closed": False,
                    **_style(),
                },
            ),
            _Entity(
                "POINT",
                5,
                {"location": (1.0, 2.0, 3.0), "x_axis_angle": 0.0, **_style()},
            ),
            _Entity(
                "TEXT",
                6,
                {
                    "insert": (1.0, 1.0, 0.0),
                    "height": 2.5,
                    "text": "note",
                    "rotation": 15.0,
                    "width": 1.0,
                    "halign": 0,
                    "valign": 0,
                    **_style(),
                },
            ),
            _Entity(
                "SPLINE",
                7,
                {
                    "degree": 3,
                    "control_points": [
                        (0.0, 0.0, 0.0),
                        (1.0, 2.0, 0.0),
                        (2.0, 2.0, 0.0),
                        (3.0, 0.0, 0.0),
                    ],
                    "knots": [0.0, 0.0, 0.0, 0.0, 1.0, 1.0, 1.0, 1.0],
                    "weights": [],
                    "closed": False,
                    **_style(),
                },
            ),
            _Entity(
                "HATCH",
                8,
                {
                    "pattern_name": "SOLID",
                    "solid_fill": True,
                    "associative": False,
                    "paths": [
                        {
                            "closed": True,
                            "points": [
                                (0.0, 0.0, 0.0),
                                (2.0, 0.0, 0.0),
                                (2.0, 2.0, 0.0),
                                (0.0, 2.0, 0.0),
                                (0.0, 0.0, 0.0),
                            ],
                        }
                    ],
                    **_style(),
                },
            ),
            _Entity(
                "INSERT",
                9,
                {
                    "insert": (20.0, 30.0, 0.0),
                    "xscale": -1.0,
                    "yscale": 2.0,
                    "rotation": 90.0,
                    "name": "SYMBOL",
                    **_style(),
                },
            ),
            _Entity(
                "DIMENSION",
                10,
                {
                    "dimtype": "ALIGNED",
                    "defpoint": (0.0, 0.0, 0.0),
                    "defpoint2": (0.0, 0.0, 0.0),
                    "defpoint3": (10.0, 0.0, 0.0),
                    "text_midpoint": (5.0, 1.0, 0.0),
                    "text": "10",
                    "actual_measurement": 10.0,
                    **_style(),
                },
            ),
            _Entity("VIEWPORT", 11, _style()),
            _Entity(
                "LINE",
                12,
                {
                    "start": (0.0, 0.0, 0.0),
                    "end": (5.0, 0.0, 0.0),
                    **_style(owner_handle=100),
                },
            ),
        ]
    )


def test_dwg_document_to_ir_preserves_native_entities_and_blocks() -> None:
    result = dwg_document_to_ir(
        _document(),
        source_name="fixture.dwg",
        source_sha256="a" * 64,
        layer_names_by_handle={16: "Geometry"},
        layer_colors_by_handle={16: (7, None)},
        block_names_by_handle={100: "SYMBOL"},
    )
    document = result.document

    assert document["source"] == {
        "format": "dwg",
        "version": "AC1027",
        "name": "fixture.dwg",
        "sha256": "a" * 64,
    }
    assert document["header"]["units"] == "unknown"
    assert set(document["tables"]["layers"]) == {"0", "Geometry"}
    assert len(document["tables"]["blocks"]["SYMBOL"]["entities"]) == 1

    kinds = [entity["kind"] for entity in document["entities"]]
    assert kinds == [
        "LINE",
        "CIRCLE",
        "ELLIPSE",
        "LWPOLYLINE",
        "POINT",
        "TEXT",
        "SPLINE",
        "HATCH",
        "INSERT",
        "DIMENSION",
    ]
    assert document["entities"][3]["vertices"][1] == [1.0, 1.0, 0.25]
    assert document["entities"][8]["scale"] == [-1.0, 2.0]
    assert document["entities"][9]["dim_kind"] == "ALIGNED"
    assert result.statistics["preserved_dimensions"] == 1
    assert result.statistics["projected_entities"] == 1
    assert result.statistics["skipped_entity_counts"] == {"VIEWPORT": 1}
    assert {diagnostic.code for diagnostic in result.diagnostics} == {
        "DWG_NONPLANAR_PROJECTED",
        "DWG_UNSUPPORTED_ENTITY",
    }
    validate_ir(document, strict_jsonschema=True)


def test_dwg_document_to_ir_leniently_skips_malformed_entities() -> None:
    document = _Document(
        [_Entity("CIRCLE", 1, {"center": (0.0, 0.0, 0.0), "radius": 0.0})]
    )

    result = dwg_document_to_ir(
        document,
        options=ImportOptions(strict=False),
    )

    assert result.document["entities"] == []
    assert result.statistics["skipped_entity_counts"] == {"CIRCLE": 1}
    assert result.diagnostics[0].code == "DWG_ENTITY_CONVERSION_FAILED"

    with pytest.raises(ValueError, match="DWG CIRCLE"):
        dwg_document_to_ir(document)


class _DocumentWithHeader(_Document):
    def __init__(
        self, entities: list[_Entity], header_variables: dict[str, Any] | Exception
    ) -> None:
        super().__init__(entities)
        self._header_variables = header_variables

    def header_variables(self) -> dict[str, Any]:
        if isinstance(self._header_variables, Exception):
            raise self._header_variables
        return self._header_variables


def _line_entities() -> list[_Entity]:
    return [
        _Entity(
            "LINE",
            1,
            {"start": (0.0, 0.0, 0.0), "end": (10.0, 0.0, 0.0), **_style()},
        )
    ]


def test_dwg_units_resolved_from_insunits() -> None:
    result = dwg_document_to_ir(
        _DocumentWithHeader(_line_entities(), {"insunits": 4})
    )

    header = result.document["header"]
    assert header["units"] == "mm"
    assert header["metadata"]["dwg"]["insunits"] == 4
    assert "units_status" not in header["metadata"]["dwg"]
    assert not [d for d in result.diagnostics if "UNITS" in d.code]
    validate_ir(result.document, strict_jsonschema=True)


def test_dwg_units_imperial_code_maps_to_inch() -> None:
    result = dwg_document_to_ir(
        _DocumentWithHeader(_line_entities(), {"insunits": 1})
    )

    assert result.document["header"]["units"] == "inch"


def test_dwg_units_r14_without_insunits_stays_unknown() -> None:
    result = dwg_document_to_ir(
        _DocumentWithHeader(_line_entities(), {"insunits": None})
    )

    header = result.document["header"]
    assert header["units"] == "unknown"
    assert header["metadata"]["dwg"]["units_status"] == "INSUNITS not present (R14)"


def test_dwg_units_unsupported_code_falls_back_with_diagnostic() -> None:
    result = dwg_document_to_ir(
        _DocumentWithHeader(_line_entities(), {"insunits": 3})
    )

    header = result.document["header"]
    assert header["units"] == "unknown"
    assert header["metadata"]["dwg"]["insunits"] == 3
    assert header["metadata"]["dwg"]["units_status"] == "unsupported INSUNITS code"
    codes = [diagnostic.code for diagnostic in result.diagnostics]
    assert "DWG_UNSUPPORTED_INSUNITS" in codes


def test_dwg_units_absent_api_keeps_previous_behavior() -> None:
    result = dwg_document_to_ir(_document())

    header = result.document["header"]
    assert header["units"] == "unknown"
    assert header["metadata"]["dwg"]["units_status"] == "not exposed by ezdwg"


def test_dwg_units_header_decode_failure_is_lenient() -> None:
    result = dwg_document_to_ir(
        _DocumentWithHeader(_line_entities(), ValueError("corrupt header"))
    )

    header = result.document["header"]
    assert header["units"] == "unknown"
    assert header["metadata"]["dwg"]["units_status"] == "header variables unreadable"
    codes = [diagnostic.code for diagnostic in result.diagnostics]
    assert "DWG_HEADER_UNITS_UNREADABLE" in codes
    assert result.document["entities"], "import itself must still succeed"
