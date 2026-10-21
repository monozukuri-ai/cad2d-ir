"""3D side channel collected while importing DWG and DXF (``keep_3d``).

The IR is two-dimensional: the importers project every entity onto XY. With
``ImportOptions(keep_3d=True)`` they additionally hand back, outside the IR
document, the three-dimensional geometry they had before projecting:

- ``curves``: world-space polylines sampled from lines, arcs, circles,
  ellipses, polylines (bulges expanded), splines, leaders and multilines;
- ``faces``: triangles from 3DFACE, SOLID/TRACE, polyface and polygon meshes
  and DXF MESH entities;
- ``inserts``: block references with their full 3D placement matrix;
- ``blocks``: the same three lists per block definition, in block space;
- ``unsupported``: counts of 3D entities nobody could read (3DSOLID, REGION,
  BODY, surfaces, DWG MESH);
- ``stats``: totals a caller can decide on without walking the lists
  (``curves_3d`` counts curves that do not lie in a plane parallel to XY,
  ``elevations`` the distinct z levels of the ones that do).

Entries carry the IR entity id when an IR entity exists (``None`` otherwise)
and the source handle, so a consumer can relate a picked triangle or segment
back to the drawing. The dictionary is a transfer format for the importer's
caller, not part of the IR schema: it is versioned separately
(:data:`SIDE_CHANNEL_VERSION`) and its compatibility promise is weaker.
"""

from __future__ import annotations

import math
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

SIDE_CHANNEL_VERSION = 1

Point3 = tuple[float, float, float]
Matrix4 = tuple[
    tuple[float, float, float, float],
    tuple[float, float, float, float],
    tuple[float, float, float, float],
    tuple[float, float, float, float],
]

_EPSILON = 1e-9
_Z_AXIS: Point3 = (0.0, 0.0, 1.0)
IDENTITY: Matrix4 = (
    (1.0, 0.0, 0.0, 0.0),
    (0.0, 1.0, 0.0, 0.0),
    (0.0, 0.0, 1.0, 0.0),
    (0.0, 0.0, 0.0, 1.0),
)

# Source entity kinds that hold 3D geometry no importer can read yet.
UNSUPPORTED_3D_KINDS = frozenset(
    {
        "3DSOLID",
        "BODY",
        "REGION",
        "SURFACE",
        "PLANESURFACE",
        "EXTRUDEDSURFACE",
        "REVOLVEDSURFACE",
        "SWEPTSURFACE",
        "LOFTEDSURFACE",
        "NURBSURFACE",
        "MESH",
    }
)


# ---------------------------------------------------------------------------
# vector helpers
# ---------------------------------------------------------------------------


def point3(value: Any, default_z: float = 0.0) -> Point3:
    """Read ``(x, y[, z])`` as finite floats; a missing z takes ``default_z``."""
    if isinstance(value, (str, bytes)):
        raise TypeError("point must be a coordinate sequence")
    try:
        x = float(value[0])
        y = float(value[1])
    except (TypeError, IndexError) as exc:
        raise ValueError("point must contain x and y") from exc
    try:
        third = value[2]
    except (TypeError, IndexError):
        third = None
    z = default_z if third is None else float(third)
    if not (math.isfinite(x) and math.isfinite(y) and math.isfinite(z)):
        raise ValueError("point coordinates must be finite")
    return (x, y, z)


def normalize_extrusion(value: Any) -> Point3:
    """Normalize an extrusion vector; ``None`` or a zero vector means +Z."""
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return _Z_AXIS
    if len(value) < 3:
        return _Z_AXIS
    try:
        vector = (float(value[0]), float(value[1]), float(value[2]))
    except (TypeError, ValueError):
        return _Z_AXIS
    length = math.sqrt(sum(component * component for component in vector))
    if not math.isfinite(length) or length <= 1e-15:
        return _Z_AXIS
    return (vector[0] / length, vector[1] / length, vector[2] / length)


def is_z_extrusion(extrusion: Point3) -> bool:
    return abs(extrusion[0]) <= _EPSILON and abs(extrusion[1]) <= _EPSILON


def _cross(left: Point3, right: Point3) -> Point3:
    return (
        left[1] * right[2] - left[2] * right[1],
        left[2] * right[0] - left[0] * right[2],
        left[0] * right[1] - left[1] * right[0],
    )


def _normalized(vector: Point3) -> Point3:
    length = math.sqrt(sum(component * component for component in vector))
    if length <= 1e-15:
        raise ValueError("vector must be non-zero")
    return (vector[0] / length, vector[1] / length, vector[2] / length)


def ocs_axes(extrusion: Point3) -> tuple[Point3, Point3]:
    """OCS X and Y axes for an extrusion (DXF arbitrary-axis algorithm)."""
    if abs(extrusion[0]) < 1.0 / 64.0 and abs(extrusion[1]) < 1.0 / 64.0:
        x_axis = _normalized(_cross((0.0, 1.0, 0.0), extrusion))
    else:
        x_axis = _normalized(_cross(_Z_AXIS, extrusion))
    return x_axis, _cross(extrusion, x_axis)


def ocs_to_wcs(point: Point3, extrusion: Point3) -> Point3:
    """Map an OCS point to world coordinates."""
    if is_z_extrusion(extrusion) and extrusion[2] > 0:
        return point
    x_axis, y_axis = ocs_axes(extrusion)
    return tuple(  # type: ignore[return-value]
        point[0] * x_axis[axis] + point[1] * y_axis[axis] + point[2] * extrusion[axis]
        for axis in range(3)
    )


def matmul(left: Matrix4, right: Matrix4) -> Matrix4:
    return tuple(  # type: ignore[return-value]
        tuple(
            sum(left[row][k] * right[k][column] for k in range(4))
            for column in range(4)
        )
        for row in range(4)
    )


def ocs_matrix(extrusion: Point3) -> Matrix4:
    """Rotation that maps OCS axes to world axes (identity for +Z)."""
    if is_z_extrusion(extrusion) and extrusion[2] > 0:
        return IDENTITY
    x_axis, y_axis = ocs_axes(extrusion)
    return (
        (x_axis[0], y_axis[0], extrusion[0], 0.0),
        (x_axis[1], y_axis[1], extrusion[1], 0.0),
        (x_axis[2], y_axis[2], extrusion[2], 0.0),
        (0.0, 0.0, 0.0, 1.0),
    )


def insert_matrix(
    *,
    insert: Point3,
    extrusion: Point3,
    scale: Point3,
    rotation_deg: float,
    base_point: Point3,
    offset: Point3 = (0.0, 0.0, 0.0),
) -> Matrix4:
    """World placement of a block reference.

    ``world = OCS(extrusion) · T(insert + offset) · Rz(rotation) · S(scale) ·
    T(-base_point) · block``. The offset is the array cell displacement of a
    MINSERT, expressed in the rotated OCS frame of the reference.
    """
    radians = math.radians(rotation_deg)
    cos_r = math.cos(radians)
    sin_r = math.sin(radians)
    rotation: Matrix4 = (
        (cos_r, -sin_r, 0.0, 0.0),
        (sin_r, cos_r, 0.0, 0.0),
        (0.0, 0.0, 1.0, 0.0),
        (0.0, 0.0, 0.0, 1.0),
    )
    scaling: Matrix4 = (
        (scale[0], 0.0, 0.0, 0.0),
        (0.0, scale[1], 0.0, 0.0),
        (0.0, 0.0, scale[2], 0.0),
        (0.0, 0.0, 0.0, 1.0),
    )
    to_base: Matrix4 = (
        (1.0, 0.0, 0.0, -base_point[0]),
        (0.0, 1.0, 0.0, -base_point[1]),
        (0.0, 0.0, 1.0, -base_point[2]),
        (0.0, 0.0, 0.0, 1.0),
    )
    translation: Matrix4 = (
        (1.0, 0.0, 0.0, insert[0] + offset[0]),
        (0.0, 1.0, 0.0, insert[1] + offset[1]),
        (0.0, 0.0, 1.0, insert[2] + offset[2]),
        (0.0, 0.0, 0.0, 1.0),
    )
    local = matmul(translation, matmul(rotation, matmul(scaling, to_base)))
    return matmul(ocs_matrix(extrusion), local)


# ---------------------------------------------------------------------------
# curve sampling
# ---------------------------------------------------------------------------


def _segments_for_sweep(sweep_deg: float, curve_segments: int) -> int:
    fraction = min(abs(sweep_deg), 360.0) / 360.0
    return max(2, math.ceil(curve_segments * fraction))


def arc_points(
    center: Point3,
    radius: float,
    start_deg: float,
    end_deg: float,
    *,
    extrusion: Point3,
    curve_segments: int,
    full: bool = False,
) -> list[Point3]:
    """Sample a circle or counter-clockwise arc given in OCS coordinates."""
    if full:
        start_deg, end_deg = 0.0, 360.0
    sweep = (end_deg - start_deg) % 360.0
    if full or sweep <= _EPSILON:
        sweep = 360.0
    count = _segments_for_sweep(sweep, curve_segments)
    cx, cy, cz = center
    step = math.radians(sweep) / count
    start = math.radians(start_deg)
    cos, sin = math.cos, math.sin
    points: list[Point3] = [
        (
            cx + radius * cos(start + step * index),
            cy + radius * sin(start + step * index),
            cz,
        )
        for index in range(count + 1)
    ]
    if not (is_z_extrusion(extrusion) and extrusion[2] > 0):
        points = [ocs_to_wcs(point, extrusion) for point in points]
    if full:
        points.pop()
    return points


def bulge_polyline_points(
    vertices: Sequence[Point3],
    bulges: Sequence[float],
    *,
    closed: bool,
    extrusion: Point3,
    curve_segments: int,
) -> list[Point3]:
    """Expand bulge arcs of an OCS polyline into world-space points."""
    count = len(vertices)
    if count == 0:
        return []
    points: list[Point3] = []
    edge_count = count if closed else count - 1
    for index in range(edge_count):
        start = vertices[index]
        end = vertices[(index + 1) % count]
        bulge = float(bulges[index]) if index < len(bulges) else 0.0
        points.append(start)
        if abs(bulge) <= _EPSILON:
            continue
        chord_x = end[0] - start[0]
        chord_y = end[1] - start[1]
        chord = math.hypot(chord_x, chord_y)
        if chord <= _EPSILON:
            continue
        theta = 4.0 * math.atan(bulge)
        radius = chord / (2.0 * math.sin(abs(theta) / 2.0))
        mid_x = (start[0] + end[0]) / 2.0
        mid_y = (start[1] + end[1]) / 2.0
        # The centre sits on the left of the chord for a counter-clockwise arc
        # (positive bulge) and on the right for a clockwise one; for arcs wider
        # than a half turn cos(theta / 2) is negative and flips it across.
        distance = radius * math.cos(theta / 2.0) * (1.0 if theta > 0 else -1.0)
        normal_x = -chord_y / chord
        normal_y = chord_x / chord
        center_x = mid_x + normal_x * distance
        center_y = mid_y + normal_y * distance
        start_angle = math.atan2(start[1] - center_y, start[0] - center_x)
        steps = _segments_for_sweep(math.degrees(abs(theta)), curve_segments)
        for step in range(1, steps):
            angle = start_angle + theta * step / steps
            points.append(
                (
                    center_x + radius * math.cos(angle),
                    center_y + radius * math.sin(angle),
                    start[2],
                )
            )
    if not closed:
        points.append(vertices[-1])
    return [ocs_to_wcs(point, extrusion) for point in points]


def ellipse_points(
    center: Point3,
    major_axis: Point3,
    ratio: float,
    start_param: float,
    end_param: float,
    *,
    extrusion: Point3,
    curve_segments: int,
) -> list[Point3]:
    """Sample an ellipse arc given in world coordinates (DXF/DWG ELLIPSE)."""
    try:
        minor_direction = _normalized(_cross(extrusion, major_axis))
    except ValueError:
        return []
    major_length = math.sqrt(sum(component * component for component in major_axis))
    minor = tuple(component * major_length * ratio for component in minor_direction)
    sweep = (end_param - start_param) % (2.0 * math.pi)
    full = sweep <= _EPSILON
    if full:
        sweep = 2.0 * math.pi
    count = _segments_for_sweep(math.degrees(sweep), curve_segments)
    points: list[Point3] = []
    for index in range(count + 1):
        parameter = start_param + sweep * index / count
        cos_p = math.cos(parameter)
        sin_p = math.sin(parameter)
        points.append(
            tuple(  # type: ignore[arg-type]
                center[axis] + major_axis[axis] * cos_p + minor[axis] * sin_p
                for axis in range(3)
            )
        )
    if full:
        points.pop()
    return points


def _default_knots(control_point_count: int, degree: int) -> list[float]:
    inner = control_point_count - degree - 1
    return (
        [0.0] * (degree + 1)
        + [float(index) for index in range(1, inner + 1)]
        + [float(inner + 1)] * (degree + 1)
    )


def _find_span(parameter: float, degree: int, knots: list[float], count: int) -> int:
    """Index ``k`` with ``knots[k] <= u < knots[k + 1]`` inside the valid domain."""
    high = count  # knots[count] is the domain end
    if parameter >= knots[high]:
        return high - 1
    low = degree
    while low < high - 1:
        middle = (low + high) // 2
        if parameter < knots[middle]:
            high = middle
        else:
            low = middle
    return low


def _basis_functions(
    span: int, parameter: float, degree: int, knots: list[float]
) -> list[float]:
    """The ``degree + 1`` non-zero B-spline basis values at ``parameter`` (Cox-de Boor)."""
    basis = [1.0] + [0.0] * degree
    left = [0.0] * (degree + 1)
    right = [0.0] * (degree + 1)
    for order in range(1, degree + 1):
        left[order] = parameter - knots[span + 1 - order]
        right[order] = knots[span + order] - parameter
        saved = 0.0
        for index in range(order):
            denominator = right[index + 1] + left[order - index]
            if denominator == 0.0:
                temp = 0.0
            else:
                temp = basis[index] / denominator
            basis[index] = saved + right[index + 1] * temp
            saved = left[order - index] * temp
        basis[order] = saved
    return basis


def spline_points(
    control_points: Sequence[Point3],
    degree: int,
    knots: Sequence[float] | None,
    weights: Sequence[float] | None,
    *,
    closed: bool,
    curve_segments: int,
) -> list[Point3]:
    """Sample a (rational) B-spline in 3D; falls back to the control polygon."""
    controls = list(control_points)
    if len(controls) < 2:
        return []
    degree = max(1, min(int(degree), len(controls) - 1))
    count = len(controls)
    expected = count + degree + 1
    knot_values = [float(value) for value in knots] if knots else []
    if len(knot_values) != expected or any(
        knot_values[index] > knot_values[index + 1]
        for index in range(len(knot_values) - 1)
    ):
        knot_values = _default_knots(count, degree)
    weight_values = [float(value) for value in weights] if weights else []
    if len(weight_values) != count or any(value <= 0.0 for value in weight_values):
        weight_values = [1.0] * count
    start = knot_values[degree]
    end = knot_values[count]
    if end <= start:
        return controls + ([controls[0]] if closed else [])
    points: list[Point3] = []
    span_count = max(2, int(curve_segments))
    for sample in range(span_count + 1):
        parameter = (
            end if sample == span_count else start + (end - start) * sample / span_count
        )
        span = _find_span(parameter, degree, knot_values, count)
        basis = _basis_functions(span, parameter, degree, knot_values)
        denominator = 0.0
        x = y = z = 0.0
        for offset in range(degree + 1):
            index = span - degree + offset
            factor = basis[offset] * weight_values[index]
            if factor == 0.0:
                continue
            denominator += factor
            control = controls[index]
            x += factor * control[0]
            y += factor * control[1]
            z += factor * control[2]
        if abs(denominator) < 1e-15:
            continue
        points.append((x / denominator, y / denominator, z / denominator))
    if closed and points and points[0] != points[-1]:
        points.append(points[0])
    return points if len(points) >= 2 else controls


# ---------------------------------------------------------------------------
# faces
# ---------------------------------------------------------------------------

Triangle = tuple[Point3, Point3, Point3]


def _distinct(points: Sequence[Point3]) -> list[Point3]:
    kept: list[Point3] = []
    for point in points:
        if kept and all(
            abs(point[axis] - kept[-1][axis]) <= _EPSILON for axis in range(3)
        ):
            continue
        kept.append(point)
    if len(kept) > 1 and all(
        abs(kept[0][axis] - kept[-1][axis]) <= _EPSILON for axis in range(3)
    ):
        kept.pop()
    return kept


def _is_degenerate(triangle: Triangle) -> bool:
    a, b, c = triangle
    u = (b[0] - a[0], b[1] - a[1], b[2] - a[2])
    v = (c[0] - a[0], c[1] - a[1], c[2] - a[2])
    normal = _cross(u, v)
    return all(abs(component) <= 1e-18 for component in normal)


def polygon_triangles(points: Sequence[Point3]) -> list[Triangle]:
    """Fan-triangulate a (planar, convex enough) polygon; drops degenerate parts."""
    corners = _distinct(points)
    if len(corners) < 3:
        return []
    triangles: list[Triangle] = []
    for index in range(1, len(corners) - 1):
        triangle = (corners[0], corners[index], corners[index + 1])
        if not _is_degenerate(triangle):
            triangles.append(triangle)
    return triangles


def face3d_triangles(points: Sequence[Point3]) -> list[Triangle]:
    """3DFACE corners are given in outline order (unlike SOLID's bow-tie order)."""
    return polygon_triangles(points)


def solid_triangles(points: Sequence[Point3]) -> list[Triangle]:
    """SOLID/TRACE corners come in bow-tie order 1, 2, 4, 3."""
    corners = list(points)
    if len(corners) == 4:
        corners = [corners[0], corners[1], corners[3], corners[2]]
    return polygon_triangles(corners)


def polyface_triangles(
    vertices: Sequence[Point3], faces: Sequence[Sequence[int]]
) -> list[Triangle]:
    """Triangles of a polyface mesh (1-based indices, negative = hidden edge)."""
    triangles: list[Triangle] = []
    for face in faces:
        corners: list[Point3] = []
        for raw_index in face:
            try:
                index = abs(int(raw_index))
            except (TypeError, ValueError):
                continue
            if 1 <= index <= len(vertices):
                corners.append(vertices[index - 1])
        triangles.extend(polygon_triangles(corners))
    return triangles


def grid_triangles(
    points: Sequence[Point3],
    m_count: int,
    n_count: int,
    *,
    closed_m: bool = False,
    closed_n: bool = False,
) -> list[Triangle]:
    """Triangles of an M×N polygon mesh (points stored row by row)."""
    if m_count < 2 or n_count < 2 or len(points) < m_count * n_count:
        return []
    triangles: list[Triangle] = []
    rows = m_count if closed_m else m_count - 1
    columns = n_count if closed_n else n_count - 1
    for row in range(rows):
        next_row = (row + 1) % m_count
        for column in range(columns):
            next_column = (column + 1) % n_count
            a = points[row * n_count + column]
            b = points[row * n_count + next_column]
            c = points[next_row * n_count + next_column]
            d = points[next_row * n_count + column]
            triangles.extend(polygon_triangles([a, b, c, d]))
    return triangles


def is_planar(points: Sequence[Point3]) -> bool:
    """Whether every point shares one z (the curve lies in a plane parallel to XY)."""
    if not points:
        return True
    first = points[0][2]
    return all(abs(point[2] - first) <= _EPSILON for point in points)


# ---------------------------------------------------------------------------
# collector
# ---------------------------------------------------------------------------


@dataclass(slots=True)
class _Scope:
    curves: list[dict[str, Any]] = field(default_factory=list)
    faces: list[dict[str, Any]] = field(default_factory=list)
    inserts: list[dict[str, Any]] = field(default_factory=list)


@dataclass(slots=True)
class _PendingInsert:
    scope: Any
    entity: dict[str, Any] | None
    source_id: str | None
    block: str
    insert: Point3
    extrusion: Point3
    scale: Point3
    rotation_deg: float
    cells: list[tuple[int, int, Point3]]


class Model3DCollector:
    """Accumulates the 3D side channel for one import.

    Scopes are ``None`` for model space or an opaque key for a block
    definition (the DWG importer uses block header handles and maps them to
    the final IR block names at :meth:`finalize`). Entries keep a reference to
    their IR entity so ids assigned later are picked up when finalizing.
    """

    def __init__(self, *, curve_segments: int = 96) -> None:
        self.curve_segments = max(8, int(curve_segments))
        self._scopes: dict[Any, _Scope] = {}
        self._pending_inserts: list[_PendingInsert] = []
        self._block_base_points: dict[Any, Point3] = {}
        self.unsupported: Counter[str] = Counter()

    # -- registration --------------------------------------------------------

    def _scope(self, key: Any) -> _Scope:
        scope = self._scopes.get(key)
        if scope is None:
            scope = _Scope()
            self._scopes[key] = scope
        return scope

    def set_block_base_point(self, scope: Any, base_point: Point3) -> None:
        self._block_base_points[scope] = base_point

    def add_curve(
        self,
        scope: Any,
        *,
        entity: dict[str, Any] | None,
        source_id: str | None,
        kind: str,
        points: Sequence[Point3],
        closed: bool = False,
    ) -> None:
        if len(points) < 2:
            return
        coordinates = [[float(x), float(y), float(z)] for x, y, z in points]
        first_z = coordinates[0][2]
        planar = all(abs(point[2] - first_z) <= _EPSILON for point in coordinates)
        self._scope(scope).curves.append(
            {
                "entity": entity,
                "source_id": source_id,
                "kind": kind,
                "closed": bool(closed),
                "planar": planar,
                "points": coordinates,
            }
        )

    def add_faces(
        self,
        scope: Any,
        *,
        entity: dict[str, Any] | None,
        source_id: str | None,
        kind: str,
        triangles: Sequence[Triangle],
    ) -> None:
        if not triangles:
            return
        self._scope(scope).faces.append(
            {
                "entity": entity,
                "source_id": source_id,
                "kind": kind,
                "triangles": [[list(corner) for corner in tri] for tri in triangles],
            }
        )

    def add_insert(
        self,
        scope: Any,
        *,
        entity: dict[str, Any] | None,
        source_id: str | None,
        block: str,
        insert: Point3,
        extrusion: Point3 = _Z_AXIS,
        scale: Point3 = (1.0, 1.0, 1.0),
        rotation_deg: float = 0.0,
        columns: int = 1,
        rows: int = 1,
        column_spacing: float = 0.0,
        row_spacing: float = 0.0,
    ) -> None:
        cells: list[tuple[int, int, Point3]] = []
        radians = math.radians(rotation_deg)
        cos_r = math.cos(radians)
        sin_r = math.sin(radians)
        for row in range(max(1, int(rows))):
            for column in range(max(1, int(columns))):
                local_x = column * column_spacing
                local_y = row * row_spacing
                cells.append(
                    (
                        column,
                        row,
                        (
                            local_x * cos_r - local_y * sin_r,
                            local_x * sin_r + local_y * cos_r,
                            0.0,
                        ),
                    )
                )
        self._pending_inserts.append(
            _PendingInsert(
                scope=scope,
                entity=entity,
                source_id=source_id,
                block=block,
                insert=insert,
                extrusion=extrusion,
                scale=scale,
                rotation_deg=float(rotation_deg),
                cells=cells,
            )
        )

    def add_unsupported(self, kind: str, count: int = 1) -> None:
        if count > 0:
            self.unsupported[kind] += int(count)

    # -- output --------------------------------------------------------------

    def finalize(
        self,
        *,
        units: str | None,
        block_names: Mapping[Any, str] | None = None,
    ) -> dict[str, Any]:
        """Resolve ids and block names; return the side-channel dictionary."""
        names = dict(block_names or {})

        def block_name_of(key: Any) -> str:
            return names.get(key, key if isinstance(key, str) else str(key))

        def base_point_of(block: str) -> Point3:
            for key, base in self._block_base_points.items():
                if block_name_of(key) == block:
                    return base
            return (0.0, 0.0, 0.0)

        for pending in self._pending_inserts:
            base = base_point_of(pending.block)
            scope = self._scope(pending.scope)
            for column, row, offset in pending.cells:
                matrix = insert_matrix(
                    insert=pending.insert,
                    extrusion=pending.extrusion,
                    scale=pending.scale,
                    rotation_deg=pending.rotation_deg,
                    base_point=base,
                    offset=offset,
                )
                record: dict[str, Any] = {
                    "entity": pending.entity,
                    "source_id": pending.source_id,
                    "block": pending.block,
                    "matrix": [list(row_values) for row_values in matrix],
                }
                if len(pending.cells) > 1:
                    record["cell"] = [column, row]
                scope.inserts.append(record)
        self._pending_inserts.clear()

        def finish(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
            finished: list[dict[str, Any]] = []
            for item in items:
                entity = item.pop("entity", None)
                record = {
                    "id": entity.get("id") if isinstance(entity, dict) else None,
                    "layer": entity.get("layer") if isinstance(entity, dict) else None,
                    **item,
                }
                finished.append(record)
            return finished

        model_scope = self._scopes.get(None, _Scope())
        blocks: dict[str, dict[str, Any]] = {}
        for key, scope in self._scopes.items():
            if key is None:
                continue
            blocks[block_name_of(key)] = {
                "curves": finish(scope.curves),
                "faces": finish(scope.faces),
                "inserts": finish(scope.inserts),
            }
        for key, base in self._block_base_points.items():
            name = block_name_of(key)
            if name in blocks:
                blocks[name]["base_point"] = list(base)

        curves = finish(model_scope.curves)
        faces = finish(model_scope.faces)
        inserts = finish(model_scope.inserts)
        all_curves = curves + [
            curve for block in blocks.values() for curve in block["curves"]
        ]
        all_faces = faces + [
            face for block in blocks.values() for face in block["faces"]
        ]
        elevations = {
            round(curve["points"][0][2], 6) for curve in all_curves if curve["planar"]
        }
        stats = {
            "curves_total": len(all_curves),
            "curves_3d": sum(not curve["planar"] for curve in all_curves),
            "elevations": len(elevations),
            "faces": sum(len(face["triangles"]) for face in all_faces),
            "inserts": len(inserts)
            + sum(len(block["inserts"]) for block in blocks.values()),
            "unsupported": sum(self.unsupported.values()),
        }
        result: dict[str, Any] = {
            "version": SIDE_CHANNEL_VERSION,
            "units": units,
            "curves": curves,
            "faces": faces,
            "inserts": inserts,
            "blocks": blocks,
            "unsupported": dict(sorted(self.unsupported.items())),
            "stats": stats,
        }
        self._scopes.clear()
        return result


def has_3d_content(model3d: Mapping[str, Any] | None) -> bool:
    """Whether a side channel holds anything a 2D drawing cannot show."""
    if not model3d:
        return False
    stats = model3d.get("stats", {})
    return bool(
        stats.get("curves_3d")
        or stats.get("faces")
        or stats.get("unsupported")
        or (stats.get("elevations") or 0) > 1
    )


__all__ = [
    "IDENTITY",
    "SIDE_CHANNEL_VERSION",
    "UNSUPPORTED_3D_KINDS",
    "Model3DCollector",
    "arc_points",
    "bulge_polyline_points",
    "ellipse_points",
    "face3d_triangles",
    "grid_triangles",
    "has_3d_content",
    "insert_matrix",
    "is_planar",
    "matmul",
    "normalize_extrusion",
    "ocs_to_wcs",
    "point3",
    "polyface_triangles",
    "polygon_triangles",
    "solid_triangles",
    "spline_points",
]
