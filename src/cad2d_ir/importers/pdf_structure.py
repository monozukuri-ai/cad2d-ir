"""Structure a plot loses and a PDF importer can put back: dashed lines and layers.

A printer driver draws a dashed line as its dashes, each one a separate stroke,
and writes no layers. The two passes here work on IR entities in paper
millimetres, after the page objects have been read and before the plot scale
is applied:

- :func:`rebuild_dashed_lines` joins the collinear strokes of one dashed line
  into a single ``LINE`` with a linetype that carries the measured pattern.
- :func:`infer_layers` sorts the entities of layer ``0`` into layers by what
  they are (text, fill, image) and how they are drawn (linetype, line width,
  colour).

Both are inferences. The callers disclose them with diagnostics and counts.
"""

from __future__ import annotations

import itertools
import math
from collections import Counter, defaultdict
from collections.abc import Callable, Sequence
from statistics import median
from typing import Any

Point = tuple[float, float]

# A gap between two dashes of one line, in paper millimetres. Strokes that touch
# (within JOIN_MM) are one dash drawn in parts.
MIN_GAP_MM = 0.05
JOIN_MM = 0.02
MAX_GAP_MM = 8.0
# A gap may be longer than the dashes beside it (a dotted line), but only up to
# this length; beyond it a gap has to be shorter than 1.5 times a neighbour.
FREE_GAP_MM = 2.0
# Printer drivers snap coordinates to their resolution (0.042 mm at 600 dpi), so
# the dashes of a slanted line sit this far off their common line.
COLLINEAR_TOLERANCE_MM = 0.08
# Lengths within this ratio count as the same dash or gap.
LENGTH_RATIO = 1.35
LENGTH_SLACK_MM = 0.1
MIN_PIECES = 3
# Strokes shorter than this are the facets of a flattened curve, not dots, and
# a grid cell (2 mm) with more stroke ends than this is a texture, not dashes.
# Both are left out of the search, which would otherwise take seconds on them.
MIN_PIECE_MM = 0.08
MAX_CELL_ENDS = 400
# The end pieces of a dashed line are cut or stretched to fit its length.
END_DASH_RATIO = 2.5
# A run of long dashes with only two gaps is more often a line broken twice.
LONG_DASH_MM = 4.0
MIN_LONG_DASH_PIECES = 5
# A single short piece between two long ones is a chain line only when the
# piece and the gaps are this small.
CHAIN_SHORT_MAX_MM = 2.0
# A dashed line with some merged or cut dashes: how many pieces, and how many alike.
MIN_WORN_PIECES = 6
MIN_WORN_ALIKE_RATIO = 0.6
# A dash is a free stroke: its ends touch nothing. Strokes of lettering drawn
# as lines (the bars of ``E E E``) are collinear and evenly spaced too, but
# other strokes start at their ends.
TOUCH_TOLERANCE_MM = 0.03
MAX_ATTACHED_RATIO = 0.25
# Shapes filled with scan strokes (a logo) make rows of collinear, evenly spaced
# strokes as well; there the next row ends right beside every stroke.
CROWD_MM = 0.3
MAX_CROWDED_RATIO = 0.5
# A line of nothing but dots needs more pieces to be believed than one with dashes.
MIN_DASH_MM = 0.5
MIN_DOTTED_PIECES = 6

MAX_INFERRED_LAYERS = 24


def _style_key(entity: dict[str, Any]) -> tuple[Any, ...]:
    return (
        entity.get("layer"),
        entity.get("color"),
        entity.get("lineweight_mm"),
    )


class _Piece:
    __slots__ = ("ends", "index", "length", "links", "u")

    def __init__(self, index: int, p1: Point, p2: Point) -> None:
        self.index = index
        self.ends = (p1, p2)
        self.length = math.hypot(p2[0] - p1[0], p2[1] - p1[1])
        self.u = ((p2[0] - p1[0]) / self.length, (p2[1] - p1[1]) / self.length)
        # Per end: the stroke ahead on the same line and which of its ends faces us.
        self.links: list[tuple[_Piece, int] | None] = [None, None]

    def outward(self, end: int) -> Point:
        a, b = self.ends[1 - end], self.ends[end]
        return ((b[0] - a[0]) / self.length, (b[1] - a[1]) / self.length)


def _off_line(origin: Point, d: Point, p: Point) -> float:
    return abs((p[0] - origin[0]) * d[1] - (p[1] - origin[1]) * d[0])


def _along(origin: Point, d: Point, p: Point) -> float:
    return (p[0] - origin[0]) * d[0] + (p[1] - origin[1]) * d[1]


def _same_length(a: float, b: float) -> bool:
    low, high = (a, b) if a <= b else (b, a)
    return high <= low * LENGTH_RATIO + LENGTH_SLACK_MM


def _length_classes(values: Sequence[float]) -> list[float]:
    """Medians of the groups of similar lengths, shortest first."""
    groups: list[list[float]] = []
    for value in sorted(values):
        if groups and _same_length(groups[-1][0], value):
            groups[-1].append(value)
        else:
            groups.append([value])
    return [median(group) for group in groups]


def _pattern_of(
    lengths: Sequence[float], gaps: Sequence[float], axis_aligned: bool
) -> tuple[list[float], bool] | None:
    """The pattern of a chain read strictly, else as a plain line with worn dashes."""
    gap = median(gaps)
    if any(not _same_length(g, gap) for g in gaps):
        return None
    return _regular_pattern(lengths, gap, axis_aligned) or _worn_pattern(lengths, gap)


def _regular_pattern(
    lengths: Sequence[float], gap: float, axis_aligned: bool
) -> tuple[list[float], bool] | None:
    """Dash pattern (dash positive, gap negative) of a chain, or ``None``.

    The flag is false when the long dash is not known: the first and the last
    piece are cut where the line ends, and a centre line is drawn in parts whose
    long dashes differ.
    """
    plain = _plain_pattern(lengths, gap)
    if plain is not None:
        return plain, True
    return _chain_pattern(lengths, gap, axis_aligned)


def _plain_pattern(lengths: Sequence[float], gap: float) -> list[float] | None:
    """Dashes of one length; only the two end pieces may differ."""
    inner = lengths[1:-1]
    classes = _length_classes(inner)
    if len(classes) != 1:
        return None
    dash = classes[0]
    if any(
        v > dash * END_DASH_RATIO + LENGTH_SLACK_MM for v in (lengths[0], lengths[-1])
    ):
        return None
    if dash > LONG_DASH_MM and len(lengths) < MIN_LONG_DASH_PIECES:
        return None
    return [dash, -gap]


def _chain_pattern(
    lengths: Sequence[float], gap: float, axis_aligned: bool
) -> tuple[list[float], bool] | None:
    """Long dashes with one or two short dashes between them (chain lines).

    The short dashes have to be alike and the long ones at least twice as long;
    how long a long dash is varies along a centre line, so it is not compared.
    Call it on a run without two long dashes in a row (:func:`_chain_parts`).
    """
    short = min(lengths)
    kinds = [_same_length(v, short) for v in lengths]  # True: a short dash
    longs = [v for v, is_short in zip(lengths, kinds, strict=True) if not is_short]
    if not longs or min(longs) < 2.0 * short:
        return None
    runs: list[int] = []
    run = 0
    for is_short in kinds:
        if is_short:
            run += 1
        else:
            if run:
                runs.append(run)
            run = 0
    if run:
        runs.append(run)
    if not runs or max(runs) > 2:
        return None
    shorts = [v for v, is_short in zip(lengths, kinds, strict=True) if is_short]
    short = median(shorts)
    if len(shorts) == 1:
        # One short piece between two lines. Hatch lines broken by a label look
        # the same, so only a small piece on a horizontal or vertical line counts.
        if not axis_aligned or short > CHAIN_SHORT_MAX_MM or gap > CHAIN_SHORT_MAX_MM:
            return None
    elif short > 2.0 * gap + LENGTH_SLACK_MM:
        return None
    long_ = median(longs)
    # The long dash differs from part to part of a centre line: not compared.
    reliable = False
    if Counter(runs).most_common(1)[0][0] == 2:
        return [long_, -gap, short, -gap, short, -gap], reliable
    return [long_, -gap, short, -gap], reliable


def _worn_pattern(
    lengths: Sequence[float], gap: float
) -> tuple[list[float], bool] | None:
    """A plain dashed line whose dashes are not all alike.

    Where dashed lines cross or lie on each other their dashes merge or are cut,
    so the gaps stay even while some dashes come out longer or shorter. Most of
    the dashes still have to agree.
    """
    if len(lengths) < MIN_WORN_PIECES:
        return None
    dash = median(lengths)
    if dash > LONG_DASH_MM or any(
        v > dash * END_DASH_RATIO + LENGTH_SLACK_MM for v in lengths
    ):
        return None
    alike = sum(1 for v in lengths if _same_length(v, dash))
    if alike < MIN_WORN_ALIKE_RATIO * len(lengths):
        return None
    return [dash, -gap], True


def _even_gap_runs(gaps: Sequence[float]) -> list[tuple[int, int]]:
    """Ranges of pieces ``(first, last)`` between which the gaps stay the same.

    Strokes on one straight line are not all one dashed line: another line may
    go on behind it, and marks sit on centre lines. An uneven gap ends a run.
    """
    runs: list[tuple[int, int]] = []
    first = 0
    current: list[float] = []
    for i, gap in enumerate(gaps):
        if current and not _same_length(gap, median(current)):
            runs.append((first, i))
            first = i + 1
            current = []
            continue
        current.append(gap)
    runs.append((first, len(gaps)))
    return [(a, b) for a, b in runs if b - a + 1 >= MIN_PIECES]


def _chain_parts(lengths: Sequence[float]) -> list[tuple[int, int]]:
    """Split a run where two long dashes follow each other.

    A centre line and the centre mark next to it lie on one line with the same
    gap between them; two long dashes in a row are where one ends and the next
    begins. A run of dashes of one length is left whole.
    """
    whole = [(0, len(lengths) - 1)]
    if len(_length_classes(lengths[1:-1])) <= 1:
        return whole
    short = min(lengths)
    kinds = [_same_length(v, short) for v in lengths]
    if any(
        v < 2.0 * short
        for v, is_short in zip(lengths, kinds, strict=True)
        if not is_short
    ):
        return whole  # not long and short dashes: a plain line with uneven dashes
    parts: list[tuple[int, int]] = []
    first = 0
    for i in range(1, len(lengths)):
        if not kinds[i - 1] and not kinds[i]:
            parts.append((first, i - 1))
            first = i
    parts.append((first, len(lengths) - 1))
    return [(a, b) for a, b in parts if b - a + 1 >= MIN_PIECES] or (
        whole if len(parts) == 1 else []
    )


def _cluster_patterns(found: Sequence[tuple[list[float], bool]]) -> list[int]:
    """Group the patterns that are one linetype; returns a group number per pattern.

    Dashes and gaps within ``LENGTH_RATIO`` are the same. A long dash that was
    read off an end piece says nothing, so such a pattern joins any group whose
    short dash and gaps agree.
    """
    groups: list[tuple[list[float], bool]] = []
    result = [-1] * len(found)
    order = sorted(range(len(found)), key=lambda i: not found[i][1])
    for i in order:
        pattern, reliable = found[i]
        for number, (other, _other_reliable) in enumerate(groups):
            if len(other) != len(pattern):
                continue
            start = 0 if reliable else 1
            if all(
                _same_length(abs(a), abs(b))
                for a, b in zip(pattern[start:], other[start:], strict=True)
            ):
                result[i] = number
                break
        else:
            groups.append((pattern, reliable))
            result[i] = len(groups) - 1
    return result


def rebuild_dashed_lines(
    entities: list[dict[str, Any]],
    linetype_for: Callable[[Sequence[float]], str],
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Join the separate strokes of dashed lines into single lines.

    ``linetype_for`` registers a dash pattern in paper millimetres and returns
    the linetype name. Returns the new entity list (a joined line stands where
    its first stroke was) and ``{"lines": n, "pieces": m}``.
    """
    pieces_by_style: dict[tuple[Any, ...], list[_Piece]] = defaultdict(list)
    for index, entity in enumerate(entities):
        if entity.get("kind") != "LINE" or entity.get("linetype") != "CONTINUOUS":
            continue
        p1, p2 = entity["p1"], entity["p2"]
        a, b = (float(p1[0]), float(p1[1])), (float(p2[0]), float(p2[1]))
        if math.hypot(b[0] - a[0], b[1] - a[1]) < MIN_PIECE_MM:
            continue
        pieces_by_style[_style_key(entity)].append(_Piece(index, a, b))

    # Where strokes of any kind end, to tell free dashes from joined strokes.
    touch_cell = 0.5
    ends: dict[tuple[int, int], list[tuple[float, float, int]]] = defaultdict(list)

    def add_end(p: Sequence[float], index: int) -> None:
        x, y = float(p[0]), float(p[1])
        ends[(math.floor(x / touch_cell), math.floor(y / touch_cell))].append(
            (x, y, index)
        )

    for index, entity in enumerate(entities):
        kind = entity.get("kind")
        if kind == "LINE":
            add_end(entity["p1"], index)
            add_end(entity["p2"], index)
        elif kind == "LWPOLYLINE":
            for vertex in entity.get("vertices") or ():
                add_end(vertex, index)
        elif kind == "ARC":
            cx, cy, radius = entity["center"][0], entity["center"][1], entity["radius"]
            for angle in (entity["start_angle"], entity["end_angle"]):
                add_end(
                    (
                        cx + radius * math.cos(math.radians(angle)),
                        cy + radius * math.sin(math.radians(angle)),
                    ),
                    index,
                )

    def attached(a: Point, b: Point, own: set[int], skip: Sequence[Point]) -> bool:
        """Whether a stroke outside the chain ends on the stroke ``a``-``b``."""
        length = math.hypot(b[0] - a[0], b[1] - a[1])
        d = ((b[0] - a[0]) / length, (b[1] - a[1]) / length)
        steps = max(1, math.ceil(length / touch_cell))
        visited: set[tuple[int, int]] = set()
        for step in range(steps + 1):
            t = length * step / steps
            cx = math.floor((a[0] + d[0] * t) / touch_cell)
            cy = math.floor((a[1] + d[1] * t) / touch_cell)
            for ix in (cx - 1, cx, cx + 1):
                for iy in (cy - 1, cy, cy + 1):
                    if (ix, iy) in visited:
                        continue
                    visited.add((ix, iy))
                    for x, y, index in ends.get((ix, iy), ()):
                        if index in own:
                            continue
                        along = (x - a[0]) * d[0] + (y - a[1]) * d[1]
                        if (
                            along < -TOUCH_TOLERANCE_MM
                            or along > length + TOUCH_TOLERANCE_MM
                        ):
                            continue
                        if (
                            abs((x - a[0]) * d[1] - (y - a[1]) * d[0])
                            > TOUCH_TOLERANCE_MM
                        ):
                            continue
                        if any(
                            math.hypot(x - q[0], y - q[1]) <= TOUCH_TOLERANCE_MM
                            for q in skip
                        ):
                            continue
                        return True
        return False

    def crowded(p: Point, d: Point, own: set[int]) -> bool:
        """Whether a stroke outside the chain ends right beside ``p``, off the line."""
        cx, cy = math.floor(p[0] / touch_cell), math.floor(p[1] / touch_cell)
        for ix in (cx - 1, cx, cx + 1):
            for iy in (cy - 1, cy, cy + 1):
                for x, y, index in ends.get((ix, iy), ()):
                    if index in own or math.hypot(x - p[0], y - p[1]) > CROWD_MM:
                        continue
                    if abs((x - p[0]) * d[1] - (y - p[1]) * d[0]) > TOUCH_TOLERANCE_MM:
                        return True
        return False

    replaced: dict[int, dict[str, Any]] = {}
    found: list[tuple[list[float], bool]] = []
    found_at: list[int] = []
    removed: set[int] = set()
    stats = {"lines": 0, "pieces": 0}
    cell = 2.0
    for pieces in pieces_by_style.values():
        if len(pieces) < MIN_PIECES:
            continue
        grid: dict[tuple[int, int], list[tuple[_Piece, int]]] = defaultdict(list)
        for piece in pieces:
            for end in (0, 1):
                p = piece.ends[end]
                grid[(math.floor(p[0] / cell), math.floor(p[1] / cell))].append(
                    (piece, end)
                )
        # The nearest stroke ahead on the same line, for every end of every stroke.
        nearest: dict[tuple[int, int], tuple[float, _Piece, int]] = {}
        for piece in pieces:
            for end in (0, 1):
                p = piece.ends[end]
                u = piece.outward(end)
                best: tuple[float, _Piece, int] | None = None
                # The cells a gap of up to MAX_GAP_MM ahead of this end can reach.
                cells: set[tuple[int, int]] = set()
                for step in range(int(MAX_GAP_MM / cell) + 1):
                    cx = math.floor((p[0] + u[0] * cell * step) / cell)
                    cy = math.floor((p[1] + u[1] * cell * step) / cell)
                    for ix in (cx - 1, cx, cx + 1):
                        for iy in (cy - 1, cy, cy + 1):
                            cells.add((ix, iy))
                for key in cells:
                    bucket = grid.get(key, ())
                    if len(bucket) > MAX_CELL_ENDS:
                        continue
                    for other, other_end in bucket:
                        if other is piece:
                            continue
                        q = other.ends[other_end]
                        dx, dy = q[0] - p[0], q[1] - p[1]
                        longer = piece.length >= other.length
                        # Off the line of the longer of the two: most pairs end here.
                        w = u if longer else other.u
                        if abs(dx * w[1] - dy * w[0]) > COLLINEAR_TOLERANCE_MM:
                            continue
                        gap = math.hypot(dx, dy)
                        if gap > MAX_GAP_MM or JOIN_MM < gap < MIN_GAP_MM:
                            continue
                        if best is not None and gap >= best[0]:
                            continue
                        if gap > FREE_GAP_MM and gap > 1.5 * max(
                            piece.length, other.length
                        ):
                            continue
                        far = other.ends[1 - other_end]
                        # Ahead of this end, and running away from it.
                        ahead_q = dx * u[0] + dy * u[1]
                        if ahead_q < 0.5 * gap or _along(p, u, far) <= ahead_q:
                            continue
                        # Judge by the longer stroke: a short one has no reliable direction.
                        if longer:
                            origin, d, tested = p, u, (q, far)
                        else:
                            origin, d = q, other.outward(other_end)
                            tested = (p, piece.ends[1 - end])
                        if any(
                            _off_line(origin, d, t) > COLLINEAR_TOLERANCE_MM
                            for t in tested
                        ):
                            continue
                        best = (gap, other, other_end)
                if best is not None:
                    nearest[(piece.index, end)] = best
        for piece in pieces:
            for end in (0, 1):
                ahead = nearest.get((piece.index, end))
                if ahead is None:
                    continue
                _gap, other, other_end = ahead
                back = nearest.get((other.index, other_end))
                if back is not None and back[1] is piece and back[2] == end:
                    piece.links[end] = (other, other_end)
        for head in pieces:
            linked = [end for end in (0, 1) if head.links[end] is not None]
            if len(linked) != 1:
                continue
            # Walk from the free end of the head: (piece, end we leave through).
            chain: list[tuple[_Piece, int]] = [(head, linked[0])]
            seen = {head.index}
            while True:
                piece, out = chain[-1]
                link = piece.links[out]
                if link is None:
                    break
                other, entered = link
                if other.index in seen:
                    break
                seen.add(other.index)
                chain.append((other, 1 - entered))
            if len(chain) < MIN_PIECES or chain[-1][0].index < head.index:
                continue  # too short, or the same chain seen from its other head
            # One dash may be drawn as several strokes end to end (a line split
            # where it crosses another): (start, end, length, strokes).
            whole: list[tuple[Point, Point, float, list[_Piece]]] = []
            for piece, out in chain:
                a, b = piece.ends[1 - out], piece.ends[out]
                if (
                    whole
                    and math.hypot(a[0] - whole[-1][1][0], a[1] - whole[-1][1][1])
                    <= JOIN_MM
                ):
                    last = whole[-1]
                    last[3].append(piece)
                    whole[-1] = (last[0], b, last[2] + piece.length, last[3])
                else:
                    whole.append((a, b, piece.length, [piece]))
            if len(whole) < MIN_PIECES:
                continue
            whole_gaps = [
                math.hypot(second[0][0] - first[1][0], second[0][1] - first[1][1])
                for first, second in itertools.pairwise(whole)
            ]
            runs = [
                (run_first + part_first, run_first + part_last)
                for run_first, run_last in _even_gap_runs(whole_gaps)
                for part_first, part_last in _chain_parts(
                    [dash[2] for dash in whole[run_first : run_last + 1]]
                )
            ]
            for run_first, run_last in runs:
                oriented = whole[run_first : run_last + 1]
                start, finish = oriented[0][0], oriented[-1][1]
                span = math.hypot(finish[0] - start[0], finish[1] - start[1])
                if span < 1e-6:
                    continue
                d = ((finish[0] - start[0]) / span, (finish[1] - start[1]) / span)
                if any(
                    _off_line(start, d, p) > COLLINEAR_TOLERANCE_MM
                    for a, b, _length, _strokes in oriented
                    for p in (a, b)
                ):
                    continue
                gaps = [
                    _along(first[1], d, second[0])
                    for first, second in itertools.pairwise(oriented)
                ]
                axis_aligned = (
                    min(abs(d[0]), abs(d[1])) * span <= COLLINEAR_TOLERANCE_MM
                )
                read = _pattern_of([dash[2] for dash in oriented], gaps, axis_aligned)
                if read is None:
                    continue
                pattern = read[0]
                if max(pattern) < MIN_DASH_MM and len(oriented) < MIN_DOTTED_PIECES:
                    continue
                # The two ends of the whole line may meet other lines; its dashes may not.
                strokes = [piece for dash in oriented for piece in dash[3]]
                own = {piece.index for piece in strokes}
                touching = sum(
                    1
                    for a, b, _length, _strokes in oriented
                    if attached(a, b, own, (start, finish))
                )
                if touching > MAX_ATTACHED_RATIO * len(oriented):
                    continue
                beside = sum(
                    1
                    for a, b, _length, _strokes in oriented
                    if crowded(a, d, own) or crowded(b, d, own)
                )
                if beside > MAX_CROWDED_RATIO * len(oriented):
                    continue
                first_piece = min(strokes, key=lambda piece: piece.index)
                joined = dict(entities[first_piece.index])
                joined["p1"] = [start[0], start[1]]
                joined["p2"] = [finish[0], finish[1]]
                found.append(read)
                found_at.append(first_piece.index)
                meta = joined.get("metadata")
                if isinstance(meta, dict) and isinstance(meta.get("pdf"), dict):
                    joined["metadata"] = {
                        **meta,
                        "pdf": {**meta["pdf"], "dash_pieces": len(oriented)},
                    }
                replaced[first_piece.index] = joined
                removed.update(
                    piece.index for piece in strokes if piece is not first_piece
                )
                stats["lines"] += 1
                stats["pieces"] += len(oriented)
    if not replaced:
        return entities, stats
    groups = _cluster_patterns(found)
    names: dict[int, str] = {}
    for group in sorted(set(groups)):
        # The pattern of the group is the one it was opened with (a reliable one first).
        pattern = next(
            found[i][0]
            for i in sorted(range(len(found)), key=lambda i: not found[i][1])
            if groups[i] == group
        )
        names[group] = linetype_for([round(v, 2) for v in pattern])
    for index, group in zip(found_at, groups, strict=True):
        replaced[index]["linetype"] = names[group]
    result = [
        replaced.get(index, entity)
        for index, entity in enumerate(entities)
        if index not in removed
    ]
    return result, stats


# ------------------------------------------------------------------------ layers


def _width_label(width: Any) -> str:
    if not isinstance(width, (int, float)) or isinstance(width, bool) or width <= 0:
        return "0"
    return f"{width:.2f}"


def _linetype_class(name: Any) -> str:
    text = str(name or "CONTINUOUS").upper()
    if text in ("CONTINUOUS", "BYLAYER", "BYBLOCK"):
        return "LINE"
    # DASHED_2 and DASHED share a layer: the layer says what kind of line it is.
    head, _, tail = text.rpartition("_")
    return head if head and tail.isdigit() else text


def infer_layers(
    entities: list[dict[str, Any]], layers: dict[str, dict[str, Any]]
) -> dict[str, int]:
    """Sort the entities of layer ``0`` into layers by kind and pen.

    Text goes to ``TEXT``, fills to ``FILL``, images to ``IMAGE``. Strokes go to
    ``<linetype class>_<width in mm>``, such as ``LINE_0.25`` or ``CENTER_0.13``,
    with ``_<RRGGBB>`` appended when the strokes have several colours. When that
    would make more than ``MAX_INFERRED_LAYERS`` layers the colour, and then
    the width, is left out of the names. Entities on other layers (optional
    content groups) are not touched. Returns ``{layer name: entity count}``.
    """
    keys: list[tuple[str, str, str] | str | None] = []
    colours: set[str] = set()
    for entity in entities:
        if entity.get("layer") != "0":
            keys.append(None)
            continue
        kind = entity.get("kind")
        if kind in ("TEXT", "MTEXT"):
            keys.append("TEXT")
        elif kind == "HATCH":
            keys.append("FILL")
        elif kind == "IMAGE":
            keys.append("IMAGE")
        else:
            colour = str(entity.get("color") or "").lstrip("#").upper()
            colours.add(colour)
            keys.append(
                (
                    _linetype_class(entity.get("linetype")),
                    _width_label(entity.get("lineweight_mm")),
                    colour,
                )
            )

    def names(with_width: bool, with_colour: bool) -> list[str | None]:
        result: list[str | None] = []
        for key in keys:
            if key is None or isinstance(key, str):
                result.append(key)
                continue
            name = key[0]
            if with_width:
                name += "_" + key[1]
            if with_colour and key[2]:
                name += "_" + key[2]
            result.append(name)
        return result

    chosen: list[str | None] = []
    for with_width, with_colour in (
        (True, len(colours) > 1),
        (True, False),
        (False, False),
    ):
        chosen = names(with_width, with_colour)
        if len({name for name in chosen if name is not None}) <= MAX_INFERRED_LAYERS:
            break
    counts: Counter[str] = Counter()
    for entity, name in zip(entities, chosen, strict=True):
        if name is None:
            continue
        entity["layer"] = name
        counts[name] += 1
    if not counts:
        return {}
    for name in sorted(counts):
        layers.setdefault(name, {})
    if not any(entity.get("layer") == "0" for entity in entities):
        layers.pop("0", None)
    return dict(sorted(counts.items()))
