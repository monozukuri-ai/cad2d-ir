"""Native SXF (SFC/P21) to CAD 2D IR importer backed by :mod:`ezsxf`."""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
import hashlib
import math
from pathlib import Path
import re
from typing import Any, Iterable, Mapping, Sequence

from cad2d_ir.importers.base import (
    ImportDiagnostic,
    ImporterError,
    ImportOptions,
    ImportResult,
    MissingOptionalDependencyError,
)
from cad2d_ir.importers.linetypes import sxf_linetype_pattern
from cad2d_ir.schema import validate_ir

_EPSILON = 1.0e-12

_CURVE_KINDS = {"arc", "circle", "ellipse", "ellipse_arc", "spline", "clothoid"}
_DIMENSION_KINDS = {
    "linear_dim": "LINEAR",
    "curve_dim": "ALIGNED",
    "angular_dim": "ANGULAR",
    "radius_dim": "RADIAL",
    "diameter_dim": "DIAMETER",
}
# Compound figure kinds (``sfig_org`` kind flag): 1 = partial drawing in the
# mathematical coordinate system, 2 = partial drawing in the geodetic system,
# 3 = drawing group, 4 = drawing part. Only the first two are partial drawings.
_PARTIAL_DRAWING_SYSTEMS = {1: "mathematical", 2: "geodetic"}
# AP202 subfigure names carry the kind as a prefix.
_P21_PARTIAL_PREFIXES = {"$$SXF_FM_": "mathematical", "$$SXF_FG_": "geodetic"}
# Standard sheets (sheet type 0-4 = A0-A4) in landscape millimetres; 9 = FREE.
_STANDARD_SHEETS_MM = {
    0: (1189.0, 841.0),
    1: (841.0, 594.0),
    2: (594.0, 420.0),
    3: (420.0, 297.0),
    4: (297.0, 210.0),
}
_FREE_SHEET_TYPE = 9
_SHEET_SIZE_TOLERANCE_MM = 0.5
_STEP_ESCAPE_RE = re.compile(r"\\X([24])\\([0-9A-Fa-f]+)\\X0\\")
_P21_SHEET_NAME_RE = re.compile(r"^A([0-4])_(horizontal|vertical)$", re.IGNORECASE)

_Affine = tuple[float, float, float, float, float, float]
_IDENTITY: _Affine = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)


@dataclass(slots=True)
class _PartialDrawing:
    """A partial drawing placed on the sheet: its placement and its entities."""

    name: str
    coordinate_system: str
    position: tuple[float, float]
    angle_deg: float
    ratio_x: float
    ratio_y: float
    definition_id: int
    placement_id: int
    entity_count: int = 0

    def as_metadata(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "name": self.name,
            "coordinate_system": self.coordinate_system,
            "position": [self.position[0], self.position[1]],
            "angle_deg": self.angle_deg,
            "ratio_x": self.ratio_x,
            "ratio_y": self.ratio_y,
            "entity_count": self.entity_count,
            "definition_id": self.definition_id,
            "placement_id": self.placement_id,
        }
        if math.isclose(self.ratio_x, self.ratio_y, rel_tol=1.0e-9):
            result["scale_denominator"] = 1.0 / self.ratio_x
        return result


@dataclass(slots=True)
class _ConversionContext:
    options: ImportOptions
    container: str
    feature_by_id: dict[int, Mapping[str, Any]]
    diagnostics: list[ImportDiagnostic] = field(default_factory=list)
    converted_counts: Counter[str] = field(default_factory=Counter)
    approximation_counts: Counter[str] = field(default_factory=Counter)
    skipped_counts: Counter[str] = field(default_factory=Counter)
    layers: dict[str, dict[str, Any]] = field(default_factory=dict)
    linetypes: dict[str, dict[str, Any]] = field(default_factory=dict)
    user_linetype_patterns: dict[str, list[float]] = field(default_factory=dict)
    text_styles: dict[str, dict[str, Any]] = field(default_factory=dict)
    preserved_dimensions: int = 0
    next_entity_number: int = 1
    # partial drawings placed on the sheet, by name; which source feature each
    # entity was rendered through; features reachable from several placements
    partials: dict[str, _PartialDrawing] = field(default_factory=dict)
    partial_of: dict[int, str] = field(default_factory=dict)
    ambiguous_ids: set[int] = field(default_factory=set)
    ambiguous_entities: int = 0

    def allocate_id(self) -> str:
        entity_id = f"SXF_E{self.next_entity_number:08d}"
        self.next_entity_number += 1
        return entity_id


def convert_sxf_file_to_ir(
    path: str | Path,
    *,
    options: ImportOptions | None = None,
) -> ImportResult:
    """Read an SFC/P21 file with ``ezsxf`` and convert its drawing model to IR."""
    try:
        import ezsxf
        from ezsxf import parse_p21, parse_sfc
    except ImportError as exc:
        raise MissingOptionalDependencyError(
            "SXF support requires the optional dependency ezsxf; "
            'install it with `pip install "cad2d-ir[sxf]"`.'
        ) from exc
    build_drawing = getattr(ezsxf, "build_drawing", None)
    if build_drawing is None:  # ezsxf < 0.3.1 has no public entry point
        from ezsxf._drawing import build_drawing

    import_options = options or ImportOptions()
    source_path = Path(path)
    container = _detect_container(source_path)
    parser = parse_p21 if container == "p21" else parse_sfc
    try:
        parsed = parser(str(source_path), strict=import_options.strict)
        drawing = build_drawing(
            parsed,
            strict=import_options.strict,
            curve_segments=import_options.curve_segments,
        )
    except Exception as exc:
        raise ImporterError(
            f"Failed to parse {container.upper()} input: {exc}"
        ) from exc

    return sxf_drawing_to_ir(
        drawing,
        parsed=parsed,
        source_name=source_path.name,
        source_sha256=_sha256_file(source_path),
        options=import_options,
    )


def sxf_drawing_to_ir(
    drawing: Any,
    *,
    parsed: Mapping[str, Any] | None = None,
    source_name: str | None = None,
    source_sha256: str | None = None,
    options: ImportOptions | None = None,
) -> ImportResult:
    """Convert an ``ezsxf._drawing.Drawing``-compatible object to IR.

    ``parsed`` should be the native ``parse_sfc`` or ``parse_p21`` result. It is
    used to retain source feature identities and preserve SFC dimensions as
    semantic IR ``DIMENSION`` entities.
    """
    import_options = options or ImportOptions()
    parsed_map = parsed or {}
    container = str(parsed_map.get("format", "sfc")).lower()
    if container not in {"sfc", "p21"}:
        raise ImporterError(f"Unsupported SXF container: {container!r}")

    typed_features = _mapping_sequence(
        parsed_map.get("typed_features", []), "typed_features"
    )
    feature_by_id = {
        int(feature["id"]): feature for feature in typed_features if "id" in feature
    }
    partials, partial_of, ambiguous_ids = _partial_drawings(parsed_map, feature_by_id)
    context = _ConversionContext(
        options=import_options,
        container=container,
        feature_by_id=feature_by_id,
        user_linetype_patterns=_user_linetype_patterns(typed_features),
        partials={partial.name: partial for partial in partials},
        partial_of=partial_of,
        ambiguous_ids=ambiguous_ids,
    )

    paths = list(_iter_attr(drawing, "paths"))
    fills = list(_iter_attr(drawing, "fills"))
    texts = list(_iter_attr(drawing, "texts"))
    markers = list(_iter_attr(drawing, "markers"))

    paths_by_source = _group_by_source(paths)
    texts_by_source = _group_by_source(texts)
    dimension_source_ids = {
        feature_id
        for feature_id, feature in feature_by_id.items()
        if str(feature.get("kind")) in _DIMENSION_KINDS
    }
    fill_source_ids = {_source_id(fill) for fill in fills}

    entities: list[dict[str, Any]] = []
    for source_id in sorted(dimension_source_ids):
        feature = feature_by_id[source_id]
        rendered_paths = paths_by_source.get(source_id, [])
        rendered_texts = texts_by_source.get(source_id, [])
        try:
            dimension = _convert_dimension(
                feature,
                rendered_paths,
                rendered_texts,
                context,
            )
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            if import_options.strict:
                raise ImporterError(
                    f"Failed to preserve SXF dimension #{source_id}: {exc}"
                ) from exc
            context.skipped_counts[str(feature.get("kind", "dimension"))] += 1
            context.diagnostics.append(
                ImportDiagnostic(
                    code="SXF_DIMENSION_CONVERSION_FAILED",
                    severity="error",
                    message=f"Failed to preserve SXF dimension: {exc}",
                    source_id=str(source_id),
                    source_kind=str(feature.get("kind", "dimension")),
                    action="skipped",
                )
            )
        else:
            entities.append(dimension)
            context.converted_counts["DIMENSION"] += 1
            context.preserved_dimensions += 1

    for fill in fills:
        _append_converted(
            entities,
            fill,
            context,
            _convert_fill,
            source_kind=_feature_kind(fill, context, "fill"),
        )
    for path in paths:
        source_id = _source_id(path)
        if source_id in dimension_source_ids or source_id in fill_source_ids:
            continue
        _append_converted(
            entities,
            path,
            context,
            _convert_path,
            source_kind=_feature_kind(path, context, "path"),
        )
    for text in texts:
        if _source_id(text) in dimension_source_ids:
            continue
        _append_converted(
            entities,
            text,
            context,
            _convert_text,
            source_kind=_feature_kind(text, context, "text"),
        )
    for marker in markers:
        _append_converted(
            entities,
            marker,
            context,
            _convert_marker,
            source_kind=_feature_kind(marker, context, "marker"),
        )

    _append_drawing_warnings(drawing, context)
    _append_summary_diagnostics(context)

    source: dict[str, Any] = {
        "format": "sxf",
        "metadata": {
            "sxf": {
                "container": container,
                "header": _json_safe(parsed_map.get("header", {})),
            }
        },
    }
    source_version = _source_version(parsed_map)
    if source_version is not None:
        source["version"] = source_version
    if source_name is not None:
        source["name"] = source_name
    if source_sha256 is not None:
        source["sha256"] = source_sha256

    header: dict[str, Any] = {
        "units": "mm",
        "angle_unit": "deg",
        "coord_space": "world",
        "metadata": {
            "sxf": {
                "container": container,
                "background_color": _color_hex(
                    _attr(drawing, "background_color", (255, 255, 255))
                ),
                "curve_segments": import_options.curve_segments,
            }
        },
    }
    sheet = _sheet_metadata(parsed_map, feature_by_id)
    if sheet is not None:
        header["metadata"]["sxf"]["sheet"] = sheet
    if partials:
        header["metadata"]["sxf"]["partial_drawings"] = [
            partial.as_metadata() for partial in partials
        ]
    bounds = _drawing_bounds(drawing)
    if bounds is not None:
        header["bbox"] = {
            "min": [bounds[0], bounds[1]],
            "max": [bounds[2], bounds[3]],
        }

    tables: dict[str, Any] = {
        "layers": context.layers,
        "linetypes": context.linetypes,
        "text_styles": context.text_styles,
    }
    document: dict[str, Any] = {
        "format": "cad2d-ir",
        "version": import_options.ir_version,
        "header": header,
        "source": source,
        "tables": tables,
        "entities": entities,
    }
    if import_options.validate:
        validate_ir(document)

    source_counts = Counter(
        str(feature.get("kind", "UNKNOWN")) for feature in typed_features
    )
    statistics: dict[str, Any] = {
        "source_format": "sxf",
        "source_container": container,
        "source_entities": len(_sequence(parsed_map.get("entities", []), "entities")),
        "source_typed_features": len(typed_features),
        "source_feature_counts": dict(sorted(source_counts.items())),
        "drawing_primitives": {
            "paths": len(paths),
            "fills": len(fills),
            "texts": len(texts),
            "markers": len(markers),
        },
        "converted_entities": len(entities),
        "converted_entity_counts": dict(sorted(context.converted_counts.items())),
        "skipped_entities": sum(context.skipped_counts.values()),
        "skipped_entity_counts": dict(sorted(context.skipped_counts.items())),
        "approximated_entities": sum(context.approximation_counts.values()),
        "preserved_dimensions": context.preserved_dimensions,
        "partial_drawings": len(partials),
    }
    return ImportResult(
        document=document,
        diagnostics=context.diagnostics,
        statistics=statistics,
    )


def _append_converted(
    entities: list[dict[str, Any]],
    primitive: Any,
    context: _ConversionContext,
    converter: Any,
    *,
    source_kind: str,
) -> None:
    try:
        entity = converter(primitive, context)
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        if context.options.strict:
            raise ImporterError(
                f"Failed to convert SXF {source_kind} #{_source_id(primitive)}: {exc}"
            ) from exc
        context.skipped_counts[source_kind] += 1
        context.diagnostics.append(
            ImportDiagnostic(
                code="SXF_PRIMITIVE_CONVERSION_FAILED",
                severity="error",
                message=f"Failed to convert SXF {source_kind}: {exc}",
                source_id=str(_source_id(primitive)),
                source_kind=source_kind,
                action="skipped",
            )
        )
        return
    entities.append(entity)
    context.converted_counts[str(entity["kind"])] += 1


def _convert_path(primitive: Any, context: _ConversionContext) -> dict[str, Any]:
    source_id = _source_id(primitive)
    source_kind = _feature_kind(primitive, context, "path")
    points = [_point(value) for value in _iter_attr(primitive, "points")]
    closed = bool(_attr(primitive, "closed", False))
    if closed and len(points) > 2 and _near(points[0], points[-1]):
        points.pop()
    if len(points) < 2:
        raise ValueError("path requires at least two points")

    common = _primitive_common(primitive, source_kind, context)
    # ezsxf >= 0.1.2 attaches the exact circle/arc/ellipse to curved paths (already
    # transformed through compound-figure placements). Emit it as a true curve so
    # DXF output stays editable; older ezsxf falls through to the sampled polyline.
    curve_entity = _curve_entity(_attr(primitive, "curve", None), common)
    if curve_entity is not None:
        curve_entity["metadata"]["sxf"]["source_id"] = source_id
        return curve_entity
    if len(points) == 2 and not closed:
        return {**common, "kind": "LINE", "p1": points[0], "p2": points[1]}

    entity: dict[str, Any] = {
        **common,
        "kind": "LWPOLYLINE",
        "vertices": points,
        "closed": closed,
    }
    if _path_is_approximated(source_kind, context.container, len(points)):
        approximation_kind = (
            "P21_DRAWING_PATH" if context.container == "p21" else source_kind
        )
        entity["approximation"] = {
            "method": "polyline",
            "source_kind": approximation_kind,
            "segments": len(points) if closed else len(points) - 1,
        }
        context.approximation_counts[approximation_kind] += 1
    entity["metadata"]["sxf"]["source_id"] = source_id
    return entity


_TAU = 2.0 * math.pi
_CURVE_TOLERANCE = 1.0e-9


def _curve_entity(curve: Any, common: dict[str, Any]) -> dict[str, Any] | None:
    """Convert an ``ezsxf`` exact curve to an IR CIRCLE, ARC or ELLIPSE.

    The curve is ``center + u*cos(t) + v*sin(t)`` for ``t`` from ``start_param`` to
    ``end_param``; ``u``/``v`` are conjugate semi-diameters, so a circle placed with
    unequal X/Y ratios arrives here as a (possibly sheared) ellipse. Returns ``None``
    when the curve is missing or degenerate and the sampled polyline should be used.
    """
    if curve is None:
        return None
    try:
        center = _point(_attr(curve, "center"))
        u = _point(_attr(curve, "axis_u"))
        v = _point(_attr(curve, "axis_v"))
        start = float(_attr(curve, "start_param"))
        end = float(_attr(curve, "end_param"))
    except (TypeError, ValueError, KeyError, IndexError):
        return None
    values = (*center, *u, *v, start, end)
    if not all(math.isfinite(value) for value in values):
        return None

    length_u = math.hypot(u[0], u[1])
    length_v = math.hypot(v[0], v[1])
    scale = max(length_u, length_v)
    determinant = u[0] * v[1] - u[1] * v[0]
    if scale <= _EPSILON or abs(determinant) <= _CURVE_TOLERANCE * scale * scale:
        return None
    orientation = 1.0 if determinant > 0.0 else -1.0
    sweep = end - start
    is_full = (
        bool(_attr(curve, "closed", False)) or abs(sweep) >= _TAU - _CURVE_TOLERANCE
    )
    if not is_full and abs(sweep) <= _CURVE_TOLERANCE:
        return None
    # Parameter t increases counter-clockwise only when (u, v) is right-handed.
    runs_ccw = orientation * sweep > 0.0

    is_circle = (
        abs(length_u - length_v) <= _CURVE_TOLERANCE * scale
        and abs(u[0] * v[0] + u[1] * v[1]) <= _CURVE_TOLERANCE * scale * scale
    )
    if is_circle:
        radius = (length_u + length_v) * 0.5
        if is_full:
            return {**common, "kind": "CIRCLE", "center": center, "radius": radius}
        base = math.atan2(u[1], u[0])
        first = math.degrees(base + orientation * start)
        last = math.degrees(base + orientation * end)
        if not runs_ccw:
            first, last = last, first
        return {
            **common,
            "kind": "ARC",
            "center": center,
            "radius": radius,
            "start_angle": _normalize_degrees(first),
            "end_angle": _normalize_degrees(last),
            "ccw": True,
        }

    # Principal axes of the ellipse spanned by the conjugate semi-diameters:
    # eigen-decomposition of M*M^T with M = [u v].
    a = u[0] * u[0] + v[0] * v[0]
    b = u[0] * u[1] + v[0] * v[1]
    c = u[1] * u[1] + v[1] * v[1]
    mean = (a + c) * 0.5
    spread = math.hypot((a - c) * 0.5, b)
    major_squared = mean + spread
    minor_squared = mean - spread
    if major_squared <= 0.0 or minor_squared <= 0.0:
        return None
    major = math.sqrt(major_squared)
    minor = math.sqrt(minor_squared)
    if abs(b) > _EPSILON * major_squared:
        direction = (major_squared - c, b)
    elif a >= c:
        direction = (1.0, 0.0)
    else:
        direction = (0.0, 1.0)
    norm = math.hypot(direction[0], direction[1])
    if norm <= 0.0:
        return None
    axis = (direction[0] / norm, direction[1] / norm)
    normal = (-axis[1], axis[0])
    ratio = min(1.0, minor / major)

    def ellipse_param(t: float) -> float:
        dx = u[0] * math.cos(t) + v[0] * math.sin(t)
        dy = u[1] * math.cos(t) + v[1] * math.sin(t)
        return math.atan2(
            (dx * normal[0] + dy * normal[1]) / minor,
            (dx * axis[0] + dy * axis[1]) / major,
        )

    if is_full:
        start_param, end_param = 0.0, _TAU
    else:
        first = ellipse_param(start if runs_ccw else end) % _TAU
        start_param, end_param = first, first + abs(sweep)
    return {
        **common,
        "kind": "ELLIPSE",
        "center": center,
        "major_axis": [major * axis[0], major * axis[1]],
        "ratio": ratio,
        "start_param": start_param,
        "end_param": end_param,
        "ccw": True,
    }


def _normalize_degrees(value: float) -> float:
    normalized = value % 360.0
    return 0.0 if math.isclose(normalized, 360.0, abs_tol=1e-12) else normalized


def _convert_fill(primitive: Any, context: _ConversionContext) -> dict[str, Any]:
    source_kind = _feature_kind(primitive, context, "fill")
    outer = _ring(_iter_attr(primitive, "outer"), "outer")
    holes = [
        _ring(hole, f"hole {index}")
        for index, hole in enumerate(_iter_attr(primitive, "holes"))
    ]
    return {
        **_primitive_common(primitive, source_kind, context),
        "kind": "HATCH",
        "solid": True,
        "pattern": "SOLID",
        "loops": [
            {"vertices": outer, "is_outer": True},
            *({"vertices": hole, "is_outer": False} for hole in holes),
        ],
    }


def _convert_text(primitive: Any, context: _ConversionContext) -> dict[str, Any]:
    source_kind = _feature_kind(primitive, context, "text")
    height = float(_attr(primitive, "height", 0.0))
    if not math.isfinite(height) or height <= 0.0:
        raise ValueError("text height must be positive and finite")
    text = str(_attr(primitive, "text", ""))
    base_point = int(_attr(primitive, "base_point", 1))
    common = _primitive_common(primitive, source_kind, context)
    style = _style(primitive)
    font_name = _optional_text(_attr(style, "font_name", None))
    if "\n" in text or "\r" in text:
        result: dict[str, Any] = {
            **common,
            "kind": "MTEXT",
            "insert": _point(_attr(primitive, "anchor")),
            "height": height,
            "rotation": float(_attr(primitive, "angle_deg", 0.0)),
            "width": max(0.0, float(_attr(primitive, "width", 0.0))),
            "text": text,
            "attach": _mtext_attachment(base_point),
        }
    else:
        width = max(0.0, float(_attr(primitive, "width", 0.0)))
        estimated_width = max(height * 0.6 * max(1, len(text)), _EPSILON)
        result = {
            **common,
            "kind": "TEXT",
            "insert": _point(_attr(primitive, "anchor")),
            "height": height,
            "rotation": float(_attr(primitive, "angle_deg", 0.0)),
            "text": text,
            "halign": ("left", "center", "right")[(base_point - 1) % 3],
            "valign": ("bottom", "middle", "top")[
                max(0, min(2, (base_point - 1) // 3))
            ],
            "width_factor": max(0.01, width / estimated_width),
        }
    if font_name is not None:
        result["style"] = font_name
    result["metadata"]["sxf"].update(
        {
            "width": float(_attr(primitive, "width", 0.0)),
            "base_point": base_point,
            "direction": int(_attr(primitive, "direction", 1)),
        }
    )
    return result


def _convert_marker(primitive: Any, context: _ConversionContext) -> dict[str, Any]:
    source_kind = _feature_kind(primitive, context, "marker")
    result: dict[str, Any] = {
        **_primitive_common(primitive, source_kind, context),
        "kind": "POINT",
        "position": _point(_attr(primitive, "position")),
        "marker_code": int(_attr(primitive, "marker_code", 0)),
    }
    scale = float(_attr(primitive, "scale", 0.0))
    if scale > 0.0 and math.isfinite(scale):
        result["scale"] = scale
    name = _optional_text(_attr(primitive, "name", None))
    if name is not None:
        result["metadata"]["sxf"]["symbol_name"] = name
    return result


def _convert_dimension(
    feature: Mapping[str, Any],
    paths: Sequence[Any],
    texts: Sequence[Any],
    context: _ConversionContext,
) -> dict[str, Any]:
    source_id = int(feature["id"])
    source_kind = str(feature["kind"])
    style_owner = paths[0] if paths else texts[0] if texts else None
    common = _primitive_common_values(
        source_id,
        source_kind,
        _style(style_owner) if style_owner is not None else None,
        context,
    )
    rendered_paths = [
        {
            "points": [_point(value) for value in _iter_attr(path, "points")],
            "closed": bool(_attr(path, "closed", False)),
        }
        for path in paths
    ]
    rendered_texts = [
        {
            "text": str(_attr(text, "text", "")),
            "anchor": _point(_attr(text, "anchor")),
            "height": float(_attr(text, "height", 0.0)),
            "width": float(_attr(text, "width", 0.0)),
            "angle_deg": float(_attr(text, "angle_deg", 0.0)),
        }
        for text in texts
    ]
    definition: dict[str, Any] = {
        "source_feature": _json_safe(
            {
                key: value
                for key, value in feature.items()
                if key not in {"raw_parameters", "style"}
            }
        ),
        "rendered_paths": rendered_paths,
        "rendered_texts": rendered_texts,
    }
    if rendered_paths and rendered_paths[0]["points"]:
        first_points = rendered_paths[0]["points"]
        definition["points"] = {
            "p1": first_points[0],
            "p2": first_points[-1],
        }
    if rendered_texts:
        definition["text"] = rendered_texts[0]["text"]
        definition["location"] = rendered_texts[0]["anchor"]
    common["metadata"]["sxf"]["rendered_primitive_counts"] = {
        "paths": len(paths),
        "texts": len(texts),
    }
    return {
        **common,
        "kind": "DIMENSION",
        "dim_kind": _DIMENSION_KINDS[source_kind],
        "definition": definition,
    }


def _primitive_common(
    primitive: Any, source_kind: str, context: _ConversionContext
) -> dict[str, Any]:
    return _primitive_common_values(
        _source_id(primitive), source_kind, _style(primitive), context
    )


def _primitive_common_values(
    source_id: int,
    source_kind: str,
    style: Any,
    context: _ConversionContext,
) -> dict[str, Any]:
    layer = str(_attr(style, "layer", "0")) if style is not None else "0"
    color = (
        _color_hex(_attr(style, "color", (0, 0, 0))) if style is not None else "#000000"
    )
    linetype = (
        str(_attr(style, "line_type", "continuous"))
        if style is not None
        else "continuous"
    )
    lineweight = (
        max(0.0, float(_attr(style, "line_width_mm", 0.25)))
        if style is not None
        else 0.25
    )
    visible = bool(_attr(style, "visible", True)) if style is not None else True
    font_name = (
        _optional_text(_attr(style, "font_name", None)) if style is not None else None
    )
    _register_style(
        context,
        layer=layer,
        color=color,
        linetype=linetype,
        lineweight=lineweight,
        visible=visible,
        font_name=font_name,
    )
    feature = context.feature_by_id.get(source_id, {})
    metadata: dict[str, Any] = {
        "source_id": source_id,
        "container": context.container,
    }
    if feature.get("keyword") is not None:
        metadata["keyword"] = str(feature["keyword"])
    partial_name = context.partial_of.get(source_id)
    if partial_name is not None:
        metadata["partial_drawing"] = partial_name
        partial = context.partials.get(partial_name)
        if partial is not None:
            partial.entity_count += 1
    elif source_id in context.ambiguous_ids:
        context.ambiguous_entities += 1
    return {
        "id": context.allocate_id(),
        "layer": layer,
        "linetype": linetype,
        "color": color,
        "lineweight_mm": lineweight,
        "visible": visible,
        "source": {
            "format": "sxf",
            "id": str(source_id),
            "kind": source_kind,
            "metadata": {"container": context.container},
        },
        "metadata": {"sxf": metadata},
    }


def _user_linetype_patterns(
    typed_features: Sequence[Mapping[str, Any]],
) -> dict[str, list[float]]:
    """Dash patterns of SXF user-defined line types, keyed by line type name.

    ``pitch`` alternates drawn and blank lengths in millimetres on the sheet. Only
    SFC exposes these features; P21 line types keep their name-derived pattern.
    """
    patterns: dict[str, list[float]] = {}
    for feature in typed_features:
        if str(feature.get("kind")) != "user_defined_font":
            continue
        name = feature.get("name")
        pitch = feature.get("pitch")
        if not isinstance(name, str) or not name:
            continue
        if not isinstance(pitch, Sequence) or isinstance(pitch, (str, bytes)):
            continue
        count = feature.get("segment_count")
        values = list(pitch)[: int(count)] if isinstance(count, int) else list(pitch)
        try:
            lengths = [abs(float(value)) for value in values]
        except (TypeError, ValueError):
            continue
        if len(lengths) < 2 or not all(math.isfinite(value) for value in lengths):
            continue
        patterns.setdefault(
            name,
            [
                value if index % 2 == 0 else -value
                for index, value in enumerate(lengths)
            ],
        )
    return patterns


def _register_style(
    context: _ConversionContext,
    *,
    layer: str,
    color: str,
    linetype: str,
    lineweight: float,
    visible: bool,
    font_name: str | None,
) -> None:
    layer_def = context.layers.setdefault(
        layer,
        {
            "color": color,
            "linetype": linetype,
            "lineweight_mm": lineweight,
            "plot": visible,
            "metadata": {"sxf": {"style_source": "first rendered primitive"}},
        },
    )
    layer_def["plot"] = bool(layer_def.get("plot", False)) or visible
    if linetype not in context.linetypes:
        definition: dict[str, Any] = {"description": f"SXF line type: {linetype}"}
        pattern = context.user_linetype_patterns.get(linetype)
        if pattern is None:
            pattern = sxf_linetype_pattern(linetype)
        if pattern is not None:
            definition["pattern_mm"] = pattern
        context.linetypes[linetype] = definition
    if font_name is not None:
        context.text_styles.setdefault(font_name, {"font": font_name})


def _append_drawing_warnings(drawing: Any, context: _ConversionContext) -> None:
    for warning in _iter_attr(drawing, "warnings"):
        context.diagnostics.append(
            ImportDiagnostic(
                code="SXF_DRAWING_WARNING",
                severity="warning",
                message=str(warning),
                action="preserved_available_geometry",
            )
        )


def _append_summary_diagnostics(context: _ConversionContext) -> None:
    for source_kind, count in sorted(context.approximation_counts.items()):
        context.diagnostics.append(
            ImportDiagnostic(
                code="SXF_CURVE_APPROXIMATED",
                severity="warning",
                message=f"Approximated {count} SXF {source_kind} paths as polylines.",
                source_kind=source_kind,
                action="approximated",
            )
        )
    for source_kind, count in sorted(context.skipped_counts.items()):
        if count and not any(
            diagnostic.source_kind == source_kind
            and diagnostic.code.endswith("CONVERSION_FAILED")
            for diagnostic in context.diagnostics
        ):
            context.diagnostics.append(
                ImportDiagnostic(
                    code="SXF_PRIMITIVE_SKIPPED",
                    severity="warning",
                    message=f"Skipped {count} SXF {source_kind} primitives.",
                    source_kind=source_kind,
                    action="skipped",
                )
            )
    if context.partials:
        placed = sum(partial.entity_count for partial in context.partials.values())
        context.diagnostics.append(
            ImportDiagnostic(
                code="SXF_PARTIAL_DRAWING_FLATTENED",
                severity="info",
                message=(
                    f"Flattened {len(context.partials)} partial drawings "
                    f"({placed} entities) to sheet coordinates; their placements are "
                    "in header.metadata.sxf.partial_drawings and each entity names "
                    "its partial drawing in metadata.sxf.partial_drawing."
                ),
                action="flattened",
            )
        )
    if context.ambiguous_entities:
        context.diagnostics.append(
            ImportDiagnostic(
                code="SXF_PARTIAL_DRAWING_AMBIGUOUS",
                severity="warning",
                message=(
                    f"{context.ambiguous_entities} entities come from a compound "
                    "figure that is placed in more than one partial drawing; no "
                    "partial drawing was recorded for them."
                ),
                action="flattened",
            )
        )
    if context.container == "p21":
        context.diagnostics.append(
            ImportDiagnostic(
                code="SXF_P21_SEMANTICS_FLATTENED",
                severity="warning",
                message=(
                    "P21 typed feature semantics are not exposed by ezsxf; "
                    "the adapter preserved its backend-neutral drawing primitives."
                ),
                action="flattened",
            )
        )


def _path_is_approximated(source_kind: str, container: str, point_count: int) -> bool:
    if point_count <= 2:
        return False
    if container == "p21":
        return True
    return source_kind in _CURVE_KINDS or source_kind in {
        "curve_dim",
        "angular_dim",
        "balloon",
    }


def _feature_kind(primitive: Any, context: _ConversionContext, fallback: str) -> str:
    source_id = _source_id(primitive)
    feature = context.feature_by_id.get(source_id)
    if feature is not None and feature.get("kind") is not None:
        return str(feature["kind"])
    return f"{context.container}_{fallback}"


def _group_by_source(values: Iterable[Any]) -> dict[int, list[Any]]:
    result: dict[int, list[Any]] = defaultdict(list)
    for value in values:
        result[_source_id(value)].append(value)
    return result


def _ring(values: Iterable[Any], label: str) -> list[list[float]]:
    points = [_point(value) for value in values]
    if len(points) > 2 and _near(points[0], points[-1]):
        points.pop()
    if len(points) < 3:
        raise ValueError(f"{label} ring requires at least three points")
    return points


def _point(value: Any) -> list[float]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise TypeError("point must be a coordinate sequence")
    if len(value) < 2:
        raise ValueError("point must contain x and y")
    point = [float(value[0]), float(value[1])]
    if not all(math.isfinite(item) for item in point):
        raise ValueError("point coordinates must be finite")
    return point


def _source_id(primitive: Any) -> int:
    return int(_attr(primitive, "source_id", 0))


def _style(primitive: Any) -> Any:
    return _attr(primitive, "style", None)


def _attr(value: Any, name: str, default: Any = None) -> Any:
    if isinstance(value, Mapping):
        return value.get(name, default)
    return getattr(value, name, default)


def _iter_attr(value: Any, name: str) -> Iterable[Any]:
    result = _attr(value, name, ())
    if result is None:
        return ()
    if isinstance(result, Iterable) and not isinstance(result, (str, bytes)):
        return result
    raise TypeError(f"{name} must be iterable")


def _sequence(value: Any, label: str) -> Sequence[Any]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise TypeError(f"{label} must be a sequence")
    return value


def _mapping_sequence(value: Any, label: str) -> list[Mapping[str, Any]]:
    result = []
    for index, item in enumerate(_sequence(value, label)):
        if not isinstance(item, Mapping):
            raise TypeError(f"{label}[{index}] must be a mapping")
        result.append(item)
    return result


def _color_hex(value: Any) -> str:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise TypeError("color must be an RGB sequence")
    if len(value) < 3:
        raise ValueError("color must contain red, green, and blue")
    components = [max(0, min(255, int(component))) for component in value[:3]]
    return "#{0:02X}{1:02X}{2:02X}".format(*components)


def _mtext_attachment(base_point: int) -> str:
    horizontal = max(0, min(2, (base_point - 1) % 3))
    vertical = max(0, min(2, (base_point - 1) // 3))
    rows = (
        ("bottom_left", "bottom_center", "bottom_right"),
        ("middle_left", "middle_center", "middle_right"),
        ("top_left", "top_center", "top_right"),
    )
    return rows[vertical][horizontal]


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _drawing_bounds(drawing: Any) -> tuple[float, float, float, float] | None:
    bounds_method = getattr(drawing, "bounds", None)
    if not callable(bounds_method):
        return None
    raw = bounds_method()
    if raw is None or not isinstance(raw, Sequence) or len(raw) != 4:
        return None
    bounds = tuple(float(value) for value in raw)
    if not all(math.isfinite(value) for value in bounds):
        return None
    return bounds  # type: ignore[return-value]


def _source_version(parsed: Mapping[str, Any]) -> str | None:
    header = parsed.get("header")
    if not isinstance(header, Mapping):
        return None
    description = header.get("file_description")
    if not isinstance(description, Mapping):
        return None
    parameters = description.get("parameters")
    if not isinstance(parameters, Sequence) or not parameters:
        return None
    match = re.search(r"level\s*(\d+)", str(parameters[0]), re.IGNORECASE)
    return f"level{match.group(1)}" if match else None


def _detect_container(path: Path) -> str:
    if path.suffix.lower() == ".p21":
        return "p21"
    try:
        with path.open("rb") as stream:
            header = stream.read(4096).decode("latin-1", errors="ignore").lower()
    except OSError:
        raise
    return "p21" if "ap202_mode" in header else "sfc"


def _near(left: Sequence[float], right: Sequence[float]) -> bool:
    return math.isclose(left[0], right[0], abs_tol=1.0e-9) and math.isclose(
        left[1], right[1], abs_tol=1.0e-9
    )


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return [_json_safe(item) for item in value]
    return str(value)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


# ---------------------------------------------------------------------------
# Sheet and partial drawings
#
# ``ezsxf`` flattens every compound figure placement into sheet coordinates. The
# sheet (paper) and the placements of the partial drawings are kept in
# ``header.metadata.sxf`` so that a writer can put the entities back into
# partial drawings at their original scale; each entity names its partial
# drawing in ``metadata.sxf.partial_drawing``.


def _sheet_metadata(
    parsed: Mapping[str, Any], feature_by_id: Mapping[int, Mapping[str, Any]]
) -> dict[str, Any] | None:
    """The drawing sheet: name, standard size (A0-A4) or FREE, orientation, mm."""
    if str(parsed.get("format", "sfc")).lower() == "p21":
        return _p21_sheet_metadata(_p21_records(parsed))
    for feature in feature_by_id.values():
        if str(feature.get("kind")) != "drawing_sheet":
            continue
        return _sheet_dict(
            name=str(feature.get("name") or ""),
            sheet_type=_optional_int(feature.get("sheet_type")),
            landscape=_optional_int(feature.get("orientation")) != 0,
            width=_optional_float(feature.get("free_x_mm")),
            height=_optional_float(feature.get("free_y_mm")),
        )
    return None


def _sheet_dict(
    *,
    name: str,
    sheet_type: int | None,
    landscape: bool,
    width: float | None,
    height: float | None,
) -> dict[str, Any] | None:
    if sheet_type in _STANDARD_SHEETS_MM:
        paper = f"A{sheet_type}"
        long_side, short_side = _STANDARD_SHEETS_MM[sheet_type]
        width, height = (
            (long_side, short_side) if landscape else (short_side, long_side)
        )
    elif width is not None and height is not None and width > 0.0 and height > 0.0:
        paper = "FREE"
        sheet_type = _FREE_SHEET_TYPE
    else:
        return None
    return {
        "name": name,
        "sheet_type": sheet_type,
        "paper": paper,
        "orientation": "landscape" if landscape else "portrait",
        "width_mm": float(width),
        "height_mm": float(height),
    }


def _partial_drawings(
    parsed: Mapping[str, Any], feature_by_id: Mapping[int, Mapping[str, Any]]
) -> tuple[list[_PartialDrawing], dict[int, str], set[int]]:
    """Partial drawings placed on the sheet, the partial drawing of each source
    feature, and the features reachable from more than one partial drawing."""
    if str(parsed.get("format", "sfc")).lower() == "p21":
        placements = _p21_partial_placements(_p21_records(parsed))
    else:
        placements = _sfc_partial_placements(parsed, feature_by_id)
    partials: list[_PartialDrawing] = []
    membership: dict[int, str] = {}
    ambiguous: set[int] = set()
    used_names: set[str] = set()
    for partial, reachable in placements:
        if (
            not all(
                math.isfinite(value)
                for value in (
                    *partial.position,
                    partial.angle_deg,
                    partial.ratio_x,
                    partial.ratio_y,
                )
            )
            or partial.ratio_x <= 0.0
            or partial.ratio_y <= 0.0
        ):
            continue
        partial.name = _unique_partial_name(
            partial.name.strip() or f"#{partial.definition_id}", used_names
        )
        used_names.add(partial.name)
        partials.append(partial)
        for feature_id in reachable:
            if feature_id in ambiguous:
                continue
            owner = membership.get(feature_id)
            if owner is None:
                membership[feature_id] = partial.name
            elif owner != partial.name:
                del membership[feature_id]
                ambiguous.add(feature_id)
    return partials, membership, ambiguous


def _unique_partial_name(candidate: str, used: set[str]) -> str:
    if candidate not in used:
        return candidate
    number = 2
    while f"{candidate}~{number}" in used:
        number += 1
    return f"{candidate}~{number}"


def _sfc_partial_placements(
    parsed: Mapping[str, Any], feature_by_id: Mapping[int, Mapping[str, Any]]
) -> list[tuple[_PartialDrawing, set[int]]]:
    model = parsed.get("model")
    if not isinstance(model, Mapping):
        return []
    sheet = model.get("sheet")
    if not isinstance(sheet, Mapping):
        return []
    definitions: dict[int, Mapping[str, Any]] = {}
    targets: dict[int, int] = {}
    for item in _mapping_items(model.get("sfig_definitions")):
        if item.get("entity_id") is not None:
            definitions.setdefault(int(item["entity_id"]), item)
    for item in _mapping_items(model.get("attribute_attachments")):
        if item.get("definition_id") is None:
            continue
        definition_id = int(item["definition_id"])
        definitions.setdefault(definition_id, item)
        for placement_id in item.get("placement_ids") or []:
            targets[int(placement_id)] = definition_id
    for item in _mapping_items(model.get("sfig_references")):
        if (
            item.get("placement_id") is not None
            and item.get("definition_id") is not None
        ):
            targets[int(item["placement_id"])] = int(item["definition_id"])

    result: list[tuple[_PartialDrawing, set[int]]] = []
    for component_id in sheet.get("component_ids") or []:
        placement_id = int(component_id)
        feature = feature_by_id.get(placement_id)
        if feature is None or str(feature.get("kind")) != "sfig_locate":
            continue
        definition_id = targets.get(placement_id)
        definition = (
            definitions.get(definition_id) if definition_id is not None else None
        )
        if definition is None or definition_id is None:
            continue
        system = _PARTIAL_DRAWING_SYSTEMS.get(
            _optional_int(definition.get("kind_flag")) or 0
        )
        if system is None:
            continue
        position = feature.get("position")
        if not isinstance(position, Mapping):
            position = {}
        try:
            partial = _PartialDrawing(
                name=str(definition.get("name") or feature.get("name") or ""),
                coordinate_system=system,
                position=(float(position.get("x", 0.0)), float(position.get("y", 0.0))),
                angle_deg=float(feature.get("angle_deg", 0.0)),
                ratio_x=float(feature.get("ratio_x", 1.0)),
                ratio_y=float(feature.get("ratio_y", 1.0)),
                definition_id=definition_id,
                placement_id=placement_id,
            )
        except (TypeError, ValueError):
            continue
        reachable: set[int] = set()
        stack = [definition_id]
        seen: set[int] = set()
        while stack:
            current = stack.pop()
            if current in seen:
                continue
            seen.add(current)
            current_definition = definitions.get(current)
            if current_definition is None:
                continue
            for member in current_definition.get("component_ids") or []:
                member_id = int(member)
                reachable.add(member_id)
                nested = targets.get(member_id)
                if nested is not None:
                    stack.append(nested)
        result.append((partial, reachable))
    return result


def _mapping_items(value: Any) -> list[Mapping[str, Any]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return []
    return [item for item in value if isinstance(item, Mapping)]


# -- P21 (AP202): the same structure, read from the STEP records -------------


def _p21_records(parsed: Mapping[str, Any]) -> dict[int, dict[str, Mapping[str, Any]]]:
    """``{entity id: {RECORD KEYWORD: record}}`` for every DATA entity."""
    result: dict[int, dict[str, Mapping[str, Any]]] = {}
    for entity in parsed.get("entities") or []:
        if not isinstance(entity, Mapping) or entity.get("id") is None:
            continue
        records = entity.get("records")
        if not isinstance(records, list):
            record = entity.get("record")
            records = [record] if isinstance(record, Mapping) else []
        result[int(entity["id"])] = {
            str(record.get("keyword", "")).upper(): record
            for record in records
            if isinstance(record, Mapping)
        }
    return result


def _p21_sheet_metadata(
    records: Mapping[int, Mapping[str, Mapping[str, Any]]],
) -> dict[str, Any] | None:
    for sheet_id, entity_records in records.items():
        sheet = entity_records.get("DRAWING_SHEET_REVISION")
        if sheet is None:
            continue
        params = sheet.get("parameters", [])
        name = (
            _decode_step_string(params[0])
            if params and isinstance(params[0], str)
            else ""
        )
        box_ids = [
            item
            for item in (_p21_references(params[1]) if len(params) >= 2 else [])
            if "PLANAR_BOX" in records.get(item, {})
        ]
        for other in records.values():
            size = other.get("PRESENTATION_SIZE")
            if size is None:
                continue
            size_params = size.get("parameters", [])
            if len(size_params) >= 2 and _p21_reference(size_params[0]) == sheet_id:
                box_id = _p21_reference(size_params[1])
                if box_id is not None:
                    box_ids.append(box_id)
        width = height = None
        for box_id in box_ids:
            box = records.get(box_id, {}).get("PLANAR_BOX")
            box_params = box.get("parameters", []) if box is not None else []
            if len(box_params) >= 3:
                width = _optional_float(box_params[1])
                height = _optional_float(box_params[2])
                if width is not None and height is not None:
                    break
        match = _P21_SHEET_NAME_RE.match(name)
        if match:
            sheet_type: int | None = int(match.group(1))
            landscape = match.group(2).lower() == "horizontal"
        elif width is not None and height is not None:
            sheet_type = _standard_sheet_type(width, height)
            landscape = width >= height
        else:
            continue
        # The sheet revision is named after the paper; the drawing title is the
        # DRAUGHTING_TITLE of the drawing revision.
        return _sheet_dict(
            name=_p21_drawing_title(records),
            sheet_type=sheet_type,
            landscape=landscape,
            width=width,
            height=height,
        )
    return None


def _p21_drawing_title(records: Mapping[int, Mapping[str, Mapping[str, Any]]]) -> str:
    for entity_records in records.values():
        title = entity_records.get("DRAUGHTING_TITLE")
        if title is None:
            continue
        params = title.get("parameters", [])
        if len(params) >= 3 and isinstance(params[2], str):
            return _decode_step_string(params[2])
    return ""


def _standard_sheet_type(width: float, height: float) -> int:
    for sheet_type, (long_side, short_side) in _STANDARD_SHEETS_MM.items():
        for first, second in ((long_side, short_side), (short_side, long_side)):
            if (
                abs(width - first) <= _SHEET_SIZE_TOLERANCE_MM
                and abs(height - second) <= _SHEET_SIZE_TOLERANCE_MM
            ):
                return sheet_type
    return _FREE_SHEET_TYPE


def _p21_partial_placements(
    records: Mapping[int, Mapping[str, Mapping[str, Any]]],
) -> list[tuple[_PartialDrawing, set[int]]]:
    sheet_items: list[int] = []
    for entity_records in records.values():
        sheet = entity_records.get("DRAWING_SHEET_REVISION")
        if sheet is not None:
            params = sheet.get("parameters", [])
            if len(params) >= 2:
                sheet_items = list(_p21_references(params[1]))
            break
    result: list[tuple[_PartialDrawing, set[int]]] = []
    for item_id in sheet_items:
        mapped_id = _p21_mapped_item(records, item_id)
        if mapped_id is None:
            continue
        mapped_params = records[mapped_id]["MAPPED_ITEM"].get("parameters", [])
        if len(mapped_params) < 2:
            continue
        map_id = _p21_reference(mapped_params[0])
        target_id = _p21_reference(mapped_params[1])
        map_record = records.get(map_id, {}).get("SYMBOL_REPRESENTATION_MAP")
        if map_id is None or target_id is None or map_record is None:
            continue
        map_params = map_record.get("parameters", [])
        if len(map_params) < 2:
            continue
        source_axis_id = _p21_reference(map_params[0])
        representation_id = _p21_reference(map_params[1])
        representation = records.get(representation_id, {}).get(
            "DRAUGHTING_SUBFIGURE_REPRESENTATION"
        )
        if representation_id is None or representation is None:
            continue
        representation_params = representation.get("parameters", [])
        raw_name = (
            _decode_step_string(representation_params[0])
            if representation_params and isinstance(representation_params[0], str)
            else ""
        )
        system = None
        name = raw_name
        for prefix, value in _P21_PARTIAL_PREFIXES.items():
            if raw_name.startswith(prefix):
                system, name = value, raw_name[len(prefix) :]
                break
        if system is None:
            continue
        source = (
            _p21_axis_transform(records, source_axis_id)
            if source_axis_id is not None
            else _IDENTITY
        )
        inverse = _inverse_affine(source)
        if inverse is None:
            continue
        a, b, c, d, e, f = _compose_affine(
            _p21_symbol_target_transform(records, target_id), inverse
        )
        partial = _PartialDrawing(
            name=name,
            coordinate_system=system,
            position=(e, f),
            angle_deg=math.degrees(math.atan2(b, a)),
            ratio_x=math.hypot(a, b),
            ratio_y=math.hypot(c, d),
            definition_id=representation_id,
            placement_id=item_id,
        )
        reachable: set[int] = set()
        items = (
            list(_p21_references(representation_params[1]))
            if len(representation_params) >= 2
            else []
        )
        _p21_collect_items(records, items, reachable, {representation_id})
        result.append((partial, reachable))
    return result


def _p21_mapped_item(
    records: Mapping[int, Mapping[str, Mapping[str, Any]]], item_id: int
) -> int | None:
    """The entity holding the ``MAPPED_ITEM`` of a sheet item (the item itself or
    the target of its ``STYLED_ITEM``), or ``None``."""
    entity_records = records.get(item_id, {})
    if "MAPPED_ITEM" in entity_records:
        return item_id
    styled = entity_records.get("STYLED_ITEM")
    if styled is not None:
        _, target_id = _p21_styled_item_parts(styled)
        if target_id is not None and "MAPPED_ITEM" in records.get(target_id, {}):
            return target_id
    return None


def _p21_collect_items(
    records: Mapping[int, Mapping[str, Mapping[str, Any]]],
    item_ids: Sequence[int],
    reachable: set[int],
    active: set[int],
) -> None:
    """Collect every entity id rendered for *item_ids*, through styled items,
    callouts and nested subfigure placements (the ids ``ezsxf`` reports as
    ``source_id``)."""
    for item_id in item_ids:
        if item_id in reachable:
            continue
        reachable.add(item_id)
        entity_records = records.get(item_id, {})
        styled = entity_records.get("STYLED_ITEM")
        if styled is not None:
            _, target_id = _p21_styled_item_parts(styled)
            if target_id is not None:
                _p21_collect_items(records, [target_id], reachable, active)
        callout = entity_records.get("DRAUGHTING_CALLOUT")
        if callout is not None:
            params = callout.get("parameters", [])
            if params:
                _p21_collect_items(
                    records, list(_p21_references(params[0])), reachable, active
                )
        mapped = entity_records.get("MAPPED_ITEM")
        if mapped is not None:
            params = mapped.get("parameters", [])
            map_record = (
                records.get(_p21_reference(params[0]), {}).get(
                    "SYMBOL_REPRESENTATION_MAP"
                )
                if params
                else None
            )
            map_params = (
                map_record.get("parameters", []) if map_record is not None else []
            )
            representation_id = (
                _p21_reference(map_params[1]) if len(map_params) >= 2 else None
            )
            if representation_id is not None and representation_id not in active:
                _p21_collect_items(
                    records,
                    [representation_id],
                    reachable,
                    active | {representation_id},
                )
        representation = entity_records.get("DRAUGHTING_SUBFIGURE_REPRESENTATION")
        if representation is not None:
            params = representation.get("parameters", [])
            if len(params) >= 2:
                _p21_collect_items(
                    records, list(_p21_references(params[1])), reachable, active
                )


def _p21_styled_item_parts(
    record: Mapping[str, Any],
) -> tuple[tuple[int, ...], int | None]:
    params = record.get("parameters", [])
    offset = 1 if len(params) >= 3 and isinstance(params[0], str) else 0
    if len(params) < offset + 2:
        return (), None
    return tuple(_p21_references(params[offset])), _p21_reference(params[offset + 1])


def _p21_axis_transform(
    records: Mapping[int, Mapping[str, Mapping[str, Any]]], axis_id: int
) -> _Affine:
    axis = records.get(axis_id, {}).get("AXIS2_PLACEMENT_2D")
    if axis is None:
        return _IDENTITY
    params = axis.get("parameters", [])
    origin = (0.0, 0.0)
    direction = (1.0, 0.0)
    origin_id = _p21_reference(params[1]) if len(params) >= 2 else None
    if origin_id is not None:
        origin = _p21_coordinates(records, origin_id, "CARTESIAN_POINT", origin)
    direction_id = _p21_reference(params[2]) if len(params) >= 3 else None
    if direction_id is not None:
        dx, dy = _p21_coordinates(records, direction_id, "DIRECTION", direction)
        length = math.hypot(dx, dy)
        if length > 1.0e-15:
            direction = (dx / length, dy / length)
    return (
        direction[0],
        direction[1],
        -direction[1],
        direction[0],
        origin[0],
        origin[1],
    )


def _p21_coordinates(
    records: Mapping[int, Mapping[str, Mapping[str, Any]]],
    entity_id: int,
    keyword: str,
    default: tuple[float, float],
) -> tuple[float, float]:
    record = records.get(entity_id, {}).get(keyword)
    if record is None:
        return default
    params = record.get("parameters", [])
    values = params[1] if len(params) >= 2 else None
    if (
        not isinstance(values, Sequence)
        or isinstance(values, (str, bytes))
        or len(values) < 2
    ):
        return default
    x, y = _optional_float(values[0]), _optional_float(values[1])
    if x is None or y is None:
        return default
    return (x, y)


def _p21_symbol_target_transform(
    records: Mapping[int, Mapping[str, Mapping[str, Any]]], target_id: int
) -> _Affine:
    target = records.get(target_id, {}).get("SYMBOL_TARGET")
    if target is None:
        return _IDENTITY
    params = target.get("parameters", [])
    axis_id = _p21_reference(params[1]) if len(params) >= 2 else None
    axis = _p21_axis_transform(records, axis_id) if axis_id is not None else _IDENTITY
    ratio_x = (_optional_float(params[2]) if len(params) >= 3 else None) or 1.0
    ratio_y = (_optional_float(params[3]) if len(params) >= 4 else None) or ratio_x
    return _compose_affine(axis, (ratio_x, 0.0, 0.0, ratio_y, 0.0, 0.0))


def _p21_reference(value: Any) -> int | None:
    if isinstance(value, Mapping) and value.get("kind") == "reference":
        try:
            return int(value["value"])
        except (KeyError, TypeError, ValueError):
            return None
    return None


def _p21_references(value: Any) -> Iterable[int]:
    reference = _p21_reference(value)
    if reference is not None:
        yield reference
    elif isinstance(value, Mapping):
        for child in value.values():
            yield from _p21_references(child)
    elif isinstance(value, (list, tuple)):
        for child in value:
            yield from _p21_references(child)


def _decode_step_string(value: str) -> str:
    """Decode Part 21 ``\\X2\\``/``\\X4\\`` Unicode escapes (names stay raw otherwise)."""

    def replace(match: re.Match[str]) -> str:
        encoding = "utf-16-be" if match.group(1) == "2" else "utf-32-be"
        try:
            return bytes.fromhex(match.group(2)).decode(encoding)
        except (UnicodeDecodeError, ValueError):
            return match.group(0)

    return _STEP_ESCAPE_RE.sub(replace, value)


def _compose_affine(outer: _Affine, inner: _Affine) -> _Affine:
    """``outer`` applied after ``inner`` (x' = a*x + c*y + e, y' = b*x + d*y + f)."""
    a1, b1, c1, d1, e1, f1 = outer
    a2, b2, c2, d2, e2, f2 = inner
    return (
        a1 * a2 + c1 * b2,
        b1 * a2 + d1 * b2,
        a1 * c2 + c1 * d2,
        b1 * c2 + d1 * d2,
        a1 * e2 + c1 * f2 + e1,
        b1 * e2 + d1 * f2 + f1,
    )


def _inverse_affine(transform: _Affine) -> _Affine | None:
    a, b, c, d, e, f = transform
    determinant = a * d - b * c
    if abs(determinant) <= 1.0e-15:
        return None
    ia, ib, ic, id_ = (
        d / determinant,
        -b / determinant,
        -c / determinant,
        a / determinant,
    )
    return (ia, ib, ic, id_, -(ia * e + ic * f), -(ib * e + id_ * f))


def _optional_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value)


def _optional_float(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


__all__ = ["convert_sxf_file_to_ir", "sxf_drawing_to_ir"]
