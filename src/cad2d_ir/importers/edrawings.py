"""eDrawings drawings (``.edrw``) to IR through ``edrawings-kit``.

An eDrawings drawing is a HOOPS Stream File (alone, inside a ZIP container or
behind a short preamble) whose segment tree holds one ``sheetN`` segment per
drawing sheet; each sheet has ``viewN`` / ``borderviewN`` children whose
``2dmodel`` subtrees carry the sheet-space line work (lines, polylines,
polygons, circles, arcs, markers and text) and whose ``3dmodel`` subtrees carry
the model the view projects (not drawn here). Coordinates are stored in metres
by the SOLIDWORKS writers; the kit's drawing-plane profile, the ``fit width`` /
``fit height`` options or a paper-size fit decide the unit.

The importer composes each sheet in paper millimetres (the chain of modelling
matrices, as eDrawings draws it), takes the first drawing sheet as the IR model
space and the other sheets as ``layouts``, and divides the drawing by the
dominant view scale so the main view is at real size (like the iCAD SX
importer). Dimensions arrive as the lines, arrowheads and text the writer saved;
text that the writer stroked into polylines (``textasgeom`` segments, old
``DXF/DWG Reader`` files) stays geometry.
"""

from __future__ import annotations

import dataclasses
import hashlib
import math
import re
import struct
from collections import Counter, defaultdict
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

Point = tuple[float, float]
Matrix = tuple[float, ...]

TEXT_STYLE = "EDRW"
DEFAULT_TEXT_HEIGHT_MM = 2.5
"""Paper height of a text whose font size is not stored in a usable unit."""
MIN_LENGTH_MM = 0.005
"""Segments shorter than this (the writer's zero-length gap marks) are dropped."""
PAPER_SIZES: dict[str, tuple[float, float]] = {
    # ISO A, landscape (width, height) in millimetres
    "A5": (210.0, 148.0),
    "A4": (297.0, 210.0),
    "A3": (420.0, 297.0),
    "A2": (594.0, 420.0),
    "A1": (841.0, 594.0),
    "A0": (1189.0, 841.0),
    # JIS B
    "B4": (364.0, 257.0),
    "B3": (515.0, 364.0),
    "B2": (728.0, 515.0),
    "B1": (1030.0, 728.0),
    # ANSI (inch sheets)
    "ANSI_A": (279.4, 215.9),
    "ANSI_B": (431.8, 279.4),
    "ANSI_C": (558.8, 431.8),
    "ANSI_D": (863.6, 558.8),
    "ANSI_E": (1117.6, 863.6),
    # Architectural
    "ARCH_A": (304.8, 228.6),
    "ARCH_B": (457.2, 304.8),
    "ARCH_C": (609.6, 457.2),
    "ARCH_D": (914.4, 609.6),
    "ARCH_E": (1219.2, 914.4),
}
_PAPER_TOLERANCE_MM = 3.0
# Smaller sheets first so the smallest paper that holds the geometry is found
_PAPER_BY_AREA = sorted(PAPER_SIZES.items(), key=lambda kv: kv[1][0] * kv[1][1])
_INCH_SERIES = ("ANSI_", "ARCH_")
# millimetres per stored unit, by the unit a writer may have used
_UNIT_SCALES: dict[str, float] = {
    "meter": 1000.0,
    "centimeter": 10.0,
    "millimeter": 1.0,
    "inch": 25.4,
}
_UNIT_CANDIDATES = ("meter", "inch", "millimeter")
# `length` user option of the root: the unit eDrawings displays dimensions in
_DISPLAY_LENGTH_UNITS = {"3": "mm", "4": "cm", "5": "m", "8": "inch"}
# HOOPS geometry slots of the colour / visibility arrays (TKO_Geo_* bit order)
_SLOT_FACES, _SLOT_EDGES, _SLOT_LINES, _SLOT_MARKERS, _SLOT_TEXT = 0, 1, 2, 3, 4
_FONT_SIZE_WSU = 6  # TKO_Font_Size_WSU: the size is in world (sheet) units
_FONT_SIZE_POINTS = 3
_POINT_MM = 25.4 / 72.0
_SEGMENT_ACI_RE = re.compile(r"^color(\d+)thickness(\d+)$")
_SEGMENT_RGB_RE = re.compile(r"^c(\d+)t(\d+)$")
_TEXT_AS_GEOMETRY = "textasgeom"
_FILLED = "filled"
_SKIPPED_SUBTREES = frozenset({"3dmodel"})
_MODEL_SHEET_NAMES = frozenset({"model", "モデル"})
_HSF_SIGNATURE = b";; HSF V"
_ZIP_SIGNATURE = b"PK\x03\x04"
_PREAMBLE_WINDOW = 512
_MODEL_ENTRY = b"eModel"
_ARCHIVE_MARKER = b"2008+ archive"
# edrawings-kit's service limits, with the container and XML sizes production
# drawings reach (a materials.xml of 2 MB holds more than 20,000 elements)
_KIT_LIMIT_OVERRIDES: dict[str, int] = {
    "max_file_bytes": 256 << 20,
    "max_entry_bytes": 256 << 20,
    "max_expanded_bytes": 512 << 20,
    "max_output_bytes": 3072 << 20,
    "max_xml_bytes": 64 << 20,
    "max_xml_elements": 2_000_000,
    "max_hsf_vertices": 10_000_000,
    "max_hsf_face_values": 50_000_000,
    "max_scene_primitives": 1_000_000,
    "max_hsf_records": 4_000_000,
}
_IDENTITY: Matrix = (
    1.0,
    0.0,
    0.0,
    0.0,
    0.0,
    1.0,
    0.0,
    0.0,
    0.0,
    0.0,
    1.0,
    0.0,
    0.0,
    0.0,
    0.0,
    1.0,
)
_EPSILON = 1e-9
_YEAR_RE = re.compile(r"(?<!\d)(19\d\d|20\d\d)(?!\d)")


def is_edrw_signature(head: bytes) -> bool:
    """True for an eDrawings container: HSF stream, ``eModel`` ZIP or prefixed ZIP."""
    if head.startswith(_HSF_SIGNATURE):
        return True
    if head.startswith(_ZIP_SIGNATURE):
        return _zip_first_entry_name(head, 0) == _MODEL_ENTRY
    at = head.find(_ZIP_SIGNATURE, 16, _PREAMBLE_WINDOW)
    return at >= 0 and (
        _zip_first_entry_name(head, at) == _MODEL_ENTRY or _ARCHIVE_MARKER in head[:at]
    )


def _zip_first_entry_name(head: bytes, at: int) -> bytes | None:
    if len(head) < at + 30:
        return None
    length = int.from_bytes(head[at + 26 : at + 28], "little")
    if len(head) < at + 30 + length:
        return None
    return head[at + 30 : at + 30 + length]


def _backend() -> Any:
    try:
        import edrawings_kit
    except ImportError as exc:
        raise MissingOptionalDependencyError(
            "eDrawings support requires the optional dependency edrawings-kit; "
            'install it with `pip install "cad2d-ir[edrawings]"`.'
        ) from exc
    return edrawings_kit


def _kit_limits(kit: Any) -> Any:
    return dataclasses.replace(kit.Limits.service(), **_KIT_LIMIT_OVERRIDES)


# ---------------------------------------------------------------------------
# Scene index


@dataclass(slots=True)
class _Font:
    size: float | None  # in stored units when ``units`` is WSU
    units: int | None
    name: str | None


class _Scene:
    """Node tree, options, world matrices, inherited attributes and fonts."""

    def __init__(self, drawing: Any, hsf: Any | None) -> None:
        self.summary = drawing.summary
        self.scene = self.summary.scene
        self.drawing = drawing
        self.nodes: dict[str, Any] = {n.id: n for n in self.scene.nodes}
        self.children: dict[str | None, list[Any]] = defaultdict(list)
        for node in self.scene.nodes:
            self.children[node.parent_id].append(node)
        self.options: dict[str, dict[str, str]] = defaultdict(dict)
        for record in self.summary.user_option_records:
            for assignment in record.assignments:
                self.options[record.node_id].setdefault(
                    assignment.name, assignment.value
                )
        self._world: dict[str, Matrix] = {}
        self._chain: dict[str, list[Any]] = {}
        self.fonts: dict[str, _Font] = {}
        if hsf is not None:
            self._index_fonts(hsf)

    # -- tree -------------------------------------------------------------

    @property
    def root(self) -> Any | None:
        roots = self.children.get(None) or []
        return roots[0] if roots else None

    def chain(self, node_id: str) -> list[Any]:
        """The node and its ancestors, nearest first."""
        cached = self._chain.get(node_id)
        if cached is not None:
            return cached
        out: list[Any] = []
        seen: set[str] = set()
        current = self.nodes.get(node_id)
        while current is not None and current.id not in seen:
            seen.add(current.id)
            out.append(current)
            current = self.nodes.get(current.parent_id) if current.parent_id else None
        self._chain[node_id] = out
        return out

    def names(self, node_id: str) -> list[str]:
        return [n.name or "" for n in reversed(self.chain(node_id))]

    def world(self, node_id: str) -> Matrix:
        """Row-vector world matrix: ``p_world = p · M_node · M_parent · … · M_root``."""
        cached = self._world.get(node_id)
        if cached is not None:
            return cached
        node = self.nodes[node_id]
        parent = (
            self.world(node.parent_id)
            if node.parent_id is not None and node.parent_id in self.nodes
            else _IDENTITY
        )
        local = node.local_transform.value if node.local_transform is not None else None
        matrix = _multiply(tuple(local), parent) if local is not None else parent
        self._world[node_id] = matrix
        return matrix

    def under(self, node_id: str, ancestor_id: str) -> bool:
        return any(n.id == ancestor_id for n in self.chain(node_id))

    def skipped(self, node_id: str) -> bool:
        return any((n.name or "") in _SKIPPED_SUBTREES for n in self.chain(node_id))

    # -- inherited attributes ------------------------------------------------

    def visible(
        self, node_id: str, slot: int, *, below: str | None = None, default: bool = True
    ) -> bool:
        """Inherited visibility of one geometry slot, evaluated below ``below``.

        Sheets other than the shown one are saved with every geometry kind off
        (the DXF/DWG Reader saves all of them that way) and eDrawings turns the
        shown sheet on at runtime, so the sheet node and its ancestors do not
        take part; what counts is what the writer set inside the sheet. Faces
        are off at sheet level in every writer (``filled`` segments turn them on),
        so polygons pass ``default=False``.
        """
        for node in self.chain(node_id):
            if node.id == below:
                break
            visibility = node.display.visibility if node.display is not None else ()
            if slot < len(visibility) and visibility[slot] is not None:
                return bool(visibility[slot].value)
        return default

    def color(self, node_id: str, slot: int) -> int | str | None:
        """ACI index or ``#rrggbb`` from the nearest segment name or colour record."""
        for node in self.chain(node_id):
            name = node.name or ""
            match = _SEGMENT_ACI_RE.match(name)
            if match is not None:
                return int(match.group(1)) % 257
            match = _SEGMENT_RGB_RE.match(name)
            if match is not None:
                value = int(match.group(1))
                return _rgb_color(
                    (value & 0xFF, (value >> 8) & 0xFF, (value >> 16) & 0xFF)
                )
            colors = node.display.colors if node.display is not None else ()
            if slot < len(colors) and colors[slot] is not None:
                rgb = colors[slot].value
                if isinstance(rgb, (tuple, list)) and len(rgb) >= 3:
                    return _rgb_color((int(rgb[0]), int(rgb[1]), int(rgb[2])))
        return None

    def thickness(self, node_id: str, *, edges: bool) -> float | None:
        """Relative line weight from the segment name or the stored attribute."""
        for node in self.chain(node_id):
            name = node.name or ""
            match = _SEGMENT_ACI_RE.match(name) or _SEGMENT_RGB_RE.match(name)
            if match is not None:
                return float(match.group(2))
            display = node.display
            if display is None:
                continue
            weight = display.edge_weight if edges else display.line_weight
            if weight is None and not edges:
                weight = display.edge_weight
            if weight is not None:
                value = getattr(weight.value, "value", weight.value)
                if isinstance(value, (int, float)) and math.isfinite(value):
                    return float(value)
        return None

    def layer(self, node_id: str) -> str:
        for node in self.chain(node_id):
            name = self.options.get(node.id, {}).get("layername")
            if name:
                return str(name)
        for node in self.chain(node_id):
            name = node.name or ""
            if name.lower().startswith("layer") and len(name) > 5 and name[5] != "-":
                return name
        return "0"

    def option(self, node_id: str, name: str) -> str | None:
        for node in self.chain(node_id):
            value = self.options.get(node.id, {}).get(name)
            if value is not None:
                return value
        return None

    def font(self, node_id: str) -> _Font | None:
        for node in self.chain(node_id):
            found = self.fonts.get(node.id)
            if found is not None:
                return found
        return None

    def text_anchor(self, node_id: str) -> str | None:
        """The stored anchor name (``center_right``, ``lower_left``, …) in force."""
        for node in self.chain(node_id):
            anchor = node.display.text_anchor if node.display is not None else None
            if anchor is not None:
                name = getattr(anchor.value, "name", None)
                return str(name) if name else None
        return None

    # -- fonts ------------------------------------------------------------------

    def _index_fonts(self, hsf: Any) -> None:
        records = hsf.summary.records
        by_offset: dict[tuple[str, int], str] = {}
        for record in records:
            if record.kind in ("open_segment", "reopen_segment"):
                by_offset[(record.source.domain_id, record.source.offset)] = record.id
        node_by_record: dict[str, str] = {}
        for node in self.scene.nodes:
            if node.source is None:
                continue
            record_id = by_offset.get((node.source.domain_id, node.source.offset))
            if record_id is not None:
                node_by_record[record_id] = node.id
        for record in records:
            if record.kind != "text_font" or record.rendering is None:
                continue
            node_id = node_by_record.get(record.scope or "")
            if node_id is None:
                continue
            size: float | None = None
            units: int | None = None
            name: str | None = None
            for operand in record.rendering.fields:
                data = _source_bytes(hsf, operand.source)
                if data is None:
                    continue
                if operand.name == "size" and len(data) >= 4:
                    size = struct.unpack("<f", data[:4])[0]
                elif operand.name == "size_units" and data:
                    units = data[0]
                elif operand.name == "names":
                    name = data.decode("latin-1", "replace").strip("`")
            self.fonts[node_id] = _Font(size, units, name)


def _multiply(a: Matrix, b: Matrix) -> Matrix:
    return tuple(
        sum(a[i * 4 + k] * b[k * 4 + j] for k in range(4))
        for i in range(4)
        for j in range(4)
    )


def _apply(matrix: Matrix, x: float, y: float, z: float) -> Point:
    return (
        x * matrix[0] + y * matrix[4] + z * matrix[8] + matrix[12],
        x * matrix[1] + y * matrix[5] + z * matrix[9] + matrix[13],
    )


def _matrix_scale(matrix: Matrix) -> float:
    return math.hypot(matrix[0], matrix[1])


def _rgb_color(rgb: tuple[int, int, int]) -> int | str:
    if rgb in ((255, 255, 255), (0, 0, 0)):
        # the DXF/DWG Reader saves white lines (eDrawings' dark background), the
        # SOLIDWORKS writers black ones (paper); both are the DXF convention
        # "colour 7" (white or black by background)
        return 7
    return "#{:02x}{:02x}{:02x}".format(*rgb)


def _alignment(name: str) -> tuple[str, str]:
    """IR alignment for a stored anchor name.

    eDrawings' writers put the text on the side of the point the anchor names:
    the DXF/DWG Reader stores a left-aligned DXF text at its insertion point with
    the anchor ``center_right`` (checked against the source DXF), and the
    SOLIDWORKS writers use the same anchor for title-block labels. The vertical
    part is read the same way; ``center`` keeps the point on the baseline, where
    the DXF/DWG Reader puts it.
    """
    if name == "insertion_point":
        return "left", "baseline"
    halign = "left" if "right" in name else "right" if "left" in name else "center"
    valign = "top" if "lower" in name else "bottom" if "upper" in name else "baseline"
    return halign, valign


# ---------------------------------------------------------------------------
# Context


@dataclass(slots=True)
class _Context:
    options: ImportOptions
    unit_scale: float
    divisor: float = 1.0
    entities: list[dict[str, Any]] = field(default_factory=list)
    layers: dict[str, dict[str, Any]] = field(default_factory=dict)
    diagnostics: list[ImportDiagnostic] = field(default_factory=list)
    counts: Counter[str] = field(default_factory=Counter)
    converted: Counter[str] = field(default_factory=Counter)
    notes: dict[str, ImportDiagnostic] = field(default_factory=dict)
    next_entity: int = 1

    def allocate_id(self) -> str:
        entity_id = f"EDRW_E{self.next_entity:08d}"
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
        count: int = 1,
    ) -> None:
        """Emit a diagnostic once per code and count its occurrences."""
        existing = self.notes.get(code)
        if existing is not None:
            assert existing.details is not None
            existing.details["count"] += count
            return
        diagnostic = ImportDiagnostic(
            code=code,
            severity=severity,  # type: ignore[arg-type]
            message=message,
            action=action,
            details={"count": count, **(details or {})},
        )
        self.notes[code] = diagnostic
        self.diagnostics.append(diagnostic)


@dataclass(slots=True)
class _Sheet:
    node: Any
    index: int
    name: str
    primitives: list[Any] = field(default_factory=list)
    views: list[dict[str, Any]] = field(default_factory=list)
    skipped_3d: int = 0
    hidden: int = 0

    @property
    def is_model(self) -> bool:
        return self.name.strip().lower() in _MODEL_SHEET_NAMES


# ---------------------------------------------------------------------------
# Entry points


def convert_edrw_file_to_ir(
    path: str | Path,
    *,
    options: ImportOptions | None = None,
) -> ImportResult:
    """Read an eDrawings drawing (``.edrw``) with ``edrawings-kit``.

    Install the ``edrawings`` extra to enable this importer::

        pip install "cad2d-ir[edrawings]"

    The file must be an eDrawings container (an HSF stream, a ZIP whose first
    entry is ``eModel`` or the prefixed ZIP of older writers); anything else
    raises ``ImporterError``, as does a stream edrawings-kit cannot read.
    """
    kit = _backend()
    source_path = Path(path)
    data = source_path.read_bytes()
    if not is_edrw_signature(data[:_PREAMBLE_WINDOW]):
        raise ImporterError(
            f"Not an eDrawings file {source_path}: neither an HSF stream nor an "
            "eModel container."
        )
    try:
        document = kit.read(data, filename=source_path.name, limits=_kit_limits(kit))
    except kit.LimitExceededError as exc:
        raise ImporterError(
            f"eDrawings file {source_path} exceeds the reader limits: {exc}"
        ) from exc
    except kit.EdrawingsError as exc:
        raise ImporterError(
            f"Failed to read eDrawings file {source_path}: {exc}"
        ) from exc
    return edrawings_document_to_ir(
        document,
        options=options,
        source_name=source_path.name,
        source_sha256=hashlib.sha256(data).hexdigest(),
    )


def edrawings_document_to_ir(
    document: Any,
    *,
    options: ImportOptions | None = None,
    source_name: str | None = None,
    source_sha256: str | None = None,
) -> ImportResult:
    """Convert an ``edrawings_kit.Document`` (its drawing sheets) to IR."""
    kit = _backend()
    import_options = options or ImportOptions()
    profiles = {
        "user_data_profile": "edrawings_be_envelope_v1",
        "image_profile": "edrawings_1910_u32_v1",
    }
    try:
        drawing = document.read_drawing(**profiles)
    except kit.LimitExceededError as exc:
        raise ImporterError(
            f"eDrawings file {source_name} exceeds the reader limits: {exc}"
        ) from exc
    except kit.EdrawingsError as exc:
        raise ImporterError(
            f"Failed to read the drawing of {source_name}: {exc}"
        ) from exc
    hsf: Any | None
    try:
        hsf = document.parse_hsf(**profiles)
    except kit.EdrawingsError:
        hsf = None
    inspection = document.inspection
    header = hsf.summary.header if hsf is not None else None
    if header is None:
        try:
            header = document.inspect_hsf().header
        except kit.EdrawingsError:
            header = None
    hsf_version = header.version if header is not None else None
    scene = _Scene(drawing, hsf)
    writer, writer_year = _writer(hsf)

    sheets = _sheets(scene)
    with_geometry = [s for s in sheets if s.primitives]
    unit_scale, unit_source, unit_name = _unit_scale(scene, with_geometry)
    context = _Context(options=import_options, unit_scale=unit_scale)
    if unit_source == "profile":
        context.note(
            "EDRW_UNITS_FROM_PROFILE",
            "info",
            f"Coordinates are stored in {unit_name}s (edrawings-kit's drawing-plane "
            "profile); the sheet was composed in millimetres.",
            action="normalized",
            details={"stored_unit": unit_name, "mm_per_unit": unit_scale},
        )
    elif unit_source == "sheet":
        context.note(
            "EDRW_UNITS_FROM_SHEET",
            "info",
            "Coordinates are stored in metres (the writer's `fit width` / `fit height` "
            "sheet size); the sheet was composed in millimetres.",
            action="normalized",
            details={"stored_unit": unit_name, "mm_per_unit": unit_scale},
        )
    else:
        context.note(
            "EDRW_UNITS_ASSUMED",
            "warning",
            f"The stored unit is not declared; {unit_name}s fit a standard paper best "
            "and were assumed.",
            action="assumed",
            details={"stored_unit": unit_name, "mm_per_unit": unit_scale},
        )

    # Sheet selection: the first drawing sheet is the model space; a DXF-derived
    # "Model" sheet is skipped when layout sheets exist; other sheets are layouts
    drawn = list(with_geometry)
    if len(drawn) > 1 and any(s.is_model for s in drawn):
        drawn = [s for s in drawn if not s.is_model]
        context.note(
            "EDRW_MODEL_SHEET_SKIPPED",
            "info",
            "The writer's `Model` sheet (the DXF model space) is not drawn because "
            "the file has layout sheets.",
            action="skipped",
        )
    main = drawn[0] if drawn else None
    skipped_3d = sum(s.skipped_3d for s in sheets)
    hidden = sum(s.hidden for s in sheets)
    views = [v for s in drawn for v in s.views]
    divisor, dominant, other_scales = _document_scale(views)
    context.divisor = divisor

    sheet_info: dict[str, Any] | None = None
    layouts: list[dict[str, Any]] = []
    if main is not None:
        entities = _convert_sheet(main, scene, context)
        context.entities.extend(entities)
        sheet_info, sheet_source = _sheet_paper(main, scene, entities, context)
        for other in drawn[1:]:
            layout_entities = _convert_sheet(other, scene, context)
            paper, _ = _sheet_paper(other, scene, layout_entities, context)
            layout: dict[str, Any] = {
                "name": other.name,
                "entities": layout_entities,
                "viewports": [],
                "metadata": {
                    "edrw": {"segment": other.node.name, "index": other.index}
                },
            }
            if paper is not None:
                layout["paper"] = {
                    "name": paper["paper"],
                    "size_mm": [paper["width_mm"], paper["height_mm"]],
                    "margins_mm": [0.0, 0.0, 0.0, 0.0],
                    "units": "mm",
                    "rotation": 0,
                }
            layouts.append(layout)
        if layouts:
            context.note(
                "EDRW_SHEETS_AS_LAYOUTS",
                "info",
                f"{len(layouts)} further sheet(s) are kept as layouts; the first "
                f"drawing sheet ({main.name}) is the model space.",
                action="preserved",
                details={"sheets": [layout["name"] for layout in layouts]},
                count=len(layouts),
            )
        if sheet_info is not None:
            context.diagnostics.append(
                ImportDiagnostic(
                    code="EDRW_SHEET_DETECTED",
                    severity="info",
                    message=(
                        f"The sheet is {sheet_info['paper']} {sheet_info['orientation']} "
                        f"({sheet_info['width_mm']:g}×{sheet_info['height_mm']:g} mm), from "
                        f"{sheet_source}."
                    ),
                    action="detected",
                    details={"count": 1, **sheet_info, "source": sheet_source},
                )
            )
    placed = [v for v in views if v["scale"] is not None]
    if placed:
        context.note(
            "EDRW_VIEW_PLACED",
            "info",
            f"{len(placed)} view(s) were placed on the sheet by their stored "
            "modelling matrices (scale and offset).",
            action="normalized",
            details={
                "views": [
                    {
                        "sheet": v["sheet"],
                        "name": v["name"],
                        "scale": v["scale"],
                        "origin": v["origin"],
                    }
                    for v in placed
                ]
            },
            count=len(placed),
        )
    if divisor != 1.0:
        context.note(
            "EDRW_SCALE_APPLIED",
            "info",
            f"The sheet was divided by the dominant view scale {dominant:g} so the "
            "drawing is at real size; the denominator is header.metadata.edrw.paper_scale.",
            action="normalized",
            details={"view_scale": dominant, "paper_scale": 1.0 / divisor},
        )
    if other_scales:
        context.note(
            "EDRW_MIXED_SCALES",
            "warning",
            "Views drawn at a scale other than the dominant one keep their place on "
            "the sheet but are not at real size.",
            action="approximated",
            details={"scales": sorted(other_scales)},
        )
    if skipped_3d:
        context.note(
            "EDRW_VIEW_3D_SKIPPED",
            "info",
            f"{skipped_3d} element(s) of the projected 3D models (`3dmodel` subtrees) "
            "are not part of the 2D sheet and were not drawn.",
            action="skipped",
            count=skipped_3d,
        )
    if hidden:
        context.note(
            "EDRW_HIDDEN_SKIPPED",
            "info",
            f"{hidden} element(s) hidden in eDrawings were not drawn.",
            action="skipped",
            count=hidden,
        )
    if context.entities:
        context.note(
            "EDRW_DIMENSION_AS_LINES",
            "info",
            "Dimensions and annotations are the lines, arrowheads and text eDrawings "
            "saved; they are not editable dimensions.",
            action="flattened",
        )
    if main is None:
        context.diagnostics.append(
            ImportDiagnostic(
                code="EDRW_NO_GEOMETRY",
                severity="error",
                message=(
                    "No 2D sheet geometry could be read"
                    + (f" (HSF {hsf_version})" if hsf_version else "")
                    + "."
                ),
                action="skipped",
                details={"count": 1, "hsf_version": hsf_version, "sheets": len(sheets)},
            )
        )

    source: dict[str, Any] = {"format": "edrw"}
    version_bits = [f"HSF {hsf_version}" if hsf_version else None, writer]
    version_label = ", ".join(v for v in version_bits if v)
    if version_label:
        source["version"] = f"eDrawings ({version_label})"
    if source_name is not None:
        source["name"] = source_name
    if source_sha256 is not None:
        source["sha256"] = source_sha256
    root_options = (
        scene.options.get(scene.root.id, {}) if scene.root is not None else {}
    )
    display_length = _DISPLAY_LENGTH_UNITS.get(str(root_options.get("length", "")))
    header_metadata: dict[str, Any] = {
        "hsf_version": hsf_version,
        "writer": writer,
        "writer_year": writer_year,
        "input_kind": str(inspection.input_kind),
        "units": {
            "stored": unit_name,
            "mm_per_unit": unit_scale,
            "source": unit_source,
            "display_length": display_length,
        },
        "coordinates": "real" if divisor != 1.0 else "sheet",
        "view_scale": dominant,
        "paper_scale": 1.0 / divisor,
        "sheet": sheet_info,
        "sheets": [
            {
                "segment": s.node.name,
                "name": s.name,
                "entities": len(s.primitives),
                "views": [v["name"] for v in s.views],
                "role": (
                    "model_space"
                    if main is not None and s is main
                    else "layout"
                    if s in drawn
                    else "skipped"
                ),
            }
            for s in sheets
        ],
        "views": views,
    }
    tables: dict[str, Any] = {
        "layers": context.layers,
        "linetypes": {},
        "text_styles": {TEXT_STYLE: {"font": "msgothic.ttc"}},
    }
    doc: dict[str, Any] = {
        "format": "cad2d-ir",
        "version": import_options.ir_version,
        "source": source,
        "header": {
            "units": "mm",
            "angle_unit": "deg",
            "coord_space": "world",
            "metadata": {"edrw": header_metadata},
        },
        "tables": tables,
        "entities": context.entities,
        "layouts": layouts,
    }
    stats: dict[str, Any] = {
        "source_format": "edrw",
        "hsf_version": hsf_version,
        "writer": writer,
        "writer_year": writer_year,
        "input_kind": str(inspection.input_kind),
        "completeness": "complete"
        if main is not None and not context.counts["invalid"]
        else "partial",
        "sheets": len(sheets),
        "sheets_drawn": len(drawn),
        "sheet": main.name if main is not None else None,
        "views": len(views),
        "converted_entities": len(context.entities),
        "layout_entities": sum(len(layout["entities"]) for layout in layouts),
        "converted_entity_counts": dict(sorted(context.converted.items())),
        "skipped_entities": context.counts["invalid"] + context.counts["unsupported"],
        "skipped_3d": skipped_3d,
        "hidden": hidden,
        "texts": context.counts["text"],
        "text_geometry": context.counts["text_geometry"],
        "hatches": context.counts["hatch"],
        "degenerate": context.counts["degenerate"],
        "unit_scale": unit_scale,
        "unit_source": unit_source,
        "paper_scale": 1.0 / divisor,
    }
    source["metadata"] = {
        "edrw": {**stats, "diagnostics": [d.as_dict() for d in context.diagnostics]}
    }
    if import_options.validate:
        validate_ir(doc)
    return ImportResult(doc, context.diagnostics, stats)


# ---------------------------------------------------------------------------
# Sheets, units and paper


def _writer(hsf: Any | None) -> tuple[str | None, int | None]:
    """The writer string and its year from the stream's first user-data record.

    eDrawings writers put two leading strings into that record: a version label
    (``2016-01.00`` for SOLIDWORKS, ``<none>`` otherwise) and the writer
    (``SWeModels-24.2.0.0050-2016040``, ``DXF/DWG Reader AddIn (34.4.1.0003)``).
    """
    if hsf is None:
        return None, None
    for record in hsf.summary.records:
        layout = record.user_data
        if layout is None:
            continue
        strings: dict[str, str] = {}
        for field_ in layout.fields:
            if field_.label in ("leading_string_0", "leading_string_1"):
                data = _source_bytes(hsf, field_.source)
                if data is not None:
                    strings[field_.label] = data.decode("utf-8", "replace").strip()
        if "leading_string_1" not in strings:
            continue
        writer = strings["leading_string_1"]
        if writer in ("", "<none>"):
            writer = None
        year: int | None = None
        for text in (strings.get("leading_string_0", ""), writer or ""):
            match = _YEAR_RE.search(text)
            if match is not None:
                year = int(match.group(0))
                break
        return writer, year
    return None, None


def _source_bytes(hsf: Any, span: Any) -> bytes | None:
    try:
        return bytes(hsf.source_bytes(span))
    except Exception:  # noqa: BLE001 - a span outside the parsed domains is not an error here
        return None


def _sheet_sort_key(node: Any) -> tuple[int, str]:
    digits = "".join(ch for ch in (node.name or "") if ch.isdigit())
    return (int(digits) if digits else 0, node.name or "")


def _sheets(scene: _Scene) -> list[_Sheet]:
    root = scene.root
    if root is None:
        return []
    sheet_nodes = sorted(scene_sheets(scene), key=_sheet_sort_key)
    sheets: list[_Sheet] = []
    by_sheet: dict[str, _Sheet] = {}
    for index, node in enumerate(sheet_nodes):
        name = (
            scene.options.get(node.id, {}).get("name") or node.name or f"sheet{index}"
        )
        sheet = _Sheet(node=node, index=index, name=str(name))
        sheets.append(sheet)
        by_sheet[node.id] = sheet
        for view in scene.children.get(node.id, []):
            view_name = view.name or ""
            if not view_name.startswith(("view", "borderview")):
                continue
            scale: float | None = None
            origin: list[float] | None = None
            for child in scene.children.get(view.id, []):
                if child.name == "2dmodel" and child.local_transform is not None:
                    matrix = tuple(child.local_transform.value)
                    scale = round(_matrix_scale(matrix), 9)
                    origin = [matrix[12], matrix[13]]
            sheet.views.append(
                {
                    "sheet": sheet.name,
                    "segment": view_name,
                    "name": scene.options.get(view.id, {}).get("name") or view_name,
                    "kind": "border" if view_name.startswith("borderview") else "view",
                    "scale": scale,
                    "origin": origin,
                    "entities": 0,
                }
            )
    if not sheets:
        return []
    sheet_ids = {sheet.node.id for sheet in sheets}
    for primitive in scene.scene.primitives:
        owner = primitive.owner_node_id
        sheet_id = next((n.id for n in scene.chain(owner) if n.id in sheet_ids), None)
        if sheet_id is None:
            continue
        sheet = by_sheet[sheet_id]
        if scene.skipped(owner):
            sheet.skipped_3d += 1
            continue
        if primitive.kind == "polygon":
            shown = scene.visible(
                owner, _SLOT_FACES, below=sheet_id, default=False
            ) or scene.visible(owner, _SLOT_EDGES, below=sheet_id)
        else:
            shown = scene.visible(owner, _slot_for(primitive.kind), below=sheet_id)
        if not shown:
            sheet.hidden += 1
            continue
        sheet.primitives.append(primitive)
        view_name = next(
            (
                n.name
                for n in scene.chain(owner)
                if n.parent_id == sheet_id
                and (n.name or "").startswith(("view", "borderview"))
            ),
            None,
        )
        for view in sheet.views:
            if view["segment"] == view_name:
                view["entities"] += 1
    return sheets


def _slot_for(kind: str) -> int:
    if kind in ("text", "text_with_encoding"):
        return _SLOT_TEXT
    if kind == "marker":
        return _SLOT_MARKERS
    if kind == "polygon":
        return _SLOT_EDGES
    return _SLOT_LINES


def _stored_bounds(scene: _Scene, sheets: list[_Sheet]) -> tuple[float, float] | None:
    xs: list[float] = []
    ys: list[float] = []
    for sheet in sheets:
        for primitive in sheet.primitives:
            if primitive.text is not None:
                continue
            try:
                points = _world_points(scene, primitive)
            except (ValueError, struct.error):
                continue
            for x, y in points:
                xs.append(x)
                ys.append(y)
    if not xs:
        return None
    return (max(xs) - min(xs), max(ys) - min(ys))


def _unit_scale(scene: _Scene, sheets: list[_Sheet]) -> tuple[float, str, str]:
    """(mm per stored unit, how it was decided, the stored unit's name)."""
    plane = scene.summary.drawing_plane_profile
    stored = getattr(plane, "stored_xy_unit", None)
    if getattr(plane, "status", None) == "complete" and stored in _UNIT_SCALES:
        return _UNIT_SCALES[stored], "profile", stored
    if any(_fit_size(scene, node) is not None for node in scene_sheets(scene)):
        return _UNIT_SCALES["meter"], "sheet", "meter"
    size = _stored_bounds(scene, sheets)
    if size is None or size[0] <= 0 or size[1] <= 0:
        return _UNIT_SCALES["meter"], "assumed", "meter"
    best: tuple[float, str] | None = None
    for unit in _UNIT_CANDIDATES:
        scale = _UNIT_SCALES[unit]
        width, height = size[0] * scale, size[1] * scale
        fitted = _fit_paper(width, height)
        if fitted is None:
            continue
        _name, (pw, ph) = fitted
        fill = (width * height) / (pw * ph)
        if best is None or fill > best[0]:
            best = (fill, unit)
    unit = best[1] if best is not None else "meter"
    return _UNIT_SCALES[unit], "assumed", unit


def _fit_size(scene: _Scene, node: Any) -> tuple[float, float] | None:
    """The writer's ``fit width`` / ``fit height`` (stored units) of a sheet node or the root."""
    for candidate in (node, scene.root):
        if candidate is None:
            continue
        options = scene.options.get(candidate.id, {})
        width = _finite_positive(options.get("fit width"))
        height = _finite_positive(options.get("fit height"))
        if width is not None and height is not None:
            return width, height
    return None


def scene_sheets(scene: _Scene) -> list[Any]:
    """The ``sheetN`` segment nodes under the root, in file order."""
    root = scene.root
    if root is None:
        return []
    return [
        n for n in scene.children.get(root.id, []) if (n.name or "").startswith("sheet")
    ]


def _finite_positive(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) and number > 0 else None


def _document_scale(views: list[dict[str, Any]]) -> tuple[float, float, set[float]]:
    """(divisor, dominant view scale, other scales) from the placed views."""
    votes: Counter[float] = Counter()
    for view in views:
        scale = view["scale"]
        if view["kind"] != "view" or scale is None or scale <= 0:
            continue
        votes[scale] += max(1, int(view["entities"]))
    if not votes:
        return 1.0, 1.0, set()
    dominant, _ = max(votes.items(), key=lambda kv: (kv[1], -kv[0]))
    others = {s for s in votes if not math.isclose(s, dominant, rel_tol=1e-6)}
    if math.isclose(dominant, 1.0, rel_tol=1e-6):
        return 1.0, 1.0, others
    return dominant, dominant, others


def _sheet_paper(
    sheet: _Sheet, scene: _Scene, entities: list[dict[str, Any]], context: _Context
) -> tuple[dict[str, Any] | None, str]:
    """The paper of one sheet in paper millimetres, with its extent in IR units."""
    fit = _fit_size(scene, sheet.node)
    bounds = _bounds(entities)
    divisor = context.divisor
    if fit is not None:
        pw, ph = fit[0] * context.unit_scale, fit[1] * context.unit_scale
        if bounds is not None:
            # `fit width` / `fit height` give the size; the orientation and the
            # origin come from the drawn sheet (SOLIDWORKS frames sit inside the
            # paper from (0, 0), and portrait sheets store the same two numbers)
            min_x, min_y, max_x, max_y = (v * divisor for v in bounds)
            width_mm, height_mm = max_x - min_x, max_y - min_y
            tolerance = _PAPER_TOLERANCE_MM
            landscape = width_mm <= pw + tolerance and height_mm <= ph + tolerance
            portrait = width_mm <= ph + tolerance and height_mm <= pw + tolerance
            if portrait and (not landscape or height_mm > width_mm):
                pw, ph = ph, pw
            if min_x < -_PAPER_TOLERANCE_MM or max_x > pw + _PAPER_TOLERANCE_MM:
                cx = (min_x + max_x) / 2.0
                extent_x = (cx - pw / 2.0, cx + pw / 2.0)
            else:
                extent_x = (0.0, pw)
            if min_y < -_PAPER_TOLERANCE_MM or max_y > ph + _PAPER_TOLERANCE_MM:
                cy = (min_y + max_y) / 2.0
                extent_y = (cy - ph / 2.0, cy + ph / 2.0)
            else:
                extent_y = (0.0, ph)
        else:
            extent_x, extent_y = (0.0, pw), (0.0, ph)
        paper, orientation = _paper_name(pw, ph)
        extent = (extent_x[0], extent_y[0], extent_x[1], extent_y[1])
        return (
            {
                "paper": paper,
                "orientation": orientation,
                "width_mm": round(pw, 3),
                "height_mm": round(ph, 3),
                "extent": [round(v / divisor, 6) for v in extent],
            },
            "the writer's sheet size (`fit width` / `fit height`)",
        )
    if bounds is None:
        return None, ""
    min_x, min_y, max_x, max_y = (v * divisor for v in bounds)
    width_mm, height_mm = max_x - min_x, max_y - min_y
    fitted = _fit_paper(width_mm, height_mm, inch=_display_inches(scene))
    paper, size = fitted if fitted is not None else ("FREE", (width_mm, height_mm))
    cx, cy = (min_x + max_x) / 2.0, (min_y + max_y) / 2.0
    extent = (
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
            "extent": [round(v / divisor, 6) for v in extent],
        },
        "the smallest standard sheet that holds the geometry",
    )


def _fit_paper(
    width: float, height: float, *, inch: bool = False
) -> tuple[str, tuple[float, float]] | None:
    """The smallest standard sheet (either orientation) that holds ``width`` × ``height``.

    The series the drawing is dimensioned in (ISO/JIS for millimetres, ANSI/Arch
    for inches) is tried first, the other one only when nothing of it fits.
    """
    preferred = [
        (n, s) for n, s in _PAPER_BY_AREA if n.startswith(_INCH_SERIES) == inch
    ]
    others = [(n, s) for n, s in _PAPER_BY_AREA if n.startswith(_INCH_SERIES) != inch]
    for name, (pw, ph) in preferred + others:
        if width <= pw + _PAPER_TOLERANCE_MM and height <= ph + _PAPER_TOLERANCE_MM:
            return name, (pw, ph)
        if width <= ph + _PAPER_TOLERANCE_MM and height <= pw + _PAPER_TOLERANCE_MM:
            return name, (ph, pw)
    return None


def _display_inches(scene: _Scene) -> bool:
    """True when the drawing's display unit (the root ``length`` option) is inches."""
    root = scene.root
    options = scene.options.get(root.id, {}) if root is not None else {}
    return _DISPLAY_LENGTH_UNITS.get(str(options.get("length", ""))) == "inch"


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
        elif kind == "LWPOLYLINE":
            for vertex in entity["vertices"]:
                add(vertex)
        elif kind == "HATCH":
            for loop in entity["loops"]:
                for vertex in loop["vertices"]:
                    add(vertex)
    if not xs:
        return None
    return min(xs), min(ys), max(xs), max(ys)


# ---------------------------------------------------------------------------
# Entities


def _world_points(scene: _Scene, primitive: Any) -> list[Point]:
    """The primitive's points in stored sheet units (the HSF matrices applied)."""
    data = scene.drawing.primitive_points(primitive.id).data
    count = len(data) // 12
    if count == 0:
        return []
    flat = struct.unpack(f"<{count * 3}f", data[: count * 12])
    matrix = scene.world(primitive.owner_node_id)
    points: list[Point] = []
    for i in range(count):
        x, y = _apply(matrix, flat[3 * i], flat[3 * i + 1], flat[3 * i + 2])
        if not (math.isfinite(x) and math.isfinite(y)):
            raise ValueError("coordinate is not finite")
        points.append((x, y))
    return points


def _convert_sheet(
    sheet: _Sheet, scene: _Scene, context: _Context
) -> list[dict[str, Any]]:
    entities: list[dict[str, Any]] = []
    for primitive in sheet.primitives:
        try:
            entities.extend(_convert_primitive(primitive, sheet, scene, context))
        except (ValueError, struct.error) as exc:
            context.counts["invalid"] += 1
            context.note(
                "EDRW_ENTITY_INVALID",
                "warning",
                f"An element could not be converted and was skipped: {exc}",
                action="skipped",
            )
    return entities


def _convert_primitive(
    primitive: Any, sheet: _Sheet, scene: _Scene, context: _Context
) -> list[dict[str, Any]]:
    kind = primitive.kind
    owner = primitive.owner_node_id
    factor = context.unit_scale / context.divisor
    points = [(x * factor, y * factor) for x, y in _world_points(scene, primitive)]
    names = scene.names(owner)
    text_geometry = _TEXT_AS_GEOMETRY in names
    out: list[dict[str, Any]] = []

    def common(slot: int, *, edges: bool = False) -> dict[str, Any]:
        layer = scene.layer(owner)
        context.layers.setdefault(layer, {})
        entity: dict[str, Any] = {"id": context.allocate_id(), "layer": layer}
        color = scene.color(owner, slot)
        if color is not None:
            entity["color"] = color
        weight = scene.thickness(owner, edges=edges)
        if weight is not None and weight > 1.0:
            entity["lineweight_mm"] = round(min(1.0, 0.25 * weight), 3)
            context.note(
                "EDRW_LINE_WIDTH_ASSUMED",
                "info",
                "Relative line weights were mapped to millimetre widths (1 = default, "
                "2 = 0.5 mm, …).",
                action="assumed",
            )
        metadata: dict[str, Any] = {
            "edrw": {
                "sheet": sheet.name,
                "segment": names[-1] if names else "",
                "kind": kind,
            }
        }
        if text_geometry:
            metadata["edrw"]["text_geometry"] = True
        if context.options.entity_provenance:
            entity["metadata"] = metadata
            entity["source"] = {
                "format": "edrw",
                "id": str(primitive.id),
                "kind": f"HSF_{kind}",
                "metadata": {
                    "record": str(primitive.source_record_id),
                    "sheet": sheet.name,
                },
            }
        return entity

    if primitive.text is not None:
        value = primitive.text.value
        if not value or not points:
            context.counts["unsupported"] += 1
            return out
        font = scene.font(owner)
        height = DEFAULT_TEXT_HEIGHT_MM / context.divisor
        if font is not None and font.size is not None and font.size > 0:
            if font.units == _FONT_SIZE_WSU:
                height = font.size * factor
            elif font.units == _FONT_SIZE_POINTS:
                height = font.size * _POINT_MM / context.divisor
            else:
                context.note(
                    "EDRW_TEXT_HEIGHT_ASSUMED",
                    "info",
                    f"Text sizes stored in an unsupported unit were replaced by "
                    f"{DEFAULT_TEXT_HEIGHT_MM:g} mm on paper.",
                    action="assumed",
                )
        else:
            context.note(
                "EDRW_TEXT_HEIGHT_ASSUMED",
                "info",
                f"Texts without a stored font size are drawn {DEFAULT_TEXT_HEIGHT_MM:g} mm "
                "high on paper.",
                action="assumed",
            )
        anchor = scene.text_anchor(owner)
        halign, valign = _alignment(anchor) if anchor else ("left", "baseline")
        if anchor:
            context.note(
                "EDRW_TEXT_ANCHOR_ASSUMED",
                "info",
                "Text anchors were read with the convention eDrawings' writers use "
                "(`center_right` = text to the right of the point, on its baseline).",
                action="approximated",
            )
        entity = {
            **common(_SLOT_TEXT),
            "kind": "TEXT",
            "insert": list(points[0]),
            "height": height
            if math.isfinite(height) and height > 0
            else DEFAULT_TEXT_HEIGHT_MM,
            "text": str(value),
            "style": TEXT_STYLE,
            "halign": halign,
            "valign": valign,
        }
        if context.options.entity_provenance:
            details = entity.setdefault("metadata", {}).setdefault("edrw", {})
            if anchor:
                details["anchor"] = anchor
            if font is not None and font.name:
                details["font"] = font.name
        out.append(entity)
        context.converted["TEXT"] += 1
        context.counts["text"] += 1
        return out

    if kind == "line":
        if len(points) < 2:
            raise ValueError("a line needs two points")
        if math.dist(points[0], points[1]) < MIN_LENGTH_MM:
            context.counts["degenerate"] += 1
            return out
        out.append(
            {
                **common(_SLOT_LINES),
                "kind": "LINE",
                "p1": list(points[0]),
                "p2": list(points[1]),
            }
        )
        context.converted["LINE"] += 1
    elif kind in ("polyline", "poly_polyline"):
        parts = (
            list(primitive.part_lengths)
            if kind == "poly_polyline" and primitive.part_lengths
            else [len(points)]
        )
        cursor = 0
        for length in parts:
            part = _dedupe(points[cursor : cursor + length])
            cursor += length
            if len(part) < 2:
                context.counts["degenerate"] += 1
                continue
            out.append(
                {
                    **common(_SLOT_LINES),
                    "kind": "LWPOLYLINE",
                    "vertices": [list(p) for p in part],
                }
            )
            context.converted["LWPOLYLINE"] += 1
            if text_geometry:
                context.counts["text_geometry"] += 1
                context.note(
                    "EDRW_TEXT_AS_GEOMETRY",
                    "info",
                    "Text the writer stroked into line work stays geometry (it is not "
                    "searchable text).",
                    action="preserved",
                )
    elif kind == "polygon":
        part = _dedupe(points)
        filled = scene.visible(owner, _SLOT_FACES, below=sheet.node.id, default=False)
        if filled and len(part) >= 3 and abs(_area(part)) > _EPSILON:
            out.append(
                {
                    **common(_SLOT_FACES, edges=True),
                    "kind": "HATCH",
                    "solid": True,
                    "loops": [{"vertices": [list(p) for p in part]}],
                }
            )
            context.converted["HATCH"] += 1
            context.counts["hatch"] += 1
            context.note(
                "EDRW_POLYGON_AS_HATCH",
                "info",
                "Filled polygons (arrowheads, stroked text of old writers) are solid "
                "hatches; outlined ones are closed polylines.",
                action="normalized",
            )
        elif len(part) >= 2:
            out.append(
                {
                    **common(_SLOT_EDGES, edges=True),
                    "kind": "LWPOLYLINE",
                    "vertices": [list(p) for p in part],
                    "closed": True,
                }
            )
            context.converted["LWPOLYLINE"] += 1
        else:
            context.counts["degenerate"] += 1
    elif kind == "circle":
        circle = _circle_through(points)
        if circle is None:
            raise ValueError("circle points are collinear")
        center, radius = circle
        if radius < MIN_LENGTH_MM:
            context.counts["degenerate"] += 1
            return out
        out.append(
            {
                **common(_SLOT_LINES),
                "kind": "CIRCLE",
                "center": list(center),
                "radius": radius,
            }
        )
        context.converted["CIRCLE"] += 1
    elif kind == "circular_arc":
        circle = _circle_through(points)
        if circle is None:
            # three collinear points: the arc degenerates to its chord
            if len(points) >= 3 and math.dist(points[0], points[2]) >= MIN_LENGTH_MM:
                out.append(
                    {
                        **common(_SLOT_LINES),
                        "kind": "LINE",
                        "p1": list(points[0]),
                        "p2": list(points[2]),
                    }
                )
                context.converted["LINE"] += 1
            return out
        center, radius = circle
        start, end = _arc_angles(center, points[0], points[1], points[2])
        out.append(
            {
                **common(_SLOT_LINES),
                "kind": "ARC",
                "center": list(center),
                "radius": radius,
                "start_angle": start,
                "end_angle": end,
            }
        )
        context.converted["ARC"] += 1
    elif kind == "marker":
        if not points:
            raise ValueError("a marker needs a point")
        out.append(
            {**common(_SLOT_MARKERS), "kind": "POINT", "position": list(points[0])}
        )
        context.converted["POINT"] += 1
    else:
        context.counts["unsupported"] += 1
        context.note(
            "EDRW_ELEMENT_UNSUPPORTED",
            "info",
            "Elements of kinds the importer does not draw (meshes, images, splines) were skipped.",
            action="skipped",
            details={"kinds": [kind]},
        )
        details = context.notes["EDRW_ELEMENT_UNSUPPORTED"].details
        assert details is not None
        if kind not in details["kinds"]:
            details["kinds"].append(kind)
    return out


def _dedupe(points: list[Point]) -> list[Point]:
    out: list[Point] = []
    for point in points:
        if not out or math.dist(out[-1], point) >= MIN_LENGTH_MM:
            out.append(point)
    return out


def _area(points: list[Point]) -> float:
    total = 0.0
    for (x1, y1), (x2, y2) in zip(points, points[1:] + points[:1]):
        total += x1 * y2 - x2 * y1
    return total / 2.0


def _circle_through(points: list[Point]) -> tuple[Point, float] | None:
    """Centre and radius of the circle through three points, or None when collinear."""
    if len(points) < 3:
        return None
    (ax, ay), (bx, by), (cx, cy) = points[0], points[1], points[2]
    d = 2.0 * (ax * (by - cy) + bx * (cy - ay) + cx * (ay - by))
    if abs(d) < 1e-12:
        return None
    ux = (
        (ax * ax + ay * ay) * (by - cy)
        + (bx * bx + by * by) * (cy - ay)
        + (cx * cx + cy * cy) * (ay - by)
    ) / d
    uy = (
        (ax * ax + ay * ay) * (cx - bx)
        + (bx * bx + by * by) * (ax - cx)
        + (cx * cx + cy * cy) * (bx - ax)
    ) / d
    radius = math.hypot(ax - ux, ay - uy)
    if (
        not (math.isfinite(ux) and math.isfinite(uy) and math.isfinite(radius))
        or radius <= 0
    ):
        return None
    return (ux, uy), radius


def _arc_angles(
    center: Point, start: Point, middle: Point, end: Point
) -> tuple[float, float]:
    """Counter-clockwise start/end angles (degrees) of the arc from start through middle to end."""
    a0 = _angle(center, start)
    a1 = _angle(center, middle)
    a2 = _angle(center, end)
    ccw = (a1 - a0) % 360.0 <= (a2 - a0) % 360.0
    return (a0, a2) if ccw else (a2, a0)


def _angle(center: Point, point: Point) -> float:
    """Polar angle in degrees, in [0, 360) and free of float32 noise around 0."""
    angle = round(
        math.degrees(math.atan2(point[1] - center[1], point[0] - center[0])), 9
    )
    return angle % 360.0


__all__ = [
    "DEFAULT_TEXT_HEIGHT_MM",
    "PAPER_SIZES",
    "TEXT_STYLE",
    "convert_edrw_file_to_ir",
    "edrawings_document_to_ir",
    "is_edrw_signature",
]
