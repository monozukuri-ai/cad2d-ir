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
    text_styles: dict[str, dict[str, Any]] = field(default_factory=dict)
    preserved_dimensions: int = 0
    next_entity_number: int = 1

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
        from ezsxf import parse_p21, parse_sfc
        from ezsxf._drawing import build_drawing
    except ImportError as exc:
        raise MissingOptionalDependencyError(
            "SXF support requires the optional dependency ezsxf; "
            'install it with `pip install "cad2d-ir[sxf]"`.'
        ) from exc

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
    context = _ConversionContext(
        options=import_options,
        container=container,
        feature_by_id=feature_by_id,
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
    context.linetypes.setdefault(
        linetype,
        {"description": f"SXF line type: {linetype}"},
    )
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


__all__ = ["convert_sxf_file_to_ir", "sxf_drawing_to_ir"]
