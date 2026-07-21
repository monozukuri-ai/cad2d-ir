from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from cad2d_ir.importers.sxf import sxf_drawing_to_ir
from cad2d_ir.schema import validate_ir


@dataclass(frozen=True)
class _Style:
    layer: str = "Geometry"
    color: tuple[int, int, int] = (255, 0, 0)
    line_type: str = "continuous"
    line_width_mm: float = 0.25
    font_name: str | None = None
    visible: bool = True


@dataclass(frozen=True)
class _Path:
    points: tuple[tuple[float, float], ...]
    closed: bool
    style: _Style
    source_id: int


@dataclass(frozen=True)
class _Fill:
    outer: tuple[tuple[float, float], ...]
    holes: tuple[tuple[tuple[float, float], ...], ...]
    style: _Style
    source_id: int


@dataclass(frozen=True)
class _Text:
    text: str
    anchor: tuple[float, float]
    height: float
    width: float
    angle_deg: float
    base_point: int
    direction: int
    style: _Style
    source_id: int


@dataclass(frozen=True)
class _Marker:
    position: tuple[float, float]
    marker_code: int
    scale: float
    style: _Style
    source_id: int
    name: str | None = None


@dataclass
class _Drawing:
    paths: list[_Path] = field(default_factory=list)
    fills: list[_Fill] = field(default_factory=list)
    texts: list[_Text] = field(default_factory=list)
    markers: list[_Marker] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    background_color: tuple[int, int, int] = (0, 0, 0)

    def bounds(self) -> tuple[float, float, float, float]:
        return (0.0, 0.0, 10.0, 10.0)


def _parsed() -> dict[str, Any]:
    features = [
        {"id": 1, "kind": "line", "keyword": "line_feature"},
        {"id": 2, "kind": "circle", "keyword": "circle_feature"},
        {
            "id": 3,
            "kind": "linear_dim",
            "keyword": "linear_dim_feature",
            "start": {"x": 0.0, "y": 5.0},
            "end": {"x": 10.0, "y": 5.0},
            "text": {"present_flag": 1, "text": "10"},
        },
        {"id": 4, "kind": "text", "keyword": "text_string_feature"},
        {
            "id": 5,
            "kind": "fill_area_style_colour",
            "keyword": "fill_area_style_colour_feature",
        },
        {"id": 6, "kind": "point_marker", "keyword": "point_marker_feature"},
    ]
    return {
        "format": "sfc",
        "header": {
            "file_description": {"parameters": [["SCADEC level2 feature_mode"], "2;1"]}
        },
        "entities": [{"id": feature["id"]} for feature in features],
        "typed_features": features,
        "model": {},
    }


def test_sfc_drawing_to_ir_preserves_dimensions_and_records_curves() -> None:
    style = _Style()
    text_style = _Style(font_name="Test Font")
    square = ((0.0, 0.0), (2.0, 0.0), (2.0, 2.0), (0.0, 2.0))
    drawing = _Drawing(
        paths=[
            _Path(((0.0, 0.0), (10.0, 0.0)), False, style, 1),
            _Path(((7.0, 5.0), (5.0, 7.0), (3.0, 5.0), (5.0, 3.0)), True, style, 2),
            _Path(((0.0, 5.0), (10.0, 5.0)), False, style, 3),
            _Path(square, True, style, 5),
        ],
        fills=[_Fill(square, (), style, 5)],
        texts=[
            _Text("10", (5.0, 5.5), 1.0, 1.2, 0.0, 2, 1, text_style, 3),
            _Text("note", (1.0, 1.0), 2.0, 4.8, 15.0, 1, 1, text_style, 4),
        ],
        markers=[_Marker((4.0, 4.0), 3, 0.5, style, 6)],
        warnings=["fixture warning"],
    )

    result = sxf_drawing_to_ir(
        drawing,
        parsed=_parsed(),
        source_name="fixture.sfc",
        source_sha256="b" * 64,
    )
    document = result.document

    assert document["source"]["version"] == "level2"
    assert document["source"]["metadata"]["sxf"]["container"] == "sfc"
    assert document["header"]["units"] == "mm"
    assert document["header"]["bbox"] == {
        "min": [0.0, 0.0],
        "max": [10.0, 10.0],
    }
    assert [entity["kind"] for entity in document["entities"]] == [
        "DIMENSION",
        "HATCH",
        "LINE",
        "LWPOLYLINE",
        "TEXT",
        "POINT",
    ]
    dimension = document["entities"][0]
    assert dimension["dim_kind"] == "LINEAR"
    assert dimension["definition"]["text"] == "10"
    assert dimension["definition"]["rendered_paths"][0]["points"] == [
        [0.0, 5.0],
        [10.0, 5.0],
    ]
    assert document["entities"][3]["approximation"]["source_kind"] == "circle"
    assert result.statistics["preserved_dimensions"] == 1
    assert result.statistics["approximated_entities"] == 1
    assert {diagnostic.code for diagnostic in result.diagnostics} == {
        "SXF_CURVE_APPROXIMATED",
        "SXF_DRAWING_WARNING",
    }
    validate_ir(document, strict_jsonschema=True)


def test_p21_drawing_to_ir_marks_flattened_semantics() -> None:
    drawing = _Drawing(
        paths=[
            _Path(
                ((0.0, 0.0), (1.0, 1.0), (2.0, 0.0)),
                False,
                _Style(),
                100,
            )
        ]
    )
    parsed = {
        "format": "p21",
        "header": {},
        "entities": [{"id": 100}],
        "typed_features": [],
        "model": None,
    }

    result = sxf_drawing_to_ir(drawing, parsed=parsed)

    entity = result.document["entities"][0]
    assert entity["approximation"]["source_kind"] == "P21_DRAWING_PATH"
    assert {diagnostic.code for diagnostic in result.diagnostics} == {
        "SXF_CURVE_APPROXIMATED",
        "SXF_P21_SEMANTICS_FLATTENED",
    }
    validate_ir(result.document, strict_jsonschema=True)
