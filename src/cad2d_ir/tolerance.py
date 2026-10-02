"""Feature control frames (DXF/DWG ``TOLERANCE``) as IR geometry.

A ``TOLERANCE`` entity stores one string: rows of a frame separated by ``^J``,
the compartments of a row separated by ``%%v``, and the geometric
characteristic symbols as characters of the GDT font (``{\\Fgdt;j}`` is the
position symbol). A CAD program draws it as rows of boxes with one text per
compartment.

The IR has no entity for it. The importers draw the frame instead: the box of
each row and the lines between its compartments as ``LWPOLYLINE`` / ``LINE``,
and each compartment as a centered ``TEXT`` with the symbols as Unicode
characters. The width of a compartment depends on the font and is estimated.
"""

from __future__ import annotations

import math
import re
from typing import Any, Mapping, Sequence

# Characters of the GDT font (gdt.shx) and their Unicode counterparts.
GDT_SYMBOLS: dict[str, str] = {
    "a": "∠",  # angularity
    "b": "⊥",  # perpendicularity
    "c": "⏥",  # flatness
    "d": "⌓",  # profile of a surface
    "e": "○",  # circularity
    "f": "∥",  # parallelism
    "g": "⌭",  # cylindricity
    "h": "↗",  # circular runout
    "i": "⌯",  # symmetry
    "j": "⌖",  # position
    "k": "⌒",  # profile of a line
    "l": "Ⓛ",  # least material condition
    "m": "Ⓜ",  # maximum material condition
    "n": "⌀",  # diameter
    "p": "Ⓟ",  # projected tolerance zone
    "r": "◎",  # concentricity
    "s": "Ⓢ",  # regardless of feature size
    "t": "⌰",  # total runout
    "u": "⏤",  # straightness
}

_ROW_BREAK = re.compile(r"\^J|\r\n|\n|\\P")
_CELL_BREAK = re.compile(r"%%v", re.IGNORECASE)
# "{\Fgdt;j}" or "{\Fgdt|c0;j}": text of one font. The font ends at ";".
_FONT_GROUP = re.compile(r"\{\\[Ff]([^;{}]*);([^{}]*)\}")
# Formatting that carries no text: "\H2.5;", "\C1;", "\Q15;", "\W0.8;", "\T1.2;", "\A1;".
_FORMAT_CODE = re.compile(r"\\[HhCcQqWwTtAa][^;\\{}]*;")
_PERCENT_CODES = {"c": "⌀", "d": "°", "p": "±"}
_PERCENT_CODE = re.compile(r"%%([cdpCDP])")

# Advance of a character in units of the text height.
_NARROW_ADVANCE = 0.8
_SYMBOL_ADVANCE = 1.2
_WIDE_ADVANCE = 1.0


def tolerance_rows(text: str) -> list[list[str]]:
    """Rows and compartments of a tolerance string, as plain text.

    Empty compartments are dropped, like a CAD program leaves them out of the
    frame, and so are rows without any compartment.
    """
    rows: list[list[str]] = []
    for row in _ROW_BREAK.split(text):
        cells = [_plain_cell(cell) for cell in _CELL_BREAK.split(row)]
        cells = [cell for cell in cells if cell]
        if cells:
            rows.append(cells)
    return rows


def _plain_cell(cell: str) -> str:
    def font_group(match: re.Match[str]) -> str:
        font = match.group(1).split("|", 1)[0].strip().lower()
        content = match.group(2)
        if font.startswith("gdt"):
            return "".join(GDT_SYMBOLS.get(char.lower(), char) for char in content)
        return content

    previous = None
    while previous != cell:
        previous = cell
        cell = _FONT_GROUP.sub(font_group, cell)
    cell = _FORMAT_CODE.sub("", cell)
    cell = _PERCENT_CODE.sub(lambda match: _PERCENT_CODES[match.group(1).lower()], cell)
    return cell.replace("{", "").replace("}", "").strip()


def _text_width(text: str, height: float) -> float:
    """Estimated width of a compartment text: fonts are not available here."""
    advance = 0.0
    symbols = set(GDT_SYMBOLS.values()) | set(_PERCENT_CODES.values())
    for char in text:
        if char in symbols:
            advance += _SYMBOL_ADVANCE
        elif ord(char) < 0x2E80:
            advance += _NARROW_ADVANCE
        else:
            advance += _WIDE_ADVANCE
    return advance * height


def explode_tolerance(
    *,
    text: str,
    insert: Sequence[float],
    height: float,
    rotation_deg: float = 0.0,
    gap: float | None = None,
    common: Mapping[str, Any],
) -> list[dict[str, Any]]:
    """Frame and texts of a feature control frame.

    ``insert`` is the middle of the left edge of the first row, the rows stack
    downwards and ``rotation_deg`` turns the frame around ``insert``. ``gap`` is
    the distance between a text and the frame (``DIMGAP``); half the text
    height when it is not known. ``common`` holds the fields every produced
    entity shares (``id``, ``layer``, ``source``, ...); the ids get a suffix.

    Returns no entity for a string without any compartment.
    """
    if not math.isfinite(height) or height <= 0.0:
        raise ValueError("tolerance text height must be positive and finite")
    rows = tolerance_rows(text)
    if not rows:
        return []
    padding = gap if gap is not None and 0.0 < gap <= 2.0 * height else height / 2.0
    row_height = height + 2.0 * padding
    angle = math.radians(rotation_deg)
    cos_a, sin_a = math.cos(angle), math.sin(angle)
    origin_x, origin_y = float(insert[0]), float(insert[1])

    def place(x: float, y: float) -> list[float]:
        return [origin_x + x * cos_a - y * sin_a, origin_y + x * sin_a + y * cos_a]

    base_id = str(common.get("id", "TOLERANCE"))
    entities: list[dict[str, Any]] = []

    def emit(suffix: str, fields: dict[str, Any]) -> None:
        entity = _copy_common(common)
        entity["id"] = f"{base_id}_{suffix}"[:64]
        entity.update(fields)
        entities.append(entity)

    for row_index, cells in enumerate(rows):
        middle = -row_index * row_height
        top, bottom = middle + row_height / 2.0, middle - row_height / 2.0
        widths = [_text_width(cell, height) + 2.0 * padding for cell in cells]
        total = sum(widths)
        emit(
            f"R{row_index + 1}",
            {
                "kind": "LWPOLYLINE",
                "vertices": [
                    place(0.0, bottom),
                    place(total, bottom),
                    place(total, top),
                    place(0.0, top),
                ],
                "closed": True,
            },
        )
        left = 0.0
        for cell_index, (cell, width) in enumerate(zip(cells, widths)):
            if cell_index:
                emit(
                    f"R{row_index + 1}D{cell_index}",
                    {
                        "kind": "LINE",
                        "p1": place(left, bottom),
                        "p2": place(left, top),
                    },
                )
            text_fields: dict[str, Any] = {
                "kind": "TEXT",
                "insert": place(left + width / 2.0, middle),
                "height": height,
                "text": cell,
                "halign": "center",
                "valign": "middle",
            }
            if rotation_deg:
                text_fields["rotation"] = rotation_deg
            emit(f"R{row_index + 1}T{cell_index + 1}", text_fields)
            left += width
    return entities


def _copy_common(common: Mapping[str, Any]) -> dict[str, Any]:
    """A copy of the shared fields whose nested objects are not shared."""
    result: dict[str, Any] = {}
    for key, value in common.items():
        if isinstance(value, dict):
            result[key] = _copy_common(value)
        elif isinstance(value, list):
            result[key] = list(value)
        else:
            result[key] = value
    return result


__all__ = ["GDT_SYMBOLS", "explode_tolerance", "tolerance_rows"]
