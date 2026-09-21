from __future__ import annotations

from dataclasses import dataclass, field
import math
from pathlib import Path
from typing import Any

import pytest

from cad2d_ir.importers.sxf import convert_sxf_file_to_ir, sxf_drawing_to_ir
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
class _Curve:
    """Mirror of ``ezsxf._drawing.CurveGeometry`` (ezsxf >= 0.1.2)."""

    kind: str
    center: tuple[float, float]
    axis_u: tuple[float, float]
    axis_v: tuple[float, float]
    start_param: float
    end_param: float
    closed: bool

    def at(self, t: float) -> tuple[float, float]:
        return (
            self.center[0]
            + self.axis_u[0] * math.cos(t)
            + self.axis_v[0] * math.sin(t),
            self.center[1]
            + self.axis_u[1] * math.cos(t)
            + self.axis_v[1] * math.sin(t),
        )

    def sample(self, count: int = 8) -> tuple[tuple[float, float], ...]:
        sweep = self.end_param - self.start_param
        last = count if self.closed else count + 1
        return tuple(self.at(self.start_param + sweep * i / count) for i in range(last))


@dataclass(frozen=True)
class _CurvePath:
    points: tuple[tuple[float, float], ...]
    closed: bool
    style: _Style
    source_id: int
    curve: _Curve | None = None


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


def _curve_path(curve: _Curve, source_id: int) -> _CurvePath:
    return _CurvePath(curve.sample(), curve.closed, _Style(), source_id, curve)


def _ir_curve_point(entity: dict[str, Any], param: float) -> tuple[float, float]:
    """Point of an IR ARC (degrees) or ELLIPSE (radians) at its own parameter."""
    cx, cy = entity["center"]
    if entity["kind"] == "ARC":
        angle = math.radians(param)
        return cx + entity["radius"] * math.cos(angle), cy + entity[
            "radius"
        ] * math.sin(angle)
    ax, ay = entity["major_axis"]
    bx, by = -ay * entity["ratio"], ax * entity["ratio"]
    return (
        cx + ax * math.cos(param) + bx * math.sin(param),
        cy + ay * math.cos(param) + by * math.sin(param),
    )


def _assert_same_open_curve(entity: dict[str, Any], curve: _Curve) -> None:
    """The IR curve is CCW; it must cover the same points as the source curve."""
    if entity["kind"] == "ARC":
        start, end = entity["start_angle"], entity["end_angle"]
        if end <= start:
            end += 360.0
    else:
        start, end = entity["start_param"], entity["end_param"]
    assert end > start
    middle = (start + end) / 2.0
    source_ends = [curve.at(curve.start_param), curve.at(curve.end_param)]
    ir_ends = [_ir_curve_point(entity, start), _ir_curve_point(entity, end)]
    forward = all(
        math.dist(a, b) < 1e-9 for a, b in zip(ir_ends, source_ends, strict=True)
    )
    backward = all(
        math.dist(a, b) < 1e-9
        for a, b in zip(ir_ends, reversed(source_ends), strict=True)
    )
    assert forward or backward
    source_middle = curve.at((curve.start_param + curve.end_param) / 2.0)
    assert math.dist(_ir_curve_point(entity, middle), source_middle) < 1e-9


def test_sfc_exact_curves_become_circle_arc_and_ellipse() -> None:
    r30, r120 = math.radians(30.0), math.radians(120.0)
    circle = _Curve("circle", (5.0, 5.0), (2.0, 0.0), (0.0, 2.0), 0.0, math.tau, True)
    ccw_arc = _Curve("arc", (10.0, 10.0), (5.0, 0.0), (0.0, 5.0), r30, r120, False)
    cw_arc = _Curve(
        "arc", (10.0, 10.0), (5.0, 0.0), (0.0, 5.0), r30, math.radians(-240.0), False
    )
    # 鏡映つきの複合図形配置: (u, v) が左手系になり、t の増加が時計回りになる
    mirrored_arc = _Curve("arc", (0.0, 0.0), (5.0, 0.0), (0.0, -5.0), r30, r120, False)
    # 90度回転・X倍率2の配置を通った円 = 楕円(ezsxf のテストと同じ値)
    stretched = _Curve(
        "circle", (8.0, 24.0), (0.0, 2.0), (-1.0, 0.0), 0.0, math.tau, True
    )
    # せん断を含む共役半径の楕円弧(時計回り)
    sheared_arc = _Curve(
        "ellipse_arc",
        (1.0, 2.0),
        (4.0, 1.0),
        (-1.0, 2.0),
        r120,
        math.radians(-100.0),
        False,
    )
    full_arc = _Curve(
        "arc", (0.0, 0.0), (3.0, 0.0), (0.0, 3.0), r30, r30 + math.tau, False
    )
    curves = [circle, ccw_arc, cw_arc, mirrored_arc, stretched, sheared_arc, full_arc]
    features = [
        {"id": index, "kind": curve.kind, "keyword": f"{curve.kind}_feature"}
        for index, curve in enumerate(curves, start=1)
    ]
    parsed = {**_parsed(), "typed_features": features}
    drawing = _Drawing(
        paths=[_curve_path(curve, index) for index, curve in enumerate(curves, start=1)]
    )

    result = sxf_drawing_to_ir(drawing, parsed=parsed)
    entities = result.document["entities"]

    assert [entity["kind"] for entity in entities] == [
        "CIRCLE",
        "ARC",
        "ARC",
        "ARC",
        "ELLIPSE",
        "ELLIPSE",
        "CIRCLE",
    ]
    assert entities[0]["center"] == [5.0, 5.0]
    assert entities[0]["radius"] == pytest.approx(2.0)
    assert (entities[1]["start_angle"], entities[1]["end_angle"]) == pytest.approx(
        (30.0, 120.0)
    )
    # 時計回りの 30度→-240度 は、反時計回りの 120度→30度 と同じ弧
    assert (entities[2]["start_angle"], entities[2]["end_angle"]) == pytest.approx(
        (120.0, 30.0)
    )
    assert (entities[3]["start_angle"], entities[3]["end_angle"]) == pytest.approx(
        (240.0, 330.0)
    )
    assert entities[4]["major_axis"] == pytest.approx([0.0, 2.0])
    assert entities[4]["ratio"] == pytest.approx(0.5)
    assert (entities[4]["start_param"], entities[4]["end_param"]) == pytest.approx(
        (0.0, math.tau)
    )
    assert entities[6]["radius"] == pytest.approx(3.0)
    for entity, curve in (
        (entities[1], ccw_arc),
        (entities[2], cw_arc),
        (entities[3], mirrored_arc),
        (entities[5], sheared_arc),
    ):
        _assert_same_open_curve(entity, curve)
        assert entity["ccw"] is True
    # せん断楕円: 全サンプル点が IR の楕円上にある
    ellipse = entities[5]
    ax, ay = ellipse["major_axis"]
    major = math.hypot(ax, ay)
    for x, y in sheared_arc.sample(32):
        dx, dy = x - ellipse["center"][0], y - ellipse["center"][1]
        along = (dx * ax + dy * ay) / major
        across = (-dx * ay + dy * ax) / major
        assert (along / major) ** 2 + (
            across / (major * ellipse["ratio"])
        ) ** 2 == pytest.approx(1.0)

    assert all("approximation" not in entity for entity in entities)
    assert all(entity["metadata"]["sxf"]["source_id"] for entity in entities)
    assert result.statistics["approximated_entities"] == 0
    assert "SXF_CURVE_APPROXIMATED" not in {d.code for d in result.diagnostics}
    validate_ir(result.document, strict_jsonschema=True)


def test_degenerate_exact_curves_fall_back_to_the_sampled_polyline() -> None:
    flat = _Curve("circle", (0.0, 0.0), (2.0, 0.0), (4.0, 0.0), 0.0, math.tau, True)
    points = ((0.0, 0.0), (1.0, 1.0), (2.0, 0.0), (1.0, -1.0))
    drawing = _Drawing(paths=[_CurvePath(points, True, _Style(), 2, flat)])

    result = sxf_drawing_to_ir(drawing, parsed=_parsed())

    (entity,) = [e for e in result.document["entities"] if e["kind"] == "LWPOLYLINE"]
    assert entity["approximation"]["source_kind"] == "circle"
    assert not [
        e for e in result.document["entities"] if e["kind"] in ("CIRCLE", "ELLIPSE")
    ]


_ARC_SFC = """ISO-10303-21;
HEADER;
FILE_DESCRIPTION(('SCADEC level2 feature_mode'),'2;1');
FILE_NAME('arcs.sfc','2007-06-29T07:56:58',('author'),('organization'),'translator$$3.1','system','');
FILE_SCHEMA(('ASSOCIATIVE_DRAUGHTING'));
ENDSEC;
DATA;
/*SXF
#1 = pre_defined_colour_feature('red')
SXF*/
/*SXF
#2 = pre_defined_font_feature('continuous')
SXF*/
/*SXF
#3 = width_feature('0.25')
SXF*/
/*SXF
#20 = circle_feature('1','2','1','3','2','2','1')
SXF*/
/*SXF
#30 = sfig_org_feature('stretched','1')
SXF*/
/*SXF
#21 = circle_feature('1','2','1','3','2','2','1')
SXF*/
/*SXF
#31 = sfig_org_feature('scaled','1')
SXF*/
/*SXF
#40 = sfig_locate_feature('1','stretched','10','20','90','2','1')
SXF*/
/*SXF
#41 = sfig_locate_feature('1','scaled','0','0','0','3','3')
SXF*/
/*SXF
#81 = arc_feature('1','2','1','3','10','10','5','1','30','120')
SXF*/
/*SXF
#82 = circle_feature('1','2','1','3','50','50','7.5')
SXF*/
/*SXF
#99 = drawing_sheet_feature('sheet','9','1','100','300')
SXF*/
/*SXF
#100 = layer_feature('VISIBLE','1')
SXF*/
ENDSEC;
END-ISO-10303-21;
"""


def test_real_ezsxf_curves_survive_compound_figure_placements(tmp_path: Path) -> None:
    ezsxf_drawing = pytest.importorskip("ezsxf._drawing")
    if not hasattr(ezsxf_drawing, "CurveGeometry"):
        pytest.skip("ezsxf < 0.1.2 does not expose exact curves")
    source = tmp_path / "arcs.sfc"
    source.write_text(_ARC_SFC, encoding="utf-8")

    result = convert_sxf_file_to_ir(source)
    by_kind: dict[str, list[dict[str, Any]]] = {}
    for entity in result.document["entities"]:
        by_kind.setdefault(entity["kind"], []).append(entity)

    # 倍率 3x3 の配置を通った円は円のまま(半径3倍)、倍率 2x1 の配置では楕円になる
    radii = sorted(circle["radius"] for circle in by_kind["CIRCLE"])
    assert radii == pytest.approx([3.0, 7.5])
    scaled = next(c for c in by_kind["CIRCLE"] if c["radius"] == pytest.approx(3.0))
    assert scaled["center"] == pytest.approx([6.0, 6.0])
    (ellipse,) = by_kind["ELLIPSE"]
    assert ellipse["center"] == pytest.approx([8.0, 24.0])
    assert ellipse["ratio"] == pytest.approx(0.5)
    (arc,) = by_kind["ARC"]
    assert (arc["start_angle"], arc["end_angle"]) == pytest.approx((120.0, 30.0))
    assert "LWPOLYLINE" not in by_kind
    assert result.statistics["approximated_entities"] == 0
    validate_ir(result.document, strict_jsonschema=True)
