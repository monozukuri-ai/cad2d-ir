"""Bounded HP-GL plot import. Parsing and text placement belong to ezhpgl's Rust core.

Each plot page stays in its own paper-space layout, in millimetres. This adapter
maps the resulting geometry to IR and records any additional conversion loss.
"""

from __future__ import annotations

import hashlib
import math
from collections import Counter
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

_PATTERNS = {
    1: (0, 100),
    2: (50, 50),
    3: (70, 30),
    4: (80, 10, 0, 10),
    5: (70, 10, 10, 10),
    6: (50, 10, 10, 10, 10, 10),
    7: (70, 10, 0, 10, 0, 10),
    8: (50, 10, 0, 10, 10, 10, 0, 10),
}


def _backend() -> Any:
    try:
        import ezhpgl
    except ImportError as exc:
        raise MissingOptionalDependencyError(
            "HP-GL import requires ezhpgl; install cad2d-ir[hpgl]."
        ) from exc
    return ezhpgl


def _xy(p: Any) -> list[float]:
    return [p.x, p.y]


def _span(s: Any) -> list[int]:
    return [s.start, s.end]


def _font(font: Any) -> dict[str, Any]:
    return {
        key: getattr(font, key)
        for key in (
            "symbol_set",
            "spacing",
            "pitch",
            "height_points",
            "posture",
            "weight",
            "typeface",
        )
    }


def _text_state(state: Any) -> dict[str, Any]:
    result = {
        key: getattr(state, key)
        for key in (
            "use_alternate",
            "size_mode",
            "width",
            "height",
            "relative_direction",
            "origin",
            "slant",
            "path",
            "line",
            "transparent",
        )
    }
    result.update(
        standard=_font(state.standard),
        alternate=_font(state.alternate),
        direction=_xy(state.direction),
        extra_space=_xy(state.extra_space),
        carriage_return=_xy(state.carriage_return),
    )
    return result


def _clip_box(clip: Any) -> list[float] | None:
    if clip is None:
        return None
    if clip.kind == "empty":
        return []
    b = clip.bounds
    if b.min.x >= b.max.x or b.min.y >= b.max.y:
        return []
    return [b.min.x, b.min.y, b.max.x, b.max.y]


def _inside(p: list[float], box: list[float] | None) -> bool:
    return box is None or bool(
        box and box[0] <= p[0] <= box[2] and box[1] <= p[1] <= box[3]
    )


def _segment(
    a: list[float], b: list[float], box: list[float] | None
) -> tuple[list[float], list[float]] | None:
    if box is None:
        return a, b
    if not box:
        return None
    dx, dy = b[0] - a[0], b[1] - a[1]
    lo, hi = 0.0, 1.0
    for p, q in (
        (-dx, a[0] - box[0]),
        (dx, box[2] - a[0]),
        (-dy, a[1] - box[1]),
        (dy, box[3] - a[1]),
    ):
        if p == 0:
            if q < 0:
                return None
        elif p < 0:
            lo = max(lo, q / p)
        else:
            hi = min(hi, q / p)
        if lo > hi:
            return None
    return [a[0] + lo * dx, a[1] + lo * dy], [a[0] + hi * dx, a[1] + hi * dy]


def _curve_entity(curve: Any) -> dict[str, Any] | None:
    if curve.kind == "cubic_bezier":
        return {
            "kind": "SPLINE",
            "degree": 3,
            "control_points": [_xy(p) for p in curve.control_points],
            "knots": [0.0] * 4 + [1.0] * 4,
            "closed": False,
        }
    u, v = _xy(curve.axis_u), _xy(curve.axis_v)
    a, b = math.hypot(*u), math.hypot(*v)
    if min(a, b) <= 1e-12 or abs(curve.sweep_angle_deg) <= 1e-12:
        return None
    # The core's SC/RO pipeline keeps ellipse axes perpendicular.
    if abs(u[0] * v[0] + u[1] * v[1]) > 1e-9 * a * b:
        return None
    t = math.radians(curve.start_angle_deg)
    first = [u[i] * math.cos(t) + v[i] * math.sin(t) for i in (0, 1)]
    sweep = math.copysign(1, u[0] * v[1] - u[1] * v[0]) * curve.sweep_angle_deg
    center = _xy(curve.center)
    if math.isclose(a, b, rel_tol=1e-10, abs_tol=1e-12):
        if math.isclose(abs(sweep), 360, abs_tol=1e-9):
            return {"kind": "CIRCLE", "center": center, "radius": a}
        start = math.degrees(math.atan2(first[1], first[0]))
        end = start + sweep
        if sweep < 0:
            start, end = end, start
        return {
            "kind": "ARC",
            "center": center,
            "radius": a,
            "start_angle": start % 360,
            "end_angle": end % 360,
        }
    major, radius, minor_radius = (u, a, b) if a >= b else (v, b, a)
    minor = [-major[1] * minor_radius / radius, major[0] * minor_radius / radius]
    start = (
        math.atan2(
            (first[0] * minor[0] + first[1] * minor[1]) / minor_radius**2,
            (first[0] * major[0] + first[1] * major[1]) / radius**2,
        )
        % math.tau
    )
    return {
        "kind": "ELLIPSE",
        "center": center,
        "major_axis": major,
        "ratio": minor_radius / radius,
        "start_param": start,
        "end_param": start + math.radians(sweep),
        "ccw": sweep > 0,
    }


def _curve_inside(curve: Any, box: list[float] | None) -> bool:
    if box is None:
        return True
    if not box:
        return False
    if curve.kind == "cubic_bezier":
        return all(_inside(_xy(p), box) for p in curve.control_points)
    c, u, v = curve.center, curve.axis_u, curve.axis_v
    dx, dy = math.hypot(u.x, v.x), math.hypot(u.y, v.y)
    return _inside([c.x - dx, c.y - dy], box) and _inside([c.x + dx, c.y + dy], box)


class _Converter:
    def __init__(self, options: ImportOptions, max_entities: int) -> None:
        if (
            isinstance(max_entities, bool)
            or not isinstance(max_entities, int)
            or max_entities < 0
        ):
            raise ValueError("max_entities must be a non-negative integer")
        self.options = options
        self.max_entities = max_entities
        self.count = 0
        self.counts: Counter[str] = Counter()
        self.diagnostics: list[ImportDiagnostic] = []
        self.notes: dict[str, ImportDiagnostic] = {}
        self.layers: dict[str, Any] = {}
        self.linetypes: dict[str, Any] = {}
        self.blocks: dict[str, Any] = {}
        self.current: list[dict[str, Any]] = []
        self.base: dict[str, Any] = {}
        self.source: dict[str, Any] = {}
        self.loss: set[str] = set()
        self.dot_mode = False

    def note(self, code: str, message: str, action: str = "approximated") -> None:
        self.loss.add(code)
        if code in self.notes:
            self.notes[code].details["occurrences"] += 1
            return
        d = ImportDiagnostic(
            code=code,
            severity="warning",
            message=message,
            action=action,
            source_id=self.source.get("id"),
            source_kind=self.source.get("kind"),
            details={"occurrences": 1, "first_source": self.source},
        )
        self.notes[code] = d
        self.diagnostics.append(d)

    def emit(
        self, shape: dict[str, Any], *, target: list[dict[str, Any]] | None = None
    ) -> dict[str, Any]:
        if self.count >= self.max_entities:
            raise ImporterError(
                "HP-GL IR entity limit exceeded (including block contents)"
            )
        self.count += 1
        entity = {**self.base, **shape, "id": f"HP{self.count}"}
        if self.options.entity_provenance:
            entity["source"] = self.source
        if self.loss:
            entity["approximation"] = {
                "method": "other",
                "source_kind": self.source.get("kind", "HPGL"),
                "metadata": {"diagnostic_codes": sorted(self.loss)},
            }
        (self.current if target is None else target).append(entity)
        self.counts[entity["kind"]] += 1
        return entity

    def path(self, points: list[list[float]], box: list[float] | None) -> None:
        if not points:
            return
        if len(points) == 1 or self.dot_mode:
            for point in (points[1:] or points) if self.dot_mode else points:
                if not _inside(point, box):
                    continue
                self.note(
                    "HPGL_DOT_APPROXIMATED", "Pen dots use DXF POINT display semantics."
                )
                self.emit({"kind": "POINT", "position": point})
            return
        run: list[list[float]] = []

        def flush() -> None:
            if len(run) == 2:
                self.emit({"kind": "LINE", "p1": run[0], "p2": run[1]})
            elif run:
                closed = len(run) > 2 and run[0] == run[-1]
                self.emit(
                    {
                        "kind": "LWPOLYLINE",
                        "vertices": list(run[:-1] if closed else run),
                        "closed": closed,
                    }
                )
            run.clear()

        for a, b in pairwise(points):
            part = _segment(a, b, box)
            if part is None:
                flush()
                continue
            first, last = part
            if run and run[-1] != first:
                flush()
            if not run:
                run.append(first)
            run.append(last)
        flush()

    def operation(self, op: Any, page: int, index: int) -> None:
        self.loss = set()
        self.source = {
            "format": "hpgl",
            "id": f"page:{page}:op:{index}",
            "kind": op.source.command,
            "metadata": {
                "page_index": page,
                "operation_index": index,
                "command_index": op.source.command_index,
                "span": _span(op.source.span),
                "pen": op.style.pen,
                "path_id": op.path_id,
            },
        }
        style = op.style
        self.dot_mode = style.line is not None and style.line.line_type == 0
        layer = f"HPGL_PEN_{style.pen}"
        self.layers.setdefault(layer, {"color": 7})
        self.base = {
            "layer": layer,
            "color": style.color,
            "lineweight_mm": style.width_mm,
        }
        if op.approximate:
            self.loss.add("HPGL_SOURCE_APPROXIMATION")
        if style.line is not None:
            line = style.line
            self.note(
                "HPGL_LINE_STYLE_APPROXIMATED",
                "IR retains nominal dash lengths; per-path phase, adaptive repeats, caps and joins may differ.",
            )
            kind = line.line_type
            if kind not in (None, 0):
                key = f"HPGL_LT_{abs(kind)}_{line.pattern_length_mm:.12g}"
                pattern = [
                    v * line.pattern_length_mm / 100 * (1 if i % 2 == 0 else -1)
                    for i, v in enumerate(_PATTERNS[abs(kind)])
                ]
                self.linetypes[key] = {"pattern_mm": pattern}
                self.base["linetype"] = key
        box = _clip_box(op.clip)
        if box == []:
            return
        if box is not None:
            self.note(
                "HPGL_CLIP_APPROXIMATED",
                "Rectangular clips trim centerlines; DXF stroke edges and clipped caps may differ.",
            )
        g = op.geometry
        if g.kind == "label":
            self.label(g.label, box)
        elif g.kind == "polygon":
            self.polygon(g, box)
        else:
            points = [_xy(p) for p in g.points]
            if self.dot_mode:
                self.path(points, box)
                return
            if g.start_dot and len(points) > 1:
                self.path(points[:1], box)
            if g.curve is not None and _curve_inside(g.curve, box):
                shape = _curve_entity(g.curve)
                if shape is not None:
                    self.emit(shape)
                    return
            if g.curve is not None:
                self.note(
                    "HPGL_CURVE_APPROXIMATED",
                    "A clipped or degenerate curve uses the core's bounded preview vertices.",
                )
            self.path(points, box)

    def polygon(self, geometry: Any, box: list[float] | None) -> None:
        contours = geometry.contours
        fill = geometry.fill
        if fill is not None and fill.fill_type >= 3:
            self.note(
                "HPGL_HATCH_EXPLODED",
                "Hatch/crosshatch uses the core's explicit paint segments.",
                "exploded",
            )
            for a, b in fill.hatch_lines:
                self.path([_xy(a), _xy(b)], box)
            return
        if fill is not None:
            loops = [[_xy(p) for p in c.points] for c in contours if len(c.points) >= 3]
            usable = []
            for loop in loops:
                origin = loop[0]
                direction = next((p for p in loop if p != origin), origin)
                dx, dy = direction[0] - origin[0], direction[1] - origin[1]
                if all(
                    abs(dx * (p[1] - origin[1]) - dy * (p[0] - origin[0])) <= 1e-12
                    for p in loop
                ):
                    self.note(
                        "HPGL_FILL_OMITTED",
                        "A collinear fill's device-specific stroke fallback was omitted.",
                        "skipped",
                    )
                else:
                    usable.append(loop)
            loops = usable
            if not loops:
                return
            if any(not _inside(p, box) for loop in loops for p in loop):
                self.note(
                    "HPGL_FILL_OMITTED",
                    "A fill crossing the clip boundary cannot be represented safely as IR HATCH; it was omitted.",
                    "skipped",
                )
                return
            if fill.rule != "evenodd":
                self.note(
                    "HPGL_FILL_OMITTED",
                    "Nonzero winding fills are outside the IR even-odd HATCH contract; they were omitted.",
                    "skipped",
                )
                return
            if any(e.curve is not None for c in contours for e in c.edges):
                self.note(
                    "HPGL_CURVE_APPROXIMATED",
                    "Curved fill boundaries use the core's preview vertices.",
                )
            self.emit(
                {
                    "kind": "HATCH",
                    "solid": True,
                    "loops": [{"vertices": p} for p in loops],
                }
            )
            return
        for contour in contours:
            points = [_xy(p) for p in contour.points]
            run: list[list[float]] = []
            for e in contour.edges:
                if e.pen_down:
                    if e.curve is not None:
                        self.note(
                            "HPGL_CURVE_APPROXIMATED",
                            "Polygon edge curves use the core's preview vertices.",
                        )
                    part = points[e.start : e.end + 1]
                    run.extend(
                        part[1:] if run and part and run[-1] == part[0] else part
                    )
                elif run:
                    self.path(run, box)
                    run = []
            self.path(run, box)

    def label(self, label: Any, box: list[float] | None) -> None:
        self.note(
            "HPGL_TEXT_APPROXIMATED",
            "Each glyph becomes substitute-font TEXT; native font metrics and outlines are not reproduced.",
        )
        for glyph in label.glyphs:
            if glyph.character.isspace():
                continue
            if any(not _inside(_xy(p), box) for p in glyph.envelope):
                self.note(
                    "HPGL_TEXT_CLIP_OMITTED",
                    "A glyph whose preview envelope crosses/is outside the clip was omitted; IR TEXT has no clip.",
                    "skipped",
                )
                continue
            x, y = glyph.axis_x, glyph.axis_y
            width = math.hypot(x.x, x.y)
            if width == 0:
                continue
            ux, uy = x.x / width, x.y / width
            rise, shear = ux * y.y - uy * y.x, ux * y.x + uy * y.y
            if abs(rise) <= 1e-12:
                continue
            shape = {
                "kind": "TEXT",
                "insert": _xy(glyph.origin),
                "text": glyph.character,
                "height": abs(rise),
                "rotation": math.degrees(math.atan2(uy, ux)),
                "width_factor": width / abs(rise),
                "oblique_deg": math.degrees(math.atan2(shear, abs(rise))),
                "style": "HPGL_FALLBACK",
            }
            self.source = {
                **self.source,
                "metadata": {
                    **self.source["metadata"],
                    "glyph_offset": glyph.source_offset,
                    "font": _font(glyph.font),
                    "axis_x": _xy(x),
                    "axis_y": _xy(y),
                },
            }
            if rise < 0:
                # Negative text axes use a block reflection; TEXT width/height stay positive.
                name = f"HPGL_GLYPH_{self.count + 1}"
                content: list[dict[str, Any]] = []
                self.emit(
                    {**shape, "insert": [0.0, 0.0], "rotation": 0.0}, target=content
                )
                self.blocks[name] = {"base_point": [0.0, 0.0], "entities": content}
                self.emit(
                    {
                        "kind": "INSERT",
                        "block": name,
                        "insert": shape["insert"],
                        "scale": [1.0, -1.0],
                        "rotation": shape["rotation"],
                    }
                )
            else:
                self.emit(shape)


def hpgl_document_to_ir(
    plot: Any,
    *,
    options: ImportOptions | None = None,
    source_name: str | None = None,
    source_sha256: str | None = None,
    max_entities: int = 200_000,
) -> ImportResult:
    """Map an ezhpgl PlotDocument to paper layouts, with explicit conversion losses."""
    backend = _backend()
    if not isinstance(plot, backend.PlotDocument):
        raise TypeError("plot must be an ezhpgl.PlotDocument")
    options = options or ImportOptions()
    c = _Converter(options, max_entities)
    for d in plot.diagnostics:
        c.diagnostics.append(
            ImportDiagnostic(
                code="HPGL_SOURCE_DIAGNOSTIC",
                severity="error" if d.action == "stop_interpretation" else "warning",
                message=d.message,
                source_kind=d.command,
                action=d.action,
                details={"code": d.code, "span": _span(d.span)},
            )
        )
    source: dict[str, Any] = {"format": "hpgl", "version": backend.__version__}
    if source_name is not None:
        source["name"] = source_name
    if source_sha256 is not None:
        source["sha256"] = source_sha256
    doc: dict[str, Any] = {
        "format": "cad2d-ir",
        "version": options.ir_version,
        "source": source,
        "header": {"units": "mm", "angle_unit": "deg", "coord_space": "world"},
        "entities": [],
        "layouts": [],
        "tables": {
            "layers": c.layers,
            "linetypes": c.linetypes,
            "blocks": c.blocks,
            "text_styles": {"HPGL_FALLBACK": {"font": "monospace"}},
        },
    }
    for page in plot.pages:
        c.current = []
        operations: list[dict[str, Any]] = []
        labels: list[dict[str, Any]] = []
        for index, op in enumerate(page.operations):
            operations.append(
                {
                    "index": index,
                    "command": op.source.command,
                    "span": _span(op.source.span),
                    "command_index": op.source.command_index,
                    "path_id": op.path_id,
                    "pen": op.style.pen,
                    "width_mm": op.style.width_mm,
                    "color": op.style.color,
                    "approximate": op.approximate,
                    "clip_mm": _clip_box(op.clip),
                }
            )
            if op.style.line is not None:
                operations[-1]["line"] = {
                    key: getattr(op.style.line, key)
                    for key in (
                        "line_type",
                        "pattern_length_mm",
                        "phase_mm",
                        "cap",
                        "join",
                        "miter_limit",
                    )
                }
            if op.geometry.label is not None:
                label = op.geometry.label
                labels.append(
                    {
                        "operation_index": index,
                        "data_hex": label.data.hex(),
                        "payload_span": _span(label.payload_span),
                        "terminator": label.terminator,
                        "print_terminator": label.print_terminator,
                        "state": _text_state(label.state),
                    }
                )
            c.operation(op, page.index, index)
        paper: dict[str, Any] = {"units": "mm"}
        if page.paper_size_mm is not None:
            paper["size_mm"] = list(page.paper_size_mm)
        doc["layouts"].append(
            {
                "name": f"Page{page.index + 1}",
                "tab_order": page.index + 1,
                "active": page.index == 0,
                "paper": paper,
                "entities": c.current,
                "metadata": {
                    "hpgl": {
                        "page_index": page.index,
                        "plot_size_mm": page.plot_size_mm,
                        "omitted_content": page.omitted_content,
                        "operations": operations,
                        "labels": labels,
                    }
                },
            }
        )
    complete = plot.completeness == "complete" and not c.diagnostics
    stats = {
        "source_format": "hpgl",
        "completeness": "complete" if complete else "partial",
        "source_completeness": plot.completeness,
        "pages": len(plot.pages),
        "source_operations": plot.stats()["operations"],
        "converted_entities": c.count,
        "converted_entity_counts": dict(sorted(c.counts.items())),
        "interpreted_commands": plot.interpreted_commands,
    }
    source["metadata"] = {
        "hpgl": {
            **stats,
            "diagnostics": [d.as_dict() for d in c.diagnostics],
            "input_events": [
                {
                    "kind": e.kind,
                    "span": _span(e.span),
                    "command": e.command,
                    "payload_span": _span(e.payload_span)
                    if e.payload_span is not None
                    else None,
                }
                for e in plot.input_events
            ],
        }
    }
    if options.validate:
        validate_ir(doc)
    return ImportResult(doc, c.diagnostics, stats)


def convert_hpgl_file_to_ir(
    path: str | Path,
    *,
    options: ImportOptions | None = None,
    profile: Any = None,
    limits: Any = None,
    geometry_limits: Any = None,
    max_entities: int = 200_000,
) -> ImportResult:
    """Read bounded binary input and convert all pages. File suffix never proves HP-GL."""
    backend = _backend()
    options = options or ImportOptions()
    limit = limits if limits is not None else backend.Limits()
    with Path(path).open("rb") as stream:
        data = stream.read(limit.max_input_bytes + 1)
    try:
        plot = backend.parse(
            data,
            strict=options.strict,
            profile=profile,
            limits=limit,
            geometry_limits=geometry_limits,
        )
    except backend.ParseError as exc:
        d = exc.diagnostic
        raise ImporterError(
            f"HP-GL {d.code} at bytes {d.span.start}:{d.span.end}: {d.message}"
        ) from exc
    return hpgl_document_to_ir(
        plot,
        options=options,
        source_name=Path(path).name,
        source_sha256=hashlib.sha256(data).hexdigest(),
        max_entities=max_entities,
    )
