"""Native IDW (Autodesk Inventor drawing) to CAD 2D IR importer.

The adapter consumes the saved-display model returned by ``inventor_kit.read_drawing_file``
(sheets, items, views and image descriptors). It does not open referenced IPT/IAM files,
regenerate drawing views or reproject geometry: whatever Inventor stored for the sheet is
mapped item by item.

Coordinates are Inventor's internal sheet units. Inventor's database length unit is
centimetres and every observed sheet (A-series, ANSI) matches that assumption, so the
importer scales by ``unit_scale`` (default 10 -> millimetres) and discloses the assumption
with ``IDW_UNITS_ASSUMED_CM``. The result is paper space: view scales are already applied,
so a DXF written from this IR is the sheet at 1:1, not model-space dimensions.
"""

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

DEFAULT_UNIT_SCALE_TO_MM = 10.0
DEFAULT_SHEET_GAP_RATIO = 0.1

_EPSILON = 1.0e-9
_TAU = 2.0 * math.pi
_TEXT_LINE_SPACING = 1.2
# inventor-kit's own SVG renderer falls back to this height when no font record exists.
_FALLBACK_TEXT_HEIGHT = 0.25
_AIGDT_SYMBOLS = {"n": "⌀", "x": "↧"}
_LINE_BREAK_RE = re.compile(r"\r\n|\n|\r")
_STYLE_NAME_RE = re.compile(r"[^\w.\-]+", flags=re.UNICODE)
_MAJOR_RE = re.compile(r"major\s*(\d+)")
_VECTOR_KINDS = frozenset({"polyline", "curve", "triangles", "text"})
_OUT_OF_SHEET_MARGIN_RATIO = 0.05
_PAPER_TOLERANCE = 0.01
_STANDARD_PAPER_SIZES_MM: tuple[tuple[str, float, float], ...] = (
    ("A0", 841.0, 1189.0),
    ("A1", 594.0, 841.0),
    ("A2", 420.0, 594.0),
    ("A3", 297.0, 420.0),
    ("A4", 210.0, 297.0),
    ("JIS B0", 1030.0, 1456.0),
    ("JIS B1", 728.0, 1030.0),
    ("JIS B2", 515.0, 728.0),
    ("JIS B3", 364.0, 515.0),
    ("JIS B4", 257.0, 364.0),
    ("ANSI A", 215.9, 279.4),
    ("ANSI B", 279.4, 431.8),
    ("ANSI C", 431.8, 558.8),
    ("ANSI D", 558.8, 863.6),
    ("ANSI E", 863.6, 1117.6),
)


@dataclass(frozen=True, slots=True)
class _Frame:
    """Scale and sheet offset applied to every source coordinate."""

    scale: float
    offset_x: float
    offset_y: float

    def point(self, value: Sequence[Any]) -> list[float]:
        x = float(value[0]) * self.scale + self.offset_x
        y = float(value[1]) * self.scale + self.offset_y
        if not (math.isfinite(x) and math.isfinite(y)):
            raise ValueError("non-finite coordinate")
        return [x, y]

    def vector(self, value: Sequence[Any]) -> tuple[float, float]:
        x = float(value[0]) * self.scale
        y = float(value[1]) * self.scale
        if not (math.isfinite(x) and math.isfinite(y)):
            raise ValueError("non-finite vector")
        return x, y

    def length(self, value: Any) -> float:
        result = float(value) * self.scale
        if not math.isfinite(result):
            raise ValueError("non-finite length")
        return result


@dataclass(slots=True)
class _ConversionContext:
    options: ImportOptions
    unit_scale: float
    diagnostics: list[ImportDiagnostic] = field(default_factory=list)
    layers: dict[str, dict[str, Any]] = field(default_factory=dict)
    linetypes: dict[str, dict[str, Any]] = field(default_factory=dict)
    text_styles: dict[str, dict[str, Any]] = field(default_factory=dict)
    linetype_names: dict[tuple[float, ...], str] = field(default_factory=dict)
    text_style_names: dict[tuple[str, bool, bool], str] = field(default_factory=dict)
    source_counts: Counter[str] = field(default_factory=Counter)
    converted_counts: Counter[str] = field(default_factory=Counter)
    skipped_counts: Counter[str] = field(default_factory=Counter)
    approximation_counts: Counter[str] = field(default_factory=Counter)
    unresolved_counts: Counter[str] = field(default_factory=Counter)
    unsupported_counts: Counter[str] = field(default_factory=Counter)
    omission_counts: Counter[str] = field(default_factory=Counter)
    omission_samples: dict[str, dict[str, Any]] = field(default_factory=dict)
    out_of_sheet_ids: list[str] = field(default_factory=list)
    out_of_sheet_count: int = 0
    symbol_mapped_count: int = 0
    mirrored_text_count: int = 0
    multiline_text_count: int = 0
    image_references: list[int] = field(default_factory=list)
    next_entity_number: int = 1

    def allocate_id(self) -> str:
        entity_id = f"IDW_E{self.next_entity_number:08d}"
        self.next_entity_number += 1
        return entity_id


def convert_idw_file_to_ir(
    path: str | Path,
    *,
    options: ImportOptions | None = None,
    limits: Any | None = None,
    drawing_limits: Any | None = None,
) -> ImportResult:
    """Read an IDW file with ``inventor_kit`` and convert its saved display to IR.

    ``limits`` and ``drawing_limits`` are forwarded to ``inventor_kit.read_drawing_file``
    (``inventor_kit.Limits`` / ``inventor_kit.DrawingLimits``) to tighten the native
    resource ceilings.
    """
    try:
        import inventor_kit
    except ImportError as exc:
        raise MissingOptionalDependencyError(
            "IDW support requires the optional dependency inventor-kit; "
            'install it with `pip install "cad2d-ir[idw]"`.'
        ) from exc

    source_path = Path(path)
    try:
        document = inventor_kit.read_drawing_file(
            source_path, limits=limits, drawing_limits=drawing_limits
        )
    except Exception as exc:
        raise ImporterError(f"Failed to read IDW file {source_path}: {exc}") from exc
    return idw_document_to_ir(
        document,
        source_name=source_path.name,
        source_sha256=_sha256_file(source_path),
        parser_version=_parser_version(),
        options=options,
    )


def idw_document_to_ir(
    document: Any,
    *,
    source_name: str | None = None,
    source_sha256: str | None = None,
    parser_version: str | None = None,
    options: ImportOptions | None = None,
    unit_scale: float = DEFAULT_UNIT_SCALE_TO_MM,
    sheet_gap_ratio: float = DEFAULT_SHEET_GAP_RATIO,
) -> ImportResult:
    """Convert an ``inventor_kit.DrawingDocument``-compatible object directly to IR.

    ``unit_scale`` multiplies every source coordinate (10 = centimetres to
    millimetres). Sheets are laid out side by side along +X with a gap of
    ``sheet_gap_ratio`` times the widest sheet; a single sheet keeps its origin.
    """
    import_options = options or ImportOptions()
    if (
        isinstance(unit_scale, bool)
        or not isinstance(unit_scale, (int, float))
        or not math.isfinite(unit_scale)
        or unit_scale <= 0.0
    ):
        raise ValueError("unit_scale must be a positive finite number")
    if (
        isinstance(sheet_gap_ratio, bool)
        or not isinstance(sheet_gap_ratio, (int, float))
        or not math.isfinite(sheet_gap_ratio)
        or sheet_gap_ratio < 0.0
    ):
        raise ValueError("sheet_gap_ratio must be a non-negative finite number")
    context = _ConversionContext(options=import_options, unit_scale=float(unit_scale))

    sheets = list(getattr(document, "sheets", None) or ())
    majors = _segment_majors(document)
    available = [sheet for sheet in sheets if _sheet_available(sheet)]
    if not available:
        raise ImporterError(_unsupported_message(document, majors))

    for sheet in sheets:
        if _sheet_available(sheet):
            continue
        context.diagnostics.append(
            ImportDiagnostic(
                code="IDW_SHEET_UNAVAILABLE",
                severity="warning",
                message=(
                    f"IDW sheet {getattr(sheet, 'index', '?')} "
                    f"({getattr(sheet, 'name', '')!r}) has no decodable stored display "
                    "and was skipped."
                ),
                source_id=_text_or_none(getattr(sheet, "id", None)),
                action="skipped",
                details={
                    "sheet_index": getattr(sheet, "index", None),
                    "name": getattr(sheet, "name", None),
                    "status": getattr(sheet, "status", None),
                    "diagnostics": [str(d) for d in getattr(sheet, "diagnostics", ())],
                },
            )
        )

    scale = context.unit_scale
    widest = max(float(sheet.size_in_source_units[0]) for sheet in available)
    gap = widest * scale * float(sheet_gap_ratio) if len(available) > 1 else 0.0
    entities: list[dict[str, Any]] = []
    sheet_records: list[dict[str, Any]] = []
    paper_matches: list[str | None] = []
    bbox_min = [math.inf, math.inf]
    bbox_max = [-math.inf, -math.inf]
    offset_x = 0.0
    for sheet in available:
        width, height = (float(v) for v in sheet.size_in_source_units[:2])
        frame = _Frame(scale=scale, offset_x=offset_x, offset_y=0.0)
        sheet_index = int(getattr(sheet, "index", len(sheet_records)))
        items = list(getattr(sheet, "items", None) or ())
        placements: list[dict[str, Any]] = []
        converted_before = len(entities)
        for item in items:
            entities.extend(
                _convert_item_safe(
                    item, sheet_index, (width, height), frame, placements, context
                )
            )
        _collect_omissions(sheet, sheet_index, context)
        views = list(getattr(sheet, "views", None) or ())
        raster_only = sum(1 for view in views if _view_is_raster_only(view))
        paper = _match_paper(width * scale, height * scale)
        paper_matches.append(paper)
        size_mm = [width * scale, height * scale]
        sheet_records.append(
            {
                "index": sheet_index,
                "name": getattr(sheet, "name", None),
                "status": getattr(sheet, "status", None),
                "size_mm": size_mm,
                "offset_mm": [offset_x, 0.0],
                "paper": paper,
                "item_count": len(items),
                "entity_count": len(entities) - converted_before,
                "view_count": len(views),
                "raster_only_view_count": raster_only,
                "diagnostics": [str(d) for d in getattr(sheet, "diagnostics", ())],
                "images": placements,
            }
        )
        bbox_min[0] = min(bbox_min[0], offset_x)
        bbox_min[1] = min(bbox_min[1], 0.0)
        bbox_max[0] = max(bbox_max[0], offset_x + size_mm[0])
        bbox_max[1] = max(bbox_max[1], size_mm[1])
        offset_x += size_mm[0] + gap

    _append_units_diagnostic(sheet_records, paper_matches, context)
    if len(available) > 1:
        context.diagnostics.append(
            ImportDiagnostic(
                code="IDW_MULTISHEET_TILED",
                severity="warning",
                message=(
                    f"Placed {len(available)} IDW sheets side by side in one IR "
                    "modelspace; sheet offsets remain in source metadata."
                ),
                action="flattened",
                details={
                    "sheet_count": len(available),
                    "gap_mm": gap,
                    "offsets_mm": [record["offset_mm"] for record in sheet_records],
                },
            )
        )
    raster_only_total = sum(r["raster_only_view_count"] for r in sheet_records)
    if raster_only_total:
        context.diagnostics.append(
            ImportDiagnostic(
                code="IDW_VIEW_RASTER_ONLY",
                severity="warning",
                message=(
                    f"{raster_only_total} IDW drawing view(s) are stored only as raster "
                    "view caches; their model geometry is not available as vectors."
                ),
                action="preserved_metadata",
                details={
                    "count": raster_only_total,
                    "sheets": {
                        str(r["index"]): r["raster_only_view_count"]
                        for r in sheet_records
                        if r["raster_only_view_count"]
                    },
                },
            )
        )
    _append_item_diagnostics(context)
    _forward_document_diagnostics(document, context)

    image_records = _image_records(getattr(document, "images", None) or ())
    version = (
        "segment-major-" + "+".join(str(m) for m in majors) if majors else "unknown"
    )
    source: dict[str, Any] = {
        "format": "idw",
        "version": version,
        "metadata": {
            "idw": {
                "parser": "inventor-kit",
                "parser_version": parser_version,
                "status": _text_or_none(getattr(document, "status", None)),
                "sheet_status": _text_or_none(getattr(document, "sheet_status", None)),
                "segment_majors": majors,
                "source_sha256": _text_or_none(
                    getattr(document, "source_sha256", None)
                ),
                "units": _text_or_none(getattr(document, "units", None)),
                "sheet_count": len(sheets),
                "sheets": sheet_records,
                "images": image_records,
            }
        },
    }
    if source_name is not None:
        source["name"] = source_name
    if source_sha256 is not None:
        source["sha256"] = source_sha256

    header: dict[str, Any] = {
        "units": "mm",
        "angle_unit": "deg",
        "coord_space": "world",
        "bbox": {"min": bbox_min, "max": bbox_max},
        "metadata": {
            "idw": {
                "coordinate_space": "sheet_paper",
                "unit_scale_to_mm": scale,
                "unit_assumption": "internal_centimeters_unverified",
                "tiled_sheets": len(available) > 1,
            }
        },
    }
    document_ir: dict[str, Any] = {
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
        validate_ir(document_ir)

    statistics: dict[str, Any] = {
        "source_format": "idw",
        "source_version": version,
        "parser_version": parser_version,
        "source_status": _text_or_none(getattr(document, "status", None)),
        "source_majors": majors,
        "source_sheets": len(sheets),
        "converted_sheets": len(available),
        "source_entities": sum(context.source_counts.values()),
        "source_entity_counts": dict(sorted(context.source_counts.items())),
        "converted_entities": len(entities),
        "converted_entity_counts": dict(sorted(context.converted_counts.items())),
        "skipped_entities": sum(context.skipped_counts.values()),
        "skipped_entity_counts": dict(sorted(context.skipped_counts.items())),
        "approximated_entities": sum(context.approximation_counts.values()),
        "approximated_entity_counts": dict(
            sorted(context.approximation_counts.items())
        ),
        "out_of_sheet_entities": context.out_of_sheet_count,
        "omitted_elements": sum(context.omission_counts.values()),
        "images": len(image_records),
        "raster_only_views": raster_only_total,
    }
    return ImportResult(
        document=document_ir,
        diagnostics=context.diagnostics,
        statistics=statistics,
    )


# ---------------------------------------------------------------------------
# Items


def _convert_item_safe(
    item: Any,
    sheet_index: int,
    sheet_size: tuple[float, float],
    frame: _Frame,
    placements: list[dict[str, Any]],
    context: _ConversionContext,
) -> list[dict[str, Any]]:
    geometry = _mapping(getattr(item, "geometry", None))
    kind = str(geometry.get("kind", "unknown"))
    context.source_counts[kind] += 1
    try:
        return _convert_item(
            item, geometry, kind, sheet_index, sheet_size, frame, placements, context
        )
    except (AttributeError, KeyError, TypeError, ValueError, OverflowError) as exc:
        source_id = _text_or_none(getattr(item, "id", None))
        if context.options.strict:
            raise ImporterError(
                f"Failed to convert IDW {kind} at {source_id}: {exc}"
            ) from exc
        context.skipped_counts[kind] += 1
        context.diagnostics.append(
            ImportDiagnostic(
                code="IDW_ENTITY_CONVERSION_FAILED",
                severity="error",
                message=f"Failed to convert IDW {kind}: {exc}",
                source_id=source_id,
                source_kind=kind,
                action="skipped",
            )
        )
        return []


def _convert_item(
    item: Any,
    geometry: Mapping[str, Any],
    kind: str,
    sheet_index: int,
    sheet_size: tuple[float, float],
    frame: _Frame,
    placements: list[dict[str, Any]],
    context: _ConversionContext,
) -> list[dict[str, Any]]:
    if kind == "image":
        reference = geometry.get("reference")
        placements.append(
            {
                "reference": reference,
                "origin": frame.point(geometry["origin"]),
                "u": list(frame.vector(geometry["u"])),
                "v": list(frame.vector(geometry["v"])),
                "item_id": _text_or_none(getattr(item, "id", None)),
            }
        )
        if isinstance(reference, int):
            context.image_references.append(reference)
        context.skipped_counts[kind] += 1
        return []
    if kind not in _VECTOR_KINDS:
        context.skipped_counts[kind] += 1
        context.unsupported_counts[kind] += 1
        return []

    bounds = _geometry_bounds(kind, geometry)
    if bounds is not None and not _touches_sheet(bounds, sheet_size):
        context.skipped_counts[kind] += 1
        context.out_of_sheet_count += 1
        item_id = _text_or_none(getattr(item, "id", None))
        if item_id is not None and len(context.out_of_sheet_ids) < 10:
            context.out_of_sheet_ids.append(item_id)
        return []

    common = _entity_common(item, kind, sheet_index, context, frame)
    if kind == "polyline":
        return _convert_polyline(geometry, common, frame, context)
    if kind == "curve":
        if geometry.get("filled") is True:
            return _convert_filled_curve(geometry, common, frame, context)
        return _convert_curve(geometry, common, frame, context)
    if kind == "triangles":
        return _convert_triangles(geometry, common, frame, context)
    return _convert_text(geometry, common, frame, context)


def _convert_polyline(
    geometry: Mapping[str, Any],
    common: dict[str, Any],
    frame: _Frame,
    context: _ConversionContext,
) -> list[dict[str, Any]]:
    points: list[list[float]] = []
    for raw in geometry["points"]:
        point = frame.point(raw)
        if points and math.dist(points[-1], point) <= _EPSILON:
            continue
        points.append(point)
    if len(points) < 2:
        context.skipped_counts["polyline"] += 1
        return []
    closed = len(points) >= 4 and math.dist(points[0], points[-1]) <= _EPSILON
    if closed:
        points.pop()
    if len(points) == 2:
        return [
            _emit({**common, "kind": "LINE", "p1": points[0], "p2": points[1]}, context)
        ]
    return [
        _emit(
            {**common, "kind": "LWPOLYLINE", "vertices": points, "closed": closed},
            context,
        )
    ]


def _curve_parameters(
    geometry: Mapping[str, Any], frame: _Frame
) -> tuple[list[float], tuple[float, float], tuple[float, float], float, float]:
    center = frame.point(geometry["center"])
    u = frame.vector(geometry["u"])
    v = frame.vector(geometry["v"])
    start = float(geometry["start"])
    end = float(geometry["end"])
    if not (math.isfinite(start) and math.isfinite(end)):
        raise ValueError("non-finite curve interval")
    if end < start:
        # Reparametrize with t' = -t so the range increases while the traversal
        # direction is preserved: P(-t') = C + u cos t' - v sin t'.
        v = (-v[0], -v[1])
        start, end = -start, -end
    return center, u, v, start, end


def _curve_point(
    center: Sequence[float],
    u: tuple[float, float],
    v: tuple[float, float],
    t: float,
) -> tuple[float, float]:
    return (
        center[0] + u[0] * math.cos(t) + v[0] * math.sin(t),
        center[1] + u[1] * math.cos(t) + v[1] * math.sin(t),
    )


def _convert_curve(
    geometry: Mapping[str, Any],
    common: dict[str, Any],
    frame: _Frame,
    context: _ConversionContext,
) -> list[dict[str, Any]]:
    """Map ``P(t) = C + u cos t + v sin t`` (an affine ellipse arc) exactly.

    ``u`` and ``v`` are the XY projections of a 3D circle's axes, so they are
    generally neither orthogonal nor equal. A 2x2 singular value decomposition
    gives the principal axes; the parameter range is recovered numerically from
    the start, middle and end points so that reflected or skewed placements need
    no case analysis.
    """
    center, u, v, start, end = _curve_parameters(geometry, frame)
    span = end - start
    full = abs(span - _TAU) <= 1.0e-6
    ux, uy = u
    vx, vy = v
    e, f = (ux + vy) / 2.0, (ux - vy) / 2.0
    g, h = (uy + vx) / 2.0, (uy - vx) / 2.0
    q, r = math.hypot(e, h), math.hypot(f, g)
    major, minor = q + r, abs(q - r)
    det = ux * vy - vx * uy
    if major <= _EPSILON:
        context.skipped_counts["curve"] += 1
        return []
    if minor <= max(_EPSILON, 1.0e-6 * major):
        # Edge-on projection of a circle: the arc collapses onto a segment.
        samples = [_curve_point(center, u, v, start + span * i / 8) for i in range(9)]
        p1, p2 = min(samples), max(samples)
        context.approximation_counts["curve_edge_on"] += 1
        return [
            _emit(
                {
                    **common,
                    "kind": "LINE",
                    "p1": list(p1),
                    "p2": list(p2),
                    "approximation": {
                        "method": "other",
                        "source_kind": "curve",
                        "metadata": {"reason": "edge_on_projection"},
                    },
                },
                context,
            )
        ]
    if abs(major - minor) <= 1.0e-9 * major:
        if full:
            return [
                _emit(
                    {**common, "kind": "CIRCLE", "center": center, "radius": major},
                    context,
                )
            ]
        p0 = _curve_point(center, u, v, start)
        p1 = _curve_point(center, u, v, end)
        a0 = math.degrees(math.atan2(p0[1] - center[1], p0[0] - center[0]))
        a1 = math.degrees(math.atan2(p1[1] - center[1], p1[0] - center[0]))
        if det < 0.0:
            a0, a1 = a1, a0
        return [
            _emit(
                {
                    **common,
                    "kind": "ARC",
                    "center": center,
                    "radius": major,
                    "start_angle": a0,
                    "end_angle": a1,
                },
                context,
            )
        ]

    phi = (math.atan2(h, e) + math.atan2(g, f)) / 2.0
    e1 = (math.cos(phi), math.sin(phi))
    e2 = (-e1[1], e1[0])
    ratio = minor / major
    major_axis = [major * e1[0], major * e1[1]]

    def parameter(t: float) -> float:
        px, py = _curve_point(center, u, v, t)
        dx, dy = px - center[0], py - center[1]
        return math.atan2(
            (dx * e2[0] + dy * e2[1]) / minor, (dx * e1[0] + dy * e1[1]) / major
        )

    def canonical(tp: float) -> tuple[float, float]:
        return (
            center[0]
            + major_axis[0] * math.cos(tp)
            - major_axis[1] * ratio * math.sin(tp),
            center[1]
            + major_axis[1] * math.cos(tp)
            + major_axis[0] * ratio * math.sin(tp),
        )

    checkpoints = (start, (start + end) / 2.0, end)
    error = max(
        math.dist(canonical(parameter(t)), _curve_point(center, u, v, t))
        for t in checkpoints
    )
    if error > 1.0e-6 * max(1.0, major):
        segments = max(8, math.ceil(context.options.curve_segments * span / _TAU))
        vertices = [
            list(_curve_point(center, u, v, start + span * i / segments))
            for i in range(segments + 1)
        ]
        if full:
            vertices.pop()
        context.approximation_counts["curve"] += 1
        return [
            _emit(
                {
                    **common,
                    "kind": "LWPOLYLINE",
                    "vertices": vertices,
                    "closed": full,
                    "approximation": {
                        "method": "polyline",
                        "source_kind": "curve",
                        "segments": segments,
                    },
                },
                context,
            )
        ]
    if full:
        start_param, end_param = 0.0, _TAU
    else:
        ts, tm, te = (parameter(t) for t in checkpoints)
        sweep = (te - ts) % _TAU
        middle = (tm - ts) % _TAU
        start_param, end_param = (ts, te) if middle <= sweep else (te, ts)
    return [
        _emit(
            {
                **common,
                "kind": "ELLIPSE",
                "center": center,
                "major_axis": major_axis,
                "ratio": ratio,
                "start_param": start_param,
                "end_param": end_param,
            },
            context,
        )
    ]


def _convert_filled_curve(
    geometry: Mapping[str, Any],
    common: dict[str, Any],
    frame: _Frame,
    context: _ConversionContext,
) -> list[dict[str, Any]]:
    """A filled arc closes along its chord; represent it as a solid polyline loop."""
    center, u, v, start, end = _curve_parameters(geometry, frame)
    span = end - start
    if span <= _EPSILON:
        raise ValueError("filled curve has an empty parameter range")
    segments = max(
        8, math.ceil(context.options.curve_segments * min(span, _TAU) / _TAU)
    )
    loop = [
        list(_curve_point(center, u, v, start + span * i / segments))
        for i in range(segments + 1)
    ]
    if math.dist(loop[0], loop[-1]) <= _EPSILON:
        loop.pop()
    if len(loop) < 3:
        raise ValueError("filled curve collapsed to fewer than three vertices")
    context.approximation_counts["filled_curve"] += 1
    return [
        _emit(
            {
                **common,
                "kind": "HATCH",
                "solid": True,
                "loops": [{"vertices": loop, "is_outer": True}],
                "approximation": {
                    "method": "polyline",
                    "source_kind": "filled_curve",
                    "segments": segments,
                },
            },
            context,
        )
    ]


def _convert_triangles(
    geometry: Mapping[str, Any],
    common: dict[str, Any],
    frame: _Frame,
    context: _ConversionContext,
) -> list[dict[str, Any]]:
    vertices = [frame.point(raw) for raw in geometry["vertices"]]
    indices = [int(i) for i in geometry["indices"]]
    if len(indices) % 3 or any(i < 0 or i >= len(vertices) for i in indices):
        raise ValueError("invalid triangle topology")
    result: list[dict[str, Any]] = []
    for offset in range(0, len(indices), 3):
        a, b, c = (vertices[i] for i in indices[offset : offset + 3])
        area = (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])
        if abs(area) <= _EPSILON:
            context.skipped_counts["triangles"] += 1
            continue
        base = common if not result else _clone_common(common, context)
        result.append(
            _emit(
                {
                    **base,
                    "kind": "HATCH",
                    "solid": True,
                    "loops": [{"vertices": [a, b, c], "is_outer": True}],
                },
                context,
            )
        )
    return result


def _convert_text(
    geometry: Mapping[str, Any],
    common: dict[str, Any],
    frame: _Frame,
    context: _ConversionContext,
) -> list[dict[str, Any]]:
    text = str(geometry.get("text", ""))
    font = _mapping(geometry.get("font"))
    family = str(font.get("family") or "")
    height_source = font.get("height_candidate")
    if (
        not isinstance(height_source, (int, float))
        or isinstance(height_source, bool)
        or not math.isfinite(height_source)
        or height_source <= 0.0
    ):
        height_source = _FALLBACK_TEXT_HEIGHT
        context.unresolved_counts["text_height_fallback"] += 1
    height = frame.length(height_source)

    direction = _unit_vector(geometry.get("direction"), (1.0, 0.0))
    up = _unit_vector(geometry.get("up"), (-direction[1], direction[0]))
    rotation = math.degrees(math.atan2(direction[1], direction[0]))
    mirrored = direction[0] * up[1] - direction[1] * up[0] < -_EPSILON
    if mirrored:
        context.mirrored_text_count += 1
        if "metadata" in common:
            common["metadata"]["idw"]["mirrored"] = True

    if family.upper() == "AIGDT" and text in _AIGDT_SYMBOLS:
        text = _AIGDT_SYMBOLS[text]
        context.symbol_mapped_count += 1

    style_name = _register_text_style(font, family, context)
    width_factor = font.get("width_factor")
    extra: dict[str, Any] = {"rotation": rotation, "style": style_name}
    if (
        isinstance(width_factor, (int, float))
        and not isinstance(width_factor, bool)
        and math.isfinite(width_factor)
        and width_factor > 0.0
        and abs(width_factor - 1.0) > 1.0e-9
    ):
        extra["width_factor"] = float(width_factor)

    lines = _LINE_BREAK_RE.split(text)
    if len(lines) > 1:
        context.multiline_text_count += 1
    position = geometry["position"]
    result: list[dict[str, Any]] = []
    for index, line in enumerate(lines):
        if not line.strip():
            continue
        shift = index * _TEXT_LINE_SPACING * height_source
        source_position = (
            float(position[0]) - up[0] * shift,
            float(position[1]) - up[1] * shift,
        )
        base = common if not result else _clone_common(common, context)
        result.append(
            _emit(
                {
                    **base,
                    "kind": "TEXT",
                    "insert": frame.point(source_position),
                    "height": height,
                    "text": line,
                    **extra,
                },
                context,
            )
        )
    if not result:
        context.skipped_counts["text"] += 1
    return result


# ---------------------------------------------------------------------------
# Styles and provenance


def _entity_common(
    item: Any,
    kind: str,
    sheet_index: int,
    context: _ConversionContext,
    frame: _Frame,
) -> dict[str, Any]:
    style = _mapping(getattr(item, "style", None))
    layer_name = str(style.get("layer") or "0")
    context.layers.setdefault(layer_name, {"linetype": "CONTINUOUS", "plot": True})
    linetype = _register_linetype(style.get("dash"), frame, context)
    unresolved = [str(reason) for reason in (style.get("unresolved") or ())]
    context.unresolved_counts.update(unresolved)
    result: dict[str, Any] = {
        "id": context.allocate_id(),
        "layer": layer_name,
        "linetype": linetype,
        "visible": bool(style.get("visible", True)),
    }
    color = _rgba_hex(style.get("rgba"))
    if color is not None:
        result["color"] = color
    width = style.get("width")
    if (
        isinstance(width, (int, float))
        and not isinstance(width, bool)
        and math.isfinite(width)
        and width > 0.0
    ):
        result["lineweight_mm"] = frame.length(width)
    if context.options.entity_provenance:
        source: dict[str, Any] = {"format": "idw", "kind": kind}
        item_id = _text_or_none(getattr(item, "id", None))
        if item_id is not None:
            source["id"] = item_id
        result["source"] = source
        metadata: dict[str, Any] = {"sheet_index": sheet_index}
        for name in ("segment_id", "record_ordinal", "placement_record"):
            value = getattr(item, name, None)
            if value is not None:
                metadata[name] = _json_safe(value)
        if unresolved:
            metadata["unresolved"] = unresolved
        result["metadata"] = {"idw": metadata}
    return result


def _clone_common(
    common: dict[str, Any], context: _ConversionContext
) -> dict[str, Any]:
    clone = dict(common)
    clone["id"] = context.allocate_id()
    if "metadata" in common:
        clone["metadata"] = {"idw": dict(common["metadata"]["idw"])}
    if "source" in common:
        clone["source"] = dict(common["source"])
    return clone


def _emit(entity: dict[str, Any], context: _ConversionContext) -> dict[str, Any]:
    context.converted_counts[str(entity["kind"])] += 1
    return entity


def _register_linetype(dash: Any, frame: _Frame, context: _ConversionContext) -> str:
    values: list[float] = []
    for raw in dash or ():
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            raise ValueError("dash entries must be numbers")
        value = frame.length(raw)
        if value <= 0.0:
            raise ValueError("dash entries must be positive")
        values.append(value)
    context.linetypes.setdefault(
        "CONTINUOUS", {"description": "Continuous", "pattern_mm": []}
    )
    if not values:
        return "CONTINUOUS"
    key = tuple(round(value, 6) for value in values)
    existing = context.linetype_names.get(key)
    if existing is not None:
        return existing
    name = f"IDW_LTYPE_{len(context.linetype_names) + 1:04d}"
    context.linetype_names[key] = name
    context.linetypes[name] = {
        "description": "IDW stored dash pattern (nominal lengths)",
        "pattern_mm": values,
    }
    return name


def _register_text_style(
    font: Mapping[str, Any], family: str, context: _ConversionContext
) -> str:
    weight = font.get("weight_candidate")
    bold = (
        isinstance(weight, (int, float))
        and not isinstance(weight, bool)
        and weight >= 600
    )
    italic = font.get("flags") == 1
    key = (family, bool(bold), bool(italic))
    existing = context.text_style_names.get(key)
    if existing is not None:
        return existing
    stem = _STYLE_NAME_RE.sub("_", family).strip("_") or "DEFAULT"
    name = f"IDW_{stem}"
    if bold:
        name += "_BOLD"
    if italic:
        name += "_ITALIC"
    candidate, suffix = name, 2
    while candidate in context.text_styles:
        candidate = f"{name}_{suffix}"
        suffix += 1
    definition: dict[str, Any] = {}
    if family:
        definition["font"] = family
    if italic:
        definition["oblique_deg"] = 15.0
    context.text_styles[candidate] = definition
    context.text_style_names[key] = candidate
    return candidate


# ---------------------------------------------------------------------------
# Sheets, omissions, diagnostics


def _sheet_available(sheet: Any) -> bool:
    size = getattr(sheet, "size_in_source_units", None)
    if getattr(sheet, "status", None) == "unavailable" or size is None:
        return False
    try:
        width, height = float(size[0]), float(size[1])
    except (TypeError, ValueError, IndexError):
        return False
    return math.isfinite(width) and math.isfinite(height) and width > 0 and height > 0


def _view_is_raster_only(view: Any) -> bool:
    if getattr(view, "image_reference", None) is None:
        return False
    return not tuple(getattr(view, "item_ids", ()) or ())


def _segment_majors(document: Any) -> list[int]:
    majors: set[int] = set()
    metadata = getattr(document, "metadata", None)
    for segment in getattr(metadata, "segments", None) or ():
        major = getattr(segment, "major", None)
        if isinstance(major, int) and not isinstance(major, bool):
            majors.add(major)
    if not majors:
        for diagnostic in getattr(document, "diagnostics", None) or ():
            message = str(_mapping(diagnostic).get("message", ""))
            for match in _MAJOR_RE.findall(message):
                majors.add(int(match))
    return sorted(majors)


def _unsupported_message(document: Any, majors: list[int]) -> str:
    reasons: list[str] = []
    for diagnostic in getattr(document, "diagnostics", None) or ():
        mapping = _mapping(diagnostic)
        if str(mapping.get("code", "")) == "drawing.unsupported_profile":
            message = str(mapping.get("message", ""))
            if message and message not in reasons:
                reasons.append(message)
    label = ", ".join(str(m) for m in majors) if majors else "unknown"
    detail = f" ({'; '.join(reasons[:3])})" if reasons else ""
    return (
        f"unsupported IDW profile: segment major {label}; "
        f"no sheet has a decodable stored display{detail}"
    )


def _collect_omissions(
    sheet: Any, sheet_index: int, context: _ConversionContext
) -> None:
    for omission in getattr(sheet, "omissions", None) or ():
        mapping = _mapping(omission)
        reason = str(mapping.get("reason") or "unknown")
        context.omission_counts[reason] += 1
        if reason not in context.omission_samples:
            sample = {key: _json_safe(value) for key, value in mapping.items()}
            sample["sheet_index"] = sheet_index
            context.omission_samples[reason] = sample


def _match_paper(width_mm: float, height_mm: float) -> str | None:
    short, long = sorted((width_mm, height_mm))
    for name, paper_short, paper_long in _STANDARD_PAPER_SIZES_MM:
        if (
            abs(short - paper_short) <= _PAPER_TOLERANCE * paper_short
            and abs(long - paper_long) <= _PAPER_TOLERANCE * paper_long
        ):
            return name
    return None


def _append_units_diagnostic(
    sheet_records: list[dict[str, Any]],
    paper_matches: list[str | None],
    context: _ConversionContext,
) -> None:
    matched = all(match is not None for match in paper_matches)
    context.diagnostics.append(
        ImportDiagnostic(
            code="IDW_UNITS_ASSUMED_CM",
            severity="info" if matched else "warning",
            message=(
                "IDW coordinates were treated as Inventor internal centimetres and "
                f"scaled by {context.unit_scale:g}; the physical unit is not verified "
                "by the parser."
                + (
                    " Every sheet matches a standard paper size."
                    if matched
                    else " At least one sheet does not match a standard paper size."
                )
            ),
            action="normalized",
            details={
                "unit_scale_to_mm": context.unit_scale,
                "sheets": [
                    {
                        "index": record["index"],
                        "size_mm": record["size_mm"],
                        "paper": record["paper"],
                    }
                    for record in sheet_records
                ],
            },
        )
    )


def _append_item_diagnostics(context: _ConversionContext) -> None:
    if context.out_of_sheet_count:
        context.diagnostics.append(
            ImportDiagnostic(
                code="IDW_OUT_OF_SHEET_DROPPED",
                severity="warning",
                message=(
                    f"Dropped {context.out_of_sheet_count} IDW element(s) lying entirely "
                    "outside the sheet (typically unclipped projected curves)."
                ),
                action="skipped",
                details={
                    "count": context.out_of_sheet_count,
                    "sample_ids": list(context.out_of_sheet_ids),
                },
            )
        )
    edge_on = context.approximation_counts.get("curve_edge_on", 0)
    if edge_on:
        context.diagnostics.append(
            ImportDiagnostic(
                code="IDW_CURVE_EDGE_ON_LINE",
                severity="warning",
                message=(
                    f"Represented {edge_on} edge-on projected circle(s) as line segments."
                ),
                source_kind="curve",
                action="approximated",
                details={"count": edge_on},
            )
        )
    sampled = context.approximation_counts.get("curve", 0)
    if sampled:
        context.diagnostics.append(
            ImportDiagnostic(
                code="IDW_CURVE_APPROXIMATED",
                severity="warning",
                message=f"Sampled {sampled} IDW curve(s) as polylines.",
                source_kind="curve",
                action="approximated",
                details={"count": sampled},
            )
        )
    filled = context.approximation_counts.get("filled_curve", 0)
    if filled:
        context.diagnostics.append(
            ImportDiagnostic(
                code="IDW_FILLED_CURVE_APPROXIMATED",
                severity="warning",
                message=(
                    f"Represented {filled} filled IDW arc(s) as solid hatches with "
                    "sampled polyline loops."
                ),
                source_kind="curve",
                action="approximated",
                details={"count": filled},
            )
        )
    if context.symbol_mapped_count:
        context.diagnostics.append(
            ImportDiagnostic(
                code="IDW_TEXT_SYMBOL_MAPPED",
                severity="info",
                message=(
                    f"Mapped {context.symbol_mapped_count} AIGDT symbol glyph(s) to "
                    "Unicode characters."
                ),
                source_kind="text",
                action="normalized",
                details={
                    "count": context.symbol_mapped_count,
                    "mapping": dict(_AIGDT_SYMBOLS),
                },
            )
        )
    if context.mirrored_text_count:
        context.diagnostics.append(
            ImportDiagnostic(
                code="IDW_TEXT_MIRROR_IGNORED",
                severity="warning",
                message=(
                    f"{context.mirrored_text_count} IDW text item(s) have a mirrored "
                    "up vector; the mirror is recorded in metadata but not applied."
                ),
                source_kind="text",
                action="preserved_metadata",
                details={"count": context.mirrored_text_count},
            )
        )
    if context.multiline_text_count:
        context.diagnostics.append(
            ImportDiagnostic(
                code="IDW_TEXT_MULTILINE_SPLIT",
                severity="info",
                message=(
                    f"Split {context.multiline_text_count} multi-line IDW text item(s) "
                    "into one TEXT entity per line."
                ),
                source_kind="text",
                action="exploded",
                details={
                    "count": context.multiline_text_count,
                    "line_spacing": _TEXT_LINE_SPACING,
                },
            )
        )
    if context.image_references:
        references = sorted(set(context.image_references))
        context.diagnostics.append(
            ImportDiagnostic(
                code="IDW_IMAGE_NOT_IN_IR",
                severity="warning",
                message=(
                    f"{len(context.image_references)} IDW image placement(s) (view "
                    "caches, logos) have no IR representation; placements and image "
                    "descriptors remain in source metadata."
                ),
                source_kind="image",
                action="skipped",
                details={
                    "count": len(context.image_references),
                    "references": references[:50],
                },
            )
        )
    for kind, count in sorted(context.unsupported_counts.items()):
        context.diagnostics.append(
            ImportDiagnostic(
                code="IDW_UNSUPPORTED_ELEMENT",
                severity="warning",
                message=f"Skipped {count} unsupported IDW {kind} element(s).",
                source_kind=kind,
                action="skipped",
                details={"count": count},
            )
        )
    for reason, count in sorted(context.omission_counts.items()):
        hidden = reason == "hidden_by_stored_attribute"
        context.diagnostics.append(
            ImportDiagnostic(
                code="IDW_ELEMENT_OMITTED",
                severity="info" if hidden else "warning",
                message=(
                    f"inventor-kit omitted {count} stored element(s) from the sheet "
                    f"display: {reason}."
                ),
                source_kind=reason,
                action="skipped",
                details={
                    "count": count,
                    "reason": reason,
                    "sample": context.omission_samples.get(reason),
                },
            )
        )
    if context.unresolved_counts:
        context.diagnostics.append(
            ImportDiagnostic(
                code="IDW_STYLE_UNRESOLVED",
                severity="info",
                message=(
                    "Some IDW style attributes were not interpreted by the parser; "
                    "reasons and counts are listed in details."
                ),
                action="preserved_metadata",
                details=dict(sorted(context.unresolved_counts.items())),
            )
        )


def _forward_document_diagnostics(document: Any, context: _ConversionContext) -> None:
    grouped: dict[tuple[str, str, str], int] = {}
    for diagnostic in getattr(document, "diagnostics", None) or ():
        mapping = _mapping(diagnostic)
        code = str(mapping.get("code", "unknown"))
        severity = str(mapping.get("severity", "warning")).lower()
        if severity not in {"info", "warning", "error"}:
            severity = "warning"
        message = str(mapping.get("message", "inventor-kit drawing diagnostic"))
        key = (code, severity, message)
        grouped[key] = grouped.get(key, 0) + 1
    for (code, severity, message), count in grouped.items():
        context.diagnostics.append(
            ImportDiagnostic(
                code="IDW_DRAWING_WARNING",
                severity=severity,  # type: ignore[arg-type]
                message=message,
                action="forwarded",
                details={"upstream_code": code, "count": count},
            )
        )


def _image_records(images: Iterable[Any]) -> list[dict[str, Any]]:
    records: list[dict[str, Any]] = []
    for image in images:
        data = getattr(image, "data", None)
        records.append(
            {
                "reference": _json_safe(getattr(image, "reference", None)),
                "status": _text_or_none(getattr(image, "status", None)),
                "mime_type": _text_or_none(getattr(image, "mime_type", None)),
                "width": _json_safe(getattr(image, "width", None)),
                "height": _json_safe(getattr(image, "height", None)),
                "sha256": _text_or_none(getattr(image, "sha256", None)),
                "bytes": len(data) if data is not None else None,
                "diagnostic": _text_or_none(getattr(image, "diagnostic", None)),
            }
        )
    return records


# ---------------------------------------------------------------------------
# Geometry helpers


def _geometry_bounds(
    kind: str, geometry: Mapping[str, Any]
) -> tuple[float, float, float, float] | None:
    points: list[tuple[float, float]] = []
    if kind == "polyline":
        points = [(float(p[0]), float(p[1])) for p in geometry["points"]]
    elif kind == "curve":
        center = geometry["center"]
        u, v = geometry["u"], geometry["v"]
        start, end = float(geometry["start"]), float(geometry["end"])
        span = end - start
        for index in range(17):
            t = start + span * index / 16
            points.append(
                (
                    float(center[0])
                    + float(u[0]) * math.cos(t)
                    + float(v[0]) * math.sin(t),
                    float(center[1])
                    + float(u[1]) * math.cos(t)
                    + float(v[1]) * math.sin(t),
                )
            )
    elif kind == "triangles":
        points = [(float(p[0]), float(p[1])) for p in geometry["vertices"]]
    elif kind == "text":
        position = geometry["position"]
        points = [(float(position[0]), float(position[1]))]
    if not points:
        return None
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    if not all(math.isfinite(value) for value in (*xs, *ys)):
        raise ValueError("non-finite coordinate")
    return min(xs), min(ys), max(xs), max(ys)


def _touches_sheet(
    bounds: tuple[float, float, float, float], sheet_size: tuple[float, float]
) -> bool:
    width, height = sheet_size
    margin = _OUT_OF_SHEET_MARGIN_RATIO * max(width, height)
    min_x, min_y, max_x, max_y = bounds
    return not (
        max_x < -margin
        or min_x > width + margin
        or max_y < -margin
        or min_y > height + margin
    )


def _unit_vector(value: Any, fallback: tuple[float, float]) -> tuple[float, float]:
    if value is None:
        return fallback
    x, y = float(value[0]), float(value[1])
    length = math.hypot(x, y)
    if not math.isfinite(length) or length <= _EPSILON:
        return fallback
    return x / length, y / length


def _rgba_hex(value: Any) -> str | None:
    if value is None:
        return None
    channels = list(value)
    if len(channels) < 3:
        return None
    parts = []
    for channel in channels[:3]:
        number = float(channel)
        if not math.isfinite(number):
            return None
        parts.append(max(0, min(255, int(round(number * 255.0)))))
    return "#{:02X}{:02X}{:02X}".format(*parts)


def _mapping(value: Any) -> Mapping[str, Any]:
    if value is None:
        return {}
    if isinstance(value, Mapping):
        return value
    if hasattr(value, "__dict__"):
        return vars(value)
    raise TypeError(f"expected a mapping, got {type(value).__name__}")


def _text_or_none(value: Any) -> str | None:
    return None if value is None else str(value)


def _json_safe(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_json_safe(item) for item in value]
    if isinstance(value, (bool, int, float, str)) or value is None:
        return value
    if isinstance(value, (bytes, bytearray)):
        return {"bytes": len(value)}
    return str(value)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _parser_version() -> str | None:
    try:
        from importlib.metadata import PackageNotFoundError, version
    except ImportError:  # pragma: no cover - importlib.metadata exists on 3.8+
        return None
    try:
        return version("inventor-kit")
    except PackageNotFoundError:
        return None
