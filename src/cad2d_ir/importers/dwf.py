"""Native DWF/DWFx to CAD 2D IR importer backed by :mod:`ezdwf`."""

from __future__ import annotations

from collections import Counter
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
_IR_UNIT_MAP = {
    "mm": "mm",
    "cm": "cm",
    "m": "m",
    "in": "inch",
    "inch": "inch",
    "ft": "ft",
    "unitless": "unitless",
}
_UNIT_SCALE_TO_MM = {
    "mm": 1.0,
    "cm": 10.0,
    "m": 1000.0,
    "in": 25.4,
    "inch": 25.4,
    "ft": 304.8,
    "dip": 25.4 / 96.0,
}
_TRIANGLE_KINDS = {
    "POLYTRIANGLE",
    "GOURAUD_POLYTRIANGLE",
    "TEXTURED_POLYTRIANGLE",
}
_MTEXT_FORMAT_CODE_RE = re.compile(
    r"\\(?:P|~|[LlOoKk]|U\+[0-9A-Fa-f]{4}|[AaCcFfHhQqSsTtWwPp][^;\\{}]*;)"
)


@dataclass(slots=True)
class _ConversionContext:
    options: ImportOptions
    diagnostics: list[ImportDiagnostic] = field(default_factory=list)
    layers: dict[str, dict[str, Any]] = field(default_factory=dict)
    linetypes: dict[str, dict[str, Any]] = field(default_factory=dict)
    text_styles: dict[str, dict[str, Any]] = field(default_factory=dict)
    linetype_names: dict[tuple[Any, ...], str] = field(default_factory=dict)
    skipped_counts: Counter[str] = field(default_factory=Counter)
    skipped_sources: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    approximation_counts: Counter[str] = field(default_factory=Counter)
    appearance_effect_counts: Counter[str] = field(default_factory=Counter)
    next_entity_number: int = 1

    def allocate_id(self) -> str:
        entity_id = f"DWF_E{self.next_entity_number:08d}"
        self.next_entity_number += 1
        return entity_id


def convert_dwf_file_to_ir(
    path: str | Path,
    *,
    options: ImportOptions | None = None,
) -> ImportResult:
    """Read a 2D DWF or DWFx file with ``ezdwf`` and convert it directly to IR."""
    try:
        import ezdwf
    except ImportError as exc:
        raise MissingOptionalDependencyError(
            "DWF support requires the optional dependency ezdwf; "
            'install it with `pip install "cad2d-ir[dwf]"`.'
        ) from exc

    source_path = Path(path)
    try:
        format_info = ezdwf.detect_format(source_path)
        drawing = ezdwf.readfile(source_path)
    except Exception as exc:
        raise ImporterError(f"Failed to read DWF file {source_path}: {exc}") from exc
    return dwf_drawing_to_ir(
        drawing,
        source_name=source_path.name,
        source_sha256=_sha256_file(source_path),
        source_kind=str(getattr(format_info, "kind", "dwf")),
        source_version=getattr(format_info, "version", None),
        parser_version=str(getattr(ezdwf, "__version__", "unknown")),
        options=options,
    )


def dwf_drawing_to_ir(
    drawing: Any,
    *,
    source_name: str | None = None,
    source_sha256: str | None = None,
    source_kind: str | None = None,
    source_version: str | None = None,
    parser_version: str | None = None,
    options: ImportOptions | None = None,
) -> ImportResult:
    """Convert an ``ezdwf.Drawing``-compatible object directly to IR."""
    import_options = options or ImportOptions()
    context = _ConversionContext(options=import_options)
    try:
        sheets = tuple(drawing.sheets)
    except Exception as exc:
        raise ImporterError(f"Failed to enumerate DWF sheets: {exc}") from exc

    units, custom_scale, comparable_units = _resolve_header_units(sheets, context)
    entities: list[dict[str, Any]] = []
    source_entities: list[Any] = []
    for sheet_index, sheet in enumerate(sheets):
        primary = tuple(getattr(sheet, "entities", ()))
        markup = tuple(getattr(sheet, "markup_entities", ()))
        source_entities.extend(primary)
        source_entities.extend(markup)
        for source_entity in (*primary, *markup):
            entities.extend(
                _convert_entity_safe(source_entity, sheet, sheet_index, context)
            )

    if len(sheets) > 1:
        context.diagnostics.append(
            ImportDiagnostic(
                code="DWF_MULTISHEET_FLATTENED",
                severity="warning",
                message=(
                    f"Flattened {len(sheets)} DWF sheets into one IR modelspace; "
                    "sheet identity remains in entity metadata."
                ),
                action="flattened",
                details={"sheet_count": len(sheets)},
            )
        )
    _forward_drawing_diagnostics(drawing, context)
    _append_appearance_effects_diagnostic(context)
    _append_unsupported_diagnostics(context)

    source: dict[str, Any] = {
        "format": "dwf",
        "version": str(source_version or source_kind or "unknown"),
        "metadata": {
            "dwf": {
                "parser_version": parser_version,
                "container": source_kind,
                "sheet_count": len(sheets),
                "sheets": [
                    _sheet_metadata(sheet, index) for index, sheet in enumerate(sheets)
                ],
            }
        },
    }
    if source_name is not None:
        source["name"] = source_name
    if source_sha256 is not None:
        source["sha256"] = source_sha256

    header: dict[str, Any] = {
        "units": units,
        "angle_unit": "deg",
        "coord_space": "world",
        "metadata": {
            "dwf": {
                "coordinate_space": "sheet_paper",
                "sheet_units": [getattr(sheet, "units", None) for sheet in sheets],
                "multisheet_modelspace": len(sheets) > 1,
            }
        },
    }
    if custom_scale is not None:
        header["unit_scale_to_mm"] = custom_scale
    bbox = _drawing_bbox(sheets) if comparable_units else None
    if bbox is not None:
        header["bbox"] = {"min": [bbox[0], bbox[1]], "max": [bbox[2], bbox[3]]}

    document: dict[str, Any] = {
        "format": "cad2d-ir",
        "version": import_options.ir_version,
        "header": header,
        "source": source,
        "tables": {
            "layers": context.layers,
            "linetypes": context.linetypes,
            "text_styles": context.text_styles,
        },
        "entities": entities,
    }
    if import_options.validate:
        validate_ir(document)

    source_kinds = Counter(_kind(entity) for entity in source_entities)
    converted_kinds = Counter(str(entity["kind"]) for entity in entities)
    statistics: dict[str, Any] = {
        "source_format": "dwf",
        "source_container": source_kind,
        "source_version": source_version,
        "source_sheets": len(sheets),
        "source_entities": len(source_entities),
        "source_entity_counts": dict(sorted(source_kinds.items())),
        "source_markup_entities": sum(
            len(getattr(sheet, "markup_entities", ())) for sheet in sheets
        ),
        "converted_entities": len(entities),
        "converted_entity_counts": dict(sorted(converted_kinds.items())),
        "skipped_entities": sum(context.skipped_counts.values()),
        "skipped_entity_counts": dict(sorted(context.skipped_counts.items())),
        "approximated_entities": sum(context.approximation_counts.values()),
    }
    return ImportResult(
        document=document,
        diagnostics=context.diagnostics,
        statistics=statistics,
    )


def _convert_entity_safe(
    source_entity: Any,
    sheet: Any,
    sheet_index: int,
    context: _ConversionContext,
) -> list[dict[str, Any]]:
    source_kind = _kind(source_entity)
    source_id = _source_id(source_entity)
    try:
        result = _convert_entity(source_entity, sheet, sheet_index, context)
    except (AttributeError, KeyError, TypeError, ValueError, OverflowError) as exc:
        if context.options.strict:
            raise ImporterError(
                f"Failed to convert DWF {source_kind} at {source_id}: {exc}"
            ) from exc
        context.skipped_counts[source_kind] += 1
        _record_skipped_source(source_entity, context)
        context.diagnostics.append(
            ImportDiagnostic(
                code="DWF_ENTITY_CONVERSION_FAILED",
                severity="error",
                message=f"Failed to convert DWF {source_kind}: {exc}",
                source_id=source_id,
                source_kind=source_kind,
                action="skipped",
            )
        )
        return []
    if not result:
        context.skipped_counts[source_kind] += 1
        _record_skipped_source(source_entity, context)
    return result


def _convert_entity(
    source_entity: Any,
    sheet: Any,
    sheet_index: int,
    context: _ConversionContext,
) -> list[dict[str, Any]]:
    kind = _kind(source_entity)
    common = _entity_common(source_entity, sheet, sheet_index, context)

    if kind == "LINE":
        points = _points(getattr(source_entity, "points", ()))
        if len(points) != 2:
            raise ValueError("LINE requires exactly two points")
        return [{**common, "kind": "LINE", "p1": points[0], "p2": points[1]}]

    if kind in {"POLYLINE", "POLYGON"}:
        points = _without_repeated_closure(_points(source_entity.points))
        closed = kind == "POLYGON" or bool(getattr(source_entity, "closed", False))
        if len(points) < (3 if closed else 2):
            raise ValueError(f"{kind} has too few points")
        if closed and bool(getattr(source_entity.style, "fill", False)):
            return [
                {
                    **_with_fill_color(common, source_entity),
                    "kind": "HATCH",
                    "solid": True,
                    "loops": [{"vertices": points, "is_outer": True}],
                }
            ]
        return [
            {
                **common,
                "kind": "LWPOLYLINE",
                "vertices": points,
                "closed": closed,
            }
        ]

    if kind in {"CIRCLE", "ARC", "ELLIPSE"}:
        return [_convert_ellipse(source_entity, common, context)]

    if kind == "POLYBEZIER":
        return [_convert_polybezier(source_entity, common, context)]

    if kind == "TEXT":
        return [_convert_text(source_entity, common, context)]

    if kind in _TRIANGLE_KINDS:
        return [_convert_triangles(source_entity, common, context)]

    if kind == "GOURAUD_POLYLINE":
        _diagnose_gradient(source_entity, context)
        points = _colored_points(source_entity)
        if len(points) < 2:
            raise ValueError("GOURAUD_POLYLINE has too few points")
        return [
            {
                **common,
                "kind": "LWPOLYLINE",
                "vertices": points,
                "closed": False,
            }
        ]

    if kind == "CONTOUR_SET":
        contours = [
            _without_repeated_closure(_points(contour))
            for contour in getattr(source_entity, "contours", ())
        ]
        contours = [contour for contour in contours if len(contour) >= 3]
        if not contours:
            raise ValueError("CONTOUR_SET has no valid contours")
        return [
            {
                **_with_fill_color(common, source_entity),
                "kind": "HATCH",
                "solid": True,
                "loops": _hatch_loops(contours),
            }
        ]

    if kind == "PATH":
        return _convert_path(source_entity, common, context)

    return []


def _convert_ellipse(
    source_entity: Any,
    common: dict[str, Any],
    context: _ConversionContext,
) -> dict[str, Any]:
    kind = _kind(source_entity)
    center = _point(source_entity.center)
    x_axis = _point(source_entity.x_axis)
    y_axis = _point(source_entity.y_axis)
    x_length = math.hypot(*x_axis)
    y_length = math.hypot(*y_axis)
    if x_length <= _EPSILON or y_length <= _EPSILON:
        raise ValueError("ellipse axes must be non-zero")
    dot = x_axis[0] * y_axis[0] + x_axis[1] * y_axis[1]
    determinant = x_axis[0] * y_axis[1] - x_axis[1] * y_axis[0]
    orthogonal = abs(dot) <= 1.0e-9 * x_length * y_length
    start_degrees = float(getattr(source_entity, "start_angle_degrees", 0.0) or 0.0)
    end_degrees = float(
        getattr(source_entity, "end_angle_degrees", 360.0)
        if getattr(source_entity, "end_angle_degrees", None) is not None
        else 360.0
    )
    closed = kind == "CIRCLE" or bool(getattr(source_entity, "closed", False))
    if closed:
        start_degrees, end_degrees = 0.0, 360.0
    sweep = end_degrees - start_degrees

    if not orthogonal:
        points = _sample_affine_ellipse(
            center,
            x_axis,
            y_axis,
            start_degrees,
            end_degrees,
            closed,
            context.options.curve_segments,
        )
        context.approximation_counts[kind] += 1
        context.diagnostics.append(
            ImportDiagnostic(
                code="DWF_CURVE_APPROXIMATED",
                severity="warning",
                message=f"DWF {kind} with a sheared axis basis was sampled.",
                source_id=_source_id(source_entity),
                source_kind=kind,
                action="approximated",
                details={"segments": len(points) if closed else len(points) - 1},
            )
        )
        return {
            **common,
            "kind": "LWPOLYLINE",
            "vertices": points,
            "closed": closed,
            "approximation": {
                "method": "polyline",
                "source_kind": f"DWF_{kind}",
                "segments": len(points) if closed else len(points) - 1,
            },
        }

    circular = math.isclose(x_length, y_length, rel_tol=1.0e-9, abs_tol=_EPSILON)
    if kind == "CIRCLE" and circular:
        return {**common, "kind": "CIRCLE", "center": center, "radius": x_length}

    start_point = _ellipse_point(center, x_axis, y_axis, start_degrees)
    end_point = _ellipse_point(center, x_axis, y_axis, end_degrees)
    traversal_ccw = determinant * (sweep or 360.0) >= 0.0
    if kind == "ARC" and circular:
        start_angle = math.degrees(
            math.atan2(start_point[1] - center[1], start_point[0] - center[0])
        )
        end_angle = math.degrees(
            math.atan2(end_point[1] - center[1], end_point[0] - center[0])
        )
        return {
            **common,
            "kind": "ARC",
            "center": center,
            "radius": x_length,
            "start_angle": start_angle,
            "end_angle": end_angle,
            "ccw": traversal_ccw,
        }

    if x_length >= y_length:
        major_axis = x_axis
        major_length, minor_length = x_length, y_length
    else:
        major_axis = y_axis
        major_length, minor_length = y_length, x_length
    if closed:
        start_param, end_param = 0.0, math.tau
    else:
        start_param = _ellipse_parameter(start_point, center, major_axis, minor_length)
        end_param = _ellipse_parameter(end_point, center, major_axis, minor_length)
    return {
        **common,
        "kind": "ELLIPSE",
        "center": center,
        "major_axis": major_axis,
        "ratio": minor_length / major_length,
        "start_param": start_param,
        "end_param": end_param,
        "ccw": traversal_ccw,
    }


def _convert_polybezier(
    source_entity: Any,
    common: dict[str, Any],
    context: _ConversionContext,
) -> dict[str, Any]:
    points = _points(source_entity.points)
    if len(points) >= 4 and (len(points) - 1) % 3 == 0:
        segments = (len(points) - 1) // 3
        knots = [0.0] * 4
        for index in range(1, segments):
            knots.extend([index / segments] * 3)
        knots.extend([1.0] * 4)
        return {
            **common,
            "kind": "SPLINE",
            "degree": 3,
            "control_points": points,
            "knots": knots,
            "closed": bool(getattr(source_entity, "closed", False)),
        }
    if len(points) < 2:
        raise ValueError("POLYBEZIER has too few points")
    context.approximation_counts["POLYBEZIER"] += 1
    context.diagnostics.append(
        ImportDiagnostic(
            code="DWF_CURVE_APPROXIMATED",
            severity="warning",
            message="Malformed DWF POLYBEZIER controls were retained as a polyline.",
            source_id=_source_id(source_entity),
            source_kind="POLYBEZIER",
            action="approximated",
        )
    )
    return {
        **common,
        "kind": "LWPOLYLINE",
        "vertices": points,
        "closed": bool(getattr(source_entity, "closed", False)),
        "approximation": {
            "method": "polyline",
            "source_kind": "DWF_POLYBEZIER",
            "segments": len(points) - 1,
        },
    }


def _convert_text(
    source_entity: Any,
    common: dict[str, Any],
    context: _ConversionContext,
) -> dict[str, Any]:
    points = _points(getattr(source_entity, "points", ()))
    bounds = _points(getattr(source_entity, "bounds", ()) or ())
    if points:
        insert = points[0]
    elif bounds:
        insert = bounds[0]
    else:
        raise ValueError("TEXT has no insertion point")
    style = source_entity.style
    height = float(getattr(style, "font_height", 0.0) or 0.0)
    if height <= _EPSILON and bounds:
        height = max(point[1] for point in bounds) - min(point[1] for point in bounds)
    if not math.isfinite(height) or height <= _EPSILON:
        height = 1.0
    font_name = str(
        getattr(style, "font_canonical_name", None)
        or getattr(style, "font_name", None)
        or "DWF_DEFAULT"
    )
    style_name = _safe_name("DWF_TEXT", font_name)
    context.text_styles.setdefault(style_name, {"font": font_name, "height": height})
    text = str(getattr(source_entity, "text", "") or "")
    has_mtext_formatting = _MTEXT_FORMAT_CODE_RE.search(text) is not None
    if has_mtext_formatting:
        common["metadata"]["dwf"]["mtext_formatting_detected"] = True
    return {
        **common,
        "kind": "MTEXT" if has_mtext_formatting else "TEXT",
        "insert": insert,
        "height": height,
        "rotation": float(getattr(style, "font_rotation_degrees", 0.0) or 0.0),
        "text": text,
        "style": style_name,
    }


def _convert_triangles(
    source_entity: Any,
    common: dict[str, Any],
    context: _ConversionContext,
) -> dict[str, Any]:
    kind = _kind(source_entity)
    points = (
        _colored_points(source_entity)
        if "GOURAUD" in kind
        else _points(source_entity.points)
    )
    if len(points) < 3:
        raise ValueError(f"{kind} has too few points")
    triangles = [points[index : index + 3] for index in range(len(points) - 2)]
    if "GOURAUD" in kind:
        _diagnose_gradient(source_entity, context)
    return {
        **_with_fill_color(common, source_entity),
        "kind": "HATCH",
        "solid": True,
        "loops": [{"vertices": triangle, "is_outer": True} for triangle in triangles],
    }


def _convert_path(
    source_entity: Any,
    common: dict[str, Any],
    context: _ConversionContext,
) -> list[dict[str, Any]]:
    figures = tuple(getattr(source_entity, "path", ()))
    if not figures:
        raise ValueError("PATH has no figures")
    sampled: list[tuple[list[list[float]], bool, bool]] = []
    approximated = False
    for figure in figures:
        points, figure_approximated = _sample_path_figure(
            figure, context.options.curve_segments
        )
        closed = bool(getattr(figure, "closed", False))
        if closed:
            points = _without_repeated_closure(points)
        sampled.append((points, closed, bool(getattr(figure, "filled", False))))
        approximated = approximated or figure_approximated

    result: list[dict[str, Any]] = []
    filled = [
        points
        for points, closed, is_filled in sampled
        if closed and is_filled and len(points) >= 3
    ]
    if filled:
        hatch = {
            **_with_fill_color(common, source_entity),
            "kind": "HATCH",
            "solid": True,
            "loops": _hatch_loops(filled),
        }
        result.append(hatch)
    for points, closed, is_filled in sampled:
        if is_filled and closed:
            continue
        if len(points) < 2:
            continue
        entity_common = common if not result else _clone_common(common, context)
        result.append(
            {
                **entity_common,
                "kind": "LWPOLYLINE",
                "vertices": points,
                "closed": closed,
            }
        )
    if not result:
        raise ValueError("PATH has no representable figures")
    if approximated:
        context.approximation_counts["PATH"] += 1
        context.diagnostics.append(
            ImportDiagnostic(
                code="DWF_CURVE_APPROXIMATED",
                severity="warning",
                message="DWF path curve segments were sampled into polyline boundaries.",
                source_id=_source_id(source_entity),
                source_kind="PATH",
                action="approximated",
            )
        )
        for entity in result:
            if entity["kind"] == "LWPOLYLINE":
                segments = (
                    len(entity["vertices"])
                    if entity["closed"]
                    else len(entity["vertices"]) - 1
                )
                entity["approximation"] = {
                    "method": "polyline",
                    "source_kind": "DWF_PATH",
                    "segments": segments,
                }
    return result


def _entity_common(
    source_entity: Any,
    sheet: Any,
    sheet_index: int,
    context: _ConversionContext,
) -> dict[str, Any]:
    style = source_entity.style
    layer_name = str(
        getattr(style, "layer", None) or getattr(source_entity, "layer", "0") or "0"
    )
    source_unit = _normalized_unit(getattr(sheet, "units", None))
    scale_to_mm = _UNIT_SCALE_TO_MM.get(source_unit)
    linetype = _register_linetype(style, scale_to_mm, context)
    color = _rgba_hex(
        getattr(style, "stroke_color", None) or getattr(style, "color", None)
    )
    layer = context.layers.setdefault(
        layer_name,
        {
            "linetype": linetype,
            "plot": bool(getattr(style, "visible", True)),
            "metadata": {
                "dwf": {
                    "layer_number": getattr(style, "layer_number", None),
                    "source_sheet": sheet_index,
                }
            },
        },
    )
    if color is not None:
        layer.setdefault("color", color)
    lineweight = getattr(style, "nominal_stroke_width", None)
    lineweight_mm = None
    if lineweight is not None and scale_to_mm is not None:
        candidate = float(lineweight) * scale_to_mm
        if math.isfinite(candidate) and candidate >= 0.0:
            lineweight_mm = candidate
            layer.setdefault("lineweight_mm", candidate)

    source = getattr(source_entity, "source", None)
    appearance_effects = {
        "clips": len(getattr(source_entity, "clips", ())),
        "opacity_masks": len(getattr(source_entity, "opacity_masks", ())),
        "compositing_groups": len(getattr(source_entity, "compositing_groups", ())),
    }
    context.appearance_effect_counts.update(
        {name: count for name, count in appearance_effects.items() if count > 0}
    )
    metadata = {
        "sheet_index": sheet_index,
        "sheet_name": str(getattr(sheet, "name", f"Sheet {sheet_index + 1}")),
        "sheet_title": getattr(sheet, "title", None),
        "sheet_units": getattr(sheet, "units", None),
        "section_index": getattr(source_entity, "section_index", None),
        "stream_index": getattr(source_entity, "stream_index", None),
        "entity_index": getattr(source_entity, "entity_index", None),
        "resource_href": getattr(source_entity, "resource_href", None),
        "resource_role": getattr(source_entity, "resource_role", None),
        "is_markup": bool(getattr(source_entity, "is_markup", False)),
        "style": _style_metadata(style),
        "clip_count": appearance_effects["clips"],
        "opacity_mask_count": appearance_effects["opacity_masks"],
        "compositing_group_count": appearance_effects["compositing_groups"],
        "glyph_outline_count": len(getattr(source_entity, "glyph_outline", ()) or ()),
    }
    colored_points = tuple(getattr(source_entity, "colored_points", ()))
    if colored_points:
        metadata["colored_points"] = [
            {
                "point": _point(item.point),
                "color": _json_safe(getattr(item, "color", None)),
            }
            for item in colored_points
        ]
    result: dict[str, Any] = {
        "id": context.allocate_id(),
        "layer": layer_name,
        "linetype": linetype,
        "visible": bool(getattr(style, "visible", True)),
        "source": {
            "format": "dwf",
            "id": _source_id(source_entity),
            "kind": _kind(source_entity),
            "metadata": {
                "resource": getattr(source_entity, "resource_href", None),
                "offset": getattr(source, "offset", None),
                "length": getattr(source, "length", None),
                "opcode": getattr(source, "opcode", None),
            },
        },
        "metadata": {"dwf": metadata},
    }
    if color is not None:
        result["color"] = color
    if lineweight_mm is not None:
        result["lineweight_mm"] = lineweight_mm
    return result


def _register_linetype(
    style: Any,
    scale_to_mm: float | None,
    context: _ConversionContext,
) -> str:
    pattern = getattr(style, "line_pattern", None)
    dash = tuple(float(value) for value in getattr(style, "stroke_dash_array", ()))
    key = (str(pattern) if pattern is not None else None, dash, scale_to_mm)
    existing = context.linetype_names.get(key)
    if existing is not None:
        return existing
    if pattern is None and not dash:
        name = "CONTINUOUS"
    else:
        name = f"DWF_LTYPE_{len(context.linetype_names) + 1:04d}"
    pattern_mm = [value * scale_to_mm for value in dash] if scale_to_mm else []
    context.linetypes.setdefault(
        name,
        {
            "description": f"DWF line pattern {pattern!r}",
            "pattern_mm": pattern_mm,
        },
    )
    context.linetype_names[key] = name
    return name


def _resolve_header_units(
    sheets: Sequence[Any], context: _ConversionContext
) -> tuple[str, float | None, bool]:
    normalized = {_normalized_unit(getattr(sheet, "units", None)) for sheet in sheets}
    normalized.discard("")
    if len(normalized) > 1:
        context.diagnostics.append(
            ImportDiagnostic(
                code="DWF_MIXED_SHEET_UNITS",
                severity="warning",
                message=(
                    "DWF sheets use different paper units; coordinates were preserved "
                    "per sheet and IR header units remain 'unknown'."
                ),
                action="preserved_metadata",
                details={"units": sorted(normalized)},
            )
        )
        return "unknown", None, False
    if not normalized:
        return "unknown", None, True
    unit = next(iter(normalized))
    if unit == "dip":
        return "custom", _UNIT_SCALE_TO_MM["dip"], True
    return _IR_UNIT_MAP.get(unit, "unknown"), None, True


def _forward_drawing_diagnostics(drawing: Any, context: _ConversionContext) -> None:
    for diagnostic in getattr(drawing, "diagnostics", ()):
        severity = str(getattr(diagnostic, "severity", "warning")).lower()
        if severity not in {"info", "warning", "error"}:
            severity = "warning"
        details = dict(getattr(diagnostic, "details", {}) or {})
        details["upstream_code"] = str(getattr(diagnostic, "code", "unknown"))
        details["section"] = getattr(diagnostic, "section", None)
        details["resource"] = getattr(diagnostic, "resource", None)
        details["offset"] = getattr(diagnostic, "offset", None)
        context.diagnostics.append(
            ImportDiagnostic(
                code="DWF_DRAWING_WARNING",
                severity=severity,  # type: ignore[arg-type]
                message=str(getattr(diagnostic, "message", "DWF parser diagnostic")),
                source_id=(
                    str(getattr(diagnostic, "offset"))
                    if getattr(diagnostic, "offset", None) is not None
                    else None
                ),
                action=str(getattr(diagnostic, "action", "reported")),
                details=details,
            )
        )


def _append_unsupported_diagnostics(context: _ConversionContext) -> None:
    for kind, count in sorted(context.skipped_counts.items()):
        sources = context.skipped_sources.get(kind, [])
        details: dict[str, Any] = {"count": count, "sources": sources}
        if len(sources) < count:
            details["sources_truncated"] = count - len(sources)
        context.diagnostics.append(
            ImportDiagnostic(
                code="DWF_UNSUPPORTED_ENTITY",
                severity="warning",
                message=f"Skipped {count} unsupported DWF {kind} entity/entities.",
                source_kind=kind,
                action="skipped",
                details=details,
            )
        )


def _append_appearance_effects_diagnostic(context: _ConversionContext) -> None:
    if not context.appearance_effect_counts:
        return
    details = dict(sorted(context.appearance_effect_counts.items()))
    context.diagnostics.append(
        ImportDiagnostic(
            code="DWF_APPEARANCE_EFFECTS_FLATTENED",
            severity="warning",
            message=(
                "DWF clipping, opacity-mask, or compositing effects were retained "
                "as metadata counts but not applied to IR geometry."
            ),
            action="flattened",
            details=details,
        )
    )


def _record_skipped_source(source_entity: Any, context: _ConversionContext) -> None:
    kind = _kind(source_entity)
    samples = context.skipped_sources.setdefault(kind, [])
    if len(samples) >= 20:
        return
    details: dict[str, Any] = {
        "source_id": _source_id(source_entity),
        "resource_href": getattr(source_entity, "resource_href", None),
        "resource_role": getattr(source_entity, "resource_role", None),
        "section_index": getattr(source_entity, "section_index", None),
        "stream_index": getattr(source_entity, "stream_index", None),
        "entity_index": getattr(source_entity, "entity_index", None),
    }
    image_metadata = _image_metadata(getattr(source_entity, "image", None))
    if image_metadata is not None:
        details["image"] = image_metadata
    samples.append(details)


def _image_metadata(image: Any) -> dict[str, Any] | None:
    if image is None:
        return None
    result: dict[str, Any] = {
        "format": getattr(image, "format", None),
        "identifier": getattr(image, "identifier", None),
        "columns": getattr(image, "columns", None),
        "rows": getattr(image, "rows", None),
        "color_map_size": len(getattr(image, "color_map", ())),
    }
    for name in ("min", "max"):
        value = getattr(image, name, None)
        if value is not None:
            result[name] = _point(value)
    data = getattr(image, "data", None)
    if data is not None:
        payload = bytes(data)
        result["data_size"] = len(payload)
        result["sha256"] = hashlib.sha256(payload).hexdigest()
    return result


def _diagnose_gradient(source_entity: Any, context: _ConversionContext) -> None:
    context.diagnostics.append(
        ImportDiagnostic(
            code="DWF_COLOR_GRADIENT_FLATTENED",
            severity="warning",
            message="DWF per-vertex colors were preserved in metadata but not IR style fields.",
            source_id=_source_id(source_entity),
            source_kind=_kind(source_entity),
            action="flattened",
        )
    )


def _sample_path_figure(
    figure: Any, curve_segments: int
) -> tuple[list[list[float]], bool]:
    current = _point(figure.start)
    points = [current]
    approximated = False
    for segment in getattr(figure, "segments", ()):
        kind = str(getattr(segment, "kind", "line")).lower()
        end = _point(segment.end)
        if kind == "line":
            points.append(end)
        elif kind == "cubic_bezier":
            control1 = _point(segment.control1)
            control2 = _point(segment.control2)
            for index in range(1, curve_segments + 1):
                points.append(
                    _cubic_bezier(
                        current, control1, control2, end, index / curve_segments
                    )
                )
            approximated = True
        elif kind == "quadratic_bezier":
            control = _point(segment.control)
            for index in range(1, curve_segments + 1):
                points.append(
                    _quadratic_bezier(current, control, end, index / curve_segments)
                )
            approximated = True
        elif kind == "elliptical_arc":
            center = _point(segment.center)
            x_axis = _point(segment.x_axis)
            y_axis = _point(segment.y_axis)
            start = float(segment.start_angle_degrees)
            sweep = float(segment.sweep_angle_degrees)
            count = max(1, math.ceil(abs(sweep) / 360.0 * curve_segments))
            for index in range(1, count + 1):
                points.append(
                    _ellipse_point(
                        center, x_axis, y_axis, start + sweep * index / count
                    )
                )
            approximated = True
        else:
            raise ValueError(f"unsupported path segment kind {kind!r}")
        current = end
    return points, approximated


def _sample_affine_ellipse(
    center: list[float],
    x_axis: list[float],
    y_axis: list[float],
    start_degrees: float,
    end_degrees: float,
    closed: bool,
    curve_segments: int,
) -> list[list[float]]:
    sweep = 360.0 if closed else end_degrees - start_degrees
    count = max(3 if closed else 1, math.ceil(abs(sweep) / 360.0 * curve_segments))
    limit = count if closed else count + 1
    return [
        _ellipse_point(
            center,
            x_axis,
            y_axis,
            start_degrees + sweep * index / count,
        )
        for index in range(limit)
    ]


def _ellipse_point(
    center: list[float], x_axis: list[float], y_axis: list[float], degrees: float
) -> list[float]:
    angle = math.radians(degrees)
    return [
        center[0] + x_axis[0] * math.cos(angle) + y_axis[0] * math.sin(angle),
        center[1] + x_axis[1] * math.cos(angle) + y_axis[1] * math.sin(angle),
    ]


def _ellipse_parameter(
    point: list[float],
    center: list[float],
    major_axis: list[float],
    minor_length: float,
) -> float:
    major_length = math.hypot(*major_axis)
    major_unit = [major_axis[0] / major_length, major_axis[1] / major_length]
    minor_unit = [-major_unit[1], major_unit[0]]
    delta = [point[0] - center[0], point[1] - center[1]]
    major_value = (delta[0] * major_unit[0] + delta[1] * major_unit[1]) / major_length
    minor_value = (delta[0] * minor_unit[0] + delta[1] * minor_unit[1]) / minor_length
    return math.atan2(minor_value, major_value)


def _cubic_bezier(
    start: list[float],
    control1: list[float],
    control2: list[float],
    end: list[float],
    value: float,
) -> list[float]:
    inverse = 1.0 - value
    return [
        inverse**3 * start[axis]
        + 3.0 * inverse**2 * value * control1[axis]
        + 3.0 * inverse * value**2 * control2[axis]
        + value**3 * end[axis]
        for axis in (0, 1)
    ]


def _quadratic_bezier(
    start: list[float], control: list[float], end: list[float], value: float
) -> list[float]:
    inverse = 1.0 - value
    return [
        inverse**2 * start[axis]
        + 2.0 * inverse * value * control[axis]
        + value**2 * end[axis]
        for axis in (0, 1)
    ]


def _hatch_loops(contours: Sequence[list[list[float]]]) -> list[dict[str, Any]]:
    reference = max(contours, key=lambda contour: abs(_signed_area(contour)))
    reference_sign = _signed_area(reference) >= 0.0
    return [
        {
            "vertices": contour,
            "is_outer": (_signed_area(contour) >= 0.0) == reference_sign,
        }
        for contour in contours
    ]


def _signed_area(points: Sequence[list[float]]) -> float:
    return 0.5 * sum(
        first[0] * second[1] - second[0] * first[1]
        for first, second in zip(points, (*points[1:], points[0]))
    )


def _clone_common(
    common: dict[str, Any], context: _ConversionContext
) -> dict[str, Any]:
    return {**common, "id": context.allocate_id()}


def _with_fill_color(common: dict[str, Any], source_entity: Any) -> dict[str, Any]:
    result = dict(common)
    style = source_entity.style
    color = _rgba_hex(
        getattr(style, "fill_color", None) or getattr(style, "color", None)
    )
    if color is not None:
        result["color"] = color
    return result


def _style_metadata(style: Any) -> dict[str, Any]:
    snapshot = getattr(style, "snapshot", None)
    if callable(snapshot):
        return _json_safe(snapshot())
    return {
        "layer": getattr(style, "layer", None),
        "layer_number": getattr(style, "layer_number", None),
        "color": _json_safe(getattr(style, "color", None)),
        "color_index": getattr(style, "color_index", None),
        "line_pattern": getattr(style, "line_pattern", None),
        "line_weight_logical": getattr(style, "line_weight_logical", None),
        "nominal_stroke_width": getattr(style, "nominal_stroke_width", None),
        "fill": bool(getattr(style, "fill", False)),
        "font_name": getattr(style, "font_name", None),
        "font_height": getattr(style, "font_height", None),
        "visible": bool(getattr(style, "visible", True)),
        "opacity": getattr(style, "opacity", 1.0),
    }


def _sheet_metadata(sheet: Any, index: int) -> dict[str, Any]:
    return {
        "index": index,
        "section_index": getattr(sheet, "section_index", None),
        "name": getattr(sheet, "name", None),
        "title": getattr(sheet, "title", None),
        "plot_order": getattr(sheet, "plot_order", None),
        "units": getattr(sheet, "units", None),
        "paper_bounds": _json_safe(getattr(sheet, "paper_bounds", None)),
        "content_bounds": _json_safe(getattr(sheet, "content_bounds", None)),
        "entity_count": len(getattr(sheet, "entities", ())),
        "markup_entity_count": len(getattr(sheet, "markup_entities", ())),
    }


def _drawing_bbox(sheets: Sequence[Any]) -> tuple[float, float, float, float] | None:
    bounds = [
        tuple(float(value) for value in sheet.content_bounds)
        for sheet in sheets
        if getattr(sheet, "content_bounds", None) is not None
    ]
    if not bounds:
        return None
    return (
        min(bound[0] for bound in bounds),
        min(bound[1] for bound in bounds),
        max(bound[2] for bound in bounds),
        max(bound[3] for bound in bounds),
    )


def _colored_points(source_entity: Any) -> list[list[float]]:
    return [
        _point(value.point) for value in getattr(source_entity, "colored_points", ())
    ]


def _points(values: Iterable[Any]) -> list[list[float]]:
    return [_point(value) for value in values]


def _point(value: Any) -> list[float]:
    if value is None:
        raise ValueError("point is missing")
    if hasattr(value, "x") and hasattr(value, "y"):
        x, y = float(value.x), float(value.y)
    else:
        sequence = value
        if len(sequence) < 2:
            raise ValueError("point requires two coordinates")
        x, y = float(sequence[0]), float(sequence[1])
    if not math.isfinite(x) or not math.isfinite(y):
        raise ValueError("point coordinates must be finite")
    return [x, y]


def _without_repeated_closure(points: list[list[float]]) -> list[list[float]]:
    if len(points) > 1 and points[0] == points[-1]:
        return points[:-1]
    return points


def _kind(source_entity: Any) -> str:
    value = getattr(source_entity, "kind", None)
    if value is None:
        dxftype = getattr(source_entity, "dxftype", None)
        value = dxftype() if callable(dxftype) else type(source_entity).__name__
    return str(value).strip().upper().replace("-", "_").replace(" ", "_")


def _source_id(source_entity: Any) -> str:
    section = getattr(source_entity, "section_index", 0)
    stream = getattr(source_entity, "stream_index", 0)
    entity = getattr(source_entity, "entity_index", 0)
    return f"{section}:{stream}:{entity}"


def _normalized_unit(value: Any) -> str:
    return str(value or "").strip().casefold()


def _rgba_hex(value: Any) -> str | None:
    if value is None:
        return None
    values = [max(0, min(255, int(component))) for component in value]
    if len(values) < 3:
        return None
    if len(values) >= 4 and values[3] < 255:
        return f"#{values[0]:02X}{values[1]:02X}{values[2]:02X}{values[3]:02X}"
    return f"#{values[0]:02X}{values[1]:02X}{values[2]:02X}"


def _safe_name(prefix: str, value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.:\-]", "_", value).strip("_") or "DEFAULT"
    return f"{prefix}_{cleaned}"


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, bytes):
        return {"size": len(value), "sha256": hashlib.sha256(value).hexdigest()}
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]
    return str(value)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
