"""Native DRA-CAD (MPZ / MPS / MPX / MPUW) to CAD 2D IR importer backed by :mod:`ezdracad`.

``ezdracad``'s best-effort reader returns every object whose saved layout is
known, in the units and with the table references the file stores. This adapter
turns that into a drawing as DRA-CAD shows it:

* coordinates are taken as millimetres at real size (a DRA-CAD drawing carries
  no unit, ``DRACAD_UNITS_ASSUMED_MM``); the drawing scale, the paper size and
  the sheet in drawing coordinates are in ``header.metadata.dracad``;
* a text or an arrowhead whose size is given on the output sheet (出力サイズ
  指定) is multiplied by the drawing scale, as are the line-type patterns
  (``header.linetype_scale``);
* a text is laid out in character cells: its height is the cell height, and a
  stored character advance becomes a width factor against one cell per
  full-width character and half a cell per half-width one;
* a dimension, a leader and a hatch are stored twice: as a definition and as a
  package of the lines, arcs, arrowheads and texts DRA-CAD drew for it. The
  package is what is converted. A dimension becomes a ``DIMENSION`` whose
  ``definition.block`` holds that geometry (``DRACAD_DIMENSION_FROM_PACKAGE``);
  the lines of a hatch, including those of a named pattern, become ``LINE``
  entities; a definition without a package draws nothing, as in DRA-CAD
  (``DRACAD_DEFINITION_NOT_DRAWN``);
* the fill of a circle, an ellipse, a polyline or a region becomes a solid
  ``HATCH`` in front of which the outline is drawn, with the transparency as the
  alpha of its colour;
* splines and Bezier curves are polylines through the points ``ezdracad``
  evaluates (``DRACAD_CURVE_APPROXIMATED``);
* an arrowhead is drawn as lines, a circle or a solid ``HATCH`` by its type;
* an image keeps the path of its file (``DRACAD_IMAGE_REFERENCED``); a
  reference to another drawing cannot be resolved and is left out
  (``DRACAD_EXTERNAL_REFERENCE_SKIPPED``);
* a colour is the RGB value of the palette entry; white and black lines are the
  colour that contrasts with the background (ACI 7), as DRA-CAD draws them;
* DRA-CAD paints the last object of the file first, so the entities are listed
  in the reverse of the stored order: a later IR entity is drawn over an
  earlier one, which matters where fills overlap.
"""

from __future__ import annotations

import hashlib
import math
import re
from collections import Counter
from collections.abc import Iterable, Sequence
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

# ezdracad's defaults (64 MiB, one million records) are sized for untrusted
# input; a production drawing can be larger. Budgets are ceilings, not allocations.
DEFAULT_PARSE_LIMITS: dict[str, int] = {
    "max_file_size": 1024**3,
    "max_stream_size": 1024**3,
    "max_total_stream_size": 2 * 1024**3,
    "max_records": 20_000_000,
    "max_decompressed_size": 2 * 1024**3,
}
DEFAULT_READ_LIMITS: dict[str, int] = {
    "max_entities": 20_000_000,
    "max_curve_points": 40_000_000,
}

# Attribute values that defer to the layer or to the enclosing block.
_BY_LAYER_COLOR = 257
_BY_BLOCK_COLOR = 258
_BY_LAYER_LINE_TYPE = 33
_BY_LAYER_WIDTH = 17
# Fill colours that are not palette entries.
_RGB_FILL = 257
_BACKGROUND_FILL = 258

DIMENSION_KINDS: dict[int, str] = {
    1: "ALIGNED",
    2: "LINEAR",
    3: "ANGULAR",
    4: "RADIAL",
    5: "DIAMETER",
}
# Package kinds: what the members of a package are the drawn form of.
_PACKAGE_REFERENCE = 1
_PACKAGE_DIMENSION = 3
_PACKAGE_HATCH = 5

CONTINUOUS = "CONTINUOUS"
_EPSILON = 1e-9
_TAU = 2.0 * math.pi
# Bulge of a quarter circle.
_QUARTER_BULGE = math.tan(math.pi / 8.0)
_ARROW_CIRCLES = frozenset({1, 2, 3})
_ARROW_SQUARES = frozenset({8, 9, 10})
_ARROW_FILLED = frozenset({3, 7, 10, 15})

# A text field: ``{TCODE:name}`` prints a value of the drawing or of the
# application, ``{PSET:name}`` a value of the drawing's property set.
_FIELD = re.compile(r"\{(TCODE|PSET):([^{}]*)\}")
_FIELD_NAME = re.compile(r'^([A-Za-z]+)(?:@"([^"]*)")?$')
# ``@"L4"`` keeps the first four characters, ``@"R3"`` the last three,
# ``@"M6"`` everything from the sixth on and ``@"M2,3"`` three from the second.
_FIELD_CUT = re.compile(r"^([LRM])(\d+)(?:,(\d+))?$")
# Fields whose value is not in the drawing: the application and its licence,
# the user, the clock, and where and how the file is stored.
_OUTSIDE_FIELDS = frozenset(
    {
        "UserName",
        "CompanyName",
        "Version",
        "SerialNo",
        "CreateAuthor",
        "ModifiedAuthor",
        "LastPrintedAuthor",
        "CreateDate",
        "ModifiedDate",
        "LastPrintedDate",
        "Date",
        "Time",
        "PageNumber",
        "ProjectName",
        "FolderName",
        "PathName",
        "FullPathName",
        "FileSize",
        "DataNumber",
    }
)
_PAPER_NAMES = {
    0: "Free",
    **{1 + size: f"A{size}" for size in range(7)},
    **{8 + size: f"B{size}" for size in range(7)},
    15: "Letter",
    16: "Legal",
}


@dataclass(slots=True)
class _Scope:
    """What one record table (the root or a block) says about its packages."""

    dimensions: dict[int, Any] = field(default_factory=dict)
    drawn_dimensions: set[int] = field(default_factory=set)
    placed_references: set[int] = field(default_factory=set)
    drawn_hatches: set[int] = field(default_factory=set)


@dataclass(slots=True)
class _ConversionContext:
    options: ImportOptions
    drawing: Any
    scale: float
    palette: dict[int, tuple[int, int, int]] = field(default_factory=dict)
    widths: dict[int, float] = field(default_factory=dict)
    #: Pen width of each colour, used instead of ``widths`` when the drawing is
    #: printed with widths by colour.
    pen_widths: dict[int, float] = field(default_factory=dict)
    width_by_color: bool = False
    layer_names: dict[int, str] = field(default_factory=dict)
    linetype_names: dict[int, str] = field(default_factory=dict)
    block_names: dict[bytes, str] = field(default_factory=dict)
    used_layers: set[int] = field(default_factory=set)
    used_linetypes: set[int] = field(default_factory=set)
    text_styles: dict[str, dict[str, Any]] = field(default_factory=dict)
    blocks: dict[str, dict[str, Any]] = field(default_factory=dict)
    diagnostics: list[ImportDiagnostic] = field(default_factory=list)
    converted_counts: Counter[str] = field(default_factory=Counter)
    skipped_counts: Counter[str] = field(default_factory=Counter)
    counts: Counter[str] = field(default_factory=Counter)
    external_references: list[str] = field(default_factory=list)
    undefined_blocks: set[str] = field(default_factory=set)
    #: File name the drawing was read from; its fields print it.
    source_name: str | None = None
    property_set: dict[str, str] = field(default_factory=dict)
    #: Names of fields left empty because their value is not in the drawing.
    blank_fields: set[str] = field(default_factory=set)
    next_entity_number: int = 1

    def allocate_id(self) -> str:
        entity_id = f"DRA_E{self.next_entity_number:08d}"
        self.next_entity_number += 1
        return entity_id

    def add_converted(self, entity: dict[str, Any]) -> dict[str, Any]:
        self.converted_counts[str(entity["kind"])] += 1
        return entity


def default_limits(ezdracad: Any) -> tuple[Any, Any]:
    """``ezdracad`` parse and read limits with budgets sized for production drawings."""
    return (
        ezdracad.ParseLimits(**DEFAULT_PARSE_LIMITS),
        ezdracad.best_effort.BestEffortLimits(**DEFAULT_READ_LIMITS),
    )


def convert_dracad_file_to_ir(
    path: str | Path,
    *,
    options: ImportOptions | None = None,
    limits: Any | None = None,
    read_limits: Any | None = None,
    source_name: str | None = None,
) -> ImportResult:
    """Read a DRA-CAD drawing with ``ezdracad`` and convert it to IR.

    Install the ``dracad`` extra to enable this importer::

        pip install "cad2d-ir[dracad]"

    MPZ, flat MPX, and MPS and MPUW files saved without a password are read,
    identified by content. ``limits`` is an ``ezdracad.ParseLimits`` and
    ``read_limits`` an ``ezdracad.best_effort.BestEffortLimits``; the defaults
    raise ``ezdracad``'s budgets (see ``DEFAULT_PARSE_LIMITS``). Objects that
    ``ezdracad`` cannot decode are counted in ``DRACAD_PARTIAL_READ`` instead of
    stopping the read.

    ``source_name`` is the name the drawing has for its user, when ``path`` is
    a temporary copy under another name: text fields print the file name.
    """
    try:
        import ezdracad
        from ezdracad import best_effort
    except ImportError as exc:
        raise MissingOptionalDependencyError(
            "DRA-CAD support requires the optional dependency ezdracad; "
            'install it with `pip install "cad2d-ir[dracad]"`.'
        ) from exc

    source_path = Path(path)
    default_parse, default_read = default_limits(ezdracad)
    try:
        drawing = best_effort.readfile(
            source_path,
            limits=limits if limits is not None else default_parse,
            read_limits=read_limits if read_limits is not None else default_read,
        )
    except ezdracad.UnsupportedFormatError as exc:
        raise ImporterError(f"Unsupported DRA-CAD file {source_path}: {exc}") from exc
    except ezdracad.LimitExceededError as exc:
        raise ImporterError(
            f"DRA-CAD file {source_path} exceeds a read limit: {exc}"
        ) from exc
    except ezdracad.DracadError as exc:
        raise ImporterError(
            f"Failed to read DRA-CAD file {source_path}: {exc}"
        ) from exc
    return dracad_drawing_to_ir(
        drawing,
        source_name=source_name if source_name is not None else source_path.name,
        source_sha256=_sha256_file(source_path),
        parser_version=str(ezdracad.__version__),
        options=options,
    )


def dracad_drawing_to_ir(
    drawing: Any,
    *,
    source_name: str | None = None,
    source_sha256: str | None = None,
    parser_version: str | None = None,
    options: ImportOptions | None = None,
) -> ImportResult:
    """Convert an ``ezdracad.best_effort.Drawing`` to IR."""
    import_options = options or ImportOptions()
    settings = drawing.settings
    scale = float(settings.drawing_scale) if settings is not None else 1.0
    if not (math.isfinite(scale) and scale > 0):
        scale = 1.0
    context = _ConversionContext(
        options=import_options,
        drawing=drawing,
        scale=scale,
        source_name=source_name,
        property_set=_property_set(drawing),
    )
    _build_tables(drawing, context)

    for block in drawing.blocks:
        name = context.block_names[bytes(block.name_bytes)]
        if name in context.blocks:
            continue  # a later definition with the same name is never referenced
        context.blocks[name] = {
            "base_point": [0.0, 0.0],
            "entities": _convert_scope(block.entities, context),
            "metadata": {"dracad": {"index": int(block.index)}},
        }
    model, sheets = _split_sheets(drawing.entities)
    entities = _convert_scope(model, context)
    layouts = _layouts(sheets, context)

    paper = _paper(settings, scale)
    header_metadata: dict[str, Any] = {
        "format": str(drawing.format),
        "marker": int(drawing.marker),
        "units_assumed": True,
        "paper_scale": scale,
        "paper": paper,
    }
    if settings is not None:
        header_metadata["drawing_origin"] = [
            float(value) for value in settings.drawing_origin
        ]
        ratio = float(getattr(settings, "scale_ratio", 1.0) or 1.0)
        if math.isfinite(ratio) and ratio > 0 and abs(ratio - 1.0) > _EPSILON:
            # The scale the drawing states in its scale field and status bar.
            header_metadata["stated_scale"] = scale / ratio
        comment = str(getattr(settings, "comment", "") or "")
        if comment:
            header_metadata["comment"] = comment
        if context.width_by_color:
            header_metadata["width_by_color"] = True
    tables: dict[str, Any] = {
        "layers": _layer_table(drawing, context),
        "linetypes": _linetype_table(drawing, context),
    }
    if context.text_styles:
        tables["text_styles"] = context.text_styles
    if context.blocks:
        tables["blocks"] = context.blocks

    report = drawing.report
    source: dict[str, Any] = {
        "format": "dracad",
        "version": str(drawing.marker),
        "metadata": {
            "dracad": {
                "parser": "ezdracad",
                "parser_version": parser_version,
                "container": str(drawing.format),
                "marker": int(drawing.marker),
                "marker_observed": bool(drawing.marker_observed),
                "complete": not bool(report.lossy),
                "decoded_counts": dict(sorted(report.decoded_counts.items())),
                "unsupported_kinds": {
                    str(kind): int(count)
                    for kind, count in sorted(report.unsupported_counts.items())
                },
                "failed_kinds": {
                    str(kind): int(count)
                    for kind, count in sorted(report.failed_counts.items())
                },
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
        "metadata": {"dracad": header_metadata},
    }
    if abs(scale - 1.0) > _EPSILON:
        # Line-type patterns are millimetres on the output sheet.
        header["linetype_scale"] = scale
    ir_document: dict[str, Any] = {
        "format": "cad2d-ir",
        "version": import_options.ir_version,
        "header": header,
        "source": source,
        "tables": tables,
        "entities": entities,
    }
    if layouts:
        ir_document["layouts"] = layouts

    _append_summary_diagnostics(drawing, context, paper)
    if import_options.validate:
        validate_ir(ir_document)

    block_entity_count = sum(
        len(block["entities"]) for block in context.blocks.values()
    )
    statistics: dict[str, Any] = {
        "source_format": "dracad",
        "container": str(drawing.format),
        "marker": int(drawing.marker),
        "read_complete": not bool(report.lossy),
        "source_entity_counts": dict(sorted(report.decoded_counts.items())),
        "source_unsupported_entities": int(sum(report.unsupported_counts.values())),
        "source_block_definitions": len(drawing.blocks),
        "converted_entities": len(entities),
        "converted_block_entities": block_entity_count,
        "converted_entity_counts": {
            kind: count
            for kind, count in sorted(context.converted_counts.items())
            if count > 0
        },
        "skipped_entities": sum(context.skipped_counts.values()),
        "skipped_entity_counts": dict(sorted(context.skipped_counts.items())),
        "approximated_entities": context.counts["curve_approximated"],
    }
    return ImportResult(
        document=ir_document, diagnostics=context.diagnostics, statistics=statistics
    )


# ---------------------------------------------------------------------------
# Tables


def _build_tables(drawing: Any, context: _ConversionContext) -> None:
    context.palette = {
        int(color.index): (int(color.rgb[0]), int(color.rgb[1]), int(color.rgb[2]))
        for color in drawing.colors
    }
    context.widths = {int(width.index): float(width.width) for width in drawing.widths}
    context.pen_widths = {
        int(color.index): float(color.pen_width) for color in drawing.colors
    }
    context.width_by_color = bool(getattr(drawing.settings, "width_by_color", False))
    for line_type in drawing.line_types:
        index = int(line_type.index)
        context.linetype_names[index] = (
            f"DRACAD_LT{index:02d}" if line_type.pattern else CONTINUOUS
        )
    names = Counter(str(layer.name).strip() for layer in drawing.layers if layer.name)
    for layer in drawing.layers:
        index = int(layer.index)
        name = str(layer.name).strip()
        if not name:
            # DRA-CAD itself names a layer without a name by its number.
            name = f"{index:03d}"
        elif names[name] > 1:
            name = f"{name}_{index:03d}"
        context.layer_names[index] = name
    used: set[str] = set()
    for block in drawing.blocks:
        key = bytes(block.name_bytes)
        if key in context.block_names:
            continue
        name = str(block.name) or f"DRACAD_BLOCK_{int(block.index)}"
        if name in used:
            name = f"{name}_{int(block.index)}"
        used.add(name)
        context.block_names[key] = name


def _layer_table(drawing: Any, context: _ConversionContext) -> dict[str, Any]:
    table: dict[str, Any] = {}
    for layer in drawing.layers:
        index = int(layer.index)
        if not layer.name and index not in context.used_layers:
            continue
        color_index, line_type, width = (
            int(layer.color),
            int(layer.line_type),
            int(layer.width),
        )
        if width == 0:
            # Files of the oldest applications leave the layer attributes unset;
            # DRA-CAD reads such a layer as colour 7, line type 1 and width 1.
            color_index, line_type, width = 7, 1, 1
        definition: dict[str, Any] = {
            "color": _line_color(color_index, context) or 7,
            "visible": not bool(layer.hidden),
            "plot": not int(layer.flags) & 0x0800,
            "metadata": {"dracad": {"index": index, "flags": int(layer.flags)}},
        }
        linetype_name = context.linetype_names.get(line_type)
        if linetype_name is not None:
            definition["linetype"] = linetype_name
            context.used_linetypes.add(line_type)
        lineweight = (
            context.pen_widths.get(color_index)
            if context.width_by_color
            else context.widths.get(width)
        )
        if lineweight is not None and lineweight > 0:
            definition["lineweight_mm"] = round(lineweight, 4)
        table[context.layer_names[index]] = definition
    for index in sorted(context.used_layers):
        # A layer number outside the table of the file.
        name = _layer_name(index, context)
        table.setdefault(name, {"metadata": {"dracad": {"index": index}}})
    return table


def _linetype_table(drawing: Any, context: _ConversionContext) -> dict[str, Any]:
    table: dict[str, Any] = {
        CONTINUOUS: {"description": "Continuous line", "pattern_mm": []}
    }
    for line_type in drawing.line_types:
        index = int(line_type.index)
        if index not in context.used_linetypes or not line_type.pattern:
            continue
        # Dash and gap lengths alternate, starting with a dash.
        table[context.linetype_names[index]] = {
            "description": f"DRA-CAD line type {index}",
            "pattern_mm": [
                float(value) if position % 2 == 0 else -float(value)
                for position, value in enumerate(line_type.pattern)
            ],
        }
    return table


def _paper(settings: Any, scale: float) -> dict[str, Any] | None:
    if settings is None:
        return None
    width, height = (float(value) for value in settings.paper_size)
    if not (width > 0 and height > 0):
        return None
    # The saved corner is the upper left one, in drawing coordinates.
    left, top = (float(value) for value in settings.paper_origin)
    return {
        "width_mm": width,
        "height_mm": height,
        "extent": [left, top - height * scale, left + width * scale, top],
    }


def _layer_name(index: int, context: _ConversionContext) -> str:
    context.used_layers.add(index)
    name = context.layer_names.get(index)
    if name is None:
        name = context.layer_names[index] = f"{index:03d}"
    return name


def _line_color(index: int, context: _ConversionContext) -> int | str | None:
    """IR colour of a line or text with a palette colour; ``None`` when unknown."""
    rgb = context.palette.get(index)
    if rgb is None:
        return None
    if rgb in ((255, 255, 255), (0, 0, 0)):
        # DRA-CAD draws the colour of its background in the contrasting colour.
        return 7
    return "#{:02X}{:02X}{:02X}".format(*rgb)


def _fill_color(fill: Any, context: _ConversionContext) -> str:
    index = int(fill["color"])
    if index in (_RGB_FILL, _BACKGROUND_FILL):
        rgb = tuple(int(value) for value in fill["rgb"])
    else:
        rgb = context.palette.get(index, (0, 0, 0))
    color = "#{:02X}{:02X}{:02X}".format(*rgb)
    alpha = 255 - int(fill["transparency"])
    return color if alpha >= 255 else f"{color}{max(alpha, 0):02X}"


# ---------------------------------------------------------------------------
# Scopes and packages


def _walk(entities: Iterable[Any]) -> Iterable[Any]:
    for entity in entities:
        yield entity
        yield from _walk(entity.children)


def _convert_scope(
    entities: Sequence[Any], context: _ConversionContext
) -> list[dict[str, Any]]:
    """Convert the entities of the root table or of one block definition."""
    scope = _Scope()
    regions: dict[int, Any] = {}
    for entity in _walk(entities):
        kind = entity.type
        if kind == "DIMENSION" and entity.link_id:
            scope.dimensions.setdefault(int(entity.link_id), entity)
        elif kind == "PACKAGE":
            number = int(entity.data["reference_number"])
            if int(entity.data["package_kind"]) == _PACKAGE_HATCH:
                scope.drawn_hatches.add(number)
        elif kind == "INSERT":
            if bytes(entity.data["name_bytes"]) in context.block_names:
                scope.placed_references.add(int(entity.data["reference_number"]))
        elif kind == "EXTENDED_POLYGON" and entity.data["reference_number"]:
            regions[int(entity.data["reference_number"])] = entity
    result: list[dict[str, Any]] = []
    # DRA-CAD paints from the end of the table; the IR lists what is painted first.
    for entity in reversed(entities):
        result.extend(_convert_entity_safe(entity, context, scope))
    undrawn = sum(1 for link in scope.dimensions if link not in scope.drawn_dimensions)
    context.counts["dimension_not_drawn"] += undrawn
    context.counts["hatch_not_drawn"] += sum(
        1 for number in regions if number not in scope.drawn_hatches
    )
    return result


def _split_sheets(
    entities: Sequence[Any],
) -> tuple[list[Any], dict[int, dict[str, Any]]]:
    """Separate the model from the sheets.

    An object names its sheet in ``segment``; zero is the model. The layout
    object of a sheet carries its name and paper, a viewport is a window of
    the sheet onto the model.
    """
    model: list[Any] = []
    sheets: dict[int, dict[str, Any]] = {}

    def sheet(number: int) -> dict[str, Any]:
        return sheets.setdefault(
            number, {"layout": None, "entities": [], "viewports": []}
        )

    for entity in entities:
        kind = str(entity.type)
        if kind == "LAYOUT":
            sheet(int(entity.data["number"]))["layout"] = entity
            continue
        number = int(getattr(entity, "segment", 0) or 0)
        if kind == "VIEWPORT":
            if number:
                sheet(number)["viewports"].append(entity)
        elif number:
            sheet(number)["entities"].append(entity)
        else:
            model.append(entity)
    return model, sheets


def _layouts(
    sheets: dict[int, dict[str, Any]], context: _ConversionContext
) -> list[dict[str, Any]]:
    layouts: list[dict[str, Any]] = []
    for number in sorted(sheets):
        sheet = sheets[number]
        # A sheet is drawn in millimetres on its paper: sizes given on the
        # output sheet are already final there.
        model_scale, context.scale = context.scale, 1.0
        try:
            converted = _convert_scope(sheet["entities"], context)
        finally:
            context.scale = model_scale
        viewports = [
            viewport
            for entity in sheet["viewports"]
            if (viewport := _viewport(entity, context)) is not None
        ]
        if not converted and not viewports:
            continue
        definition = sheet["layout"]
        data = definition.data if definition is not None else {}
        layout: dict[str, Any] = {
            "name": str(data.get("name") or "") or f"Layout{number}",
            "tab_order": number,
            "entities": converted,
        }
        if viewports:
            layout["viewports"] = viewports
        size = data.get("paper_size")
        if size is not None and all(
            math.isfinite(float(side)) and float(side) > 0 for side in size
        ):
            paper: dict[str, Any] = {
                "size_mm": [float(size[0]), float(size[1])],
                "units": "mm",
            }
            name = _PAPER_NAMES.get(int(data.get("paper_type", 0)))
            if name and name != "Free":
                paper["name"] = name
            layout["paper"] = paper
        layout["metadata"] = {"dracad": {"number": number}}
        layouts.append(layout)
        context.counts["layouts"] += 1
        context.counts["layout_entities"] += len(converted)
    return layouts


def _viewport(entity: Any, context: _ConversionContext) -> dict[str, Any] | None:
    """A window of a sheet: ``sheet = paper_point + R * (model - model_point) * scale``."""
    data = entity.data
    center, half = data.get("center"), data.get("half_size")
    try:
        scale = float(data["scale"][0])
        if center is None or half is None:
            raise ValueError("the window is not a rectangle")
        cx, cy = _xy(center)
        width = 2.0 * _positive(half[0], "viewport width")
        height = 2.0 * _positive(half[1], "viewport height")
        px, py = _xy(data["paper_point"])
        mx, my = _xy(data["model_point"])
        rotation = float(data["rotation_degrees"])
        if not (math.isfinite(scale) and scale > 0 and math.isfinite(rotation)):
            raise ValueError("the view of the window is not finite")
    except (KeyError, TypeError, ValueError, IndexError):
        context.counts["viewport_skipped"] += 1
        return None
    # The model point shown at the middle of the window.
    dx, dy = (cx - px) / scale, (cy - py) / scale
    angle = math.radians(-rotation)
    cos, sin = math.cos(angle), math.sin(angle)
    viewport: dict[str, Any] = {
        "id": context.allocate_id(),
        "center": [cx, cy],
        "width": width,
        "height": height,
        "view_center": [mx + dx * cos - dy * sin, my + dx * sin + dy * cos],
        "view_height": height / scale,
        "layer": _layer_name(int(entity.layer), context),
    }
    if abs(rotation) > _EPSILON:
        viewport["rotation"] = rotation
    if context.options.entity_provenance:
        viewport["source"] = {"format": "dracad", "id": _source_id(entity)}
        viewport["metadata"] = {
            "dracad": {
                "scale": [float(value) for value in data["scale"]],
                "flags": int(data.get("flags", 0)),
            }
        }
    context.counts["viewports"] += 1
    return viewport


def _convert_entity_safe(
    entity: Any, context: _ConversionContext, scope: _Scope
) -> list[dict[str, Any]]:
    try:
        return _convert_entity(entity, context, scope)
    except (AttributeError, KeyError, TypeError, ValueError) as exc:
        if context.options.strict:
            raise ImporterError(
                f"Failed to convert DRA-CAD {entity.type} at record "
                f"{entity.record_index}: {exc}"
            ) from exc
        context.skipped_counts[str(entity.type)] += 1
        context.diagnostics.append(
            ImportDiagnostic(
                code="DRACAD_ENTITY_CONVERSION_FAILED",
                severity="error",
                message=f"Failed to convert DRA-CAD {entity.type}: {exc}",
                source_id=_source_id(entity),
                source_kind=str(entity.type),
                action="skipped",
            )
        )
        return []


def _convert_entity(
    entity: Any, context: _ConversionContext, scope: _Scope
) -> list[dict[str, Any]]:
    kind = str(entity.type)
    data = entity.data
    if kind == "LINE":
        return [
            context.add_converted(
                {
                    **_common(entity, context),
                    "kind": "LINE",
                    "p1": _xy(data["start"]),
                    "p2": _xy(data["end"]),
                }
            )
        ]
    if kind == "LINE3D":
        context.counts["projected_3d"] += 1
        return [
            context.add_converted(
                {
                    **_common(entity, context),
                    "kind": "LINE",
                    "p1": _xy(data["start"]),
                    "p2": _xy(data["end"]),
                }
            )
        ]
    if kind == "POINT":
        point = {
            **_common(entity, context),
            "kind": "POINT",
            "position": _xy(data["position"]),
        }
        _metadata(point)["point_type"] = int(data["point_type"])
        return [context.add_converted(point)]
    if kind == "CIRCLE":
        return _circle(entity, context)
    if kind == "ARC":
        return [
            context.add_converted({**_common(entity, context), **_arc_fields(data)})
        ]
    if kind in ("ELLIPSE", "ELLIPTICAL_ARC"):
        return _ellipse(entity, context)
    if kind == "TEXT":
        return _text(entity, context)
    if kind == "POLYLINE":
        return _polyline(entity, context)
    if kind == "EXTENDED_POLYGON":
        return _region(entity, context)
    if kind == "ARROWHEAD":
        return _arrowhead(entity, context)
    if kind == "INSERT":
        return _insert(entity, context)
    if kind == "IMAGE":
        return _image(entity, context)
    if kind == "PACKAGE":
        return _package(entity, context, scope)
    if kind in ("DIMENSION", "LEADER", "HATCH"):
        # Drawn by the package that follows the definition.
        context.counts[f"{kind.lower()}_definitions"] += 1
        return []
    if kind == "POLYGON_3D":
        # A face in space with holes: the edges of its boundary and of its holes.
        result: list[dict[str, Any]] = []
        for child in entity.children:
            if child.type == "POLYLINE":
                result.extend(_polyline(child, context))
        return result
    if kind in ("LAYOUT", "VIEWPORT"):
        # Sheets are split off before conversion; inside a block they mean nothing.
        context.counts["stray_sheet_objects"] += 1
        return []
    if kind in ("CAMERA", "LIGHT"):
        # Viewpoints and light sources for perspective views; no part of the drawing.
        context.counts["view_objects"] += 1
        return []
    if kind == "EXTERNAL_REFERENCE":
        name = str(data["file_name"]) or "?"
        context.external_references.append(name)
        context.skipped_counts[kind] += 1
        return []
    context.skipped_counts[kind] += 1
    context.counts["unmapped"] += 1
    return []


def _package(
    entity: Any, context: _ConversionContext, scope: _Scope
) -> list[dict[str, Any]]:
    package_kind = int(entity.data["package_kind"])
    number = int(entity.data["reference_number"])
    if package_kind == _PACKAGE_REFERENCE and number in scope.placed_references:
        # The placed members of a block reference that is converted as an INSERT.
        context.counts["reference_packages"] += 1
        return []
    if package_kind == _PACKAGE_DIMENSION:
        dimension = next(
            (
                scope.dimensions[int(member.link_id)]
                for member in entity.children
                if member.link_id
                and int(member.link_id) in scope.dimensions
                and int(member.link_id) not in scope.drawn_dimensions
            ),
            None,
        )
        if dimension is not None:
            scope.drawn_dimensions.add(int(dimension.link_id))
            return [_dimension(dimension, entity, context, scope)]
    if package_kind == _PACKAGE_HATCH:
        context.counts["hatch_from_package"] += 1
    result: list[dict[str, Any]] = []
    for member in reversed(entity.children):
        result.extend(_convert_entity_safe(member, context, scope))
    return result


def _dimension(
    dimension: Any, package: Any, context: _ConversionContext, scope: _Scope
) -> dict[str, Any]:
    """A dimension with the geometry of the package DRA-CAD drew for it."""
    data = dimension.data
    block_entities: list[dict[str, Any]] = []
    for member in reversed(package.children):
        if member.type == "POINT":
            # The origins of the extension lines: DRA-CAD shows no mark for them.
            continue
        block_entities.extend(_convert_entity_safe(member, context, scope))
    for converted in block_entities:
        context.converted_counts[str(converted["kind"])] -= 1
    text = dimension.children[0].data if dimension.children else None
    points: dict[str, Any] = {}
    for key, origin in zip(("p1", "p2"), data["extension_origins"], strict=False):
        points[key] = _xy(origin)
    if data["line"]:
        points["location"] = _xy(data["line"][1])
    if text is not None:
        points["text_midpoint"] = _xy(text["position"])
    definition: dict[str, Any] = {
        "points": points,
        "text": str(text["text"]) if text is not None else "",
        "source_geometry": {
            "dimension_type": int(data["dimension_type"]),
            "flags": int(data["flags"]),
            "flags2": int(data["flags2"]),
            "arrow_styles": [int(value) for value in data["arrow_styles"]],
            "text_position": int(data["text_position"]),
            "line": [_xy(point) for point in data["line"]] if data["line"] else None,
            "arc": dict(data["arc"]) if data["arc"] else None,
        },
    }
    if text is not None and text["number"] is not None:
        definition["measurement"] = float(text["number"])
    common = _common(dimension, context)
    if block_entities:
        name = f"*DRA_D{common['id'][5:]}"
        context.blocks[name] = {
            "base_point": [0.0, 0.0],
            "entities": block_entities,
            "metadata": {"dracad": {"dimension_presentation": True}},
        }
        definition["block"] = name
    context.counts["dimension_from_package"] += 1
    return context.add_converted(
        {
            **common,
            "kind": "DIMENSION",
            "dim_kind": DIMENSION_KINDS.get(int(data["dimension_type"]), "GENERIC"),
            "definition": definition,
        }
    )


# ---------------------------------------------------------------------------
# Entities


def _common(entity: Any, context: _ConversionContext) -> dict[str, Any]:
    result: dict[str, Any] = {
        "id": context.allocate_id(),
        "layer": _layer_name(int(entity.layer), context),
    }
    color = int(entity.color)
    if color == _BY_BLOCK_COLOR:
        result["color"] = 0
    elif color != _BY_LAYER_COLOR:
        resolved = _line_color(color, context)
        if resolved is not None:
            result["color"] = resolved
    line_type = int(entity.line_type)
    if line_type < _BY_LAYER_LINE_TYPE:
        name = context.linetype_names.get(line_type)
        if name is not None:
            result["linetype"] = name
            context.used_linetypes.add(line_type)
    width = int(entity.width)
    if context.width_by_color:
        # The drawing prints every colour with a pen width of its own; an
        # object in the colour of its layer takes the width of the layer.
        lineweight = context.pen_widths.get(color)
        if lineweight is not None and lineweight > 0:
            result["lineweight_mm"] = round(lineweight, 4)
    elif width < _BY_LAYER_WIDTH:
        lineweight = context.widths.get(width)
        if lineweight is not None and lineweight > 0:
            result["lineweight_mm"] = round(lineweight, 4)
    if context.options.entity_provenance:
        result["source"] = {
            "format": "dracad",
            "id": _source_id(entity),
            "kind": str(entity.type),
        }
        result["metadata"] = {
            "dracad": {
                "layer": int(entity.layer),
                "color": color,
                "line_type": line_type,
                "width": width,
                "flags": int(entity.flags),
                "group": int(entity.group),
                "link_id": int(entity.link_id),
            }
        }
    return result


def _derived(common: dict[str, Any], context: _ConversionContext) -> dict[str, Any]:
    """The attributes of ``common`` for one more entity of the same object."""
    return {**common, "id": context.allocate_id()}


def _metadata(entity: dict[str, Any]) -> dict[str, Any]:
    return entity.setdefault("metadata", {}).setdefault("dracad", {})


def _source_id(entity: Any) -> str:
    table = str(entity.table)
    if entity.block_index is not None:
        table = f"{table}{int(entity.block_index)}"
    return f"{table}:{int(entity.record_index)}"


def _fill_hatch(
    fill: Any,
    loops: list[dict[str, Any]],
    common: dict[str, Any],
    context: _ConversionContext,
) -> dict[str, Any]:
    """The fill of a shape; drawn before its outline."""
    hatch: dict[str, Any] = {
        "id": context.allocate_id(),
        "kind": "HATCH",
        "layer": common["layer"],
        "color": _fill_color(fill, context),
        "solid": True,
        "loops": loops,
        # Kept without provenance too: a consumer may treat a paint fill unlike
        # a line colour (a white fill hides what is behind it).
        "metadata": {"dracad": {"fill": True}},
    }
    if "source" in common:
        hatch["source"] = common["source"]
    context.counts["fills"] += 1
    return context.add_converted(hatch)


def _draws_outline(fill: Any) -> bool:
    return fill is None or bool(fill["outline"])


def _circle(entity: Any, context: _ConversionContext) -> list[dict[str, Any]]:
    data = entity.data
    center = _xy(data["center"])
    radius = _positive(data["radius"], "circle radius")
    common = _common(entity, context)
    result: list[dict[str, Any]] = []
    fill = data["fill"]
    if fill is not None:
        result.append(
            _fill_hatch(fill, [_circle_loop(center, radius)], common, context)
        )
    if _draws_outline(fill):
        outline = common if not result else _derived(common, context)
        result.append(
            context.add_converted(
                {**outline, "kind": "CIRCLE", "center": center, "radius": radius}
            )
        )
    return result


def _circle_loop(center: list[float], radius: float) -> dict[str, Any]:
    cx, cy = center
    return {
        "vertices": [
            [cx + radius, cy, _QUARTER_BULGE],
            [cx, cy + radius, _QUARTER_BULGE],
            [cx - radius, cy, _QUARTER_BULGE],
            [cx, cy - radius, _QUARTER_BULGE],
        ],
        "is_outer": True,
    }


def _arc_fields(data: Any) -> dict[str, Any]:
    return {
        "kind": "ARC",
        "center": _xy(data["center"]),
        "radius": _positive(data["radius"], "arc radius"),
        "start_angle": float(data["start_degrees"]) % 360.0,
        "end_angle": float(data["end_degrees"]) % 360.0,
        "ccw": True,
    }


def _ellipse(entity: Any, context: _ConversionContext) -> list[dict[str, Any]]:
    data = entity.data
    center = _xy(data["center"])
    major = _positive(data["radius"][0], "ellipse radius")
    minor = _positive(data["radius"][1], "ellipse radius")
    rotation = math.radians(float(data["rotation_degrees"]))
    partial = entity.type == "ELLIPTICAL_ARC"
    start = math.radians(float(data["start_degrees"])) if partial else 0.0
    end = math.radians(float(data["end_degrees"])) if partial else _TAU
    if major < minor:
        # IR wants the longer axis first; the parameter moves with the axis.
        major, minor = minor, major
        rotation += math.pi / 2.0
        start -= math.pi / 2.0
        end -= math.pi / 2.0
    if partial and abs(end - start) < _EPSILON:
        end = start + _TAU
    axis = [major * math.cos(rotation), major * math.sin(rotation)]
    ratio = min(minor / major, 1.0)
    common = _common(entity, context)
    result: list[dict[str, Any]] = []
    fill = None if partial else data["fill"]
    if fill is not None:
        segments = max(16, min(context.options.curve_segments, 256))
        vertices = []
        for index in range(segments):
            angle = _TAU * index / segments
            x, y = major * math.cos(angle), minor * math.sin(angle)
            vertices.append(
                [
                    center[0] + x * math.cos(rotation) - y * math.sin(rotation),
                    center[1] + x * math.sin(rotation) + y * math.cos(rotation),
                ]
            )
        loop = {"vertices": vertices, "is_outer": True}
        result.append(_fill_hatch(fill, [loop], common, context))
    if _draws_outline(fill):
        outline = common if not result else _derived(common, context)
        result.append(
            context.add_converted(
                {
                    **outline,
                    "kind": "ELLIPSE",
                    "center": center,
                    "major_axis": axis,
                    "ratio": ratio,
                    "start_param": start,
                    "end_param": end,
                }
            )
        )
    return result


def _is_half_width(character: str) -> bool:
    """One byte in Shift_JIS: ASCII and half-width katakana."""
    code = ord(character)
    return code < 0x80 or 0xFF61 <= code <= 0xFF9F


def _property_set(drawing: Any) -> dict[str, str]:
    reader = getattr(drawing, "property_set", None)
    return dict(reader()) if callable(reader) else {}


def _number_label(value: float) -> str:
    """A number as DRA-CAD prints it in a scale: no trailing zeros."""
    return f"{value:g}"


def _field_value(name: str, context: _ConversionContext) -> str | None:
    """What the drawing says a ``TCODE`` field prints; ``None`` for an unknown name."""
    settings = context.drawing.settings
    if name in ("DrawingName", "FileName"):
        if context.source_name is None:
            context.blank_fields.add(name)
            return ""
        if name == "FileName":
            return context.source_name
        return Path(context.source_name).stem
    if name in _OUTSIDE_FIELDS:
        context.blank_fields.add(name)
        return ""
    if name == "Comment":
        return str(getattr(settings, "comment", "") or "")
    if name == "PaperSizeName":
        return _PAPER_NAMES.get(int(getattr(settings, "paper_type", -1)), "")
    if name == "PaperOrientation":
        return "縦" if getattr(settings, "paper_portrait", False) else "横"
    if name in ("PaperScale", "DrawingScale"):
        scale = context.scale
        if name == "DrawingScale":
            ratio = float(getattr(settings, "scale_ratio", 1.0) or 1.0)
            if math.isfinite(ratio) and ratio > 0:
                scale /= ratio
        return f"1:{_number_label(scale)}"
    return None


def _cut(value: str, rule: str) -> str:
    match = _FIELD_CUT.match(rule)
    if match is None:
        return value
    side, first, count = match.group(1), int(match.group(2)), match.group(3)
    if side == "L":
        return value[:first]
    if side == "R":
        return value[-first:] if first else ""
    start = max(first - 1, 0)
    return value[start:] if count is None else value[start : start + int(count)]


def _fill_fields(contents: str, context: _ConversionContext) -> str:
    """Replace the fields of a text by what DRA-CAD draws in their place."""

    def replace(match: re.Match[str]) -> str:
        kind, body = match.group(1), match.group(2)
        context.counts["text_fields"] += 1
        if kind == "PSET":
            value = context.property_set.get(body)
            return value if value is not None else f"{{ERR:{body}}}"
        parts = _FIELD_NAME.match(body)
        value = _field_value(parts.group(1), context) if parts is not None else None
        if parts is None or value is None:
            # DRA-CAD prints this for a name it does not know.
            return f"{{ERR:{body}}}"
        return _cut(value, parts.group(2)) if parts.group(2) else value

    return _FIELD.sub(replace, contents)


def _text(entity: Any, context: _ConversionContext) -> list[dict[str, Any]]:
    data = entity.data
    stored = str(data["text"])
    contents = _fill_fields(stored, context) if "{" in stored else stored
    if not contents.strip():
        context.skipped_counts["TEXT"] += 1
        context.counts["empty_text" if contents == stored else "blank_field_text"] += 1
        return []
    # Sizes given on the output sheet grow with the drawing scale.
    factor = context.scale if data.get("output_size") else 1.0
    height = float(data["height"]) * factor
    if not (math.isfinite(height) and height > 0):
        raise ValueError("text height must be positive")
    advance = float(data["width"]) * factor
    width_factor: float | None = None
    characters = [character for character in contents if character != "\n"]
    if advance > 0 and characters:
        # Every character advances by the stored width; a cell is as wide as the
        # text is high for a full-width character and half of that otherwise.
        natural = sum(
            height / 2.0 if _is_half_width(character) else height
            for character in characters
        )
        candidate = advance * len(characters) / natural
        if 0.05 <= candidate <= 20.0 and abs(candidate - 1.0) > 1e-6:
            width_factor = round(candidate, 6)
    origin = int(data["origin"])
    halign = ("left", "center", "right", "left")[origin & 3]
    valign = ("bottom", "middle", "top", "bottom")[(origin >> 2) & 3]
    fields: dict[str, Any] = {
        **_common(entity, context),
        "insert": _xy(data["position"]),
        "height": height,
        "rotation": float(data["rotation_degrees"]),
    }
    font = str(data["font_name"]).strip()
    if font.startswith("@"):
        # A font for vertical writing; the text is laid out horizontally.
        font = font[1:]
        context.counts["text_vertical"] += 1
    if font and "\ufffd" not in font:
        context.text_styles.setdefault(font, {"font": font})
        fields["style"] = font
    if data["format_codes"]:
        context.counts["text_format_codes"] += 1
    if "\n" in contents:
        text = "\\P".join(_mtext_escape(line) for line in contents.split("\n"))
        if width_factor is not None:
            text = f"\\W{width_factor:g};{text}"
        fields.update(kind="MTEXT", text=text, attach=f"{valign}_{halign}")
    else:
        fields.update(kind="TEXT", text=contents, halign=halign, valign=valign)
        if width_factor is not None:
            fields["width_factor"] = width_factor
        slant = float(data["slant_degrees"])
        if math.isfinite(slant) and abs(slant) > _EPSILON:
            fields["oblique_deg"] = slant
    if context.options.entity_provenance:
        _metadata(fields).update(
            {
                "height": float(data["height"]),
                "width": float(data["width"]),
                "spacing": float(data["spacing"]),
                "output_size": bool(data.get("output_size")),
                "origin": origin,
                "numeric": bool(data["numeric"]),
            }
        )
        if contents != stored:
            _metadata(fields)["stored_text"] = stored
    return [context.add_converted(fields)]


def _mtext_escape(line: str) -> str:
    return line.replace("\\", "\\\\").replace("{", "\\{").replace("}", "\\}")


def _point_on_arc(member: Any, degrees: float) -> list[float]:
    radius = float(member.data["radius"])
    angle = math.radians(degrees)
    return [
        float(member.data["center"][0]) + radius * math.cos(angle),
        float(member.data["center"][1]) + radius * math.sin(angle),
    ]


def _near(first: Sequence[float], second: Sequence[float]) -> bool:
    tolerance = 1e-6 * max(1.0, abs(first[0]), abs(first[1]))
    return (
        abs(first[0] - second[0]) <= tolerance
        and abs(first[1] - second[1]) <= tolerance
    )


def _chain(members: Sequence[Any]) -> list[list[float]] | None:
    """Vertices of a polyline whose members follow each other, with bulges.

    A member arc stores its angles counter-clockwise; the polyline follows a
    reversed one from its end angle to its start angle. ``None`` when the
    members do not form one path.
    """
    segments: list[tuple[list[float], list[float], float]] = []
    for member in members:
        if member.type in ("LINE", "LINE3D"):
            segments.append((_xy(member.data["start"]), _xy(member.data["end"]), 0.0))
        elif member.type == "ARC":
            start, end = (
                float(member.data["start_degrees"]),
                float(member.data["end_degrees"]),
            )
            sweep = (end - start) % 360.0 or 360.0
            bulge = math.tan(math.radians(sweep) / 4.0)
            first, last = _point_on_arc(member, start), _point_on_arc(member, end)
            if member.reversed:
                first, last, bulge = last, first, -bulge
            segments.append((first, last, bulge))
        else:
            return None
    if not segments:
        return None
    if len(segments) > 1 and not (
        _near(segments[0][1], segments[1][0]) or _near(segments[0][1], segments[1][1])
    ):
        first, last, bulge = segments[0]
        segments[0] = (last, first, -bulge)
    vertices: list[list[float]] = []
    current: list[float] | None = None
    for first, last, bulge in segments:
        if current is not None and not _near(current, first):
            if not _near(current, last):
                return None
            first, last, bulge = last, first, -bulge
        vertices.append([*first, bulge] if bulge else first)
        current = last
    assert current is not None
    vertices.append(current)
    return vertices


def _close(vertices: list[list[float]]) -> list[list[float]]:
    """Without the closing vertex that repeats the first one."""
    if len(vertices) > 2 and _near(vertices[0], vertices[-1]):
        return vertices[:-1]
    return vertices


def _loop(vertices: list[list[float]], *, outer: bool) -> dict[str, Any] | None:
    """A hatch loop; one of two arcs is sampled, since a loop needs three vertices."""
    closed = _close(vertices)
    if len(closed) < 3:
        closed = _flatten(closed, closed=True)
    if len(closed) < 3:
        return None
    return {"vertices": closed, "is_outer": outer}


def _flatten(
    vertices: list[list[float]], *, closed: bool, steps: int = 16
) -> list[list[float]]:
    points: list[list[float]] = []
    count = len(vertices)
    for index, vertex in enumerate(vertices):
        points.append([vertex[0], vertex[1]])
        bulge = vertex[2] if len(vertex) > 2 else 0.0
        if not bulge or (index == count - 1 and not closed):
            continue
        target = vertices[(index + 1) % count]
        chord = math.hypot(target[0] - vertex[0], target[1] - vertex[1])
        if chord <= _EPSILON:
            continue
        angle = 4.0 * math.atan(bulge)
        radius = chord / (2.0 * math.sin(angle / 2.0))
        middle = [(vertex[0] + target[0]) / 2.0, (vertex[1] + target[1]) / 2.0]
        offset = radius * math.cos(angle / 2.0)
        center = [
            middle[0] - offset * (target[1] - vertex[1]) / chord,
            middle[1] + offset * (target[0] - vertex[0]) / chord,
        ]
        start = math.atan2(vertex[1] - center[1], vertex[0] - center[0])
        for step in range(1, steps):
            at = start + angle * step / steps
            points.append(
                [
                    center[0] + abs(radius) * math.cos(at),
                    center[1] + abs(radius) * math.sin(at),
                ]
            )
    return points


def _polyline_vertices(entity: Any) -> tuple[list[list[float]] | None, bool]:
    """Vertices of a polyline or of the curve it stands for, and whether it closes."""
    data = entity.data
    closed = bool(data["closed"])
    if data["curve_points"] is not None:
        return [_xy(point) for point in data["curve_points"]], closed
    if int(data["curve_kind"]) != 0:
        return None, closed
    return _chain(entity.children), closed


def _polyline(entity: Any, context: _ConversionContext) -> list[dict[str, Any]]:
    data = entity.data
    curve_kind = int(data["curve_kind"])
    if curve_kind == 1:
        # The saved evaluation of the curve before it, which is converted itself.
        context.counts["saved_curve_evaluations"] += 1
        return []
    vertices, closed = _polyline_vertices(entity)
    if vertices is None or len(vertices) < 2:
        # Members that are not one path, or a curve that could not be evaluated:
        # each member is drawn on its own.
        context.counts["polyline_exploded"] += 1
        result: list[dict[str, Any]] = []
        for member in entity.children:
            if member.type in ("LINE", "LINE3D"):
                item = {
                    "kind": "LINE",
                    "p1": _xy(member.data["start"]),
                    "p2": _xy(member.data["end"]),
                }
            elif member.type == "ARC":
                item = _arc_fields(member.data)
            else:
                continue
            result.append(context.add_converted({**_common(member, context), **item}))
        return result
    common = _common(entity, context)
    result = []
    fill = data["fill"]
    if data.get("three_d"):
        # The fill of a face in space is its surface for shaded views; seen
        # from above the faces of a model would cover each other.
        context.counts["projected_3d"] += 1
        fill = None
    if fill is not None:
        loop = _loop(vertices, outer=True)
        if loop is not None:
            result.append(_fill_hatch(fill, [loop], common, context))
    if _draws_outline(fill):
        outline = common if not result else _derived(common, context)
        polyline: dict[str, Any] = {
            **outline,
            "kind": "LWPOLYLINE",
            "vertices": _close(vertices) if closed else vertices,
            "closed": closed,
        }
        if curve_kind != 0:
            context.counts["curve_approximated"] += 1
            polyline["approximation"] = {
                "method": "polyline",
                "source_kind": {
                    2: "QUADRATIC_SPLINE",
                    3: "CUBIC_SPLINE",
                    4: "BEZIER",
                }.get(curve_kind, "CURVE"),
                "segments": max(len(vertices) - 1, 1),
            }
        if len(polyline["vertices"]) >= 2:
            result.append(context.add_converted(polyline))
    return result


def _region(entity: Any, context: _ConversionContext) -> list[dict[str, Any]]:
    """A region: its fill, and its boundary unless a hatch package draws it."""
    data = entity.data
    boundaries: list[tuple[Any, list[list[float]]]] = []
    for child in entity.children:
        if child.type != "POLYLINE":
            continue
        vertices, _ = _polyline_vertices(child)
        if vertices is not None and len(vertices) >= 2:
            boundaries.append((child, vertices))
    common = _common(entity, context)
    result: list[dict[str, Any]] = []
    fill = data["fill"]
    if fill is not None:
        loops = [
            loop
            for index, (_, vertices) in enumerate(boundaries)
            if (loop := _loop(vertices, outer=index == 0)) is not None
        ]
        if loops:
            result.append(_fill_hatch(fill, loops, common, context))
    hatched = int(data["reference_number"]) != 0
    hides_boundary = bool(int(data["flags"]) & 0x02)
    if not hatched and not hides_boundary and _draws_outline(fill):
        for child, vertices in boundaries:
            result.append(
                context.add_converted(
                    {
                        **_common(child, context),
                        "kind": "LWPOLYLINE",
                        "vertices": _close(vertices),
                        "closed": True,
                    }
                )
            )
    for child in entity.children:
        if child.type != "POLYLINE":
            # The label of a named area.
            result.extend(_convert_entity_safe(child, context, _Scope()))
    context.counts["regions"] += 1
    return result


def _arrowhead(entity: Any, context: _ConversionContext) -> list[dict[str, Any]]:
    """A terminator: its tip is the position, and it points along its angle."""
    data = entity.data
    arrow_type = int(data["arrow_type"])
    factor = context.scale if data.get("output_size") else 1.0
    width, length = (float(value) * factor for value in data["size"])
    if arrow_type == 0 or not (width > 0 or length > 0):
        context.counts["arrowhead_none"] += 1
        return []
    px, py = _xy(data["position"])
    degrees = float(data["angle_degrees"])
    angle = math.radians(degrees)
    ux, uy = math.cos(angle), math.sin(angle)
    # A one-sided head has its barb on the left of its direction, and on the
    # right when it points anywhere from straight up over left to straight
    # down. On a horizontal line that puts both of its heads above the line.
    turn = degrees % 360.0
    side = -1.0 if 90.0 - 1e-6 <= turn <= 270.0 + 1e-6 else 1.0

    def local(along: float, across: float) -> list[float]:
        return [px + ux * along - uy * across, py + uy * along + ux * across]

    common = _common(entity, context)
    filled = arrow_type in _ARROW_FILLED
    half = width / 2.0
    shape: dict[str, Any]
    outline: list[list[float]] | None = None
    if arrow_type in _ARROW_CIRCLES:
        if not half > 0:
            return []
        if filled:
            shape = {
                "kind": "HATCH",
                "solid": True,
                "loops": [_circle_loop([px, py], half)],
            }
        else:
            shape = {"kind": "CIRCLE", "center": [px, py], "radius": half}
    elif arrow_type in _ARROW_SQUARES:
        outline = [
            local(-half, -half),
            local(half, -half),
            local(half, half),
            local(-half, half),
        ]
    elif arrow_type == 4:
        shape = {
            "kind": "LWPOLYLINE",
            "vertices": [local(-length, half), [px, py], local(-length, -half)],
            "closed": False,
        }
    elif arrow_type in (5, 6, 7):
        outline = [[px, py], local(-length, half), local(-length, -half)]
    elif arrow_type == 11:
        shape = {"kind": "LINE", "p1": local(-half, -half), "p2": local(half, half)}
    elif arrow_type == 12:
        shape = {
            "kind": "LWPOLYLINE",
            "vertices": [
                local(-0.3 * width, -0.5 * width),
                local(-0.05 * width, -0.45 * width),
                local(0.0, -0.25 * width),
                local(0.0, 0.25 * width),
                local(0.05 * width, 0.45 * width),
                local(0.3 * width, 0.5 * width),
            ],
            "closed": False,
        }
    elif arrow_type == 13:
        shape = {"kind": "LINE", "p1": [px, py], "p2": local(-length, side * width)}
    elif arrow_type in (14, 15):
        outline = [[px, py], local(-length, side * width), local(-length, 0.0)]
    else:
        context.counts["arrowhead_unknown"] += 1
        return []
    if outline is not None:
        if filled:
            shape = {
                "kind": "HATCH",
                "solid": True,
                "loops": [{"vertices": outline, "is_outer": True}],
            }
        else:
            shape = {"kind": "LWPOLYLINE", "vertices": outline, "closed": True}
    common.pop("linetype", None)
    return [context.add_converted({**common, **shape})]


def _insert(entity: Any, context: _ConversionContext) -> list[dict[str, Any]]:
    data = entity.data
    name = context.block_names.get(bytes(data["name_bytes"]))
    if name is None:
        context.undefined_blocks.add(str(data["name"]))
        context.skipped_counts["INSERT"] += 1
        return []
    scale_x, scale_y = (float(value) for value in data["scale"])
    if abs(scale_x) <= _EPSILON or abs(scale_y) <= _EPSILON:
        raise ValueError("block reference scale must be non-zero")
    result: dict[str, Any] = {
        **_common(entity, context),
        "kind": "INSERT",
        "block": name,
        "insert": _xy(data["position"]),
        "rotation": float(data["rotation_degrees"]),
    }
    if not (math.isclose(scale_x, 1.0) and math.isclose(scale_y, 1.0)):
        result["scale"] = (
            scale_x if math.isclose(scale_x, scale_y) else [scale_x, scale_y]
        )
    return [context.add_converted(result)]


def _image(entity: Any, context: _ConversionContext) -> list[dict[str, Any]]:
    data = entity.data
    width, height = (float(value) for value in data["size"])
    if not (width > 0 and height > 0):
        raise ValueError("image size must be positive")
    name = str(data["file_name"])
    result: dict[str, Any] = {
        **_common(entity, context),
        "kind": "IMAGE",
        # The lower left corner and the drawn size, in drawing units.
        "insert": _xy(data["position"]),
        "width": width,
        "height": height,
        "rotation": float(data["rotation_degrees"]),
    }
    if name:
        result["name"] = name
        result["href"] = f"{data['directory']}{name}"
    context.counts["images"] += 1
    return [context.add_converted(result)]


# ---------------------------------------------------------------------------
# Diagnostics


def _append_summary_diagnostics(
    drawing: Any, context: _ConversionContext, paper: dict[str, Any] | None
) -> None:
    diagnostics = context.diagnostics
    counts = context.counts
    report = drawing.report
    paper_text = (
        f"; the sheet is {paper['width_mm']:g} × {paper['height_mm']:g} mm"
        if paper is not None
        else ""
    )
    diagnostics.append(
        ImportDiagnostic(
            code="DRACAD_UNITS_ASSUMED_MM",
            severity="info",
            message=(
                "A DRA-CAD drawing carries no unit; its coordinates were taken as "
                f"millimetres at real size. The drawing scale is {_scale_label(context.scale)}"
                f"{paper_text} (header.metadata.dracad)."
            ),
            action="assumed",
            details={"paper_scale": context.scale, "paper": paper},
        )
    )
    if not drawing.marker_observed:
        diagnostics.append(
            ImportDiagnostic(
                code="DRACAD_GENERATION_UNVERIFIED",
                severity="warning",
                message=(
                    f"The file was saved in generation {drawing.marker}, which is "
                    "outside the generations this reader was checked against; it was "
                    "read with the same record layouts."
                ),
                action="assumed",
                details={"marker": int(drawing.marker)},
            )
        )
    if report.lossy:
        unsupported = {
            str(k): int(v) for k, v in sorted(report.unsupported_counts.items())
        }
        failed = {str(k): int(v) for k, v in sorted(report.failed_counts.items())}
        diagnostics.append(
            ImportDiagnostic(
                code="DRACAD_PARTIAL_READ",
                severity="warning",
                message=(
                    "ezdracad could not decode every object: "
                    f"{sum(unsupported.values())} objects of kinds it does not read, "
                    f"{sum(failed.values())} objects that did not fit their layout, "
                    f"{int(report.orphan_records)} stray records. They are left out."
                ),
                action="skipped",
                details={
                    "unsupported_kinds": unsupported,
                    "failed_kinds": failed,
                    "orphan_records": int(report.orphan_records),
                    "clamped_spans": int(report.clamped_spans),
                },
            )
        )

    def summary(
        key: str, code: str, severity: str, action: str, message: str, **details: Any
    ) -> None:
        if counts[key]:
            diagnostics.append(
                ImportDiagnostic(
                    code=code,
                    severity=severity,  # type: ignore[arg-type]
                    message=message.format(count=counts[key]),
                    action=action,
                    details={"count": counts[key], **details},
                )
            )

    summary(
        "dimension_from_package",
        "DRACAD_DIMENSION_FROM_PACKAGE",
        "info",
        "exploded",
        "{count} dimensions are drawn from the lines, arrowheads and text DRA-CAD "
        "saved for them (definition.block); they are not associative.",
    )
    not_drawn = counts["dimension_not_drawn"] + counts["hatch_not_drawn"]
    if not_drawn:
        diagnostics.append(
            ImportDiagnostic(
                code="DRACAD_DEFINITION_NOT_DRAWN",
                severity="warning",
                message=(
                    f"{counts['dimension_not_drawn']} dimensions and "
                    f"{counts['hatch_not_drawn']} hatches have a definition but no "
                    "saved geometry; DRA-CAD draws nothing for them either."
                ),
                action="skipped",
                details={
                    "dimensions": counts["dimension_not_drawn"],
                    "hatches": counts["hatch_not_drawn"],
                },
            )
        )
    summary(
        "curve_approximated",
        "DRACAD_CURVE_APPROXIMATED",
        "info",
        "approximated",
        "{count} splines and Bezier curves became polylines through their evaluated points.",
    )
    summary(
        "polyline_exploded",
        "DRACAD_POLYLINE_EXPLODED",
        "info",
        "exploded",
        "{count} polylines whose members are not one path are drawn as separate "
        "lines and arcs, without their fill.",
    )
    summary(
        "projected_3d",
        "DRACAD_3D_PROJECTED",
        "info",
        "normalized",
        "{count} three-dimensional lines and faces were projected to the XY plane; "
        "faces show their edges and are not filled.",
    )
    summary(
        "images",
        "DRACAD_IMAGE_REFERENCED",
        "info",
        "normalized",
        "{count} images keep the path of their file (href); a DRA-CAD drawing does "
        "not contain the pixels.",
    )
    if context.external_references:
        names = sorted(set(context.external_references))
        diagnostics.append(
            ImportDiagnostic(
                code="DRACAD_EXTERNAL_REFERENCE_SKIPPED",
                severity="warning",
                message=(
                    f"{len(context.external_references)} references to other drawings "
                    f"({', '.join(names[:5])}) are left out: the referenced files are "
                    "not part of this file."
                ),
                action="skipped",
                details={"count": len(context.external_references), "files": names},
            )
        )
    if context.undefined_blocks:
        names = sorted(context.undefined_blocks)
        diagnostics.append(
            ImportDiagnostic(
                code="DRACAD_UNDEFINED_BLOCK",
                severity="warning",
                message=(
                    f"Block references to {len(names)} undefined blocks "
                    f"({', '.join(names[:5])}) are left out."
                ),
                action="skipped",
                details={"blocks": names},
            )
        )
    summary(
        "layouts",
        "DRACAD_LAYOUT_SHEETS",
        "info",
        "preserved",
        "{count} sheets of the drawing are kept as layouts, with "
        f"{counts['viewports']} windows onto the model; `entities` is the model.",
        viewports=int(counts["viewports"]),
        entities=int(counts["layout_entities"]),
    )
    summary(
        "viewport_skipped",
        "DRACAD_VIEWPORT_SKIPPED",
        "warning",
        "skipped",
        "{count} windows of a sheet are left out: their outline is not a "
        "rectangle or their view is not usable.",
    )
    summary(
        "text_format_codes",
        "DRACAD_TEXT_FORMAT_REMOVED",
        "info",
        "normalized",
        "{count} texts lost their inline formatting (font, height and colour of a "
        "part of the text); the characters are kept.",
    )
    summary(
        "text_fields",
        "DRACAD_TEXT_FIELD_FILLED",
        "info",
        "normalized",
        "{count} text fields ({{TCODE:…}}, {{PSET:…}}) were replaced by what "
        "DRA-CAD prints for them: the file name, the comment, the paper, the "
        "scales and the property set of the drawing.",
    )
    if context.blank_fields:
        names = sorted(context.blank_fields)
        diagnostics.append(
            ImportDiagnostic(
                code="DRACAD_TEXT_FIELD_UNRESOLVED",
                severity="info",
                message=(
                    f"Text fields for {', '.join(names)} are left empty: DRA-CAD "
                    "takes these values from the application, the user, the clock "
                    "or the file system, not from the drawing."
                ),
                action="normalized",
                details={
                    "fields": names,
                    "texts_left_out": int(counts["blank_field_text"]),
                },
            )
        )
    summary(
        "text_vertical",
        "DRACAD_TEXT_VERTICAL_IGNORED",
        "info",
        "normalized",
        "{count} texts in a font for vertical writing are laid out horizontally.",
    )
    summary(
        "arrowhead_unknown",
        "DRACAD_ARROWHEAD_UNKNOWN",
        "warning",
        "skipped",
        "{count} arrowheads of an unknown type are left out.",
    )


def _scale_label(scale: float) -> str:
    if scale >= 1.0:
        return f"1:{scale:g}"
    return f"{1.0 / scale:g}:1"


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
