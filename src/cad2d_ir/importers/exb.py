"""Native CAXA EXB (CAXA CAD / 电子图板) to CAD 2D IR importer backed by :mod:`ezexb`.

``ezexb`` returns the stored entities of an EXB drawing in their block
coordinates, with layers, line types, text styles, spaces and blocks. This
adapter maps them to IR without going through DXF:

* the first space (``-7``, ``model`` / ``Paper1``) becomes the model entities
  and every other space a layout; when the first space is empty and another
  one has entities, the fullest space is promoted (``EXB_LAYOUT_PROMOTED``);
* coordinates are taken as millimetres (an EXB carries no unit,
  ``EXB_UNITS_ASSUMED_MM``) at real size; the drawing scale is the space's
  paper scale (``1:3`` is ``3.0``, ``2:1`` is ``0.5``) and the paper size is
  measured from the frame block placed in the space (``EXB_PAPER_DETECTED``),
  both in ``header.metadata.exb``;
* text height is the stored height times the text's drawing scale, the
  alignment code is read on a 3×3 grid (0 top-left … 4 centre … 8
  bottom-right; all but 2 were verified against the CAXA viewer), CAXA's ``%c`` / ``%d`` /
  ``%p`` / ``%x`` symbols become Unicode and its deviation and fit tolerance
  escapes become DXF-style ``\\S`` stacks;
* dimensions are rebuilt from the display records CAXA cached (lines, arcs,
  arrow solids, text) in an anonymous block referenced by
  ``definition.block``; the stored value, text and locations are kept but the
  dimension semantics (construction, associativity, tolerances) are not
  decoded (``EXB_DIMENSION_FROM_CACHE``);
* explicit colours are the low 24 bits of the stored value read as a
  COLORREF (``0x00BBGGRR``); symbolic line widths map to CAXA's default
  millimetre widths (``EXB_LINE_WIDTH_PRESET``).
"""

from __future__ import annotations

import hashlib
import math
import re
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

_TAU = 2.0 * math.pi
_EPSILON = 1e-9

# ezexb's defaults (100k records, 256 MiB) stop at roughly 18k entities; a
# production drawing is routinely larger. Budgets are ceilings, not allocations.
DEFAULT_PARSE_BUDGETS: dict[str, int] = {
    "max_file_bytes": 4 * 1024**3,
    "max_entries": 1_000_000,
    "max_stream_bytes": 2 * 1024**3,
    "max_total_stream_bytes": 8 * 1024**3,
    "max_container_read_bytes": 2 * 1024**3,
    "max_decoded_stream_bytes": 2 * 1024**3,
    "max_total_decoded_bytes": 8 * 1024**3,
    "max_records": 50_000_000,
}

# Dimension type GUIDs in ezexb's ``DIMS`` order, identified from the cached
# presentation texts ("⌀…", "R…", "…°", part-number leaders) of real drawings.
DIMENSION_KINDS: dict[str, str] = {
    "1da848b5-8c84-4c11-80d6-45cf9b26aa48": "LINEAR",
    "f391cb78-240a-4fbd-9e3f-4f19572391fc": "DIAMETER",
    "aba4bd1e-d922-4b4d-931e-7f4c9d6919e7": "RADIAL",
    "cc8682e6-a8cf-4a4f-aa36-c600b07aa69c": "GENERIC",  # leader with a note
    "5f9490d4-b797-4119-90b1-93668e2db71a": "ANGULAR",
}

# Text alignment code: a 3×3 grid, rows top/middle/bottom, columns left/centre/right.
TEXT_ALIGNMENTS: dict[int, tuple[str, str]] = {
    0: ("left", "top"),
    1: ("center", "top"),
    2: ("right", "top"),
    3: ("left", "middle"),
    4: ("center", "middle"),
    5: ("right", "middle"),
    6: ("left", "bottom"),
    7: ("center", "bottom"),
    8: ("right", "bottom"),
}
# Verified against the CAXA viewer (glyph box corners read from its status bar) on
# 2026-10-19; code 2 (top-right) did not occur in the corpus and follows the grid.
VERIFIED_TEXT_ALIGNMENTS = frozenset({0, 1, 3, 4, 5, 6, 7, 8})

# CAXA's symbolic line widths and their default millimetre values (线宽设置).
PRESET_WIDTH_MM: dict[str, float] = {
    "thin": 0.25,
    "mid": 0.35,
    "wide": 0.5,
    "double_wide": 0.7,
}
_LAYER_WIDTH_FLAGS = {3: "thin", 4: "wide", 5: "mid", 6: "double_wide"}

# CAXA special characters (one percent sign, unlike AutoCAD's ``%%c``).
SYMBOL_ESCAPES: dict[str, str] = {
    "%c": "⌀",
    "%C": "⌀",
    "%d": "°",
    "%D": "°",
    "%p": "±",
    "%P": "±",
    "%x": "×",
    "%X": "×",
}
# Deviation tolerance ``10\D\H0.707x;-0.013^-0.028|b;`` and fit tolerance
# ``50\V\H0.707x;H7^m6|a;``; both become the DXF stack ``\S上^下;``. A roughness
# symbol ``\R\H0.707x;0^0^7^^0.2^^^^;`` has no text form.
_DEVIATION_RE = re.compile(r"\\D\\H[0-9.]+x;([^^|;]*)\^([^|;]*)\|b;")
_FIT_RE = re.compile(r"\\V\\H[0-9.]+x;([^^|;]*)\^([^|;]*)\|a;")
_ROUGHNESS_RE = re.compile(r"\\R\\H[0-9.]+x;[^;]*;")
_MTEXT_ESCAPE_RE = re.compile(r"\\[A-Za-z]|[{}]")

FRAME_BLOCK_PREFIX = "*FRAME"
_UNRESOLVED_LAYER_PREFIX = "EXB_LAYER_"


@dataclass(slots=True)
class _ConversionContext:
    options: ImportOptions
    document: Any
    layer_names: dict[int, str] = field(default_factory=dict)
    linetype_names: dict[int, str] = field(default_factory=dict)
    style_names: dict[int, str] = field(default_factory=dict)
    style_width_factors: dict[int, float] = field(default_factory=dict)
    block_names: dict[int, str] = field(default_factory=dict)
    blocks: dict[str, dict[str, Any]] = field(default_factory=dict)
    layers: dict[str, dict[str, Any]] = field(default_factory=dict)
    diagnostics: list[ImportDiagnostic] = field(default_factory=list)
    converted_counts: Counter[str] = field(default_factory=Counter)
    skipped_counts: Counter[str] = field(default_factory=Counter)
    counts: Counter[str] = field(default_factory=Counter)
    alignment_codes: Counter[int] = field(default_factory=Counter)
    unresolved_layers: set[int] = field(default_factory=set)
    unresolved_styles: set[int] = field(default_factory=set)
    hatch_vertices: int = 0
    next_entity_number: int = 1

    def allocate_id(self) -> str:
        entity_id = f"EXB_E{self.next_entity_number:08d}"
        self.next_entity_number += 1
        return entity_id

    def add_converted(self, entity: dict[str, Any]) -> dict[str, Any]:
        self.converted_counts[str(entity["kind"])] += 1
        return entity


def default_parse_options(ezexb: Any) -> Any:
    """``ezexb.ParseOptions`` with budgets sized for production drawings."""
    return ezexb.ParseOptions(**DEFAULT_PARSE_BUDGETS)


def convert_exb_file_to_ir(
    path: str | Path,
    *,
    options: ImportOptions | None = None,
    parse_options: Any | None = None,
) -> ImportResult:
    """Read an EXB drawing with ``ezexb`` and convert its stored entities to IR.

    Install the ``exb`` extra to enable this importer::

        pip install "cad2d-ir[exb]"

    ``parse_options`` is an ``ezexb.ParseOptions``; the default raises the
    resource budgets far above ``ezexb``'s (see ``DEFAULT_PARSE_BUDGETS``).
    The file is always read in ``ezexb``'s partial mode: records it cannot
    decode become ``EXB_UNSUPPORTED_ENTITY`` diagnostics instead of an error.
    """
    try:
        import ezexb
    except ImportError as exc:
        raise MissingOptionalDependencyError(
            "EXB support requires the optional dependency ezexb; "
            'install it with `pip install "cad2d-ir[exb]"`.'
        ) from exc

    source_path = Path(path)
    budgets = (
        parse_options if parse_options is not None else default_parse_options(ezexb)
    )
    try:
        signature = ezexb.probe(source_path, options=budgets).signature
    except ezexb.ExbError as exc:
        raise ImporterError(f"Failed to read EXB file {source_path}: {exc}") from exc
    if signature == "legacy_caxa_candidate":
        raise ImporterError(
            f"Unsupported legacy EXB file {source_path}: pre-2007 CAXA files "
            "(signature CaxaEbForWindows) are not a compound document and cannot "
            "be read; re-save the drawing with a current CAXA CAD."
        )
    if signature != "cfb":
        raise ImporterError(
            f"Not an EXB file {source_path}: the OLE2 compound document "
            "signature is missing."
        )
    try:
        document = ezexb.read(source_path, strict=False, options=budgets)
    except ezexb.UnsupportedFeatureError as exc:
        build = _product_build(ezexb, source_path, budgets)
        raise ImporterError(
            f"Unsupported EXB product build {build!r} in {source_path}: {exc} "
            f"(ezexb {ezexb.__version__} reads builds "
            f"{', '.join(ezexb.capabilities()['supported_product_builds'])})"
        ) from exc
    except ezexb.StreamNotFoundError as exc:
        raise ImporterError(
            f"Not a CAXA EXB drawing {source_path}: the compound document has no "
            f"EXB streams ({exc})."
        ) from exc
    except ezexb.ExbError as exc:
        raise ImporterError(f"Failed to read EXB file {source_path}: {exc}") from exc
    return exb_document_to_ir(
        document,
        source_name=source_path.name,
        source_sha256=_sha256_file(source_path),
        parser_version=str(ezexb.__version__),
        options=options,
    )


def _product_build(ezexb: Any, path: Path, budgets: Any) -> str | None:
    try:
        return str(ezexb.metadata(path, options=budgets).file_info.product_build.value)
    except Exception:  # noqa: BLE001 - the build string is only for the message
        return None


def exb_document_to_ir(
    document: Any,
    *,
    source_name: str | None = None,
    source_sha256: str | None = None,
    parser_version: str | None = None,
    options: ImportOptions | None = None,
) -> ImportResult:
    """Convert an ``ezexb.Document`` (``ezexb.read(..., strict=False)``) to IR."""
    import_options = options or ImportOptions()
    context = _ConversionContext(options=import_options, document=document)

    _build_tables(document, context)
    _name_blocks(document, context)
    space_blocks = {space.block_id for space in document.spaces}
    for block in document.blocks:
        if block.definition.id in space_blocks:
            continue
        _convert_block(block, context)

    main_space, promoted = _main_space(document)
    entities: list[dict[str, Any]] = []
    layouts: list[dict[str, Any]] = []
    layout_names: set[str] = set()
    for space in document.spaces:
        scope: list[dict[str, Any]] = []
        for index in _members(document, space):
            scope.extend(_convert_entity_safe(document.entities[index], context))
        if space is main_space:
            entities = scope
            continue
        name = space.name.value or f"Space{space.id}"
        if name in layout_names:
            name = f"{name}_{space.id}"
        layout_names.add(name)
        layouts.append(
            {
                "name": name,
                "entities": scope,
                "metadata": {"exb": {"space_id": space.id, "paper_scale": space.scale}},
            }
        )
    if promoted:
        context.diagnostics.append(
            ImportDiagnostic(
                code="EXB_LAYOUT_PROMOTED",
                severity="warning",
                message=(
                    "The first EXB space has no entities; space "
                    f"{main_space.name.value!r} ({main_space.id}) with "
                    f"{len(entities)} entities became the model entities."
                ),
                action="normalized",
                details={"space_id": main_space.id, "name": main_space.name.value},
            )
        )

    paper = _detect_paper(document, main_space, context) if main_space else None
    header_metadata: dict[str, Any] = {
        "product_build": document.file_info.product_build.value,
        "profile": document.profile,
        "units_assumed": True,
        "spaces": [
            {
                "id": space.id,
                "name": space.name.value,
                "scale": space.scale,
                "block_id": space.block_id,
                "entities": len(_members(document, space)),
            }
            for space in document.spaces
        ],
        "main_space_id": None if main_space is None else main_space.id,
        "paper_scale": None if main_space is None else main_space.scale,
        "paper": paper,
    }
    tables: dict[str, Any] = {
        "layers": context.layers,
        "linetypes": _linetype_table(document, context),
        "text_styles": _text_style_table(document, context),
    }
    if context.blocks:
        tables["blocks"] = context.blocks

    report = document.report
    unsupported = Counter(
        str(diagnostic.type_id)
        for diagnostic in report.diagnostics
        if diagnostic.code == "unsupported_entity"
    )
    source: dict[str, Any] = {
        "format": "exb",
        "version": str(document.file_info.product_build.value),
        "metadata": {
            "exb": {
                "parser": "ezexb",
                "parser_version": parser_version,
                "profile": report.profile,
                "complete": bool(report.complete),
                "decoded_entity_count": int(report.decoded_entity_count),
                "unsupported_entity_count": int(report.unsupported_entity_count),
                "top_level_record_count": int(report.top_level_record_count),
                "unsupported_types": dict(sorted(unsupported.items())),
            }
        },
    }
    if source_name is not None:
        source["name"] = source_name
    if source_sha256 is not None:
        source["sha256"] = source_sha256

    ir_document: dict[str, Any] = {
        "format": "cad2d-ir",
        "version": import_options.ir_version,
        "header": {
            "units": "mm",
            "angle_unit": "deg",
            "coord_space": "world",
            "metadata": {"exb": header_metadata},
        },
        "source": source,
        "tables": tables,
        "entities": entities,
    }
    if layouts:
        ir_document["layouts"] = layouts

    _append_summary_diagnostics(document, context, unsupported, layouts, paper)
    if import_options.validate:
        validate_ir(ir_document)

    source_counts = Counter(entity.kind for entity in document.entities)
    block_entity_count = sum(
        len(block["entities"]) for block in context.blocks.values()
    )
    layout_entity_count = sum(len(layout["entities"]) for layout in layouts)
    statistics: dict[str, Any] = {
        "source_format": "exb",
        "product_build": document.file_info.product_build.value,
        "read_complete": bool(report.complete),
        "source_entities": len(document.entities),
        "source_entity_counts": dict(sorted(source_counts.items())),
        "source_unsupported_entities": int(report.unsupported_entity_count),
        "source_spaces": len(document.spaces),
        "source_block_definitions": len(document.blocks) - len(space_blocks),
        "converted_entities": len(entities),
        "converted_block_entities": block_entity_count,
        "converted_layout_entities": layout_entity_count,
        "converted_entity_counts": dict(sorted(context.converted_counts.items())),
        "skipped_entities": sum(context.skipped_counts.values()),
        "skipped_entity_counts": dict(sorted(context.skipped_counts.items())),
        "approximated_entities": context.counts["hatch_sampled"],
        "layouts": len(layouts),
    }
    return ImportResult(
        document=ir_document, diagnostics=context.diagnostics, statistics=statistics
    )


# ---------------------------------------------------------------------------
# Tables, blocks and spaces


def _build_tables(document: Any, context: _ConversionContext) -> None:
    for linetype in document.linetypes:
        name = linetype.name.value or f"EXB_LINETYPE_{linetype.id}"
        context.linetype_names[linetype.id] = _unique_name(name, context.linetype_names)
    for style in document.text_styles:
        name = style.name.value or f"EXB_STYLE_{style.id}"
        context.style_names[style.id] = _unique_name(name, context.style_names)
        factor = float(style.cjk_width_factor or 0.0)
        if math.isfinite(factor) and factor > 0:
            context.style_width_factors[style.id] = factor
    for layer in document.layers:
        name = layer.name.value or f"{_UNRESOLVED_LAYER_PREFIX}{layer.id}"
        name = _unique_name(name, context.layer_names)
        context.layer_names[layer.id] = name
        definition: dict[str, Any] = {
            "color": _colorref_hex(layer.color_raw),
            "visible": bool(layer.on) and not bool(layer.frozen),
            "plot": bool(layer.printable),
            "metadata": {
                "exb": {
                    "id": layer.id,
                    "color_raw": int(layer.color_raw),
                    "width": float(layer.width),
                    "width_flag": int(layer.width_flag),
                    "locked": bool(layer.locked),
                    "frozen": bool(layer.frozen),
                    "on": bool(layer.on),
                }
            },
        }
        linetype_name = context.linetype_names.get(layer.linetype_id)
        if linetype_name is not None:
            definition["linetype"] = linetype_name
        if layer.width_flag == 2 and layer.width > 0:
            definition["lineweight_mm"] = round(float(layer.width), 4)
        elif layer.width_flag in _LAYER_WIDTH_FLAGS:
            definition["lineweight_mm"] = PRESET_WIDTH_MM[
                _LAYER_WIDTH_FLAGS[layer.width_flag]
            ]
            context.counts["width_preset"] += 1
        context.layers[name] = definition


def _linetype_table(document: Any, context: _ConversionContext) -> dict[str, Any]:
    table: dict[str, Any] = {}
    for linetype in document.linetypes:
        name = context.linetype_names[linetype.id]
        scale = float(linetype.scale or 1.0)
        dashes = [float(value) for value in linetype.dashes]
        table[name] = {
            "pattern_mm": [value * scale for value in dashes],
            "metadata": {
                "exb": {
                    "id": linetype.id,
                    "scale": scale,
                    "dashes": dashes,
                    "pattern": linetype.pattern,
                    "advanced_present": bool(linetype.advanced_present),
                }
            },
        }
    return table


def _text_style_table(document: Any, context: _ConversionContext) -> dict[str, Any]:
    table: dict[str, Any] = {}
    for style in document.text_styles:
        name = context.style_names[style.id]
        table[name] = {
            "font": style.cjk_font.value or style.western_font.value or name,
            "metadata": {
                "exb": {
                    "id": style.id,
                    "western_font": style.western_font.value,
                    "cjk_font": style.cjk_font.value,
                    "western_width_factor": float(style.western_width_factor),
                    "cjk_width_factor": float(style.cjk_width_factor),
                    "character_spacing_factor": float(style.character_spacing_factor),
                    "line_spacing_factor": float(style.line_spacing_factor),
                    "height": float(style.height),
                    "oblique_angle": float(style.oblique_angle),
                }
            },
        }
    return table


def _name_blocks(document: Any, context: _ConversionContext) -> None:
    space_blocks = {space.block_id for space in document.spaces}
    for block in document.blocks:
        definition = block.definition
        if definition.id in space_blocks:
            continue
        name = definition.name.value or f"EXB_BLOCK_{definition.id}"
        if name in context.block_names.values():
            name = f"{name}_{definition.id}"
        context.block_names[definition.id] = name


def _convert_block(block: Any, context: _ConversionContext) -> None:
    document = context.document
    definition = block.definition
    name = context.block_names[definition.id]
    entities: list[dict[str, Any]] = []
    for index in block.entity_indices:
        entities.extend(_convert_entity_safe(document.entities[index], context))
    if definition.solid_geometry is not None:
        loops = _boundary_loops(definition.solid_geometry.boundary, context)
        if loops:
            solid: dict[str, Any] = {
                "id": context.allocate_id(),
                "kind": "HATCH",
                "layer": _layer_name_for(definition.solid_attributes, context),
                "solid": True,
                "loops": loops,
            }
            if context.options.entity_provenance:
                solid["source"] = {
                    "format": "exb",
                    "id": str(definition.id),
                    "kind": "BlockSolid",
                }
            entities.append(context.add_converted(solid))
            context.counts["block_solid"] += 1
    context.blocks[name] = {
        "base_point": [0.0, 0.0],
        "entities": entities,
        "metadata": {
            "exb": {
                "id": definition.id,
                "complete": bool(definition.complete),
                "stream_path": block.stream_path,
            }
        },
    }


def _members(document: Any, space: Any) -> list[int]:
    for block in document.blocks:
        if block.definition.id == space.block_id:
            return list(block.entity_indices)
    return []


def _main_space(document: Any) -> tuple[Any | None, bool]:
    if not document.spaces:
        return None, False
    main_space = document.spaces[0]
    if _members(document, main_space):
        return main_space, False
    candidates = [space for space in document.spaces if _members(document, space)]
    if not candidates:
        return main_space, False
    return max(candidates, key=lambda space: len(_members(document, space))), True


def _detect_paper(
    document: Any, space: Any, context: _ConversionContext
) -> dict[str, Any] | None:
    """Paper size from the largest ``*FRAME…`` block placed in the space.

    CAXA defines frame blocks in paper millimetres and places them scaled by the
    space's paper scale, so the frame block's extent is the sheet.
    """
    best: dict[str, Any] | None = None
    for index in _members(document, space):
        entity = document.entities[index]
        if entity.kind != "Insert":
            continue
        name = context.block_names.get(entity.geometry.block_id)
        if name is None or not name.upper().startswith(FRAME_BLOCK_PREFIX):
            continue
        extent = _block_line_extent(document, entity.geometry.block_id)
        if extent is None:
            continue
        width, height = extent[2] - extent[0], extent[3] - extent[1]
        if width <= 0 or height <= 0:
            continue
        scale_x, scale_y = (
            float(entity.geometry.scales[0]),
            float(entity.geometry.scales[1]),
        )
        origin_x, origin_y = (
            float(entity.geometry.insertion[0]),
            float(entity.geometry.insertion[1]),
        )
        corners = [
            (origin_x + extent[0] * scale_x, origin_y + extent[1] * scale_y),
            (origin_x + extent[2] * scale_x, origin_y + extent[3] * scale_y),
        ]
        candidate = {
            "width_mm": round(width, 3),
            "height_mm": round(height, 3),
            "frame_block": name,
            "insert_scale": round(scale_x, 6),
            "insert": [origin_x, origin_y],
            # the frame in model coordinates (rotation of the frame is not applied)
            "extent": [
                round(min(c[0] for c in corners), 3),
                round(min(c[1] for c in corners), 3),
                round(max(c[0] for c in corners), 3),
                round(max(c[1] for c in corners), 3),
            ],
        }
        if best is None or width * height > best["width_mm"] * best["height_mm"]:
            best = candidate
    return best


def _block_line_extent(
    document: Any, block_id: int
) -> tuple[float, float, float, float] | None:
    for block in document.blocks:
        if block.definition.id != block_id:
            continue
        xs: list[float] = []
        ys: list[float] = []
        for index in block.entity_indices:
            geometry = document.entities[index].geometry
            if geometry.kind == "Line":
                xs.extend((geometry.start[0], geometry.end[0]))
                ys.extend((geometry.start[1], geometry.end[1]))
            elif geometry.kind == "Polyline":
                xs.extend(vertex.point[0] for vertex in geometry.vertices)
                ys.extend(vertex.point[1] for vertex in geometry.vertices)
        if xs and ys:
            return (min(xs), min(ys), max(xs), max(ys))
        return None
    return None


# ---------------------------------------------------------------------------
# Entities


def _convert_entity_safe(
    entity: Any, context: _ConversionContext
) -> list[dict[str, Any]]:
    try:
        return _convert_entity(entity, context)
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        if context.options.strict:
            raise ImporterError(
                f"Failed to convert EXB {entity.kind} {entity.id}: {exc}"
            ) from exc
        context.skipped_counts[str(entity.kind)] += 1
        context.diagnostics.append(
            ImportDiagnostic(
                code="EXB_ENTITY_CONVERSION_FAILED",
                severity="error",
                message=f"Failed to convert EXB {entity.kind}: {exc}",
                source_id=str(entity.id),
                source_kind=str(entity.kind),
                action="skipped",
            )
        )
        return []


def _convert_entity(entity: Any, context: _ConversionContext) -> list[dict[str, Any]]:
    kind = str(entity.kind)
    geometry = entity.geometry
    if kind == "AttributeDefinition":
        context.counts["attribute_definitions"] += 1
        return []
    common = _entity_common(entity, context)
    if kind == "Line":
        return [
            context.add_converted(
                {
                    **common,
                    "kind": "LINE",
                    "p1": _xy(geometry.start),
                    "p2": _xy(geometry.end),
                }
            )
        ]
    if kind == "Circle":
        return [
            context.add_converted(
                {
                    **common,
                    "kind": "CIRCLE",
                    "center": _xy(geometry.center),
                    "radius": _positive(geometry.radius, "circle radius"),
                }
            )
        ]
    if kind == "Arc":
        return [context.add_converted({**common, **_arc_fields(geometry)})]
    if kind == "Polyline":
        return [
            context.add_converted({**common, **_polyline_fields(geometry, context)})
        ]
    if kind == "Text":
        fields = _text_fields(geometry, context)
        if fields is None:
            context.skipped_counts["Text"] += 1
            context.counts["empty_text"] += 1
            return []
        return [context.add_converted({**common, **fields})]
    if kind == "Insert":
        return [context.add_converted(_insert_fields(geometry, common, context))]
    if kind in ("Hatch", "Solid"):
        loops = _boundary_loops(geometry.boundary, context)
        if not loops:
            raise ValueError("hatch has no usable boundary loop")
        result: dict[str, Any] = {
            **common,
            "kind": "HATCH",
            "loops": loops,
            "solid": kind == "Solid",
        }
        if kind == "Hatch":
            pattern = geometry.pattern
            result["pattern"] = pattern.name_ascii or "EXB_DEFAULT"
            angle = float(pattern.angle_stored)
            if math.isfinite(angle) and angle != 0.0:
                result["pattern_angle"] = angle
            scale = float(pattern.scale)
            if math.isfinite(scale) and scale > 0:
                result["pattern_scale"] = scale
            exb_metadata = _exb_metadata(result)
            if exb_metadata is not None:
                exb_metadata["hatch"] = {
                    "pattern_name": pattern.name_ascii,
                    "scale": scale,
                    "angle_stored": angle,
                    "spacing": float(pattern.spacing),
                    "lines": [
                        {
                            "parameters_stored": [
                                float(v) for v in line.parameters_stored
                            ],
                            "dashes": [float(v) for v in line.dashes],
                        }
                        for line in pattern.lines
                    ],
                }
            context.counts["hatch_pattern"] += 1
        return [context.add_converted(result)]
    if kind == "Nurbs":
        return [context.add_converted({**common, **_spline_fields(geometry)})]
    if kind == "Ellipse":
        context.counts["ellipse_axes_assumed"] += 1
        return [context.add_converted({**common, **_ellipse_fields(geometry)})]
    if kind == "Dimension":
        return [context.add_converted(_dimension_fields(entity, common, context))]
    context.skipped_counts[kind] += 1
    context.diagnostics.append(
        ImportDiagnostic(
            code="EXB_UNSUPPORTED_ENTITY",
            severity="warning",
            message=f"Skipped an EXB {kind} entity that has no IR mapping.",
            source_id=str(entity.id),
            source_kind=kind,
            action="skipped",
        )
    )
    return []


def _entity_common(entity: Any, context: _ConversionContext) -> dict[str, Any]:
    style = entity.style
    attributes = entity.attributes
    layer_name = _layer_name(style.layer_id, style.layer_name, context)
    result: dict[str, Any] = {"id": context.allocate_id(), "layer": layer_name}
    if style.color_raw.source == "explicit" and style.color_raw.value is not None:
        result["color"] = _colorref_hex(int(style.color_raw.value))
    width_source = str(style.line_width.source)
    if width_source == "explicit":
        value = style.line_width.value
        if value is not None and math.isfinite(float(value)) and float(value) > 0:
            result["lineweight_mm"] = round(float(value), 4)
    elif width_source in PRESET_WIDTH_MM:
        result["lineweight_mm"] = PRESET_WIDTH_MM[width_source]
        context.counts["width_preset"] += 1
    if style.linetype.source == "explicit" and style.linetype.value is not None:
        linetype_name = context.linetype_names.get(int(style.linetype.value))
        if linetype_name is not None:
            result["linetype"] = linetype_name
    scale = float(attributes.linetype_scale)
    if math.isfinite(scale) and scale > 0 and abs(scale - 1.0) > _EPSILON:
        result["linetype_scale"] = scale
    if int(attributes.hide_flag_raw) != 0:
        result["visible"] = False
    if context.options.entity_provenance:
        result["source"] = {
            "format": "exb",
            "id": str(entity.id),
            "kind": str(entity.kind),
            "metadata": {
                "type_id": entity.type_id,
                "block_id": entity.block_id,
                "stream_path": entity.source.stream_path,
            },
        }
        result["metadata"] = {
            "exb": {
                "color_raw": int(attributes.color_raw),
                "color_source": str(style.color_raw.source),
                "line_width_raw": float(attributes.line_width_raw),
                "line_width_source": width_source,
                "linetype_source": str(style.linetype.source),
                "hide_flag_raw": int(attributes.hide_flag_raw),
            }
        }
    return result


def _layer_name(
    layer_id: int, layer_name: str | None, context: _ConversionContext
) -> str:
    known = context.layer_names.get(int(layer_id))
    if known is not None:
        return known
    if layer_name:
        return layer_name
    context.unresolved_layers.add(int(layer_id))
    return f"{_UNRESOLVED_LAYER_PREFIX}{int(layer_id)}"


def _layer_name_for(attributes: Any, context: _ConversionContext) -> str:
    if attributes is None:
        return "0"
    return _layer_name(int(attributes.layer_id_raw), None, context)


def _exb_metadata(entity: dict[str, Any]) -> dict[str, Any] | None:
    metadata = entity.get("metadata")
    if metadata is None:
        return None
    return metadata.setdefault("exb", {})


def _arc_fields(geometry: Any) -> dict[str, Any]:
    start_angle, end_angle = _arc_angles(
        geometry.center,
        geometry.start,
        geometry.end,
        geometry.x_axis,
        geometry.y_axis,
        float(geometry.sweep_angle),
    )
    return {
        "kind": "ARC",
        "center": _xy(geometry.center),
        "radius": _positive(geometry.radius, "arc radius"),
        "start_angle": start_angle,
        "end_angle": end_angle,
        "ccw": True,
    }


def _arc_angles(
    center: Any, start: Any, end: Any, x_axis: Any, y_axis: Any, sweep: float
) -> tuple[float, float]:
    """Start and end angle (degrees, counter-clockwise) of a stored arc.

    The arc runs from ``start`` by ``sweep`` radians in the basis ``x_axis`` /
    ``y_axis``; a left-handed basis or a negative sweep runs clockwise in world
    coordinates, in which case the endpoints are swapped.
    """
    a0 = math.degrees(math.atan2(start[1] - center[1], start[0] - center[0]))
    a1 = math.degrees(math.atan2(end[1] - center[1], end[0] - center[0]))
    handedness = x_axis[0] * y_axis[1] - x_axis[1] * y_axis[0]
    if handedness * sweep < 0:
        a0, a1 = a1, a0
    return a0 % 360.0, a1 % 360.0


def _polyline_fields(geometry: Any, context: _ConversionContext) -> dict[str, Any]:
    vertices: list[list[float]] = []
    widths = False
    for vertex in geometry.vertices:
        bulge = float(vertex.bulge)
        point = _xy(vertex.point)
        vertices.append([*point, bulge] if bulge else point)
        if vertex.start_width or vertex.end_width:
            widths = True
    if len(vertices) < 2:
        raise ValueError("polyline needs at least two vertices")
    if widths:
        context.counts["polyline_width_dropped"] += 1
    return {"kind": "LWPOLYLINE", "vertices": vertices, "closed": bool(geometry.closed)}


def normalize_text(contents: str, counts: Counter[str] | None = None) -> str:
    """Rewrite CAXA text escapes into IR text.

    ``%c`` / ``%d`` / ``%p`` / ``%x`` become the Unicode symbols, deviation and
    fit tolerances become DXF-style ``\\S上^下;`` stacks and roughness symbols
    (which have no text form) are removed. ``counts`` receives what was done.
    """
    counter = counts if counts is not None else Counter()
    for code, symbol in SYMBOL_ESCAPES.items():
        if code in contents:
            contents = contents.replace(code, symbol)
            counter["text_symbol_escape"] += 1
    contents, n = _DEVIATION_RE.subn(
        lambda m: f"\\S{m.group(1)}^{m.group(2)};", contents
    )
    counter["text_deviation_stacked"] += n
    contents, n = _FIT_RE.subn(lambda m: f"\\S{m.group(1)}^{m.group(2)};", contents)
    counter["text_fit_stacked"] += n
    contents, n = _ROUGHNESS_RE.subn("", contents)
    counter["text_roughness_dropped"] += n
    return contents


def _text_fields(text: Any, context: _ConversionContext) -> dict[str, Any] | None:
    contents = normalize_text(
        str(text.contents).replace("\r\n", "\n").replace("\r", "\n"), context.counts
    )
    if not contents.strip():
        return None
    height = float(text.text_height_stored) * float(text.drawing_scale or 1.0)
    if not (math.isfinite(height) and height > 0):
        raise ValueError("text height must be positive")
    alignment = int(text.alignment_raw)
    context.alignment_codes[alignment] += 1
    halign, valign = TEXT_ALIGNMENTS.get(alignment, ("left", "top"))
    fields: dict[str, Any] = {
        "insert": _xy(text.insertion),
        "height": height,
        "rotation": math.degrees(float(text.rotation)),
    }
    style_name = context.style_names.get(int(text.style_id_raw))
    if style_name is not None:
        fields["style"] = style_name
    elif int(text.style_status_raw) != 0:
        context.unresolved_styles.add(int(text.style_id_raw))
    width_factor = context.style_width_factors.get(int(text.style_id_raw))
    if int(text.fit_mode) == 1 and width_factor:
        area_width = float(text.area_width_stored) * float(text.drawing_scale or 1.0)
        natural = _natural_text_width(contents, height, width_factor)
        if area_width > 0 and natural > area_width:
            width_factor = width_factor * area_width / natural
            context.counts["text_fit_width_factor"] += 1
    if width_factor and abs(width_factor - 1.0) > _EPSILON:
        fields["width_factor"] = round(width_factor, 6)
    if _MTEXT_ESCAPE_RE.search(contents) or "\n" in contents:
        fields["kind"] = "MTEXT"
        fields["text"] = contents
        fields["attach"] = f"{valign}_{halign}"
    else:
        fields["kind"] = "TEXT"
        fields["text"] = contents
        fields["halign"] = halign
        fields["valign"] = valign
    if int(text.writing_direction) != 0:
        context.counts["text_vertical"] += 1
    if context.options.entity_provenance:
        fields["metadata"] = {
            "exb": {
                "text_height_stored": float(text.text_height_stored),
                "drawing_scale": float(text.drawing_scale),
                "area_width_stored": float(text.area_width_stored),
                "area_height_stored": float(text.area_height_stored),
                "alignment_raw": alignment,
                "mode_flags_raw": int(text.mode_flags_raw),
                "writing_direction": int(text.writing_direction),
                "fit_mode": int(text.fit_mode),
                "style_id_raw": int(text.style_id_raw),
                "line_spacing_raw": float(text.line_spacing_raw),
                "contents_raw": str(text.contents),
            }
        }
    return fields


def _natural_text_width(contents: str, height: float, width_factor: float) -> float:
    longest = (
        max(contents.replace("\\P", "\n").split("\n"), key=len) if contents else ""
    )
    plain = _MTEXT_ESCAPE_RE.sub("", longest)
    units = sum(1.0 if ord(ch) > 0x2E7F else 0.5 for ch in plain)
    return units * height * width_factor


def _insert_fields(
    geometry: Any, common: dict[str, Any], context: _ConversionContext
) -> dict[str, Any]:
    block_name = context.block_names.get(int(geometry.block_id))
    if block_name is None:
        raise KeyError(f"block {geometry.block_id} is not defined")
    scale_x, scale_y = float(geometry.scales[0]), float(geometry.scales[1])
    if abs(scale_x) <= _EPSILON or abs(scale_y) <= _EPSILON:
        if context.options.strict:
            raise ValueError("insert scale must be non-zero")
        scale_x = 1.0 if abs(scale_x) <= _EPSILON else scale_x
        scale_y = 1.0 if abs(scale_y) <= _EPSILON else scale_y
        context.counts["zero_insert_scale"] += 1
    result: dict[str, Any] = {
        **common,
        "kind": "INSERT",
        "block": block_name,
        "insert": _xy(geometry.insertion),
        "rotation": math.degrees(float(geometry.rotation)),
    }
    if not (math.isclose(scale_x, 1.0) and math.isclose(scale_y, 1.0)):
        result["scale"] = (
            scale_x if math.isclose(scale_x, scale_y) else [scale_x, scale_y]
        )
    attributes: list[dict[str, Any]] = []
    for attribute in geometry.attached_attributes:
        fields = _text_fields(attribute.text, context)
        if fields is None:
            continue
        visible = attribute.visibility is not False and (
            int(attribute.text_attributes.hide_flag_raw) == 0
        )
        item: dict[str, Any] = {
            "tag": str(attribute.tag.value),
            "text": str(attribute.value.value) or str(fields["text"]),
            "insert": fields["insert"],
            "height": fields["height"],
            "rotation": fields["rotation"],
            "visible": visible,
        }
        if fields["kind"] == "TEXT":
            item["halign"] = fields["halign"]
            item["valign"] = fields["valign"]
        for key in ("style", "width_factor"):
            if key in fields:
                item[key] = fields[key]
        attributes.append(item)
    if attributes:
        result["attribute_texts"] = attributes
    exb_metadata = _exb_metadata(result)
    if exb_metadata is not None:
        exb_metadata["insert"] = {
            "block_id": int(geometry.block_id),
            "scales": [float(value) for value in geometry.scales],
            "matrix_stored": [float(value) for value in geometry.matrix_stored],
            "nickname": str(geometry.nickname.value),
        }
    return result


def _spline_fields(geometry: Any) -> dict[str, Any]:
    control_points = [
        [float(point[0]), float(point[1])] for point in geometry.control_points
    ]
    weights = [
        float(point[3]) if len(point) > 3 else 1.0 for point in geometry.control_points
    ]
    degree = int(geometry.degree)
    if len(control_points) < 2 or not 1 <= degree <= 7:
        raise ValueError("unsupported NURBS degree or control point count")
    result: dict[str, Any] = {
        "kind": "SPLINE",
        "degree": degree,
        "control_points": control_points,
        "knots": [float(value) for value in geometry.knots],
    }
    if any(abs(weight - 1.0) > _EPSILON for weight in weights) and all(
        weight > 0 for weight in weights
    ):
        result["weights"] = weights
    return result


def _ellipse_fields(geometry: Any) -> dict[str, Any]:
    major, minor = float(geometry.major_radius), float(geometry.minor_radius)
    cos_r, sin_r = (
        math.cos(float(geometry.rotation)),
        math.sin(float(geometry.rotation)),
    )
    axis = [
        cos_r * geometry.x_axis[0] - sin_r * geometry.x_axis[1],
        cos_r * geometry.x_axis[1] + sin_r * geometry.x_axis[0],
    ]
    if major < minor:
        major, minor = minor, major
        axis = [-axis[1], axis[0]]
    if not (major > 0 and minor > 0):
        raise ValueError("ellipse radii must be positive")
    start, end = float(geometry.start_angle), float(geometry.end_angle)
    if abs(end - start) < _EPSILON:
        start, end = 0.0, _TAU
    return {
        "kind": "ELLIPSE",
        "center": _xy(geometry.center),
        "major_axis": [axis[0] * major, axis[1] * major],
        "ratio": min(minor / major, 1.0),
        "start_param": start,
        "end_param": end,
    }


def _dimension_fields(
    entity: Any, common: dict[str, Any], context: _ConversionContext
) -> dict[str, Any]:
    geometry = entity.geometry
    block_name = f"*EXB_D{entity.id}"
    block_entities: list[dict[str, Any]] = []
    for record in geometry.presentation_records:
        if record.geometry is None:
            context.counts["dimension_presentation_unsupported"] += 1
            continue
        shape = record.geometry.geometry
        base: dict[str, Any] = {"id": context.allocate_id(), "layer": common["layer"]}
        shape_kind = type(shape).__name__
        if shape_kind == "Line":
            block_entities.append(
                {**base, "kind": "LINE", "p1": _xy(shape.start), "p2": _xy(shape.end)}
            )
        elif shape_kind == "Arc":
            block_entities.append({**base, **_arc_fields(shape)})
        elif shape_kind == "Circle":
            block_entities.append(
                {
                    **base,
                    "kind": "CIRCLE",
                    "center": _xy(shape.center),
                    "radius": _positive(shape.radius, "circle radius"),
                }
            )
        elif shape_kind == "Polyline":
            try:
                block_entities.append({**base, **_polyline_fields(shape, context)})
            except ValueError:
                context.counts["dimension_presentation_unsupported"] += 1
        elif shape_kind == "Text":
            fields = _text_fields(shape, context)
            if fields is not None:
                fields.pop("metadata", None)
                block_entities.append({**base, **fields})
        elif shape_kind == "StoredSolid":
            loops = _boundary_loops(shape.boundary, context, count=False)
            if loops:
                block_entities.append(
                    {**base, "kind": "HATCH", "solid": True, "loops": loops}
                )
        else:
            context.counts["dimension_presentation_unsupported"] += 1
    texts = [field.value for field in geometry.text_fields_stored if field.value]
    definition: dict[str, Any] = {
        "points": {
            "location": _xy(geometry.location),
            "text_midpoint": _xy(geometry.text_location),
        },
        "text": normalize_text(texts[-1]) if texts else "",
        "measurement": float(geometry.value_stored),
        "source_geometry": {
            "type_id": geometry.dimension_type_id,
            "paper_scale": float(geometry.paper_scale),
            "text_fields": [field.value for field in geometry.text_fields_stored],
            "flags_raw": [int(value) for value in geometry.flags_raw],
            "note_location": _xy(geometry.note_location),
        },
    }
    if block_entities:
        context.blocks[block_name] = {
            "base_point": [0.0, 0.0],
            "entities": block_entities,
            "metadata": {
                "exb": {"dimension_presentation": True, "dimension_id": entity.id}
            },
        }
        definition["block"] = block_name
    else:
        context.counts["dimension_without_presentation"] += 1
    context.counts["dimension_from_cache"] += 1
    return {
        **common,
        "kind": "DIMENSION",
        "dim_kind": DIMENSION_KINDS.get(geometry.dimension_type_id, "GENERIC"),
        "definition": definition,
    }


# ---------------------------------------------------------------------------
# Boundaries


def _boundary_loops(
    boundary: Any, context: _ConversionContext, *, count: bool = True
) -> list[dict[str, Any]]:
    segments = context.options.curve_segments
    loops: list[dict[str, Any]] = []
    sampled = False
    for loop in boundary.loops:
        points: list[list[float]] = []
        for curve in loop.curves:
            shape = curve.geometry
            kind = shape.kind
            if kind == "Line":
                points.extend((_xy(shape.start), _xy(shape.end)))
            elif kind == "Point":
                points.append(_xy(shape.point))
            elif kind == "Circular":
                points.extend(
                    _sample_circular(
                        shape.center,
                        float(shape.radius),
                        shape.x_axis,
                        shape.y_axis,
                        float(shape.sweep_angle),
                        segments,
                    )
                )
                sampled = True
            elif kind == "Ellipse":
                points.extend(
                    _sample_ellipse(
                        shape.center,
                        shape.x_axis,
                        shape.y_axis,
                        float(shape.major_radius),
                        float(shape.minor_radius),
                        float(shape.start_angle),
                        float(shape.end_angle),
                        float(shape.rotation),
                        segments,
                    )
                )
                sampled = True
            elif kind == "Nurbs":
                points.extend(
                    _sample_nurbs(
                        shape.control_points, shape.knots, int(shape.degree), segments
                    )
                )
                sampled = True
            elif kind == "Polyline":
                vertices = [
                    (_xy(vertex.point), float(vertex.bulge))
                    for vertex in shape.vertices
                ]
                points.extend(_flatten_polyline(vertices, bool(shape.closed), segments))
                sampled = sampled or any(bulge for _, bulge in vertices)
            else:
                raise ValueError(f"unsupported boundary curve {kind}")
        points = _dedupe(points)
        if len(points) >= 2 and math.dist(points[0], points[-1]) < _EPSILON:
            points.pop()
        if len(points) >= 3:
            loops.append({"vertices": points, "is_outer": not loops})
    if count and loops:
        if sampled:
            context.counts["hatch_sampled"] += 1
            context.hatch_vertices += sum(len(loop["vertices"]) for loop in loops)
    return loops


def _sample_circular(
    center: Any,
    radius: float,
    x_axis: Any,
    y_axis: Any,
    sweep: float,
    segments: int,
) -> list[list[float]]:
    if abs(sweep) < _EPSILON:
        sweep = _TAU
    count = max(2, int(math.ceil(segments * abs(sweep) / _TAU)))
    points = []
    for index in range(count + 1):
        t = sweep * index / count
        c, s = math.cos(t), math.sin(t)
        points.append(
            [
                center[0] + radius * (c * x_axis[0] + s * y_axis[0]),
                center[1] + radius * (c * x_axis[1] + s * y_axis[1]),
            ]
        )
    return points


def _sample_ellipse(
    center: Any,
    x_axis: Any,
    y_axis: Any,
    major: float,
    minor: float,
    start: float,
    end: float,
    rotation: float,
    segments: int,
) -> list[list[float]]:
    cos_r, sin_r = math.cos(rotation), math.sin(rotation)
    ux = (cos_r * x_axis[0] - sin_r * x_axis[1], cos_r * x_axis[1] + sin_r * x_axis[0])
    uy = (cos_r * y_axis[0] - sin_r * y_axis[1], cos_r * y_axis[1] + sin_r * y_axis[0])
    sweep = end - start
    if abs(sweep) < _EPSILON:
        sweep = _TAU
    count = max(8, int(math.ceil(segments * abs(sweep) / _TAU)))
    points = []
    for index in range(count + 1):
        t = start + sweep * index / count
        c, s = math.cos(t), math.sin(t)
        points.append(
            [
                center[0] + major * c * ux[0] + minor * s * uy[0],
                center[1] + major * c * ux[1] + minor * s * uy[1],
            ]
        )
    return points


def _de_boor(
    t: float, degree: int, knots: list[float], points: list[list[float]]
) -> list[float]:
    last = len(points) - 1
    index = last
    for candidate in range(degree, last + 1):
        if knots[candidate] <= t < knots[candidate + 1]:
            index = candidate
            break
    d = [list(points[j + index - degree]) for j in range(degree + 1)]
    for r in range(1, degree + 1):
        for j in range(degree, r - 1, -1):
            i = j + index - degree
            denominator = knots[i + degree - r + 1] - knots[i]
            alpha = 0.0 if denominator == 0 else (t - knots[i]) / denominator
            d[j] = [
                (1 - alpha) * d[j - 1][c] + alpha * d[j][c] for c in range(len(d[j]))
            ]
    return d[degree]


def _sample_nurbs(
    control_points: Any, knots: Any, degree: int, segments: int
) -> list[list[float]]:
    points = []
    for point in control_points:
        weight = float(point[3]) if len(point) > 3 else 1.0
        if abs(weight - 1.0) > _EPSILON and weight > 0:
            points.append([point[0] * weight, point[1] * weight, weight])
        else:
            points.append([float(point[0]), float(point[1]), 1.0])
    knot_values = [float(value) for value in knots]
    if len(knot_values) != len(points) + degree + 1 or len(points) < 2:
        raise ValueError("NURBS knot vector does not match the control points")
    t0, t1 = knot_values[degree], knot_values[len(points)]
    count = max(8, segments)
    out = []
    for index in range(count + 1):
        t = t0 + (t1 - t0) * index / count
        value = _de_boor(min(t, t1 - 1e-12), degree, knot_values, points)
        weight = value[2] if value[2] else 1.0
        out.append([value[0] / weight, value[1] / weight])
    return out


def _flatten_bulge(
    p0: list[float], p1: list[float], bulge: float, segments: int
) -> list[list[float]]:
    if abs(bulge) < _EPSILON:
        return [p1]
    chord = math.dist(p0, p1)
    if chord < _EPSILON:
        return [p1]
    theta = 4.0 * math.atan(bulge)
    radius = chord / (2.0 * math.sin(abs(theta) / 2.0))
    mx, my = (p0[0] + p1[0]) / 2.0, (p0[1] + p1[1]) / 2.0
    dx, dy = (p1[0] - p0[0]) / chord, (p1[1] - p0[1]) / chord
    h = math.sqrt(max(radius * radius - (chord / 2.0) ** 2, 0.0))
    sign = 1.0 if bulge > 0 else -1.0
    cx, cy = mx - sign * dy * h, my + sign * dx * h
    a0 = math.atan2(p0[1] - cy, p0[0] - cx)
    count = max(2, int(math.ceil(segments * abs(theta) / _TAU)))
    return [
        [
            cx + radius * math.cos(a0 + theta * i / count),
            cy + radius * math.sin(a0 + theta * i / count),
        ]
        for i in range(1, count + 1)
    ]


def _flatten_polyline(
    vertices: list[tuple[list[float], float]], closed: bool, segments: int
) -> list[list[float]]:
    points: list[list[float]] = []
    for index, (point, _bulge) in enumerate(vertices):
        if index == 0:
            points.append(list(point))
            continue
        points.extend(
            _flatten_bulge(points[-1], list(point), vertices[index - 1][1], segments)
        )
    if closed and len(vertices) > 1:
        points.extend(
            _flatten_bulge(points[-1], list(vertices[0][0]), vertices[-1][1], segments)
        )
    return points


def _dedupe(points: list[list[float]]) -> list[list[float]]:
    out: list[list[float]] = []
    for point in points:
        if not out or math.dist(out[-1], point) > _EPSILON:
            out.append([float(point[0]), float(point[1])])
    return out


# ---------------------------------------------------------------------------
# Summary diagnostics


def _append_summary_diagnostics(
    document: Any,
    context: _ConversionContext,
    unsupported: Counter[str],
    layouts: list[dict[str, Any]],
    paper: dict[str, Any] | None,
) -> None:
    diagnostics = context.diagnostics
    counts = context.counts
    report = document.report
    build = document.file_info.product_build.value
    diagnostics.append(
        ImportDiagnostic(
            code="EXB_UNITS_ASSUMED_MM",
            severity="info",
            message=(
                "An EXB drawing carries no unit; its coordinates were taken as "
                "millimetres at real size (the drawing scale is in "
                "header.metadata.exb.paper_scale)."
            ),
            action="assumed",
            details={"product_build": build},
        )
    )
    if paper is not None:
        diagnostics.append(
            ImportDiagnostic(
                code="EXB_PAPER_DETECTED",
                severity="info",
                message=(
                    f"The paper is {paper['width_mm']:g} × {paper['height_mm']:g} mm "
                    f"(frame block {paper['frame_block']}) at drawing scale "
                    f"{_scale_label(document.spaces[0].scale if document.spaces else 1.0)}."
                ),
                action="detected",
                details=dict(paper),
            )
        )
    # ezexb 0.3 reads product builds it has not verified against a native sample with the
    # rules of their major version and reports ``unverified_product_build``; that alone turns
    # ``report.complete`` off, which is not a partial read (no record was skipped)
    unverified_build = next(
        (d for d in report.diagnostics if d.code == "unverified_product_build"), None
    )
    if unverified_build is not None:
        diagnostics.append(
            ImportDiagnostic(
                code="EXB_PRODUCT_BUILD_UNVERIFIED",
                severity="info",
                message=str(unverified_build.message),
                action="assumed",
                details={"product_build": str(document.file_info.product_build.value)},
            )
        )
    only_unverified_build = (
        unverified_build is not None
        and int(report.unsupported_entity_count) == 0
        and all(
            d.code
            in (
                "unverified_product_build",
                "unsupported_dimension_semantics",
                "unsupported_dimension_presentation",
            )
            for d in report.diagnostics
        )
    )
    if not report.complete and not only_unverified_build:
        diagnostics.append(
            ImportDiagnostic(
                code="EXB_PARTIAL_READ",
                severity="warning",
                message=(
                    "ezexb read the drawing in partial mode: "
                    f"{report.unsupported_entity_count} of "
                    f"{report.top_level_record_count} records could not be decoded "
                    "(see EXB_UNSUPPORTED_ENTITY) and the dimension semantics are "
                    "not decoded."
                ),
                details={
                    "decoded_entity_count": int(report.decoded_entity_count),
                    "unsupported_entity_count": int(report.unsupported_entity_count),
                    "top_level_record_count": int(report.top_level_record_count),
                    "read_diagnostics": dict(
                        Counter(diagnostic.code for diagnostic in report.diagnostics)
                    ),
                },
            )
        )
    for type_id, count in sorted(
        unsupported.items(), key=lambda item: (-item[1], item[0])
    ):
        context.skipped_counts[f"unsupported:{type_id}"] += count
        diagnostics.append(
            ImportDiagnostic(
                code="EXB_UNSUPPORTED_ENTITY",
                severity="warning",
                message=(
                    f"Skipped {count} EXB records of type {type_id} that ezexb cannot "
                    "decode yet (CAXA annotations such as roughness, geometric "
                    "tolerance, chamfer and table objects are not supported)."
                ),
                source_kind=type_id,
                action="skipped",
                details={"type_id": type_id, "count": count},
            )
        )
    if counts["dimension_from_cache"]:
        diagnostics.append(
            ImportDiagnostic(
                code="EXB_DIMENSION_FROM_CACHE",
                severity="info",
                message=(
                    f"{counts['dimension_from_cache']} dimensions were rebuilt from the "
                    "display records CAXA cached (lines, arcs, arrow fills, text) as a "
                    "dimension block; the stored value and text are in definition, the "
                    "construction, associativity and tolerance semantics are not decoded."
                ),
                source_kind="Dimension",
                action="flattened",
                details={
                    "count": counts["dimension_from_cache"],
                    "without_presentation": counts["dimension_without_presentation"],
                    "unsupported_presentation_records": counts[
                        "dimension_presentation_unsupported"
                    ],
                },
            )
        )
    assumed = {
        code: count
        for code, count in sorted(context.alignment_codes.items())
        if code not in VERIFIED_TEXT_ALIGNMENTS
    }
    if assumed:
        diagnostics.append(
            ImportDiagnostic(
                code="EXB_TEXT_ALIGNMENT_ASSUMED",
                severity="info",
                message=(
                    f"{sum(assumed.values())} texts use an alignment code that was not "
                    "verified against CAXA (2 = top-right by the 3×3 grid, or an unknown "
                    "code read as top-left)."
                ),
                source_kind="Text",
                action="assumed",
                details={
                    "codes": {str(code): count for code, count in assumed.items()}
                },
            )
        )
    escaped = (
        counts["text_symbol_escape"]
        + counts["text_deviation_stacked"]
        + counts["text_fit_stacked"]
    )
    if escaped:
        diagnostics.append(
            ImportDiagnostic(
                code="EXB_TEXT_ESCAPE_NORMALIZED",
                severity="info",
                message=(
                    "CAXA text escapes were rewritten: %c/%d/%p/%x as ⌀ ° ± ×, deviation "
                    "and fit tolerances as DXF-style \\S stacks."
                ),
                source_kind="Text",
                action="normalized",
                details={
                    "symbols": counts["text_symbol_escape"],
                    "deviation_tolerances": counts["text_deviation_stacked"],
                    "fit_tolerances": counts["text_fit_stacked"],
                },
            )
        )
    if counts["text_roughness_dropped"]:
        diagnostics.append(
            ImportDiagnostic(
                code="EXB_TEXT_SYMBOL_DROPPED",
                severity="warning",
                message=(
                    f"{counts['text_roughness_dropped']} surface roughness symbols "
                    "embedded in text (\\R…;) have no text form and were removed."
                ),
                source_kind="Text",
                action="skipped",
                details={"count": counts["text_roughness_dropped"]},
            )
        )
    if counts["text_fit_width_factor"]:
        diagnostics.append(
            ImportDiagnostic(
                code="EXB_TEXT_FIT_WIDTH_FACTOR",
                severity="info",
                message=(
                    f"{counts['text_fit_width_factor']} fit-to-box texts got a width "
                    "factor derived from their stored box width; CAXA compresses the "
                    "glyphs to the box."
                ),
                source_kind="Text",
                action="approximated",
                details={"count": counts["text_fit_width_factor"]},
            )
        )
    if counts["text_vertical"]:
        diagnostics.append(
            ImportDiagnostic(
                code="EXB_TEXT_VERTICAL_IGNORED",
                severity="warning",
                message=(
                    f"{counts['text_vertical']} vertical-writing texts were converted as "
                    "horizontal text (the writing direction is in metadata.exb)."
                ),
                source_kind="Text",
                action="approximated",
                details={"count": counts["text_vertical"]},
            )
        )
    if counts["hatch_sampled"]:
        diagnostics.append(
            ImportDiagnostic(
                code="EXB_HATCH_BOUNDARY_SAMPLED",
                severity="warning",
                message=(
                    f"{counts['hatch_sampled']} hatch/solid boundaries with arcs, "
                    "ellipses, NURBS or bulges were sampled as polyline loops."
                ),
                source_kind="Hatch",
                action="approximated",
                details={
                    "count": counts["hatch_sampled"],
                    "vertices": context.hatch_vertices,
                },
            )
        )
    if counts["hatch_pattern"]:
        diagnostics.append(
            ImportDiagnostic(
                code="EXB_HATCH_PATTERN_PRESERVED",
                severity="info",
                message=(
                    f"{counts['hatch_pattern']} hatches keep their CAXA pattern name, "
                    "angle, scale and stored line definitions in metadata.exb.hatch; the "
                    "pattern lines are not regenerated."
                ),
                source_kind="Hatch",
                action="preserved_metadata",
                details={"count": counts["hatch_pattern"]},
            )
        )
    if counts["width_preset"]:
        diagnostics.append(
            ImportDiagnostic(
                code="EXB_LINE_WIDTH_PRESET",
                severity="info",
                message=(
                    "Symbolic CAXA line widths (thin / mid / wide / double wide) were "
                    "mapped to the default widths 0.25 / 0.35 / 0.5 / 0.7 mm."
                ),
                action="assumed",
                details={"count": counts["width_preset"], "widths_mm": PRESET_WIDTH_MM},
            )
        )
    if counts["polyline_width_dropped"]:
        diagnostics.append(
            ImportDiagnostic(
                code="EXB_POLYLINE_WIDTH_DROPPED",
                severity="warning",
                message=(
                    f"{counts['polyline_width_dropped']} polylines had segment widths, "
                    "which the IR polyline does not carry."
                ),
                source_kind="Polyline",
                action="approximated",
                details={"count": counts["polyline_width_dropped"]},
            )
        )
    if counts["zero_insert_scale"]:
        diagnostics.append(
            ImportDiagnostic(
                code="EXB_ZERO_INSERT_SCALE_NORMALIZED",
                severity="warning",
                message=(
                    f"{counts['zero_insert_scale']} block references had a zero scale "
                    "that was replaced with 1.0."
                ),
                source_kind="Insert",
                action="normalized",
                details={"count": counts["zero_insert_scale"]},
            )
        )
    if counts["ellipse_axes_assumed"]:
        diagnostics.append(
            ImportDiagnostic(
                code="EXB_ELLIPSE_AXES_ASSUMED",
                severity="warning",
                message=(
                    f"{counts['ellipse_axes_assumed']} ellipses: the major axis was "
                    "taken as the stored x axis rotated by the stored rotation."
                ),
                source_kind="Ellipse",
                action="approximated",
                details={"count": counts["ellipse_axes_assumed"]},
            )
        )
    if counts["attribute_definitions"]:
        diagnostics.append(
            ImportDiagnostic(
                code="EXB_ATTRIBUTE_DEFINITIONS_SKIPPED",
                severity="info",
                message=(
                    f"{counts['attribute_definitions']} attribute definitions (title "
                    "block templates) were not converted; the attribute values a block "
                    "reference carries are kept as attribute_texts on the INSERT."
                ),
                source_kind="AttributeDefinition",
                action="skipped",
                details={"count": counts["attribute_definitions"]},
            )
        )
    if counts["block_solid"]:
        diagnostics.append(
            ImportDiagnostic(
                code="EXB_BLOCK_SOLID_ATTACHED",
                severity="info",
                message=(
                    f"{counts['block_solid']} block definitions carry an attached fill "
                    "that was converted as a solid HATCH inside the block."
                ),
                source_kind="Block",
                details={"count": counts["block_solid"]},
            )
        )
    if layouts:
        diagnostics.append(
            ImportDiagnostic(
                code="EXB_LAYOUTS_PRESERVED",
                severity="info",
                message=(
                    f"{len(layouts)} further spaces were kept as layouts: "
                    + ", ".join(layout["name"] for layout in layouts)
                    + "."
                ),
                action="preserved_metadata",
                details={
                    "layouts": [
                        {"name": layout["name"], "entities": len(layout["entities"])}
                        for layout in layouts
                    ]
                },
            )
        )
    if context.unresolved_layers:
        diagnostics.append(
            ImportDiagnostic(
                code="EXB_UNRESOLVED_LAYER",
                severity="warning",
                message=(
                    f"{len(context.unresolved_layers)} layer ids referenced by entities "
                    "are not in the layer table; placeholder layer names were used."
                ),
                action="normalized",
                details={"layer_ids": sorted(context.unresolved_layers)},
            )
        )
    if context.unresolved_styles:
        diagnostics.append(
            ImportDiagnostic(
                code="EXB_TEXT_STYLE_UNRESOLVED",
                severity="warning",
                message=(
                    f"{len(context.unresolved_styles)} text style ids referenced by texts "
                    "are not in the text style table; no style was assigned."
                ),
                source_kind="Text",
                action="normalized",
                details={"style_ids": sorted(context.unresolved_styles)},
            )
        )


# ---------------------------------------------------------------------------
# Helpers


def _scale_label(scale: float) -> str:
    if not math.isfinite(scale) or scale <= 0:
        return "1:1"
    if scale >= 1.0:
        return f"1:{scale:g}"
    return f"{1.0 / scale:g}:1"


def _colorref_hex(raw: int) -> str:
    value = int(raw) & 0xFFFFFF
    red, green, blue = value & 0xFF, (value >> 8) & 0xFF, (value >> 16) & 0xFF
    return f"#{red:02X}{green:02X}{blue:02X}"


def _unique_name(candidate: str, used: dict[int, str]) -> str:
    taken = set(used.values())
    if candidate not in taken:
        return candidate
    index = 2
    while f"{candidate}_{index}" in taken:
        index += 1
    return f"{candidate}_{index}"


def _xy(point: Any) -> list[float]:
    x, y = float(point[0]), float(point[1])
    if not (math.isfinite(x) and math.isfinite(y)):
        raise ValueError("coordinate is not finite")
    return [x, y]


def _positive(value: Any, label: str) -> float:
    number = float(value)
    if not (math.isfinite(number) and number > 0):
        raise ValueError(f"{label} must be positive")
    return number


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
