"""iCAD SX (ICD) 2D drawing area to CAD 2D IR importer backed by :mod:`icadkit`.

An ``.icd`` file stores the 3D model and the 2D drawing of a part or assembly
together. ``icadkit`` reads the saved 2D entities of every 2D view in
view-local millimetres (``Document.read_drawing()``) and, from 0.3.9, the
placement record of each view on the sheet (``View.placement``: origin on the
2D global view and scale). This adapter composes the sheet and maps the
entities to IR without any iCAD process:

* every ``2d_view`` is scaled by its placement scale and translated to its
  placement origin; the ``2d_global`` view (frame, title block) is the sheet
  itself; a view without a placement record is placed at the origin with the
  scale of its header (``ICD_2D_PLACEMENT_MISSING``);
* the sheet is then divided by the dominant view scale (the one with most
  entities) so that the drawing comes out at real size, like an EXB or DXF
  model space; the denominator (``2.0`` for a 1/2 view) is
  ``header.metadata.icd.paper_scale`` (``view_scale`` keeps ``0.5``) and the
  paper (from the global view's extent, or the smallest ISO A sheet that holds
  the geometry) is ``header.metadata.icd.sheet`` (``ICD_2D_SCALE_APPLIED``,
  ``ICD_2D_SHEET_DETECTED``). Views drawn at another scale keep their place
  on the sheet but are not at real size (``ICD_2D_MIXED_SCALES``);
* points, lines, circles and arcs map one to one; a text becomes one ``TEXT``
  per line placed at the saved line origin (vertical writing is split into
  one ``TEXT`` per character, ``ICD_2D_TEXT_VERTICAL_SPLIT``); a dimension
  becomes a ``DIMENSION`` whose ``definition.block`` holds the saved
  arrowheads, lines and value text (``ICD_2D_DIMENSION_FROM_SAVED``); a hatch
  becomes its saved parallel lines (``ICD_2D_HATCH_AS_LINES``);
* the line width and style codes map to provisional millimetre widths and
  dash patterns (``ICD_2D_LINE_WIDTH_ASSUMED``, ``ICD_2D_LINE_STYLE_UNKNOWN``);
  palette colours are kept as metadata only (``ICD_2D_COLOR_PALETTE``);
  record types that icadkit does not decode are counted per type
  (``ICD_2D_TYPE_UNSUPPORTED``).

The 3D parts of the same file are not read here (see ``cad3d-ir-icad``).
"""

from __future__ import annotations

import hashlib
import itertools
import math
from collections import Counter
from dataclasses import dataclass, field
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

_SIGNATURE_HEAD = b"MOD0"
_SIGNATURE_TAIL = b"MOD1"
_SIGNATURE_TAIL_AT = 0xFC
_TAU = 2.0 * math.pi
_EPSILON = 1e-9

# Provisional millimetre widths for the saved line width code (1..8). The codes
# observed in the local corpus are 2 (thin, the default) and 3 (thick: outlines
# and dimension lines); the series follows ISO 128 line widths.
LINE_WIDTH_MM: dict[int, float] = {
    1: 0.13,
    2: 0.25,
    3: 0.35,
    4: 0.5,
    5: 0.7,
    6: 1.0,
    7: 1.4,
    8: 2.0,
}
# Saved line style codes: 1 solid, 2 dashed, 3 chain (centre line), 4 double
# chain. Patterns are millimetres on paper at linetype scale 1.
LINE_STYLES: dict[int, tuple[str, list[float]]] = {
    2: ("DASHED", [3.0, -1.5]),
    3: ("CENTER", [12.0, -2.0, 2.0, -2.0]),
    4: ("PHANTOM", [12.0, -2.0, 2.0, -2.0, 2.0, -2.0]),
}
PAPER_SIZES: dict[str, tuple[float, float]] = {
    "A4": (297.0, 210.0),
    "A3": (420.0, 297.0),
    "A2": (594.0, 420.0),
    "A1": (841.0, 594.0),
    "A0": (1189.0, 841.0),
}
_PAPER_TOLERANCE_MM = 2.0
TEXT_STYLE = "ICD"
DIMENSION_KINDS: dict[str, str] = {
    "length": "LINEAR",
    "diameter": "DIAMETER",
    "radius": "RADIAL",
    "angle": "ANGULAR",
}
_TWO_D_VIEW_KINDS = frozenset({"2d_global", "2d_view", "registered_part"})

Point = tuple[float, float]


@dataclass(frozen=True, slots=True)
class _ViewFrame:
    """How a view's local millimetres reach the IR coordinates."""

    name: str
    kind: str
    origin: Point
    scale: float
    placed: bool

    def point(self, p: Any, divisor: float) -> list[float]:
        x, y = float(p[0]), float(p[1])
        if not (math.isfinite(x) and math.isfinite(y)):
            raise ValueError("coordinate is not finite")
        return [
            (self.origin[0] + x * self.scale) / divisor,
            (self.origin[1] + y * self.scale) / divisor,
        ]

    def length(self, value: Any, divisor: float) -> float:
        number = float(value) * self.scale / divisor
        if not math.isfinite(number):
            raise ValueError("length is not finite")
        return number


@dataclass(slots=True)
class _Context:
    options: ImportOptions
    divisor: float
    entities: list[dict[str, Any]] = field(default_factory=list)
    blocks: dict[str, dict[str, Any]] = field(default_factory=dict)
    layers: dict[str, dict[str, Any]] = field(default_factory=dict)
    linetypes: dict[str, dict[str, Any]] = field(default_factory=dict)
    diagnostics: list[ImportDiagnostic] = field(default_factory=list)
    counts: Counter[str] = field(default_factory=Counter)
    converted: Counter[str] = field(default_factory=Counter)
    unsupported_types: Counter[str] = field(default_factory=Counter)
    unknown_styles: Counter[int] = field(default_factory=Counter)
    width_codes: Counter[int] = field(default_factory=Counter)
    notes: dict[str, ImportDiagnostic] = field(default_factory=dict)
    next_entity: int = 1
    next_block: int = 1

    def allocate_id(self) -> str:
        entity_id = f"ICD_E{self.next_entity:08d}"
        self.next_entity += 1
        return entity_id

    def note(
        self,
        code: str,
        severity: str,
        message: str,
        *,
        action: str | None = None,
        details: dict[str, Any] | None = None,
    ) -> None:
        """Emit a diagnostic once per code and count its occurrences."""
        existing = self.notes.get(code)
        if existing is not None:
            assert existing.details is not None
            existing.details["count"] += 1
            return
        diagnostic = ImportDiagnostic(
            code=code,
            severity=severity,  # type: ignore[arg-type]
            message=message,
            action=action,
            details={"count": 1, **(details or {})},
        )
        self.notes[code] = diagnostic
        self.diagnostics.append(diagnostic)


def is_icd_signature(head: bytes) -> bool:
    """True for the ``MOD0`` … ``MOD1`` 256-byte header of an iCAD SX file."""
    return (
        len(head) >= _SIGNATURE_TAIL_AT + 4
        and head[:4] == _SIGNATURE_HEAD
        and head[_SIGNATURE_TAIL_AT : _SIGNATURE_TAIL_AT + 4] == _SIGNATURE_TAIL
    )


def _backend() -> Any:
    try:
        import icadkit
    except ImportError as exc:
        raise MissingOptionalDependencyError(
            "iCAD SX support requires the optional dependency icadkit; "
            'install it with `pip install "cad2d-ir[icad]"`.'
        ) from exc
    return icadkit


def convert_icad_file_to_ir(
    path: str | Path,
    *,
    options: ImportOptions | None = None,
) -> ImportResult:
    """Read the 2D drawing area of an iCAD SX file with ``icadkit``.

    Install the ``icad`` extra to enable this importer::

        pip install "cad2d-ir[icad]"

    The file must carry the iCAD SX signature; other ``.icd`` files (IEC 61850
    substation descriptions, IronCAD drawings) raise ``ImporterError``, as do
    versions whose view layout icadkit has not qualified.
    """
    icadkit = _backend()
    source_path = Path(path)
    data = source_path.read_bytes()
    if not is_icd_signature(data[: _SIGNATURE_TAIL_AT + 4]):
        raise ImporterError(
            f"Not an iCAD SX file {source_path}: the MOD0/MOD1 header signature is "
            "missing (IEC 61850 and IronCAD also use the .icd suffix)."
        )
    try:
        document = icadkit.read(data)
    except icadkit.IcadError as exc:
        raise ImporterError(
            f"Failed to read iCAD SX file {source_path}: {exc}"
        ) from exc
    return icad_document_to_ir(
        document,
        options=options,
        source_name=source_path.name,
        source_sha256=hashlib.sha256(data).hexdigest(),
    )


def icad_document_to_ir(
    document: Any,
    *,
    options: ImportOptions | None = None,
    source_name: str | None = None,
    source_sha256: str | None = None,
) -> ImportResult:
    """Convert an ``icadkit.Document`` (its 2D views) to IR."""
    icadkit = _backend()
    import_options = options or ImportOptions()
    header = document.header
    label = getattr(header, "version_label", None)
    byte_order = str(getattr(header, "byte_order", "little"))
    try:
        views = document.read_views()
    except icadkit.IcadError as exc:
        raise ImporterError(
            f"Failed to read the views of {source_name}: {exc}"
        ) from exc
    if any(d.code == "views.profile" for d in views.diagnostics):
        raise ImporterError(
            f"Unqualified iCAD SX version {label or 'of unknown layout'} "
            f"({byte_order}-endian) in {source_name}: icadkit has not qualified "
            "its view layout, so the 2D drawing cannot be read."
        )
    try:
        drawing = document.read_drawing()
    except icadkit.IcadError as exc:
        raise ImporterError(
            f"Failed to read the 2D entities of {source_name}: {exc}"
        ) from exc

    frames, by_view, missing, tiled = _frames(views, drawing)
    divisor, dominant, other_scales = _document_scale(frames, by_view)
    context = _Context(options=import_options, divisor=divisor)
    context.note(
        "ICD_2D_UNITS_ASSUMED_MM",
        "info",
        "iCAD SX stores view-local coordinates in millimetres; the sheet was "
        "composed in millimetres.",
        action="assumed",
    )
    placed = [f for f in frames.values() if f.kind == "2d_view" and f.placed]
    if placed:
        context.note(
            "ICD_2D_VIEW_PLACED",
            "info",
            f"{len(placed)} 2D view(s) were placed on the sheet by their saved "
            "placement records (origin and scale).",
            action="normalized",
            details={
                "views": [
                    {"name": f.name, "origin": list(f.origin), "scale": f.scale}
                    for f in placed
                ]
            },
        )
    for frame in missing:
        context.note(
            "ICD_2D_PLACEMENT_MISSING",
            "warning",
            "A 2D view has no placement record; the unplaced views overlapped in "
            "their own coordinates and were laid out side by side by their saved "
            "extents."
            if tiled
            else "A 2D view has no placement record; it keeps its own coordinates "
            "with the scale of its header.",
            action="approximated",
            details={"views": [frame.name], "layout": "tiled" if tiled else "kept"},
        )
        existing = context.notes["ICD_2D_PLACEMENT_MISSING"].details
        assert existing is not None
        if frame.name not in existing["views"]:
            existing["views"].append(frame.name)
    if str(views.document_kind) == "registered_part":
        context.note(
            "ICD_2D_REGISTERED_PART",
            "info",
            "The file is a registered part (a 2D library symbol); its view was "
            "read as the drawing at scale 1.",
            action="normalized",
        )
    if divisor != 1.0:
        context.note(
            "ICD_2D_SCALE_APPLIED",
            "info",
            f"The sheet was divided by the dominant view scale {dominant:g} so the "
            "drawing is at real size; the denominator is "
            "header.metadata.icd.paper_scale.",
            action="normalized",
            details={"view_scale": dominant, "paper_scale": 1.0 / divisor},
        )
    if other_scales:
        context.note(
            "ICD_2D_MIXED_SCALES",
            "warning",
            "Views drawn at a scale other than the dominant one keep their place on "
            "the sheet but are not at real size.",
            action="approximated",
            details={"scales": sorted(other_scales)},
        )

    for entity in drawing.entities:
        frame = frames.get(entity.view_id)
        if frame is None:
            continue
        try:
            _convert_entity(entity, frame, context)
        except ValueError as exc:
            context.unsupported_types[str(entity.raw_type)] += 0
            context.counts["invalid"] += 1
            context.note(
                "ICD_2D_ENTITY_INVALID",
                "warning",
                f"An entity could not be converted and was skipped: {exc}",
                action="skipped",
            )

    if context.unsupported_types:
        types = {k: v for k, v in sorted(context.unsupported_types.items()) if v}
        if types:
            context.diagnostics.append(
                ImportDiagnostic(
                    code="ICD_2D_TYPE_UNSUPPORTED",
                    severity="warning",
                    message=(
                        f"{sum(types.values())} saved records of types icadkit does "
                        "not decode (symbols, projection controls, legacy layouts) "
                        "were skipped."
                    ),
                    action="skipped",
                    details={"count": sum(types.values()), "types": types},
                )
            )
    if context.width_codes:
        context.diagnostics.append(
            ImportDiagnostic(
                code="ICD_2D_LINE_WIDTH_ASSUMED",
                severity="info",
                message=(
                    "Line width codes were mapped to provisional millimetre widths "
                    "(code 2 = 0.25 mm, 3 = 0.35 mm, …)."
                ),
                action="assumed",
                details={
                    "count": sum(context.width_codes.values()),
                    "codes": {
                        str(k): v for k, v in sorted(context.width_codes.items())
                    },
                },
            )
        )
    if context.unknown_styles:
        context.diagnostics.append(
            ImportDiagnostic(
                code="ICD_2D_LINE_STYLE_UNKNOWN",
                severity="warning",
                message=(
                    "Entities with an unobserved line style code were drawn as "
                    "continuous lines."
                ),
                action="approximated",
                details={
                    "count": sum(context.unknown_styles.values()),
                    "codes": {
                        str(k): v for k, v in sorted(context.unknown_styles.items())
                    },
                },
            )
        )
    if context.counts["palette"]:
        context.diagnostics.append(
            ImportDiagnostic(
                code="ICD_2D_COLOR_PALETTE",
                severity="info",
                message=(
                    "Colours are iCAD SX palette indices (kept in metadata.icd) and "
                    "were not mapped to RGB."
                ),
                action="skipped",
                details={"count": context.counts["palette"]},
            )
        )

    sheet, sheet_source = _sheet(views, frames, context.entities, divisor)
    if sheet is not None:
        context.diagnostics.append(
            ImportDiagnostic(
                code="ICD_2D_SHEET_DETECTED",
                severity="info",
                message=(
                    f"The sheet is {sheet['paper']} {sheet['orientation']} "
                    f"({sheet['width_mm']:g}×{sheet['height_mm']:g} mm), from "
                    f"{sheet_source}."
                ),
                action="detected",
                details={"count": 1, **sheet, "source": sheet_source},
            )
        )
    two_d_views = [f for f in frames.values()]
    if not two_d_views:
        context.diagnostics.append(
            ImportDiagnostic(
                code="ICD_2D_NO_VIEWS",
                severity="warning",
                message="The file has no 2D view; it holds a 3D model only.",
                action="skipped",
                details={"count": 1},
            )
        )

    source: dict[str, Any] = {"format": "icd"}
    if label:
        source["version"] = f"iCAD SX {label}"
    if source_name is not None:
        source["name"] = source_name
    if source_sha256 is not None:
        source["sha256"] = source_sha256
    header_metadata: dict[str, Any] = {
        "version": label,
        "byte_order": byte_order,
        "document_kind": str(views.document_kind),
        "units_assumed": True,
        "coordinates": "real" if divisor != 1.0 else "sheet",
        "view_scale": dominant,
        "paper_scale": 1.0 / divisor,
        "sheet": sheet,
        "views": [
            {
                "name": f.name,
                "kind": f.kind,
                "scale": f.scale,
                "origin": list(f.origin),
                "placed": f.placed,
                "entities": by_view.get(f.name, 0),
            }
            for f in frames.values()
        ],
    }
    tables: dict[str, Any] = {
        "layers": context.layers,
        "linetypes": context.linetypes,
        "text_styles": {TEXT_STYLE: {"font": "msgothic.ttc"}},
    }
    if context.blocks:
        tables["blocks"] = context.blocks
    doc: dict[str, Any] = {
        "format": "cad2d-ir",
        "version": import_options.ir_version,
        "source": source,
        "header": {
            "units": "mm",
            "angle_unit": "deg",
            "coord_space": "world",
            "metadata": {"icd": header_metadata},
        },
        "tables": tables,
        "entities": context.entities,
        "layouts": [],
    }
    skipped = (
        sum(v for v in context.unsupported_types.values()) + context.counts["invalid"]
    )
    stats: dict[str, Any] = {
        "source_format": "icd",
        "version": label,
        "document_kind": str(views.document_kind),
        "completeness": "complete" if not skipped else "partial",
        "views_2d": len([f for f in frames.values() if f.kind != "2d_global"]),
        "entities_2d": len([e for e in drawing.entities if e.view_id in frames]),
        "converted_entities": len(context.entities),
        "converted_entity_counts": dict(sorted(context.converted.items())),
        "skipped_entities": skipped,
        "unsupported_types": dict(sorted(context.unsupported_types.items())),
        "dimensions": context.counts["dimension"],
        "hatches": context.counts["hatch"],
        "texts": context.counts["text"],
        "ellipses": context.counts["ellipse"],
        "curves": context.counts["curve"],
        "notes": context.counts["note"],
        "symbols": context.counts["symbol"],
        "attribute_records": context.counts["attribute"],
        "paper_scale": 1.0 / divisor,
    }
    source["metadata"] = {
        "icd": {
            **stats,
            "diagnostics": [d.as_dict() for d in context.diagnostics],
        }
    }
    if import_options.validate:
        validate_ir(doc)
    return ImportResult(doc, context.diagnostics, stats)


# ---------------------------------------------------------------------------
# Sheet composition


def _boxes_overlap(
    a: tuple[float, float, float, float], b: tuple[float, float, float, float]
) -> bool:
    # touching boxes (a view of horizontal lines has no height) count as overlap
    return not (a[2] < b[0] or b[2] < a[0] or a[3] < b[1] or b[3] < a[1])


def _frames(
    views: Any, drawing: Any
) -> tuple[dict[str, _ViewFrame], dict[str, int], list[_ViewFrame], bool]:
    """One frame per 2D view keyed by view id, entity counts, the unplaced views
    and whether those were tiled."""
    counts: Counter[str] = Counter()
    for entity in drawing.entities:
        counts[entity.view_id] += 1
    frames: dict[str, _ViewFrame] = {}
    missing: list[_ViewFrame] = []
    extents: dict[str, Any] = {}
    by_name: dict[str, int] = {}
    for view in views.views:
        kind = str(view.kind)
        if kind not in _TWO_D_VIEW_KINDS:
            continue
        name = (view.name or "").strip() or view.view_id
        if kind == "2d_view":
            placement = getattr(view, "placement", None)
            scale = None if placement is None else placement.scale
            if (
                placement is not None
                and placement.status != "invalid"
                and scale is not None
                and math.isfinite(scale)
                and scale > 0
                and all(math.isfinite(v) for v in placement.origin)
            ):
                frame = _ViewFrame(
                    name,
                    kind,
                    (float(placement.origin[0]), float(placement.origin[1])),
                    float(scale),
                    True,
                )
            else:
                header_scale = view.scale if view.scale and view.scale > 0 else 1.0
                frame = _ViewFrame(name, kind, (0.0, 0.0), float(header_scale), False)
                if counts[view.view_id]:
                    missing.append(frame)
                    extents[view.view_id] = getattr(view, "extent", None)
        else:
            frame = _ViewFrame(name, kind, (0.0, 0.0), 1.0, kind == "2d_global")
        frames[view.view_id] = frame
        by_name[name] = counts[view.view_id]
    # Several views without a placement record keep their own coordinates
    # (old drawings save the sheet layout there); only when their saved
    # extents overlap are they laid out left to right by those extents
    boxes: dict[str, tuple[float, float, float, float]] = {}
    if len(missing) >= 2:
        for view_id, frame in frames.items():
            if frame not in missing:
                continue
            extent = extents.get(view_id)
            box = (
                getattr(extent, "min_x", None),
                getattr(extent, "min_y", None),
                getattr(extent, "max_x", None),
                getattr(extent, "max_y", None),
            )
            if extent is None or any(v is None for v in box):
                continue
            values = tuple(float(v) * frame.scale for v in box)
            if all(math.isfinite(v) for v in values):
                boxes[view_id] = (values[0], values[1], values[2], values[3])
    ids = list(boxes)
    tiled = any(
        _boxes_overlap(boxes[a], boxes[b])
        for i, a in enumerate(ids)
        for b in ids[i + 1 :]
    )
    if tiled:
        cursor = 0.0
        for view_id, frame in list(frames.items()):
            box_scaled = boxes.get(view_id)
            if frame not in missing or box_scaled is None:
                continue
            minx, miny, maxx, _maxy = box_scaled
            width = max(maxx - minx, 0.0)
            placed = _ViewFrame(
                frame.name, frame.kind, (cursor - minx, -miny), frame.scale, False
            )
            frames[view_id] = placed
            missing[missing.index(frame)] = placed
            cursor += width + max(10.0, 0.1 * width)
    return frames, by_name, missing, tiled


def _document_scale(
    frames: dict[str, _ViewFrame], by_name: dict[str, int]
) -> tuple[float, float, set[float]]:
    """(divisor, dominant view scale, other scales) for real-size output.

    The dominant scale is the one carried by most entities among the placed
    ``2d_view`` frames; the divisor is that scale (sheet mm → real mm). With no
    such view the sheet stays in millimetres (divisor 1).
    """
    votes: Counter[float] = Counter()
    for frame in frames.values():
        if frame.kind != "2d_view":
            continue
        votes[frame.scale] += by_name.get(frame.name, 0)
    if not votes or not any(votes.values()):
        return 1.0, 1.0, set()
    dominant, _ = max(votes.items(), key=lambda kv: (kv[1], -kv[0]))
    others = {s for s, n in votes.items() if n and not math.isclose(s, dominant)}
    if math.isclose(dominant, 1.0):
        return 1.0, 1.0, others
    return dominant, dominant, others


def _sheet(
    views: Any,
    frames: dict[str, _ViewFrame],
    entities: list[dict[str, Any]],
    divisor: float,
) -> tuple[dict[str, Any] | None, str]:
    """The paper, from the 2D global view's extent or the geometry bounds."""
    extent = None
    for view in views.views:
        if str(view.kind) == "2d_global" and view.extent is not None:
            e = view.extent
            if e.max_x > e.min_x and e.max_y > e.min_y:
                extent = (e.min_x, e.min_y, e.max_x, e.max_y)
            break
    if extent is not None:
        width, height = extent[2] - extent[0], extent[3] - extent[1]
        paper, orientation = _paper_name(width, height)
        source = "the extent of the 2D global view"
        if paper == "FREE":
            # The saved extent is the frame drawn on the sheet, inset from the paper
            # edge; take the smallest ISO A sheet that holds it, concentric with it.
            fitted = _fit_paper(width, height)
            if fitted is not None:
                paper, (pw, ph) = fitted
                orientation = "landscape" if pw >= ph else "portrait"
                cx, cy = (extent[0] + extent[2]) / 2.0, (extent[1] + extent[3]) / 2.0
                extent = (cx - pw / 2.0, cy - ph / 2.0, cx + pw / 2.0, cy + ph / 2.0)
                width, height = pw, ph
                source = (
                    "the frame extent of the 2D global view (paper fitted around it)"
                )
        return (
            {
                "paper": paper,
                "orientation": orientation,
                "width_mm": round(width, 3),
                "height_mm": round(height, 3),
                "extent": [round(v / divisor, 6) for v in extent],
            },
            source,
        )
    bounds = _bounds(entities)
    if bounds is None:
        return None, ""
    min_x, min_y, max_x, max_y = (v * divisor for v in bounds)
    width, height = max_x - min_x, max_y - min_y
    fitted = _fit_paper(width, height)
    paper, size = fitted if fitted is not None else ("FREE", (width, height))
    cx, cy = (min_x + max_x) / 2.0, (min_y + max_y) / 2.0
    sheet_extent = (
        cx - size[0] / 2.0,
        cy - size[1] / 2.0,
        cx + size[0] / 2.0,
        cy + size[1] / 2.0,
    )
    return (
        {
            "paper": paper,
            "orientation": "landscape" if size[0] >= size[1] else "portrait",
            "width_mm": round(size[0], 3),
            "height_mm": round(size[1], 3),
            "extent": [round(v / divisor, 6) for v in sheet_extent],
        },
        "the smallest ISO A sheet that holds the geometry",
    )


def _fit_paper(width: float, height: float) -> tuple[str, tuple[float, float]] | None:
    """The smallest ISO A sheet (either orientation) that holds ``width`` × ``height``."""
    for name, (pw, ph) in PAPER_SIZES.items():
        if width <= pw and height <= ph:
            return name, (pw, ph)
        if width <= ph and height <= pw:
            return name, (ph, pw)
    return None


def _paper_name(width: float, height: float) -> tuple[str, str]:
    for name, (pw, ph) in PAPER_SIZES.items():
        if (
            abs(width - pw) <= _PAPER_TOLERANCE_MM
            and abs(height - ph) <= _PAPER_TOLERANCE_MM
        ):
            return name, "landscape"
        if (
            abs(width - ph) <= _PAPER_TOLERANCE_MM
            and abs(height - pw) <= _PAPER_TOLERANCE_MM
        ):
            return name, "portrait"
    return "FREE", "landscape" if width >= height else "portrait"


def _bounds(entities: list[dict[str, Any]]) -> tuple[float, float, float, float] | None:
    xs: list[float] = []
    ys: list[float] = []

    def add(p: Any) -> None:
        xs.append(float(p[0]))
        ys.append(float(p[1]))

    for entity in entities:
        kind = entity.get("kind")
        if kind == "LINE":
            add(entity["p1"])
            add(entity["p2"])
        elif kind in ("CIRCLE", "ARC"):
            cx, cy = entity["center"]
            r = entity["radius"]
            xs.extend((cx - r, cx + r))
            ys.extend((cy - r, cy + r))
        elif kind == "POINT":
            add(entity["position"])
        elif kind == "TEXT":
            add(entity["insert"])
        elif kind == "HATCH":
            for loop in entity["loops"]:
                for vertex in loop["vertices"]:
                    add(vertex)
    if not xs:
        return None
    return min(xs), min(ys), max(xs), max(ys)


# ---------------------------------------------------------------------------
# Entities


def _convert_entity(entity: Any, frame: _ViewFrame, context: _Context) -> None:
    if entity.status == "invalid":
        context.counts["invalid"] += 1
        context.note(
            "ICD_2D_ENTITY_INVALID",
            "warning",
            "A saved entity with invalid geometry was skipped.",
            action="skipped",
        )
        return
    if entity.dimension is not None:
        _convert_dimension(entity, frame, context)
        return
    if entity.hatch is not None:
        _convert_hatch(entity, frame, context)
        return
    if entity.text is not None:
        _convert_text(entity, entity.text, frame, context, None)
        underline = getattr(entity.text, "underline", None)
        if underline is not None:
            # view labels ("A-A") are underlined: one LINE below the text
            p1, p2 = (frame.point(p, context.divisor) for p in underline)
            if math.dist(p1, p2) > _EPSILON:
                context.entities.append(
                    {
                        **_common(entity, frame, context),
                        "id": context.allocate_id(),
                        "kind": "LINE",
                        "p1": p1,
                        "p2": p2,
                    }
                )
                context.converted["LINE"] += 1
        return
    if getattr(entity, "curve", None) is not None:
        _convert_curve(entity, frame, context)
        return
    if getattr(entity, "note", None) is not None:
        _convert_note(entity, frame, context)
        return
    if getattr(entity, "symbol", None) is not None:
        _convert_symbol(entity, frame, context)
        return
    primitive = entity.primitive
    if primitive is None:
        if any(d.code == "drawing.attribute_record" for d in entity.diagnostics):
            context.counts["attribute"] += 1
            context.note(
                "ICD_2D_ATTRIBUTE_RECORDS",
                "info",
                "Attribute records (line information, motion verification, 3D "
                "projection frames) carry no drawing geometry and were not drawn.",
                action="skipped",
            )
            return
        context.unsupported_types[str(entity.raw_type)] += 1
        return
    common = _common(entity, frame, context)
    kind = primitive.kind
    if kind == "point":
        context.entities.append(
            {
                **common,
                "kind": "POINT",
                "position": frame.point(primitive.points[0], context.divisor),
            }
        )
    elif kind == "line":
        context.entities.append(
            {
                **common,
                "kind": "LINE",
                "p1": frame.point(primitive.points[0], context.divisor),
                "p2": frame.point(primitive.points[1], context.divisor),
            }
        )
    elif kind == "circle":
        context.entities.append(
            {
                **common,
                "kind": "CIRCLE",
                "center": frame.point(primitive.center, context.divisor),
                "radius": _positive(frame.length(primitive.radius, context.divisor)),
            }
        )
    elif kind == "arc":
        context.entities.append(
            {
                **common,
                **_arc_fields(
                    frame.point(primitive.center, context.divisor),
                    frame.length(primitive.radius, context.divisor),
                    primitive.start_angle,
                    primitive.sweep_angle,
                ),
            }
        )
    elif kind == "vectors":
        # two segments from a point (view-direction and section marks)
        ends = [frame.point(p, context.divisor) for p in primitive.points]
        base = {k: v for k, v in common.items() if k != "id"}
        for end in ends[1:]:
            if math.dist(ends[0], end) <= _EPSILON:
                continue
            context.entities.append(
                {
                    **base,
                    "id": context.allocate_id(),
                    "kind": "LINE",
                    "p1": ends[0],
                    "p2": end,
                }
            )
            context.converted["LINE"] += 1
        context.counts["vectors"] += 1
        return
    elif kind in ("ellipse", "elliptical_arc"):
        context.entities.append(
            {**common, **_ellipse_fields(primitive, frame, context)}
        )
        context.counts["ellipse"] += 1
    else:
        context.unsupported_types[str(entity.raw_type)] += 1
        return
    context.converted[context.entities[-1]["kind"]] += 1


def _ellipse_fields(
    primitive: Any, frame: _ViewFrame, context: _Context
) -> dict[str, Any]:
    """An ELLIPSE from the saved semi-axes, axis rotation and parametric angles
    (the DXF convention). The major axis is the longer one; when the saved y
    axis is longer, the axes are swapped and the parameters shift by a quarter
    turn. Negative sweeps are written counter-clockwise from their end."""
    a = _positive(frame.length(primitive.radius, context.divisor))
    b = _positive(frame.length(primitive.minor_radius, context.divisor))
    rotation = float(primitive.rotation or 0.0)
    start = float(primitive.start_angle or 0.0)
    sweep = float(primitive.sweep_angle or 0.0)
    if not all(math.isfinite(v) for v in (rotation, start, sweep)):
        raise ValueError("ellipse angles are not finite")
    if b > a:
        a, b = b, a
        rotation += math.pi / 2
        start -= math.pi / 2
    if sweep < 0:
        start, sweep = start + sweep, -sweep
    if sweep >= _TAU - _EPSILON:
        start, sweep = 0.0, _TAU
    return {
        "kind": "ELLIPSE",
        "center": frame.point(primitive.center, context.divisor),
        "major_axis": [a * math.cos(rotation), a * math.sin(rotation)],
        "ratio": min(b / a, 1.0),
        "start_param": start,
        "end_param": start + sweep,
    }


_CURVE_SAMPLES = 6


def _smooth_through(points: list[list[float]], samples: int) -> list[list[float]]:
    """A C1 curve through ``points`` (centripetal Catmull-Rom, ``samples``
    vertices per segment) as a polyline; the end segments use their chord
    tangent."""
    if len(points) < 3:
        return points
    out: list[list[float]] = [points[0]]
    extended = [points[0]] + points + [points[-1]]
    for i in range(1, len(extended) - 2):
        p0, p1, p2, p3 = extended[i - 1], extended[i], extended[i + 1], extended[i + 2]
        m1 = [(p2[0] - p0[0]) / 2.0, (p2[1] - p0[1]) / 2.0]
        m2 = [(p3[0] - p1[0]) / 2.0, (p3[1] - p1[1]) / 2.0]
        for k in range(1, samples + 1):
            t = k / samples
            h00 = 2 * t**3 - 3 * t**2 + 1
            h10 = t**3 - 2 * t**2 + t
            h01 = -2 * t**3 + 3 * t**2
            h11 = t**3 - t**2
            out.append(
                [
                    h00 * p1[0] + h10 * m1[0] + h01 * p2[0] + h11 * m2[0],
                    h00 * p1[1] + h10 * m1[1] + h01 * p2[1] + h11 * m2[1],
                ]
            )
    return out


def _convert_curve(entity: Any, frame: _ViewFrame, context: _Context) -> None:
    """A free curve as an LWPOLYLINE: a smooth interpolation through the saved
    knots (iCAD's own segment coefficients are not decoded)."""
    curve = entity.curve
    knots = [frame.point(k, context.divisor) for k in curve.knots]
    if len(knots) < 2:
        context.counts["invalid"] += 1
        return
    common = _common(entity, frame, context)
    vertices = _smooth_through(knots, _CURVE_SAMPLES)
    context.entities.append(
        {**common, "kind": "LWPOLYLINE", "vertices": vertices, "closed": False}
    )
    context.converted["LWPOLYLINE"] += 1
    context.counts["curve"] += 1
    context.note(
        "ICD_2D_CURVE_AS_POLYLINE",
        "info",
        "Free curves were drawn as polylines interpolated through the knots iCAD SX "
        "saved; the segment coefficients of the smooth curve are not decoded.",
        action="approximated",
    )


def _convert_note(entity: Any, frame: _ViewFrame, context: _Context) -> None:
    """A leader note as its text lines, a solid arrowhead and the leader lines."""
    note = entity.note
    common = _common(entity, frame, context)
    for run in note.texts or ((note.text,) if note.text is not None else ()):
        _convert_text(entity, run, frame, context, None, base=common)
    base = {k: v for k, v in common.items() if k != "id"}
    if note.arrow is not None:
        context.entities.append(
            {
                **base,
                "id": context.allocate_id(),
                "kind": "HATCH",
                "solid": True,
                "loops": [
                    {"vertices": [frame.point(p, context.divisor) for p in note.arrow]}
                ],
            }
        )
        context.converted["HATCH"] += 1
    circle = getattr(note, "circle", None)
    if circle is not None:
        context.entities.append(
            {
                **base,
                "id": context.allocate_id(),
                "kind": "CIRCLE",
                "center": frame.point(circle[0], context.divisor),
                "radius": _positive(frame.length(circle[1], context.divisor)),
            }
        )
        context.converted["CIRCLE"] += 1
    path = [frame.point(p, context.divisor) for p in note.path]
    for p1, p2 in itertools.pairwise(path):
        if math.dist(p1, p2) <= _EPSILON:
            continue
        context.entities.append(
            {**base, "id": context.allocate_id(), "kind": "LINE", "p1": p1, "p2": p2}
        )
        context.converted["LINE"] += 1
    context.counts["note"] += 1
    context.note(
        "ICD_2D_LEADER_AS_LINES",
        "info",
        "Leader notes were drawn as their text, a solid arrowhead and the leader "
        "lines (there is no LEADER entity); hidden alternative items were not drawn.",
        action="flattened",
    )


def _convert_symbol(entity: Any, frame: _ViewFrame, context: _Context) -> None:
    """A symbol record: its drawn point markers, lines, arcs and vectors, and its
    text runs. Hidden items and the member lists of groups carry no geometry."""
    symbol = entity.symbol
    common = _common(entity, frame, context)
    name = getattr(symbol, "name", None)
    if name and "metadata" in common:
        common["metadata"]["icd"]["symbol"] = name
    base = {k: v for k, v in common.items() if k != "id"}
    drawn = 0
    for item in symbol.items:
        if not item.raw_marker & 0x40:
            continue
        points = [frame.point(p, context.divisor) for p in item.points]
        if item.kind == "point" and points:
            context.entities.append(
                {
                    **base,
                    "id": context.allocate_id(),
                    "kind": "POINT",
                    "position": points[0],
                }
            )
        elif item.kind == "line" and len(points) >= 2:
            context.entities.append(
                {
                    **base,
                    "id": context.allocate_id(),
                    "kind": "LINE",
                    "p1": points[0],
                    "p2": points[1],
                }
            )
        elif item.kind == "arc" and item.radius:
            context.entities.append(
                {
                    **base,
                    "id": context.allocate_id(),
                    **_arc_fields(
                        points[0],
                        frame.length(item.radius, context.divisor),
                        item.start_angle,
                        item.sweep_angle,
                    ),
                }
            )
        elif item.kind == "vectors" and len(points) >= 2:
            # a point and the vectors from it (the strokes of a symbol)
            for end in points[1:]:
                if math.dist(points[0], end) <= _EPSILON:
                    continue
                context.entities.append(
                    {
                        **base,
                        "id": context.allocate_id(),
                        "kind": "LINE",
                        "p1": points[0],
                        "p2": end,
                    }
                )
                context.converted["LINE"] += 1
                drawn += 1
            continue
        else:
            continue
        context.converted[context.entities[-1]["kind"]] += 1
        drawn += 1
    for run in symbol.texts:
        drawn += len(_convert_text(entity, run, frame, context, None, base=common))
    context.counts["symbol"] += 1
    if drawn:
        context.note(
            "ICD_2D_SYMBOL_ITEMS",
            "info",
            "Symbols were drawn from their saved items (point markers, lines, arcs) "
            "and text runs; the member lists of groups carry no geometry.",
            action="flattened",
        )


def _arc_fields(
    center: list[float], radius: float, start: float | None, sweep: float | None
) -> dict[str, Any]:
    radius = _positive(radius)
    start = float(start or 0.0)
    sweep = float(sweep or 0.0)
    if not (math.isfinite(start) and math.isfinite(sweep)):
        raise ValueError("arc angles are not finite")
    if abs(sweep) >= _TAU - _EPSILON:
        return {"kind": "CIRCLE", "center": center, "radius": radius}
    if sweep >= 0:
        a0, a1 = start, start + sweep
    else:
        a0, a1 = start + sweep, start
    return {
        "kind": "ARC",
        "center": center,
        "radius": radius,
        "start_angle": math.degrees(a0) % 360.0,
        "end_angle": math.degrees(a1) % 360.0,
    }


def _positive(value: float) -> float:
    if not (math.isfinite(value) and value > 0):
        raise ValueError("radius must be positive")
    return value


def _common(
    entity: Any, frame: _ViewFrame, context: _Context, *, layer: str | None = None
) -> dict[str, Any]:
    """The shared fields: id, layer, width, linetype, visibility, provenance."""
    layer_name = layer if layer is not None else _layer_name(entity.layer)
    context.layers.setdefault(layer_name, {})
    result: dict[str, Any] = {"id": context.allocate_id(), "layer": layer_name}
    width = entity.line_width
    if isinstance(width, int) and width > 8 and (width & 0x0F) in LINE_WIDTH_MM:
        # Lines projected from the 3D model carry a flag bit (0x10) above the
        # width index in V8 documents (18 = thin, 19 = thick).
        width &= 0x0F
    if isinstance(width, int) and width in LINE_WIDTH_MM:
        result["lineweight_mm"] = LINE_WIDTH_MM[width]
        context.width_codes[width] += 1
    style = entity.line_style
    if isinstance(style, int) and style in LINE_STYLES:
        name, pattern = LINE_STYLES[style]
        context.linetypes.setdefault(name, {"pattern_mm": list(pattern)})
        result["linetype"] = name
    elif isinstance(style, int) and style not in (0, 1):
        context.unknown_styles[style] += 1
    if entity.visible is False:
        result["visible"] = False
    if isinstance(entity.color_index, int):
        context.counts["palette"] += 1
    metadata = {
        "icd": {
            "view": frame.name,
            "layer": entity.layer,
            "line_width": entity.line_width,
            "line_style": entity.line_style,
            "color_index": entity.color_index,
            "raw_type": entity.raw_type,
        }
    }
    if context.options.entity_provenance:
        result["metadata"] = metadata
        result["source"] = {
            "format": "icd",
            "id": str(entity.entity_id),
            "kind": f"ICD_TYPE_{entity.raw_type}",
            "metadata": {
                "view": frame.name,
                "byte_range": [entity.byte_range.start, entity.byte_range.end],
                "source_id": entity.source_id,
            },
        }
    return result


def _layer_name(layer: Any) -> str:
    return str(layer) if isinstance(layer, int) else "0"


def _convert_text(
    entity: Any,
    text: Any,
    frame: _ViewFrame,
    context: _Context,
    target: list[dict[str, Any]] | None,
    *,
    base: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """One TEXT per line (per character for vertical writing)."""
    lines = [str(line) for line in text.lines]
    origins = list(text.line_origins or ())
    if not lines or not origins:
        context.note(
            "ICD_2D_TEXT_UNPLACED",
            "warning",
            "A text run has no saved line origin and was skipped.",
            action="skipped",
        )
        return []
    height = text.height
    if not (isinstance(height, (int, float)) and math.isfinite(height) and height > 0):
        height = 3.5 / frame.scale
    if text.layout_status not in ("complete",):
        context.note(
            "ICD_2D_TEXT_LAYOUT_PARTIAL",
            "info",
            "A text with a partially qualified layout uses the saved origin and "
            "default metrics.",
            action="approximated",
        )
    pitch = text.char_pitch
    width_factor = None
    if isinstance(pitch, (int, float)) and math.isfinite(pitch) and pitch > 0:
        width_factor = max(0.2, min(3.0, float(pitch) / float(height)))
    rotation = float(text.rotation or 0.0)
    vertical = text.direction == "vertical"
    out: list[dict[str, Any]] = []
    common = base if base is not None else _common(entity, frame, context)
    for index, line in enumerate(lines):
        if not line:
            continue
        origin = origins[min(index, len(origins) - 1)]
        if vertical:
            context.note(
                "ICD_2D_TEXT_VERTICAL_SPLIT",
                "info",
                "Vertical writing was split into one TEXT per character stacked "
                "downwards.",
                action="approximated",
            )
            step = float(pitch) if width_factor is not None else float(height)
            for k, ch in enumerate(line):
                point = (float(origin[0]), float(origin[1]) - k * step)
                out.append(
                    _text_entity(
                        common,
                        context,
                        frame.point(point, context.divisor),
                        frame.length(height, context.divisor),
                        ch,
                        rotation,
                        width_factor,
                    )
                )
            continue
        out.append(
            _text_entity(
                common,
                context,
                frame.point(origin, context.divisor),
                frame.length(height, context.divisor),
                line,
                rotation,
                width_factor,
            )
        )
    destination = context.entities if target is None else target
    for item in out:
        destination.append(item)
        if target is None:
            context.converted["TEXT"] += 1
            context.counts["text"] += 1
    return out


def _text_entity(
    common: dict[str, Any],
    context: _Context,
    insert: list[float],
    height: float,
    text: str,
    rotation: float,
    width_factor: float | None,
) -> dict[str, Any]:
    private = sum(1 for ch in text if 0xE000 <= ord(ch) <= 0xF8FF)
    if private:
        context.counts["private_use"] += private
        context.note(
            "ICD_2D_TEXT_PRIVATE_USE",
            "warning",
            "Texts contain characters of the private-use area (iCAD SX's own "
            "symbol glyphs); they are kept as saved and most fonts show nothing "
            "for them.",
            action="approximated",
            details={"characters": private},
        )
    return _text_entity_fields(
        common, context, insert, height, text, rotation, width_factor
    )


def _text_entity_fields(
    common: dict[str, Any],
    context: _Context,
    insert: list[float],
    height: float,
    text: str,
    rotation: float,
    width_factor: float | None,
) -> dict[str, Any]:
    entity: dict[str, Any] = {
        **common,
        "id": context.allocate_id(),
        "kind": "TEXT",
        "insert": insert,
        "height": _positive(height),
        "text": text,
        "style": TEXT_STYLE,
        "halign": "left",
        "valign": "bottom",
    }
    if rotation:
        entity["rotation"] = rotation
    if width_factor is not None and not math.isclose(width_factor, 1.0):
        entity["width_factor"] = width_factor
    return entity


def _convert_dimension(entity: Any, frame: _ViewFrame, context: _Context) -> None:
    dimension = entity.dimension
    common = _common(entity, frame, context)
    block_name = f"*ICD_D{context.next_block}"
    context.next_block += 1
    block: list[dict[str, Any]] = []
    measured = dimension.measured
    measured_points = [tuple(p) for p in measured] if measured else []
    for item in dimension.items:
        kind = item.kind
        if not item.raw_marker & 0x40:
            # hidden alternatives iCAD keeps (the measured circle, mirrored
            # arrowheads, the second aux line of an angle) are not drawn
            continue
        base = {"id": context.allocate_id(), "layer": common["layer"]}
        if "lineweight_mm" in common:
            base["lineweight_mm"] = common["lineweight_mm"]
        points = [frame.point(p, context.divisor) for p in item.points]
        if kind == "line" and len(points) >= 2:
            block.append({**base, "kind": "LINE", "p1": points[0], "p2": points[1]})
        elif kind == "arc" and item.radius:
            block.append(
                {
                    **base,
                    **_arc_fields(
                        points[0],
                        frame.length(item.radius, context.divisor),
                        item.start_angle,
                        item.sweep_angle,
                    ),
                }
            )
        elif kind == "vectors" and len(points) == 3:
            if _is_arrowhead(item.points, measured_points):
                block.append(
                    {
                        **base,
                        "kind": "HATCH",
                        "solid": True,
                        "loops": [{"vertices": [list(p) for p in points]}],
                    }
                )
            else:
                start = points[0]
                if dimension.aux_offset:
                    start = _offset_point(
                        item.points[0],
                        item.points[2],
                        dimension.aux_offset,
                        frame,
                        context,
                    )
                block.append({**base, "kind": "LINE", "p1": start, "p2": points[2]})
        # point items (line point, text anchor, measured points) carry no geometry
    text_entities: list[dict[str, Any]] = []
    if dimension.text is not None:
        text_entities = _convert_text(
            entity, dimension.text, frame, context, block, base=common
        )
    dim_kind = DIMENSION_KINDS.get(str(dimension.kind), "GENERIC")
    leader = getattr(dimension, "leader", None)
    chamfer = str(dimension.kind) == "chamfer"
    if dim_kind == "ANGULAR":
        interpreted = dimension.center is not None and len(measured_points) == 2
    elif dim_kind in ("DIAMETER", "RADIAL"):
        interpreted = dimension.center is not None
    elif chamfer:
        interpreted = dimension.line is not None
    else:
        interpreted = len(measured_points) == 2
    partial = (dim_kind == "GENERIC" and not chamfer) or not interpreted
    location = None
    if dim_kind == "ANGULAR" and interpreted and dimension.radius:
        # the arc point of a DXF angular dimension: the middle of the drawn arc
        middle = (
            float(dimension.arc_start or 0.0) + float(dimension.arc_sweep or 0.0) / 2
        )
        cx, cy = dimension.center
        location = frame.point(
            (
                cx + float(dimension.radius) * math.cos(middle),
                cy + float(dimension.radius) * math.sin(middle),
            ),
            context.divisor,
        )
    elif leader:
        location = frame.point(leader[0], context.divisor)
    elif measured_points:
        location = frame.point(measured_points[0], context.divisor)
    elif dimension.line_point is not None:
        location = frame.point(dimension.line_point, context.divisor)
    elif dimension.center is not None:
        location = frame.point(dimension.center, context.divisor)
    text_midpoint = None
    if text_entities:
        first = text_entities[0]
        text_midpoint = [
            first["insert"][0] + len(first["text"]) * first["height"] / 2.0,
            first["insert"][1] + first["height"] / 2.0,
        ]
    text_value = "".join(
        str(line) for line in (dimension.text.lines if dimension.text else ())
    )
    definition: dict[str, Any] = {
        "points": {
            "location": location or [0.0, 0.0],
            "text_midpoint": text_midpoint or location or [0.0, 0.0],
        },
        "text": text_value,
        "source_geometry": {
            "kind": str(dimension.kind),
            "layout_status": str(dimension.layout_status),
            "value": dimension.value,
            "scale_factor": dimension.scale_factor,
            "measured": [list(p) for p in measured_points],
        },
    }
    if dim_kind == "ANGULAR" and interpreted:
        # two lines through the vertex (DXF angular dimension): p1-p2 and p3-p4
        vertex = frame.point(dimension.center, context.divisor)
        leg1, leg2 = (frame.point(p, context.divisor) for p in measured_points)
        definition["points"].update(
            {"p1": vertex, "p2": leg1, "p3": vertex, "p4": leg2}
        )
        if dimension.value is not None and math.isfinite(float(dimension.value)):
            definition["measurement"] = float(dimension.value)
        else:
            definition["measurement"] = math.degrees(float(dimension.arc_sweep or 0.0))
        definition["source_geometry"]["arc"] = {
            "radius": dimension.radius,
            "start_angle": dimension.arc_start,
            "sweep_angle": dimension.arc_sweep,
        }
    elif chamfer:
        # the chamfer size of the text (C1), not a distance between points
        if dimension.value is not None and math.isfinite(float(dimension.value)):
            definition["measurement"] = float(dimension.value)
    elif measured_points and len(measured_points) == 2:
        (x0, y0), (x1, y1) = measured_points
        line = dimension.line
        if line is not None and math.dist(line[0], line[1]) > _EPSILON:
            # the saved value is the measured distance projected on the dimension
            # line (horizontal and vertical dimensions measure a component)
            (lx0, ly0), (lx1, ly1) = line
            length = math.dist(line[0], line[1])
            ux, uy = (lx1 - lx0) / length, (ly1 - ly0) / length
            definition["measurement"] = abs((x1 - x0) * ux + (y1 - y0) * uy)
        else:
            definition["measurement"] = math.hypot(x1 - x0, y1 - y0)
    elif dimension.radius:
        factor = 2.0 if dim_kind == "DIAMETER" else 1.0
        definition["measurement"] = float(dimension.radius) * factor
    if leader:
        definition["source_geometry"]["leader"] = [list(p) for p in leader]
    if block:
        context.blocks[block_name] = {
            "base_point": [0.0, 0.0],
            "entities": block,
            "metadata": {"icd": {"dimension_presentation": True}},
        }
        definition["block"] = block_name
    context.entities.append(
        {**common, "kind": "DIMENSION", "dim_kind": dim_kind, "definition": definition}
    )
    context.converted["DIMENSION"] += 1
    context.counts["dimension"] += 1
    context.note(
        "ICD_2D_DIMENSION_FROM_SAVED",
        "info",
        "Dimensions were rebuilt from the items iCAD SX saved (arrowheads, "
        "dimension and extension lines, value text) in a block referenced by "
        "definition.block; the construction semantics are not decoded.",
        action="flattened",
    )
    if partial:
        context.note(
            "ICD_2D_DIMENSION_PARTIAL",
            "warning",
            "Dimensions with an unobserved item layout keep their saved items only "
            "(the kind is GENERIC).",
            action="approximated",
        )


def _is_arrowhead(points: Any, measured: list[tuple[float, float]]) -> bool:
    """A ``vectors`` item is an arrowhead when its legs are symmetric about the tip.

    The other ``vectors`` items of a dimension are extension lines: a measured
    point, its foot on the dimension line and the line end beyond it.
    """
    (ax, ay), (tx, ty), (bx, by) = (tuple(p) for p in points)
    for mx, my in measured:
        if math.isclose(ax, mx, abs_tol=1e-6) and math.isclose(ay, my, abs_tol=1e-6):
            return False
    left = math.hypot(ax - tx, ay - ty)
    right = math.hypot(bx - tx, by - ty)
    span = math.hypot(ax - bx, ay - by)
    if left <= _EPSILON or right <= _EPSILON:
        return False
    return math.isclose(left, right, rel_tol=1e-6, abs_tol=1e-6) and span < left + right


def _offset_point(
    start: Any, end: Any, offset: float, frame: _ViewFrame, context: _Context
) -> list[float]:
    (x0, y0), (x1, y1) = (
        (float(start[0]), float(start[1])),
        (float(end[0]), float(end[1])),
    )
    length = math.hypot(x1 - x0, y1 - y0)
    if length <= _EPSILON or offset <= 0 or offset >= length:
        return frame.point(start, context.divisor)
    t = offset / length
    return frame.point((x0 + (x1 - x0) * t, y0 + (y1 - y0) * t), context.divisor)


def _convert_hatch(entity: Any, frame: _ViewFrame, context: _Context) -> None:
    hatch = entity.hatch
    common = _common(entity, frame, context)
    segments = list(hatch.segments or ())
    if not segments:
        context.unsupported_types[str(entity.raw_type)] += 1
        return
    for start, end in segments:
        context.entities.append(
            {
                **common,
                "id": context.allocate_id(),
                "kind": "LINE",
                "p1": frame.point(start, context.divisor),
                "p2": frame.point(end, context.divisor),
                "metadata": {
                    **(common.get("metadata") or {}),
                    "icd": {
                        **((common.get("metadata") or {}).get("icd") or {}),
                        "hatch": True,
                    },
                },
            }
        )
        context.converted["LINE"] += 1
    context.counts["hatch"] += 1
    context.note(
        "ICD_2D_HATCH_AS_LINES",
        "info",
        "Hatches were converted to the parallel lines iCAD SX saved; the boundary "
        "and pattern definition are not kept as a HATCH.",
        action="flattened",
        details={"segments": 0},
    )
    details = context.notes["ICD_2D_HATCH_AS_LINES"].details
    assert details is not None
    details["segments"] += len(segments)


__all__ = [
    "DIMENSION_KINDS",
    "LINE_STYLES",
    "LINE_WIDTH_MM",
    "PAPER_SIZES",
    "TEXT_STYLE",
    "convert_icad_file_to_ir",
    "icad_document_to_ir",
    "is_icd_signature",
]
