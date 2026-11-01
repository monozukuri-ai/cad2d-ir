"""Native DWF/DWFx to CAD 2D IR importer backed by :mod:`ezdwf`."""

from __future__ import annotations

import base64
import hashlib
import math
import re
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from itertools import pairwise
from pathlib import Path
from typing import Any

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
    # 内容が同じStyleメタデータ辞書を共有する。DWFのStyleは要素ごとに別
    # オブジェクトだが値の種類は少なく、10万要素級では複製が数百MBになる
    style_metadata_cache: dict[tuple[tuple[str, Any], ...], dict[str, Any]] = field(
        default_factory=dict
    )
    next_entity_number: int = 1
    embedded_images: int = 0
    # Millimetres per unit of the sheet being converted (``None`` when unknown);
    # it sets how finely curves are sampled.
    scale_to_mm: float | None = None
    # The constant part of every entity's ``metadata.dwf``, one dictionary per
    # (sheet, stream, style); see ``_entity_common``.
    entity_metadata_cache: dict[tuple[Any, ...], dict[str, Any]] = field(
        default_factory=dict
    )
    # ``approximation`` dictionaries, one per (source kind, segment count); a
    # drawing of a million short polylines would otherwise carry a million copies.
    approximation_cache: dict[tuple[str, int], dict[str, Any]] = field(
        default_factory=dict
    )
    # The ``{"dwf": ...}`` wrapper around each shared metadata dictionary, and
    # one copy of every layer name / colour string (both are built per entity).
    metadata_wrapper_cache: dict[int, dict[str, Any]] = field(default_factory=dict)
    strings: dict[str, str] = field(default_factory=dict)
    # One float object per distinct ``lineweight_mm``: a plot set of 1.7 million
    # entities carried 1.7 million copies of a handful of values (40 MB).
    floats: dict[float, float] = field(default_factory=dict)

    def shared_entity_metadata(
        self,
        sheet_index: int,
        section_index: Any,
        stream_index: Any,
        resource_href: Any,
        resource_role: Any,
        is_markup: bool,
        style: dict[str, Any],
        counts: tuple[tuple[str, int], ...] = (),
    ) -> dict[str, Any]:
        key = (
            sheet_index,
            section_index,
            stream_index,
            resource_href,
            resource_role,
            is_markup,
            id(style),
            counts,
        )
        cached = self.entity_metadata_cache.get(key)
        if cached is None:
            cached = {
                "sheet_index": sheet_index,
                "section_index": section_index,
                "stream_index": stream_index,
                "resource_href": resource_href,
                "resource_role": resource_role,
                "is_markup": is_markup,
                "style": style,
                **dict(counts),
            }
            self.entity_metadata_cache[key] = cached
        return cached

    def shared_metadata_wrapper(self, metadata: dict[str, Any]) -> dict[str, Any]:
        """``{"dwf": metadata}`` for a shared ``metadata`` (kept alive by its cache)."""
        key = id(metadata)
        cached = self.metadata_wrapper_cache.get(key)
        if cached is None:
            cached = self.metadata_wrapper_cache[key] = {"dwf": metadata}
        return cached

    def intern(self, value: str) -> str:
        return self.strings.setdefault(value, value)

    def intern_float(self, value: float) -> float:
        return self.floats.setdefault(value, value)

    def shared_approximation(self, source_kind: str, segments: int) -> dict[str, Any]:
        key = (source_kind, segments)
        cached = self.approximation_cache.get(key)
        if cached is None:
            cached = {
                "method": "polyline",
                "source_kind": source_kind,
                "segments": segments,
            }
            self.approximation_cache[key] = cached
        return cached

    def shared_style_metadata(self, style: Any) -> dict[str, Any]:
        value = _style_metadata(style)
        try:
            key = tuple(sorted((k, _hashable(v)) for k, v in value.items()))
        except TypeError:
            return value
        cached = self.style_metadata_cache.get(key)
        if cached is not None:
            return cached
        self.style_metadata_cache[key] = value
        return value

    def allocate_id(self) -> str:
        entity_id = f"DWF_E{self.next_entity_number:08d}"
        self.next_entity_number += 1
        return entity_id


# Gap between tiled sheets as a share of the cell width (the same as IDW sheets).
DEFAULT_SHEET_GAP_RATIO = 0.1


@dataclass(frozen=True, slots=True)
class _SheetFrame:
    """Where one sheet sits when several sheets share the IR modelspace."""

    offset: tuple[float, float]
    cell: tuple[int, int] | None
    bounds: tuple[float, float, float, float] | None


@dataclass(frozen=True, slots=True)
class _SheetLayout:
    frames: list[_SheetFrame]
    columns: int
    rows: int
    cell_size: tuple[float, float]
    gap: float


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
        open_drawing = getattr(ezdwf, "open_drawing", None)
        if open_drawing is not None:
            # ezdwf 0.0.13: the drawing stays in the Rust core and each sheet's
            # entities are materialized a chunk at a time while they are converted.
            # A 4 MB DWFx plot set with 4.7 million points took 2.5 GB as objects.
            # ``consume`` releases every converted chunk from the core, so the
            # drawing shrinks as the IR grows instead of both peaking together.
            try:
                drawing = open_drawing(source_path, consume=True)
            except TypeError:  # a development build without ``consume``
                drawing = open_drawing(source_path)
        else:
            # The raw W2D records are a second copy of every entity; IR never reads them.
            drawing = ezdwf.readfile(source_path, keep_raw=False)
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
    # Sheets of one DWF share paper coordinates, so a drawing set would land on
    # one A3. With comparable units they are laid out on a grid instead.
    layout = _sheet_layout(sheets) if comparable_units else None
    entities: list[dict[str, Any]] = []
    source_kinds: Counter[str] = Counter()
    # (entities, markup entities) of each sheet as read; a streaming reader's
    # sheets carry none, so they are counted while being converted.
    sheet_counts: list[tuple[int, int]] = []
    for sheet_index, sheet in enumerate(sheets):
        first = len(entities)
        counts = [0, 0]
        context.scale_to_mm = _UNIT_SCALE_TO_MM.get(
            _normalized_unit(getattr(sheet, "units", None))
        )
        for group, source_entities in enumerate(
            _sheet_entities(drawing, sheet, sheet_index)
        ):
            for source_entity in source_entities:
                counts[group] += 1
                source_kinds[_kind(source_entity)] += 1
                entities.extend(
                    _convert_entity_safe(source_entity, sheet, sheet_index, context)
                )
        sheet_counts.append((counts[0], counts[1]))
        if layout is not None and layout.frames[sheet_index].offset != (0.0, 0.0):
            _translate_entities(entities[first:], layout.frames[sheet_index].offset)

    if layout is not None:
        context.diagnostics.append(
            ImportDiagnostic(
                code="DWF_MULTISHEET_TILED",
                severity="warning",
                message=(
                    f"Placed {len(sheets)} DWF sheets on a grid of {layout.columns} "
                    "column(s) in one IR modelspace; sheet offsets remain in source "
                    "metadata."
                ),
                action="flattened",
                details={
                    "sheet_count": len(sheets),
                    "columns": layout.columns,
                    "rows": layout.rows,
                    "cell": list(layout.cell_size),
                    "gap": layout.gap,
                    "offsets": [list(frame.offset) for frame in layout.frames],
                },
            )
        )
    elif len(sheets) > 1:
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
    if context.embedded_images:
        context.diagnostics.append(
            ImportDiagnostic(
                code="DWF_IMAGE_EMBEDDED",
                severity="info",
                message=(
                    f"Embedded {context.embedded_images} DWF raster image(s) as IMAGE "
                    "entities with PNG or JPEG bytes."
                ),
                source_kind="IMAGE",
                action="converted",
                details={"count": context.embedded_images},
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
                    _sheet_metadata(
                        sheet,
                        index,
                        layout.frames[index] if layout else None,
                        sheet_counts[index],
                    )
                    for index, sheet in enumerate(sheets)
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
                "tiled_sheets": layout is not None,
            }
        },
    }
    if layout is not None:
        header["metadata"]["dwf"]["sheet_grid"] = {
            "columns": layout.columns,
            "rows": layout.rows,
            "cell": list(layout.cell_size),
            "gap": layout.gap,
        }
    if custom_scale is not None:
        header["unit_scale_to_mm"] = custom_scale
    bbox = _drawing_bbox(sheets, layout) if comparable_units else None
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

    converted_kinds = Counter(str(entity["kind"]) for entity in entities)
    statistics: dict[str, Any] = {
        "source_format": "dwf",
        "source_container": source_kind,
        "source_version": source_version,
        "source_sheets": len(sheets),
        "source_entities": sum(source_kinds.values()),
        "source_entity_counts": dict(sorted(source_kinds.items())),
        "source_markup_entities": sum(markup for _, markup in sheet_counts),
        "converted_entities": len(entities),
        "converted_entity_counts": dict(sorted(converted_kinds.items())),
        "skipped_entities": sum(context.skipped_counts.values()),
        "skipped_entity_counts": dict(sorted(context.skipped_counts.items())),
        "approximated_entities": sum(context.approximation_counts.values()),
        "embedded_images": context.embedded_images,
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

    if kind == "POLYMARKER":
        points = _points(getattr(source_entity, "points", ()))
        if not points:
            raise ValueError("POLYMARKER has no points")
        first, *rest = points
        return [
            {**common, "kind": "POINT", "position": first},
            *[
                {**_clone_common(common, context), "kind": "POINT", "position": point}
                for point in rest
            ],
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

    if kind == "IMAGE":
        return _convert_image(source_entity, common, context)

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
    if x_length <= _EPSILON and y_length <= _EPSILON:
        # A zero-radius circle/arc is how some writers emit a dot marker.
        return {**common, "kind": "POINT", "position": center}
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
    # W2D angles run counterclockwise from start to end, so 315 to 45 is a
    # 90 degree arc through zero, not 270 degrees the other way.
    sweep = (end_degrees - start_degrees) % 360.0 or 360.0
    if x_length <= _EPSILON or y_length <= _EPSILON:
        return _flat_ellipse_line(center, x_axis, y_axis, start_degrees, sweep, common)

    if not orthogonal:
        points = _sample_affine_ellipse(
            center,
            x_axis,
            y_axis,
            start_degrees,
            start_degrees + sweep,
            closed,
            context.options.curve_segments,
            context.scale_to_mm,
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
            "approximation": context.shared_approximation(
                f"DWF_{kind}", len(points) if closed else len(points) - 1
            ),
        }

    circular = math.isclose(x_length, y_length, rel_tol=1.0e-9, abs_tol=_EPSILON)
    if kind == "CIRCLE" and circular:
        return {**common, "kind": "CIRCLE", "center": center, "radius": x_length}

    start_point = _ellipse_point(center, x_axis, y_axis, start_degrees)
    end_point = _ellipse_point(center, x_axis, y_axis, end_degrees)
    traversal_ccw = determinant >= 0.0
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


def _flat_ellipse_line(
    center: list[float],
    x_axis: list[float],
    y_axis: list[float],
    start_degrees: float,
    sweep: float,
    common: dict[str, Any],
) -> dict[str, Any]:
    """An ellipse with one zero axis is the straight line its arc sweeps along the other."""
    uses_x = math.hypot(*x_axis) > _EPSILON
    axis = x_axis if uses_x else y_axis
    phase = 0.0 if uses_x else 90.0
    # cos(angle - phase) along the axis: its extremes are at the ends of the
    # arc and at any multiple of 180 degrees from the phase inside the arc.
    first = start_degrees - phase
    values = [math.cos(math.radians(first)), math.cos(math.radians(first + sweep))]
    turn = math.ceil(first / 180.0)
    while turn * 180.0 <= first + sweep:
        values.append(1.0 if turn % 2 == 0 else -1.0)
        turn += 1
    low, high = min(values), max(values)
    return {
        **common,
        "kind": "LINE",
        "p1": [center[0] + axis[0] * low, center[1] + axis[1] * low],
        "p2": [center[0] + axis[0] * high, center[1] + axis[1] * high],
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
        "approximation": context.shared_approximation(
            "DWF_POLYBEZIER", len(points) - 1
        ),
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
        # The entity's metadata may be the shared dictionary: replace, never mutate.
        common["metadata"] = {
            "dwf": {**common["metadata"]["dwf"], "mtext_formatting_detected": True}
        }
    result = {
        **common,
        "kind": "MTEXT" if has_mtext_formatting else "TEXT",
        "insert": insert,
        "height": height,
        "rotation": float(getattr(style, "font_rotation_degrees", 0.0) or 0.0),
        "text": text,
        "style": style_name,
    }
    if has_mtext_formatting:
        # The DWF text position is the start of the baseline; an MTEXT without
        # attach would hang below its insert point (top_left).
        result["attach"] = "bottom_left"
    return result


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
    quad = _strip_as_convex_quad(points)
    loops = (
        [quad]
        if quad is not None
        else [points[index : index + 3] for index in range(len(points) - 2)]
    )
    if "GOURAUD" in kind:
        _diagnose_gradient(source_entity, context)
    return {
        **_with_fill_color(common, source_entity),
        "kind": "HATCH",
        "solid": True,
        "loops": [{"vertices": loop, "is_outer": True} for loop in loops],
    }


def _strip_as_convex_quad(points: list[list[float]]) -> list[list[float]] | None:
    """The polygon of a strip of two triangles that form a convex quad, else ``None``.

    Most strips are such quads. One polygon renders without a seam along the
    shared edge and takes half the memory, and its diagonal is the shared edge,
    so it covers exactly the two triangles.
    """
    if len(points) != 4:
        return None
    quad = [points[0], points[2], points[3], points[1]]
    turns = [
        (quad[index - 1][0] - quad[index - 2][0])
        * (quad[index][1] - quad[index - 1][1])
        - (quad[index - 1][1] - quad[index - 2][1])
        * (quad[index][0] - quad[index - 1][0])
        for index in range(4)
    ]
    if all(turn > 0.0 for turn in turns) or all(turn < 0.0 for turn in turns):
        return quad
    return None


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
            figure, context.options.curve_segments, context.scale_to_mm
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
                entity["approximation"] = context.shared_approximation(
                    "DWF_PATH", segments
                )
    return result


def _convert_image(
    source_entity: Any,
    common: dict[str, Any],
    context: _ConversionContext,
) -> list[dict[str, Any]]:
    """A raster ``Image`` record becomes an ``IMAGE`` with PNG or JPEG bytes.

    PNG and JPEG data is embedded as it is. Every other W2D encoding (raw rows,
    Group 4 bare or in a TIFF) is converted to PNG by ezdwf 0.0.12 and later
    (``Image.to_png``); with an older ezdwf such a record stays unsupported.
    The image is upright, its first row along the top of its box.
    """
    image = getattr(source_entity, "image", None)
    if image is None:
        raise ValueError("IMAGE has no image record")
    low = _point(getattr(image, "min", None))
    high = _point(getattr(image, "max", None))
    x0, x1 = sorted((low[0], high[0]))
    y0, y1 = sorted((low[1], high[1]))
    if x1 - x0 <= _EPSILON or y1 - y0 <= _EPSILON:
        raise ValueError("IMAGE has no area")
    data = bytes(getattr(image, "data", b"") or b"")
    if not data:
        raise ValueError("IMAGE has no pixel data")
    image_format = str(getattr(image, "format", "") or "").lower()
    color_map = tuple(getattr(image, "color_map", ()) or ())
    identifier = getattr(image, "identifier", None)
    mime = _sniff_mime(data)
    if mime not in ("image/png", "image/jpeg"):
        to_png = getattr(image, "to_png", None)
        if to_png is None:
            return []
        try:
            data = bytes(to_png())
        except Exception as exc:  # noqa: BLE001 - ezdwf raises its own DwfError family
            context.diagnostics.append(
                ImportDiagnostic(
                    code="DWF_IMAGE_DECODE_FAILED",
                    severity="warning",
                    message=f"DWF {image_format or 'raster'} image was not decoded: {exc}",
                    source_id=_source_id(source_entity),
                    source_kind="IMAGE",
                    action="skipped",
                    details={
                        "format": image_format,
                        "identifier": identifier,
                        "columns": getattr(image, "columns", None),
                        "rows": getattr(image, "rows", None),
                    },
                )
            )
            return []
        mime = "image/png"
    image_metadata = {
        "format": image_format,
        "identifier": identifier,
        "columns": getattr(image, "columns", None),
        "rows": getattr(image, "rows", None),
        "color_map_size": len(color_map),
        "transparent": any(
            len(color) > 3 and int(color[3]) < 255 for color in color_map
        ),
    }
    # The entity's metadata may be the shared dictionary: replace, never mutate.
    common["metadata"] = {"dwf": {**common["metadata"]["dwf"], "image": image_metadata}}
    context.embedded_images += 1
    extension = "png" if mime == "image/png" else "jpg"
    stem = "dwf_image" if identifier is None else f"dwf_image_{identifier}"
    return [
        {
            **common,
            "kind": "IMAGE",
            "insert": [x0, y0],
            "width": x1 - x0,
            "height": y1 - y0,
            "name": f"{stem}.{extension}",
            "mime_type": mime,
            "data": base64.b64encode(data).decode("ascii"),
        }
    ]


def _sniff_mime(data: bytes) -> str:
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8"):
        return "image/jpeg"
    if data.startswith((b"II*\x00", b"MM\x00*")):
        return "image/tiff"
    return "application/octet-stream"


def _entity_common(
    source_entity: Any,
    sheet: Any,
    sheet_index: int,
    context: _ConversionContext,
) -> dict[str, Any]:
    style = source_entity.style
    layer_name = context.intern(
        str(
            getattr(style, "layer", None) or getattr(source_entity, "layer", "0") or "0"
        )
    )
    source_unit = _normalized_unit(getattr(sheet, "units", None))
    scale_to_mm = _UNIT_SCALE_TO_MM.get(source_unit)
    linetype = _register_linetype(style, scale_to_mm, context)
    color = _rgba_hex(
        getattr(style, "stroke_color", None) or getattr(style, "color", None)
    )
    if color is not None:
        color = context.intern(color)
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
            lineweight_mm = context.intern_float(candidate)
            layer.setdefault("lineweight_mm", lineweight_mm)

    source = getattr(source_entity, "source", None)
    appearance_effects = {
        "clips": len(getattr(source_entity, "clips", ())),
        "opacity_masks": len(getattr(source_entity, "opacity_masks", ())),
        "compositing_groups": len(getattr(source_entity, "compositing_groups", ())),
    }
    context.appearance_effect_counts.update(
        {name: count for name, count in appearance_effects.items() if count > 0}
    )
    # シート名・単位などの定数は文書メタデータのsheets一覧にあり、要素ごとには
    # 動的な最小限だけ持つ(entity_indexはsource.idの「区:流:番」と重複するため
    # 省略)。効果カウントは非ゼロのときだけ載せる
    # The constant part is one shared dictionary per (sheet, stream, style): a
    # plot set of 1.7 million entities spent 600 MB on identical copies of it.
    # Anything per entity goes into a copy; nothing may mutate the shared one.
    counts: list[tuple[str, int]] = []
    for effect_name, effect_key in (
        ("clips", "clip_count"),
        ("opacity_masks", "opacity_mask_count"),
        ("compositing_groups", "compositing_group_count"),
    ):
        if appearance_effects[effect_name]:
            counts.append((effect_key, appearance_effects[effect_name]))
    glyph_outline_count = len(getattr(source_entity, "glyph_outline", ()) or ())
    if glyph_outline_count:
        counts.append(("glyph_outline_count", glyph_outline_count))
    metadata = context.shared_entity_metadata(
        sheet_index,
        getattr(source_entity, "section_index", None),
        getattr(source_entity, "stream_index", None),
        getattr(source_entity, "resource_href", None),
        getattr(source_entity, "resource_role", None),
        bool(getattr(source_entity, "is_markup", False)),
        context.shared_style_metadata(style),
        tuple(counts),
    )
    colored_points = tuple(getattr(source_entity, "colored_points", ()))
    if colored_points:
        metadata_wrapper = {
            "dwf": {
                **metadata,
                "colored_points": [
                    {
                        "point": _point(item.point),
                        "color": _json_safe(getattr(item, "color", None)),
                    }
                    for item in colored_points
                ],
            }
        }
    else:
        metadata_wrapper = context.shared_metadata_wrapper(metadata)
    # Key count decides the dictionary's size class: with ``visible`` (default
    # true, so omitted) and ``source`` a LINE had 11 keys and took 464 bytes;
    # at 10 keys or fewer it takes 272. A plot set of 1.75 million entities
    # spent 520 MB on the entity dictionaries and 450 MB on their ``source``.
    result: dict[str, Any] = {
        "id": context.allocate_id(),
        "layer": layer_name,
        "linetype": linetype,
    }
    if not getattr(style, "visible", True):
        result["visible"] = False
    if context.options.entity_provenance:
        source_info: dict[str, Any] = {
            "format": "dwf",
            "id": _source_id(source_entity),
            "kind": _kind(source_entity),
        }
        if context.options.debug_provenance:
            source_info["metadata"] = {
                "offset": getattr(source, "offset", None),
                "length": getattr(source, "length", None),
                "opcode": getattr(source, "opcode", None),
            }
        result["source"] = source_info
    result["metadata"] = metadata_wrapper
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
                    str(diagnostic.offset)
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


# Sampling step along a curve, in sheet millimetres. The chord of a 0.25 mm step
# on a 1 mm radius lies 0.008 mm off the arc, below any plotted line width.
_CURVE_STEP_MM = 0.25


def _curve_steps(
    length: float, fraction: float, curve_segments: int, scale_to_mm: float | None
) -> int:
    """How many straight pieces a curve of ``length`` (sheet units) is sampled into.

    ``curve_segments`` is the count for a full circle and ``fraction`` of it the
    ceiling for this curve (1.0 for a bezier). The curve's length in millimetres
    over the sampling step is what the drawing needs: a 0.5 mm arrowhead arc
    takes 2 pieces rather than 96. A plot set with 1.9 million path segments
    would otherwise sample into tens of millions of points. Without known sheet
    units every curve gets the ceiling.
    """
    ceiling = max(1, math.ceil(fraction * curve_segments))
    if scale_to_mm is None or not math.isfinite(length):
        return ceiling
    needed = math.ceil(length * scale_to_mm / _CURVE_STEP_MM)
    return max(min(2, ceiling), min(ceiling, needed))


def _polygon_length(points: Sequence[Sequence[float]]) -> float:
    return sum(math.hypot(b[0] - a[0], b[1] - a[1]) for a, b in pairwise(points))


def _sample_path_figure(
    figure: Any, curve_segments: int, scale_to_mm: float | None = None
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
            count = _curve_steps(
                _polygon_length((current, control1, control2, end)),
                1.0,
                curve_segments,
                scale_to_mm,
            )
            for index in range(1, count + 1):
                points.append(
                    _cubic_bezier(current, control1, control2, end, index / count)
                )
            approximated = True
        elif kind == "quadratic_bezier":
            control = _point(segment.control)
            count = _curve_steps(
                _polygon_length((current, control, end)),
                1.0,
                curve_segments,
                scale_to_mm,
            )
            for index in range(1, count + 1):
                points.append(_quadratic_bezier(current, control, end, index / count))
            approximated = True
        elif kind == "elliptical_arc":
            center = _point(segment.center)
            x_axis = _point(segment.x_axis)
            y_axis = _point(segment.y_axis)
            start = float(segment.start_angle_degrees)
            sweep = float(segment.sweep_angle_degrees)
            count = _curve_steps(
                _arc_length(x_axis, y_axis, sweep),
                abs(sweep) / 360.0,
                curve_segments,
                scale_to_mm,
            )
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


def _arc_length(
    x_axis: Sequence[float], y_axis: Sequence[float], sweep_degrees: float
) -> float:
    """The length of an elliptical arc, taken on the mean radius (an overestimate)."""
    radius = (math.hypot(*x_axis) + math.hypot(*y_axis)) / 2.0
    return abs(math.radians(sweep_degrees)) * radius


def _sample_affine_ellipse(
    center: list[float],
    x_axis: list[float],
    y_axis: list[float],
    start_degrees: float,
    end_degrees: float,
    closed: bool,
    curve_segments: int,
    scale_to_mm: float | None = None,
) -> list[list[float]]:
    sweep = 360.0 if closed else end_degrees - start_degrees
    count = max(
        3 if closed else 1,
        _curve_steps(
            _arc_length(x_axis, y_axis, sweep),
            abs(sweep) / 360.0,
            curve_segments,
            scale_to_mm,
        ),
    )
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


def _hashable(value: Any) -> Any:
    if isinstance(value, dict):
        return tuple(sorted((k, _hashable(v)) for k, v in value.items()))
    if isinstance(value, (list, tuple)):
        return tuple(_hashable(v) for v in value)
    return value


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


def _sheet_entities(
    drawing: Any, sheet: Any, sheet_index: int
) -> tuple[Iterable[Any], Iterable[Any]]:
    """The (entities, markup entities) of a sheet, read lazily when possible.

    An ``ezdwf.DrawingReader`` (0.0.13) keeps the drawing in the Rust core and
    yields a sheet's entities a chunk at a time; a ``Drawing`` carries them on
    the sheet itself.
    """
    reader = getattr(drawing, "entities", None)
    if callable(reader):
        return reader(sheet_index), drawing.markup_entities(sheet_index)
    return getattr(sheet, "entities", ()), getattr(sheet, "markup_entities", ())


def _sheet_metadata(
    sheet: Any, index: int, frame: _SheetFrame | None, counts: tuple[int, int]
) -> dict[str, Any]:
    result = {
        "index": index,
        "section_index": getattr(sheet, "section_index", None),
        "name": getattr(sheet, "name", None),
        "title": getattr(sheet, "title", None),
        "plot_order": getattr(sheet, "plot_order", None),
        "units": getattr(sheet, "units", None),
        "paper_bounds": _json_safe(getattr(sheet, "paper_bounds", None)),
        "content_bounds": _json_safe(getattr(sheet, "content_bounds", None)),
        "content_shift": _json_safe(getattr(sheet, "content_shift", None)),
        "entity_count": counts[0],
        "markup_entity_count": counts[1],
    }
    if frame is not None:
        # Added to every coordinate of the sheet; ``placed_bounds`` is the
        # sheet's paper (or content) after the shift.
        result["offset"] = list(frame.offset)
        result["cell"] = list(frame.cell) if frame.cell is not None else None
        result["placed_bounds"] = list(frame.bounds) if frame.bounds else None
    return result


def _drawing_bbox(
    sheets: Sequence[Any], layout: _SheetLayout | None
) -> tuple[float, float, float, float] | None:
    bounds = []
    for index, sheet in enumerate(sheets):
        value = getattr(sheet, "content_bounds", None)
        if value is None:
            continue
        x0, y0, x1, y1 = (float(item) for item in value)
        if layout is not None:
            dx, dy = layout.frames[index].offset
            x0, y0, x1, y1 = x0 + dx, y0 + dy, x1 + dx, y1 + dy
        bounds.append((x0, y0, x1, y1))
    if not bounds:
        return None
    return (
        min(bound[0] for bound in bounds),
        min(bound[1] for bound in bounds),
        max(bound[2] for bound in bounds),
        max(bound[3] for bound in bounds),
    )


def _sheet_extent(sheet: Any) -> tuple[float, float, float, float] | None:
    """The sheet's paper, or its content when the DWF states no paper."""
    for name in ("paper_bounds", "content_bounds"):
        value = getattr(sheet, name, None)
        if value is None:
            continue
        try:
            x0, y0, x1, y1 = (float(item) for item in value)
        except (TypeError, ValueError):
            continue
        if (
            all(math.isfinite(item) for item in (x0, y0, x1, y1))
            and x1 > x0
            and y1 > y0
        ):
            return (x0, y0, x1, y1)
    return None


def _sheet_layout(
    sheets: Sequence[Any], gap_ratio: float = DEFAULT_SHEET_GAP_RATIO
) -> _SheetLayout | None:
    """Where each sheet goes when several share one modelspace.

    Sheets sit in reading order (left to right, then down) on a grid of equal
    cells, each the size of the largest paper, with a gap of ``gap_ratio`` times
    the cell width. Up to four sheets form one row; more form a grid of about
    ``sqrt(n)`` columns, so that a 96 sheet set is 10 by 10 rather than a strip.
    The first sheet keeps its file coordinates and anchors the grid at its top
    left corner; a sheet without paper or content has no cell and is not moved.
    Returns ``None`` when fewer than two sheets have an extent.
    """
    extents = [_sheet_extent(sheet) for sheet in sheets]
    placed = [extent for extent in extents if extent is not None]
    if len(placed) < 2:
        return None
    cell_width = max(extent[2] - extent[0] for extent in placed)
    cell_height = max(extent[3] - extent[1] for extent in placed)
    gap = cell_width * gap_ratio
    count = len(placed)
    columns = count if count <= 4 else math.ceil(math.sqrt(count))
    anchor_left, anchor_top = placed[0][0], placed[0][3]
    frames: list[_SheetFrame] = []
    position = 0
    for extent in extents:
        if extent is None:
            frames.append(_SheetFrame((0.0, 0.0), None, None))
            continue
        column, row = position % columns, position // columns
        left = anchor_left + column * (cell_width + gap)
        top = anchor_top - row * (cell_height + gap)
        dx, dy = left - extent[0], top - extent[3]
        frames.append(
            _SheetFrame(
                (dx, dy),
                (column, row),
                (extent[0] + dx, extent[1] + dy, extent[2] + dx, extent[3] + dy),
            )
        )
        position += 1
    return _SheetLayout(
        frames=frames,
        columns=columns,
        rows=math.ceil(count / columns),
        cell_size=(cell_width, cell_height),
        gap=gap,
    )


_POINT_KEYS = ("p1", "p2", "center", "insert", "position")
_POINT_LIST_KEYS = ("vertices", "control_points", "fit_points")


def _translate_entities(
    entities: Iterable[dict[str, Any]], offset: tuple[float, float]
) -> None:
    """Move the entities a DWF sheet produced by ``offset`` (in place)."""
    dx, dy = offset
    shifted_metadata: set[int] = set()
    for entity in entities:
        for key in _POINT_KEYS:
            point = entity.get(key)
            if point is not None:
                entity[key] = [point[0] + dx, point[1] + dy]
        for key in _POINT_LIST_KEYS:
            points = entity.get(key)
            if points:
                entity[key] = [[point[0] + dx, point[1] + dy] for point in points]
        for loop in entity.get("loops") or ():
            vertices = loop.get("vertices")
            if vertices:
                loop["vertices"] = [
                    [point[0] + dx, point[1] + dy] for point in vertices
                ]
        # Entities made from one record share their metadata dictionary.
        metadata = (entity.get("metadata") or {}).get("dwf") or {}
        colored = metadata.get("colored_points")
        if colored and id(metadata) not in shifted_metadata:
            shifted_metadata.add(id(metadata))
            for item in colored:
                point = item.get("point")
                if point is not None:
                    item["point"] = [point[0] + dx, point[1] + dy]


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
