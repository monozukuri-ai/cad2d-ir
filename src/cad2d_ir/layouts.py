"""Paper-space layouts: what the DXF and DWG importers share.

``document["entities"]`` is model space. A drawing can also carry sheets
(layouts): each has its own entities, in paper coordinates, and viewports that
show model space at some scale. They live in ``document["layouts"]``.
"""

from __future__ import annotations

import math
from typing import Any, Mapping, Sequence

_EPSILON = 1.0e-9

# DXF group 90 of a VIEWPORT.
_VIEWPORT_OFF = 0x20000
_VIEWPORT_CLIPPED = 0x10000
_VIEWPORT_PERSPECTIVE = 0x1

_PAPER_UNITS = {0: "inch", 1: "mm", 2: "px"}
_PAPER_ROTATIONS = {0: 0, 1: 90, 2: 180, 3: 270}


def is_paper_space_block_name(name: str) -> bool:
    """``*Paper_Space``, ``*Paper_Space0``, ... (``$PAPER_SPACE`` in R12 files)."""
    normalized = name.strip().upper()
    return normalized.startswith("*PAPER_SPACE") or normalized == "$PAPER_SPACE"


def is_active_paper_space_block_name(name: str) -> bool:
    """The block of the sheet that was current when a DXF file was written."""
    return name.strip().upper() in {"*PAPER_SPACE", "$PAPER_SPACE"}


def layout_paper(
    *,
    name: Any = None,
    width_mm: Any = None,
    height_mm: Any = None,
    margins_mm: Sequence[Any] | None = None,
    units_code: Any = None,
    rotation_code: Any = None,
) -> dict[str, Any]:
    """``paper`` of a layout from the plot settings of a DXF/DWG LAYOUT object.

    Values that are missing or unusable are left out; the result can be empty.
    """
    paper: dict[str, Any] = {}
    if isinstance(name, str) and name.strip():
        paper["name"] = name.strip()
    width = _finite(width_mm)
    height = _finite(height_mm)
    if width is not None and height is not None and width > 0.0 and height > 0.0:
        paper["size_mm"] = [width, height]
    if margins_mm is not None and len(margins_mm) == 4:
        margins = [_finite(value) for value in margins_mm]
        if all(value is not None for value in margins):
            paper["margins_mm"] = margins
    units = _PAPER_UNITS.get(_integer(units_code))
    if units is not None:
        paper["units"] = units
    rotation = _PAPER_ROTATIONS.get(_integer(rotation_code))
    if rotation is not None:
        paper["rotation"] = rotation
    return paper


def viewport_from_dxf_values(
    *,
    source_format: str,
    handle: str | None,
    center: Sequence[Any],
    width: Any,
    height: Any,
    view_center: Sequence[Any] | None = None,
    view_height: Any = None,
    twist_deg: Any = None,
    target: Sequence[Any] | None = None,
    direction: Sequence[Any] | None = None,
    status_flags: Any = None,
    status: Any = None,
    layer: str | None = None,
    frozen_layers: Sequence[str] = (),
    clip_boundary: str | None = None,
) -> dict[str, Any]:
    """IR viewport from the values a DXF VIEWPORT record holds.

    DXF gives the view in display coordinates: ``view_center`` (groups 12/22)
    is measured from ``target`` (17/27/37) in a frame that is rotated by the
    twist angle (51, degrees). The IR states the model-space point at the
    center of the window instead, so that
    ``center + R(rotation) * (p - view_center) * height / view_height`` places a
    model point ``p`` on the sheet. A view that is not a plan view (the view
    direction leaves +Z, or a perspective) has no such mapping; its values are
    kept as metadata.

    Raises ``ValueError`` for a viewport without a usable window.
    """
    center_x, center_y = _finite(center[0]), _finite(center[1])
    window_width, window_height = _finite(width), _finite(height)
    if (
        center_x is None
        or center_y is None
        or window_width is None
        or window_height is None
        or window_width <= 0.0
        or window_height <= 0.0
    ):
        raise ValueError("viewport requires a center and a positive width and height")

    viewport: dict[str, Any] = {
        "center": [center_x, center_y],
        "width": window_width,
        "height": window_height,
    }
    if handle:
        viewport["id"] = f"V{handle}"
    source: dict[str, Any] = {"format": source_format, "kind": "VIEWPORT"}
    if handle:
        source["id"] = handle
    viewport["source"] = source
    if layer:
        viewport["layer"] = layer

    flags = _integer(status_flags) or 0
    metadata: dict[str, Any] = {}
    twist = _finite(twist_deg) or 0.0
    shown_height = _finite(view_height)
    view_x = _finite(view_center[0]) if view_center is not None else None
    view_y = _finite(view_center[1]) if view_center is not None else None
    target_point = _point3(target) or (0.0, 0.0, 0.0)
    view_direction = _point3(direction) or (0.0, 0.0, 1.0)
    plan_view = (
        abs(view_direction[0]) <= _EPSILON * max(1.0, abs(view_direction[2]))
        and abs(view_direction[1]) <= _EPSILON * max(1.0, abs(view_direction[2]))
        and view_direction[2] > 0.0
        and not flags & _VIEWPORT_PERSPECTIVE
    )
    if (
        plan_view
        and shown_height is not None
        and shown_height > 0.0
        and view_x is not None
        and view_y is not None
    ):
        angle = math.radians(twist)
        cos_a, sin_a = math.cos(angle), math.sin(angle)
        viewport["view_center"] = [
            target_point[0] + view_x * cos_a + view_y * sin_a,
            target_point[1] - view_x * sin_a + view_y * cos_a,
        ]
        viewport["view_height"] = shown_height
        if abs(twist) > _EPSILON:
            viewport["rotation"] = twist
    elif shown_height is not None or view_center is not None:
        metadata["view"] = {
            "direction": list(view_direction),
            "target": list(target_point),
            "center": [view_x, view_y],
            "height": shown_height,
            "twist": twist,
            "perspective": bool(flags & _VIEWPORT_PERSPECTIVE),
        }

    if frozen_layers:
        viewport["frozen_layers"] = [str(name) for name in frozen_layers]
    # The status flags tell whether the viewport is switched off. The status
    # field of a DXF record (group 68) is 0 for every viewport of a sheet that
    # was not current when the file was written; it only counts for files
    # without the flags.
    if flags & _VIEWPORT_OFF or (
        _integer(status_flags) is None and _integer(status) == 0
    ):
        viewport["visible"] = False
    if flags:
        metadata["status_flags"] = flags
    if clip_boundary and flags & _VIEWPORT_CLIPPED:
        metadata["clip_boundary"] = clip_boundary
    if metadata:
        viewport["metadata"] = {source_format: metadata}
    return viewport


def shows_the_sheet_itself(
    *,
    center: Sequence[Any],
    height: Any,
    view_center: Sequence[Any] | None,
    view_height: Any,
) -> bool:
    """Whether a viewport looks like the one that stands for its sheet.

    Every sheet of a DXF or DWG file has one viewport that shows no model space
    but the sheet itself: its view is its own window at scale 1. Without view
    data the answer is yes, as nothing speaks against it.
    """
    window_height = _finite(height)
    shown_height = _finite(view_height)
    if view_center is None or shown_height is None or window_height is None:
        return True
    center_x, center_y = _finite(center[0]), _finite(center[1])
    view_x, view_y = _finite(view_center[0]), _finite(view_center[1])
    if None in (center_x, center_y, view_x, view_y):
        return True
    tolerance = 1e-6 * max(1.0, abs(window_height))
    return (
        abs(window_height - shown_height) <= tolerance
        and abs(center_x - view_x) <= tolerance
        and abs(center_y - view_y) <= tolerance
    )


def viewport_to_dxf_view(viewport: Mapping[str, Any]) -> dict[str, Any] | None:
    """Display-coordinate view of an IR viewport, for a DXF VIEWPORT record.

    Returns ``view_center`` (groups 12/22), ``view_height`` (45) and
    ``twist_deg`` (51) with the view target at the origin, or ``None`` when the
    viewport states no view.
    """
    view_center = viewport.get("view_center")
    view_height = viewport.get("view_height")
    if not isinstance(view_center, (list, tuple)) or view_height is None:
        return None
    twist = float(viewport.get("rotation", 0.0) or 0.0)
    angle = math.radians(twist)
    cos_a, sin_a = math.cos(angle), math.sin(angle)
    x, y = float(view_center[0]), float(view_center[1])
    return {
        "view_center": [x * cos_a - y * sin_a, x * sin_a + y * cos_a],
        "view_height": float(view_height),
        "twist_deg": twist,
    }


def unnamed_layout_name(taken: set[str]) -> str:
    """``Layout1``, ``Layout2``, ...: the first one that is not in ``taken``."""
    number = 1
    while f"Layout{number}" in taken:
        number += 1
    return f"Layout{number}"


def order_layouts(layouts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Layouts in tab order (unknown positions last, in their given order)."""
    return sorted(
        layouts,
        key=lambda layout: (
            "tab_order" not in layout,
            layout.get("tab_order", 0),
        ),
    )


def promote_layout_when_model_is_empty(
    document: dict[str, Any], *, metadata_key: str
) -> dict[str, Any] | None:
    """Make a sheet the drawing when model space holds nothing.

    Some drawings are drafted on a sheet: model space is empty and everything
    is in paper space. Their viewports show nothing, so the sheet itself is the
    drawing. The entities of the active sheet (or of the first sheet that has
    any) become ``document["entities"]``; the layout is removed from
    ``document["layouts"]`` and named in ``header.metadata``. Returns the
    promoted layout, or ``None`` when nothing changed.
    """
    if document.get("entities"):
        return None
    layouts = document.get("layouts")
    if not isinstance(layouts, list):
        return None
    candidates = [layout for layout in layouts if layout.get("entities")]
    if not candidates:
        return None
    chosen = next(
        (layout for layout in candidates if layout.get("active")), candidates[0]
    )
    document["entities"] = chosen["entities"]
    remaining = [layout for layout in layouts if layout is not chosen]
    if remaining:
        document["layouts"] = remaining
    else:
        del document["layouts"]
    promoted: dict[str, Any] = {"name": chosen["name"]}
    if chosen.get("paper"):
        promoted["paper"] = chosen["paper"]
    header = document.setdefault("header", {})
    header.setdefault("metadata", {}).setdefault(metadata_key, {})[
        "promoted_layout"
    ] = promoted
    return chosen


def layout_entity_count(document: Mapping[str, Any]) -> int:
    layouts = document.get("layouts")
    if not isinstance(layouts, list):
        return 0
    return sum(
        len(layout.get("entities") or [])
        for layout in layouts
        if isinstance(layout, Mapping)
    )


def _finite(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _integer(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        return int(value)
    except (ValueError, OverflowError):
        return None


def _point3(value: Sequence[Any] | None) -> tuple[float, float, float] | None:
    if value is None or len(value) < 2:
        return None
    x, y = _finite(value[0]), _finite(value[1])
    z = _finite(value[2]) if len(value) >= 3 else 0.0
    if x is None or y is None or z is None:
        return None
    return (x, y, z)


__all__ = [
    "is_active_paper_space_block_name",
    "is_paper_space_block_name",
    "layout_entity_count",
    "layout_paper",
    "order_layouts",
    "promote_layout_when_model_is_empty",
    "shows_the_sheet_itself",
    "unnamed_layout_name",
    "viewport_from_dxf_values",
    "viewport_to_dxf_view",
]
