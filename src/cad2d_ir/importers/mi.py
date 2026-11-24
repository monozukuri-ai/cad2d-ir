"""Native MI/BI to CAD 2D IR importer backed by :mod:`ezmi2d`."""

from __future__ import annotations

import hashlib
import math
import re
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
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

_EPSILON = 1.0e-10
# Gap between sheets laid side by side, as a share of the widest sheet (as for IDW).
DEFAULT_SHEET_GAP_RATIO = 0.1
# Block references followed when measuring a sheet.
_EXTENT_DEPTH = 64
_TAU = 2.0 * math.pi

_COLOR_MAP: dict[int, str] = {
    0: "#000000",
    1: "#FF0000",
    2: "#00FF00",
    3: "#FFFF00",
    4: "#0000FF",
    5: "#FF00FF",
    6: "#00FFFF",
    7: "#FFFFFF",
}

_LINETYPE_NAMES: dict[int, str] = {
    0: "CONTINUOUS",
    1: "MI_DASHED",
    2: "MI_DOTTED",
    3: "MI_DOT_CENTER",
    4: "MI_DASH_DOT_DOT",
    5: "MI_LONG_DASHED",
    6: "MI_DASH_CENTER",
    7: "MI_PHANTOM",
    8: "MI_LEGACY_PHANTOM",
    9: "MI_SHORT_DASH",
    10: "MI_CENTER_DASH_DASH",
    11: "MI_LONG_DASH_SHORT_DASH",
    12: "MI_LONG_DASH_TWO_SHORT_DASH",
}

_LINETYPE_DESCRIPTIONS: dict[int, str] = {
    0: "Continuous line",
    1: "MI dashed line; source dash lengths are not exposed",
    2: "MI dotted line; source dash lengths are not exposed",
    3: "MI dot-center line; source dash lengths are not exposed",
    4: "MI dash-dot-dot line; source dash lengths are not exposed",
    5: "MI long-dashed line; source dash lengths are not exposed",
    6: "MI dash-center line; source dash lengths are not exposed",
    7: "MI phantom line; source dash lengths are not exposed",
    8: "MI legacy phantom line; source dash lengths are not exposed",
    9: "MI short-dash line; source dash lengths are not exposed",
    10: "MI center-dash-dash line; source dash lengths are not exposed",
    11: "MI long-dash-short-dash line; source dash lengths are not exposed",
    12: "MI long-dash-two-short-dash line; source dash lengths are not exposed",
}

_UNIT_MAP: dict[str, str] = {
    "MM": "mm",
    "MILLIMETER": "mm",
    "MILLIMETERS": "mm",
    "CM": "cm",
    "CENTIMETER": "cm",
    "CENTIMETERS": "cm",
    "M": "m",
    "METER": "m",
    "METERS": "m",
    "IN": "inch",
    "INCH": "inch",
    "INCHES": "inch",
    "FT": "ft",
    "FOOT": "ft",
    "FEET": "ft",
    "NONE": "unitless",
    "UNITLESS": "unitless",
}

_DIMENSION_KINDS = {"DANG", "DCHMF", "DDIA", "DRAD", "DSGL"}


@dataclass(slots=True)
class _ConversionContext:
    options: ImportOptions
    block_names: dict[int, str]
    hatch_loop_roles: dict[int, dict[int, bool]]
    diagnostics: list[ImportDiagnostic] = field(default_factory=list)
    layers: dict[str, dict[str, Any]] = field(default_factory=dict)
    linetypes: dict[str, dict[str, Any]] = field(default_factory=dict)
    text_styles: dict[str, dict[str, Any]] = field(default_factory=dict)
    converted_counts: Counter[str] = field(default_factory=Counter)
    skipped_counts: Counter[str] = field(default_factory=Counter)
    approximation_counts: Counter[str] = field(default_factory=Counter)
    preserved_semantics: Counter[str] = field(default_factory=Counter)
    handled_annotation_ids: set[int] = field(default_factory=set)
    invalid_instance_edges: set[tuple[int, int]] = field(default_factory=set)
    instance_transforms: dict[tuple[int, int], Any] = field(default_factory=dict)
    # Empty parts that only place one child at the origin, mapped to that child.
    pass_through: dict[int, int] = field(default_factory=dict)
    parts_by_index: Mapping[int, Any] = field(default_factory=dict)
    mi_document: Any = None
    next_entity_number: int = 1

    def allocate_id(self) -> str:
        entity_id = f"MI_E{self.next_entity_number:08d}"
        self.next_entity_number += 1
        return entity_id

    def add_converted(self, entity: dict[str, Any]) -> dict[str, Any]:
        self.converted_counts[str(entity["kind"])] += 1
        return entity


def convert_mi_file_to_ir(
    path: str | Path,
    *,
    options: ImportOptions | None = None,
) -> ImportResult:
    """Read an MI text file or supported gzip-wrapped BI file and convert it to IR."""
    try:
        import ezmi2d
    except ImportError as exc:
        raise MissingOptionalDependencyError(
            "MI support requires the optional dependency ezmi2d; "
            'install it with `pip install "cad2d-ir[mi]"`.'
        ) from exc

    import_options = options or ImportOptions()
    source_path = Path(path)
    selected_encoding = (
        None
        if import_options.encoding.strip().lower() == "auto"
        else import_options.encoding
    )
    try:
        document = ezmi2d.readfile(source_path, encoding=selected_encoding)
    except Exception as exc:
        raise ImporterError(f"Failed to read MI file {source_path}: {exc}") from exc

    return mi_document_to_ir(
        document,
        source_name=source_path.name,
        source_sha256=_sha256_file(source_path),
        parser_version=str(getattr(ezmi2d, "__version__", "unknown")),
        requested_encoding=import_options.encoding,
        options=import_options,
    )


def mi_document_to_ir(
    mi_document: Any,
    *,
    source_name: str | None = None,
    source_sha256: str | None = None,
    parser_version: str | None = None,
    requested_encoding: str = "auto",
    options: ImportOptions | None = None,
) -> ImportResult:
    """Convert an ``ezmi2d.Document``-compatible object directly to IR."""
    import_options = options or ImportOptions()
    parts = tuple(getattr(mi_document, "parts", ()))
    parts_by_index = _parts_by_index(parts)
    block_names = _build_block_names(parts)
    context = _ConversionContext(
        options=import_options,
        block_names=block_names,
        hatch_loop_roles=_build_hatch_loop_roles(mi_document),
    )
    context.layers["0"] = {
        "linetype": "CONTINUOUS",
        "plot": True,
        "metadata": {"mi": {"synthetic_default": True}},
    }
    context.linetypes["CONTINUOUS"] = {
        "description": _LINETYPE_DESCRIPTIONS[0],
        "pattern_mm": [],
    }

    context.parts_by_index = parts_by_index
    context.mi_document = mi_document
    top_part_index = _resolve_top_part_index(mi_document, parts_by_index)
    _classify_instance_edges(parts_by_index, context)
    sheet_indices = set(getattr(mi_document, "sheet_part_indices", ()))
    context.pass_through = _pass_through_parts(
        parts_by_index, context, keep=sheet_indices | {top_part_index}
    )

    modelspace_entities: list[dict[str, Any]] = []
    if top_part_index is not None:
        modelspace_entities = _convert_part_contents(
            parts_by_index[top_part_index], context
        )

    block_part_indices = {index for index in parts_by_index if index != top_part_index}
    block_part_indices.update(
        instance.target_part_index
        for part in parts
        for instance_index, instance in enumerate(getattr(part, "instances", ()))
        if (int(getattr(part, "index")), instance_index)
        not in context.invalid_instance_edges
        if getattr(instance, "target_part_index", None) is not None
    )
    # Placements name the part behind a pass-through part, so nothing refers to these.
    block_part_indices.difference_update(context.pass_through)
    blocks: dict[str, dict[str, Any]] = {}
    for part_index in sorted(block_part_indices):
        part = parts_by_index.get(part_index)
        if part is None:
            continue
        blocks[block_names[part_index]] = {
            "base_point": [0.0, 0.0],
            "entities": _convert_part_contents(part, context),
            "metadata": {
                "mi": {
                    "part_index": part_index,
                    "name": getattr(part, "name", None),
                    "definition_section_index": getattr(
                        part, "definition_section_index", None
                    ),
                    "assembly_id": getattr(part, "assembly_id", None),
                    "child_part_indices": list(getattr(part, "child_part_indices", ())),
                    "parent_part_indices": list(
                        getattr(part, "parent_part_indices", ())
                    ),
                    "is_sheet": part_index in sheet_indices,
                    **_placement_metadata(part, context),
                }
            },
        }

    if top_part_index is None:
        root_indices = tuple(getattr(mi_document, "root_part_indices", ()))
        if not root_indices:
            root_indices = tuple(sorted(parts_by_index))
        reference_part = next(
            (parts_by_index[index] for index in root_indices if index in parts_by_index),
            None,
        )
        for root_index in root_indices:
            root_index, _ = _behind_pass_through(root_index, context)
            if root_index not in block_names or root_index not in parts_by_index:
                continue
            # Roots drawn at different scales are sized against the first one.
            placed = _with_part_placements(
                _IDENTITY, reference_part, parts_by_index[root_index], context
            )
            root_scale = (
                {}
                if math.isclose(placed.a, 1.0, abs_tol=_EPSILON)
                else {"scale": placed.a}
            )
            modelspace_entities.append(
                context.add_converted(
                    {
                        "id": context.allocate_id(),
                        "kind": "INSERT",
                        "block": block_names[root_index],
                        "insert": [placed.tx, placed.ty],
                        **root_scale,
                        "layer": "0",
                        "metadata": {
                            "mi": {
                                "synthetic_root_placement": True,
                                "target_part_index": root_index,
                            }
                        },
                    }
                )
            )

    _tile_overlapping_sheets(modelspace_entities, blocks, context)
    _forward_parser_diagnostics(mi_document, context)
    _report_unsupported_entities(mi_document, context)
    _report_unhandled_annotations(mi_document, context)
    _append_preserved_semantics_diagnostic(context)

    units, source_unit = _resolve_units(mi_document, context)
    source = _build_source(
        mi_document,
        source_name=source_name,
        source_sha256=source_sha256,
        parser_version=parser_version,
        requested_encoding=requested_encoding,
    )
    header = _build_header(mi_document, units=units, source_unit=source_unit)
    header["metadata"]["mi"]["top_part_scale"] = (
        None
        if top_part_index is None
        else _placement_metadata(parts_by_index[top_part_index], context)["scale"]
    )
    tables: dict[str, Any] = {
        "layers": context.layers,
        "linetypes": context.linetypes,
        "text_styles": context.text_styles,
    }
    if blocks:
        tables["blocks"] = blocks

    document: dict[str, Any] = {
        "format": "cad2d-ir",
        "version": import_options.ir_version,
        "header": header,
        "source": source,
        "tables": tables,
        "entities": modelspace_entities,
    }
    if import_options.validate:
        validate_ir(document)

    all_source_entities = tuple(getattr(mi_document, "all_entities", ()))
    source_counts = Counter(_kind(entity) for entity in all_source_entities)
    block_entity_count = sum(len(block["entities"]) for block in blocks.values())
    statistics: dict[str, Any] = {
        "source_format": "mi",
        "source_version": source.get("version"),
        "source_parts": len(parts),
        "source_sheets": len(sheet_indices),
        "source_entities": len(all_source_entities),
        "source_entity_counts": dict(sorted(source_counts.items())),
        "source_graphics": len(tuple(getattr(mi_document, "entities", ()))),
        "source_annotations": len(tuple(getattr(mi_document, "annotations", ()))),
        "source_parser_diagnostics": len(
            tuple(getattr(mi_document, "diagnostics", ()))
        ),
        "converted_entities": len(modelspace_entities),
        "converted_block_entities": block_entity_count,
        "converted_blocks": len(blocks),
        "passed_through_parts": len(context.pass_through),
        "converted_entity_counts": dict(sorted(context.converted_counts.items())),
        "skipped_entities": sum(context.skipped_counts.values()),
        "skipped_entity_counts": dict(sorted(context.skipped_counts.items())),
        "approximated_entities": sum(context.approximation_counts.values()),
        "preserved_semantics": dict(sorted(context.preserved_semantics.items())),
    }
    return ImportResult(
        document=document,
        diagnostics=context.diagnostics,
        statistics=statistics,
    )


def _convert_part_contents(
    part: Any, context: _ConversionContext
) -> list[dict[str, Any]]:
    converted: list[dict[str, Any]] = []
    for entity in getattr(part, "entities", ()):
        result = _convert_entity_safe(entity, context)
        if result is not None:
            converted.append(result)

    for annotation in getattr(part, "annotations", ()):
        context.handled_annotation_ids.add(int(getattr(annotation, "id")))
        result = _convert_entity_safe(annotation, context)
        if result is not None:
            converted.append(result)

    assembly = getattr(part, "assembly", None)
    if assembly is not None:
        for instance_index, instance in enumerate(getattr(assembly, "instances", ())):
            edge = (int(getattr(part, "index")), instance_index)
            if edge in context.invalid_instance_edges:
                continue
            converted.append(
                _convert_instance(part, assembly, instance, instance_index, context)
            )
    return converted


def _convert_entity_safe(
    source_entity: Any, context: _ConversionContext
) -> dict[str, Any] | None:
    source_kind = _kind(source_entity)
    source_id = str(getattr(source_entity, "id", "?"))
    try:
        if source_kind in {"LIN", "ARC", "FIL", "BSPL", "CIR", "TEX"}:
            result = _convert_graphic(source_entity, context)
        elif source_kind in _DIMENSION_KINDS:
            result = _convert_dimension(source_entity, context)
        elif source_kind == "LED":
            result = _convert_leader(source_entity, context)
        elif source_kind == "HAT":
            result = _convert_hatch(source_entity, context)
        elif source_kind == "DTV":
            return None
        else:
            _diagnose_unsupported_annotation(source_entity, context)
            return None
    except (
        AttributeError,
        IndexError,
        KeyError,
        LookupError,
        TypeError,
        ValueError,
    ) as exc:
        if context.options.strict:
            raise ImporterError(
                f"Failed to convert MI {source_kind} entity {source_id}: {exc}"
            ) from exc
        context.skipped_counts[source_kind] += 1
        context.diagnostics.append(
            ImportDiagnostic(
                code="MI_ENTITY_CONVERSION_FAILED",
                severity="error",
                message=f"Failed to convert MI {source_kind} entity {source_id}: {exc}",
                source_id=source_id,
                source_kind=source_kind,
                action="skipped",
                details=_record_details(source_entity),
            )
        )
        return None
    return context.add_converted(result)


def _convert_graphic(entity: Any, context: _ConversionContext) -> dict[str, Any]:
    source_kind = _kind(entity)
    common = _graphic_common(entity, context)
    if source_kind == "LIN":
        return {
            **common,
            "kind": "LINE",
            "p1": _point(getattr(entity, "start"), "line start"),
            "p2": _point(getattr(entity, "end"), "line end"),
        }
    if source_kind in {"ARC", "FIL"}:
        ccw = getattr(entity, "ccw", None)
        if not isinstance(ccw, bool):
            raise ValueError("arc orientation is unresolved")
        return {
            **common,
            "kind": "ARC",
            "center": _point(getattr(entity, "center"), "arc center"),
            "radius": _positive(getattr(entity, "radius"), "arc radius"),
            "start_angle": math.degrees(
                _finite(getattr(entity, "start_angle"), "arc start angle")
            ),
            "end_angle": math.degrees(
                _finite(getattr(entity, "end_angle"), "arc end angle")
            ),
            "ccw": ccw,
        }
    if source_kind == "CIR":
        return {
            **common,
            "kind": "CIRCLE",
            "center": _point(getattr(entity, "center"), "circle center"),
            "radius": _positive(getattr(entity, "radius"), "circle radius"),
        }
    if source_kind == "BSPL":
        return _convert_spline(entity, common, context)
    if source_kind == "TEX":
        return _convert_text(entity, common, context)
    raise ValueError(f"unsupported graphic kind {source_kind}")


def _convert_spline(
    entity: Any,
    common: dict[str, Any],
    context: _ConversionContext,
) -> dict[str, Any]:
    degree = int(getattr(entity, "degree"))
    control_points = tuple(getattr(entity, "control_points", ()))
    if (
        not 1 <= degree <= 7
        or len(control_points) < 2
        or any(point is None for point in control_points)
    ):
        return _approximate_spline(entity, common, context)

    points = [
        _point(point.location, "spline control point") for point in control_points
    ]
    result: dict[str, Any] = {
        **common,
        "kind": "SPLINE",
        "degree": degree,
        "control_points": points,
    }
    knots = [_finite(value, "spline knot") for value in getattr(entity, "knots", ())]
    if knots:
        result["knots"] = knots

    weights = getattr(entity, "weights", None)
    rational = getattr(entity, "rational", None)
    if rational is True and weights is None:
        return _approximate_spline(entity, common, context)
    if weights is not None:
        numeric_weights = [_positive(value, "spline weight") for value in weights]
        if len(numeric_weights) != len(points):
            return _approximate_spline(entity, common, context)
        result["weights"] = numeric_weights
    closed = getattr(entity, "closed", None)
    if isinstance(closed, bool):
        result["closed"] = closed

    mi_metadata = result.setdefault("metadata", {}).setdefault("mi", {})
    mi_metadata.update(
        {
            "order": int(getattr(entity, "order")),
            "closed": closed,
            "periodic": getattr(entity, "periodic", None),
            "rational": rational,
            "parameter_domain": list(getattr(entity, "parameter_domain", ())),
            "control_point_ids": list(getattr(entity, "control_point_ids", ())),
        }
    )
    if any(
        value is None for value in (closed, getattr(entity, "periodic", None), rational)
    ):
        context.preserved_semantics["presence-aware_spline_flags"] += 1
    return result


def _approximate_spline(
    entity: Any,
    common: dict[str, Any],
    context: _ConversionContext,
) -> dict[str, Any]:
    vertices = _sample_spline(entity, context.options.curve_segments)
    context.approximation_counts[_kind(entity)] += 1
    context.diagnostics.append(
        ImportDiagnostic(
            code="MI_CURVE_APPROXIMATED",
            severity="warning",
            message=(
                f"MI {_kind(entity)} entity {getattr(entity, 'id', '?')} was "
                "sampled as a polyline because its spline definition could not be "
                "represented safely."
            ),
            source_id=str(getattr(entity, "id", "?")),
            source_kind=_kind(entity),
            action="approximated",
            details={
                **_record_details(entity),
                "segments": len(vertices) - 1,
            },
        )
    )
    return {
        **common,
        "kind": "LWPOLYLINE",
        "vertices": vertices,
        "closed": bool(getattr(entity, "closed", False)),
        "approximation": {
            "method": "polyline",
            "source_kind": _kind(entity),
            "segments": len(vertices) - 1,
        },
    }


def _convert_text(
    entity: Any,
    common: dict[str, Any],
    context: _ConversionContext,
) -> dict[str, Any]:
    text = getattr(entity, "text", None)
    if text is None:
        raise ValueError("text content could not be decoded")
    height = _positive(getattr(entity, "height"), "text height")
    width_factor = _positive(getattr(entity, "width_factor"), "text width factor")
    horizontal = str(getattr(entity, "horizontal_alignment"))
    vertical = str(getattr(entity, "vertical_alignment"))
    if horizontal not in {"left", "center", "right"}:
        raise ValueError(f"unsupported text horizontal alignment {horizontal!r}")
    if vertical not in {"lower", "middle", "upper"}:
        raise ValueError(f"unsupported text vertical alignment {vertical!r}")

    font = (getattr(entity, "font_name", None) or "").strip()
    if font:
        style = _text_style_name(font, context)
        common["style"] = style

    mirrored = bool(getattr(entity, "mirrored", False))
    if mirrored:
        context.preserved_semantics["mirrored_text"] += 1

    mi_metadata = common.setdefault("metadata", {}).setdefault("mi", {})
    mi_metadata.update(
        {
            "alignment_code": int(getattr(entity, "alignment")),
            "transform": [
                _finite(value, "text transform")
                for value in getattr(entity, "transform_values", ())
            ],
            "mirrored": mirrored,
            "font_name": font or None,
            "alternate_font_name": getattr(entity, "alternate_font_name", None),
            "size_values": list(getattr(entity, "size_values", ())),
            "line_spacing": _finite(getattr(entity, "line_spacing"), "line spacing"),
        }
    )

    result: dict[str, Any] = {
        **common,
        "insert": _point(getattr(entity, "origin"), "text origin"),
        "height": height,
        "rotation": math.degrees(_finite(getattr(entity, "rotation"), "text rotation")),
        "text": str(text),
    }
    lines = tuple(getattr(entity, "lines", ()))
    if len(lines) > 1:
        result["kind"] = "MTEXT"
        attach_vertical = {
            "lower": "bottom",
            "middle": "middle",
            "upper": "top",
        }[vertical]
        result["attach"] = f"{attach_vertical}_{horizontal}"
    else:
        result["kind"] = "TEXT"
        result["halign"] = horizontal
        result["valign"] = {
            "lower": "bottom",
            "middle": "middle",
            "upper": "top",
        }[vertical]
        result["width_factor"] = width_factor
    return result


def _convert_dimension(entity: Any, context: _ConversionContext) -> dict[str, Any]:
    source_id = str(getattr(entity, "id"))
    points = [
        _point(point.location, "dimension reference point")
        for point in getattr(entity, "reference_points", ())
        if point is not None
    ]
    text_position = _point(getattr(entity, "text_position"), "dimension text position")
    point_definition: dict[str, Any] = {
        "location": text_position,
        "text_midpoint": text_position,
    }
    if points:
        point_definition["p1"] = points[0]
    if len(points) >= 2:
        point_definition["p2"] = points[1]
    for index, point in enumerate(points, start=1):
        point_definition[f"reference_{index}"] = point

    formatted_text = getattr(entity, "formatted_text", None)
    definition: dict[str, Any] = {
        "source_kind": _kind(entity),
        "points": point_definition,
        "location": text_position,
        "measurement": _finite(getattr(entity, "measurement"), "dimension measurement"),
        "source_geometry_ids": list(getattr(entity, "reference_geometry_ids", ())),
        "source_point_ids": list(getattr(entity, "reference_point_ids", ())),
        "property_ids": list(getattr(entity, "property_ids", ())),
        "dimension_style_id": getattr(entity, "dimension_style_id", None),
        "text_style_id": getattr(entity, "text_style_id", None),
        "tolerance_ids": list(getattr(entity, "tolerance_ids", ())),
        "tolerances": [
            _tolerance_definition(value)
            for value in getattr(entity, "tolerances", ())
            if value is not None
        ],
    }
    if _kind(entity) == "DANG":
        definition["measurement_unit"] = "rad"
    if formatted_text is not None:
        definition["text"] = _symbol_text(str(formatted_text))
    # The size and the direction of the main text (ezmi2d >= 0.2.3): the DTA text
    # attribute the dimension points to and the text box stored with it. Without
    # them a renderer has to guess the size from the size of the drawing.
    text_height = _optional_positive(getattr(entity, "text_height", None))
    if text_height is not None:
        definition["text_height"] = text_height
    text_rotation = getattr(entity, "text_rotation", None)
    if isinstance(text_rotation, (int, float)) and math.isfinite(text_rotation):
        definition["text_rotation"] = math.degrees(text_rotation)
    for tolerance, tolerance_definition in zip(
        (value for value in getattr(entity, "tolerances", ()) if value is not None),
        definition["tolerances"],
        strict=True,
    ):
        height_of = getattr(tolerance, "text_height", None)
        tolerance_height = (
            _optional_positive(height_of(text_height)) if callable(height_of) else None
        )
        if tolerance_height is not None:
            tolerance_definition["text_height"] = tolerance_height

    # The drawn form the file stores with the dimension (ezmi2d >= 0.2.4): the line
    # pieces, the terminators and where every text starts. A consumer that draws
    # ``rendered_paths`` and ``rendered_texts`` shows the dimension as the source
    # program drew it instead of one line between the reference points.
    rendering = _dimension_rendering(entity, text_height)
    if rendering is not None:
        definition["rendered_paths"], definition["rendered_texts"] = rendering
        context.preserved_semantics["dimension_graphics"] += 1
    # A linear dimension measures along its dimension line, which need not be
    # parallel to the line between the reference points. The two terminators are on
    # it: state a point of the line as ``location`` (the text position stays in
    # ``text_midpoint``) and its direction as ``rotation``, the way a rotated linear
    # dimension is described.
    dimension_line = _linear_dimension_line(entity)
    if dimension_line is not None:
        point_definition["location"], definition["rotation"] = dimension_line

    for tolerance in getattr(entity, "tolerances", ()):
        if tolerance is not None:
            context.handled_annotation_ids.add(int(getattr(tolerance, "id")))
    context.preserved_semantics["generic_dimension"] += 1
    return {
        "id": context.allocate_id(),
        "kind": "DIMENSION",
        "dim_kind": "GENERIC",
        "definition": definition,
        "source": _entity_source(entity),
        "metadata": {
            "mi": {
                "part_index": getattr(entity, "part_index", None),
                "source_kind": _kind(entity),
                "source_id": source_id,
            }
        },
    }


def _linear_dimension_line(entity: Any) -> tuple[list[float], float] | None:
    """A point on the dimension line of a linear dimension and its direction in
    degrees, from the two terminators the file stores. ``None`` for another kind of
    dimension or without them."""
    graphics = getattr(entity, "graphics", None)
    if graphics is None or _kind(entity) != "DSGL" or len(graphics.arrows) != 2:
        return None
    first = _point(graphics.arrows[0].tip, "dimension terminator")
    second = _point(graphics.arrows[1].tip, "dimension terminator")
    dx, dy = second[0] - first[0], second[1] - first[1]
    if math.hypot(dx, dy) <= 0.0:
        return None
    return second, math.degrees(math.atan2(dy, dx)) % 180.0


#: The largest step of a dimension arc drawn as a polyline, in radians.
_DIMENSION_ARC_STEP = math.radians(5.0)


def _dimension_rendering(
    entity: Any, text_height: float | None
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]] | None:
    """The drawn form of a dimension as ``rendered_paths`` and ``rendered_texts``.

    ``None`` when the reader gives no drawn form (ezmi2d before 0.2.4, or a record
    that does not follow the known layout), when the height of the main text is
    unknown, or when nothing would be drawn.
    """
    graphics = getattr(entity, "graphics", None)
    if graphics is None or text_height is None:
        return None

    paths: list[dict[str, Any]] = []
    for segment in (*graphics.lines, *graphics.extension_lines):
        start = _point(segment.start, "dimension line start")
        end = _point(segment.end, "dimension line end")
        if start != end:
            paths.append({"points": [start, end], "closed": False})
    for arc in graphics.arcs:
        points = _dimension_arc_points(arc)
        if len(points) >= 2:
            paths.append({"points": points, "closed": False})
    for arrow in graphics.arrows:
        if not arrow.visible:
            continue
        outline = arrow.outline(text_height)
        if outline is None:
            continue  # a terminator of another shape (slash, dot): its form is not known
        paths.append(
            {
                "points": [_point(corner, "dimension terminator") for corner in outline],
                "closed": True,
            }
        )

    rotation = getattr(entity, "text_rotation", None)
    angle = float(rotation) if isinstance(rotation, (int, float)) else 0.0
    if not math.isfinite(angle):
        angle = 0.0
    along = (math.cos(angle), math.sin(angle))
    texts: list[dict[str, Any]] = []

    def add_text(value: Any, origin: Any, height: float | None) -> None:
        if value is None:
            return  # bytes the reader could not decode
        content = _symbol_text(str(value))
        size = height if height is not None else text_height
        anchor = _point(origin, "dimension text start")
        # The text starts at its first character. A text that begins with spaces
        # (" R") is moved by about their width so that renderers that trim it keep
        # the letter where it was; the font advances roughly one height per space.
        lead = len(content) - len(content.lstrip(" "))
        content = content.strip(" ")
        if not content:
            return
        if lead:
            anchor = [anchor[0] + along[0] * lead * size, anchor[1] + along[1] * lead * size]
        texts.append(
            {
                "anchor": anchor,
                "height": size,
                "text": content,
                "angle_deg": math.degrees(angle),
            }
        )

    placement = graphics.tolerance
    if placement is None or not placement.replaces_main_text:
        add_text(getattr(entity, "formatted_text", None), graphics.text_origin, text_height)
    tolerance = None if placement is None else placement.tolerance
    if placement is not None and tolerance is not None:
        tolerance_height = _optional_positive(tolerance.text_height(text_height))
        add_text(tolerance.upper_text, placement.upper_origin, tolerance_height)
        if placement.layout_value != 1:
            add_text(tolerance.lower_text, placement.lower_origin, tolerance_height)
    for part in graphics.text_parts:
        add_text(part.text, part.origin, _optional_positive(part.text_height(text_height)))

    if not paths and not texts:
        return None
    return paths, texts


def _dimension_arc_points(arc: Any) -> list[list[float]]:
    """A dimension arc, counterclockwise from its start to its end, as a polyline."""
    center = _point(arc.center, "dimension arc center")
    start = _point(arc.start, "dimension arc start")
    end = _point(arc.end, "dimension arc end")
    radius = math.hypot(start[0] - center[0], start[1] - center[1])
    if not radius > 0.0:
        return []
    start_angle = math.atan2(start[1] - center[1], start[0] - center[0])
    end_angle = math.atan2(end[1] - center[1], end[0] - center[0])
    sweep = (end_angle - start_angle) % math.tau
    if sweep <= 0.0:
        return []
    steps = max(2, math.ceil(sweep / _DIMENSION_ARC_STEP - 1e-9))
    points = [
        [
            center[0] + radius * math.cos(start_angle + sweep * index / steps),
            center[1] + radius * math.sin(start_angle + sweep * index / steps),
        ]
        for index in range(1, steps)
    ]
    return [start, *points, end]


def _symbol_text(text: str) -> str:
    """Dimension text with the symbol font codes replaced by characters (degree,
    diameter, plus-minus, minutes, seconds). Needs ezmi2d >= 0.2.4; unchanged
    without it."""
    try:
        import ezmi2d
    except ImportError:  # a duck-typed document without the reader installed
        return text
    replace = getattr(ezmi2d, "replace_symbol_codes", None)
    return replace(text) if callable(replace) else text


def _convert_leader(entity: Any, context: _ConversionContext) -> dict[str, Any]:
    common = _graphic_common(entity, context)
    vertices = [
        _point(vertex, "leader vertex") for vertex in getattr(entity, "vertices", ())
    ]
    if len(vertices) < 2:
        raise ValueError("leader has fewer than two vertices")
    common.setdefault("metadata", {}).setdefault("mi", {}).update(
        {
            "annotation_kind": _kind(entity),
            "arrow_type": int(getattr(entity, "arrow_type")),
            "arrow_size": _finite(getattr(entity, "arrow_size"), "leader arrow size"),
            "elevations": [
                _finite(point.elevation, "leader elevation")
                for point in getattr(entity, "points", ())
            ],
        }
    )
    context.diagnostics.append(
        ImportDiagnostic(
            code="MI_ANNOTATION_FLATTENED",
            severity="warning",
            message=(
                f"MI leader {getattr(entity, 'id', '?')} was represented as an "
                "IR polyline; arrow semantics remain in metadata."
            ),
            source_id=str(getattr(entity, "id", "?")),
            source_kind=_kind(entity),
            action="flattened",
            details=_record_details(entity),
        )
    )
    return {
        **common,
        "kind": "LWPOLYLINE",
        "vertices": vertices,
        "closed": False,
    }


def _convert_hatch(entity: Any, context: _ConversionContext) -> dict[str, Any]:
    common = _graphic_common(entity, context)
    role_by_loop = context.hatch_loop_roles.get(int(getattr(entity, "id")), {})
    loops: list[dict[str, Any]] = []
    component_kinds: Counter[str] = Counter()
    for loop_index, contour in enumerate(getattr(entity, "boundary_loops", ())):
        if contour is None:
            raise LookupError("hatch boundary contour is unresolved")
        vertices = _sample_contour(contour, context.options.curve_segments)
        component_kinds.update(
            _kind(component)
            for component in getattr(contour, "components", ())
            if component is not None
        )
        loop_id = int(getattr(contour, "id"))
        loops.append(
            {
                "vertices": vertices,
                "is_outer": role_by_loop.get(loop_id, loop_index == 0),
            }
        )
    if not loops:
        raise ValueError("hatch has no resolved boundary loops")

    pattern = getattr(entity, "pattern", None)
    pattern_lines = []
    if pattern is not None:
        pattern_lines = [
            {
                "offset": _finite(line.offset, "hatch pattern offset"),
                "distance": _finite(line.distance, "hatch pattern distance"),
                "angle_rad": _finite(line.angle, "hatch pattern angle"),
                "color": int(line.color),
                "linetype": int(line.linetype),
            }
            for line in getattr(pattern, "lines", ())
        ]
        if pattern_lines:
            context.preserved_semantics["hatch_pattern_definition"] += 1

    common.setdefault("metadata", {}).setdefault("mi", {}).update(
        {
            "annotation_kind": _kind(entity),
            "reference_point": _point(
                getattr(entity, "reference_point"), "hatch reference point"
            ),
            "angle_rad": _finite(getattr(entity, "angle"), "hatch angle"),
            "spacing": _finite(getattr(entity, "spacing"), "hatch spacing"),
            "boundary_loop_ids": list(getattr(entity, "boundary_loop_ids", ())),
            "pattern_id": None if pattern is None else getattr(pattern, "id", None),
            "pattern_lines": pattern_lines,
        }
    )
    vertex_count = sum(len(loop["vertices"]) for loop in loops)
    context.approximation_counts[_kind(entity)] += 1
    context.diagnostics.append(
        ImportDiagnostic(
            code="MI_CURVE_APPROXIMATED",
            severity="warning",
            message=(
                f"MI hatch {getattr(entity, 'id', '?')} curve boundaries were "
                "sampled as IR hatch polylines."
            ),
            source_id=str(getattr(entity, "id", "?")),
            source_kind=_kind(entity),
            action="approximated",
            details={
                **_record_details(entity),
                "component_kinds": dict(sorted(component_kinds.items())),
                "vertices": vertex_count,
            },
        )
    )
    return {
        **common,
        "kind": "HATCH",
        "solid": False,
        "pattern": "MI_PATTERN",
        "loops": loops,
        "approximation": {
            "method": "polyline",
            "source_kind": _kind(entity),
            "segments": vertex_count,
        },
    }


def _convert_instance(
    part: Any,
    assembly: Any,
    instance: Any,
    instance_index: int,
    context: _ConversionContext,
) -> dict[str, Any]:
    parent_index = int(getattr(part, "index"))
    target_index, passed_through = _behind_pass_through(
        int(getattr(instance, "target_part_index")), context
    )
    edge = (parent_index, instance_index)
    transform = _with_part_placements(
        context.instance_transforms[edge],
        part,
        context.parts_by_index.get(target_index),
        context,
    )
    result: dict[str, Any] = {
        "id": context.allocate_id(),
        "kind": "INSERT",
        "block": context.block_names[target_index],
        "insert": [float(transform.tx), float(transform.ty)],
        "layer": "0",
        "source": {
            "format": "mi",
            "id": f"{getattr(assembly, 'id')}:{instance_index}",
            "kind": "ASSE_INSTANCE",
            "metadata": {
                **_record_details(assembly),
                "parent_part_index": parent_index,
                "target_part_index": target_index,
            },
        },
        "metadata": {
            "mi": {
                "assembly_id": int(getattr(assembly, "id")),
                "instance_index": instance_index,
                "parent_part_index": parent_index,
                "target_part_index": target_index,
                "is_sheet": bool(getattr(instance, "is_sheet", False)),
                "relation_hex": _optional_hex(
                    getattr(instance, "relation_value", None)
                ),
                "definition_values_hex": [
                    value.hex() for value in getattr(instance, "definition_values", ())
                ],
                "source_transform": list(getattr(instance, "transform_values", ())),
            }
        },
    }
    if passed_through:
        result["metadata"]["mi"]["passed_through_part_indices"] = passed_through
    decomposed = _decompose_insert_transform(transform)
    if decomposed is None:
        # IRのAffine2DはSVG順 [a,b,c,d,e,f] (x'=a*x+c*y+e, y'=b*x+d*y+f)
        result["transform"] = [
            float(transform.a),
            float(transform.b),
            float(transform.c),
            float(transform.d),
            float(transform.tx),
            float(transform.ty),
        ]
        context.preserved_semantics["complete_affine_insert"] += 1
    else:
        rotation_deg, scale_x, scale_y = decomposed
        if not math.isclose(rotation_deg, 0.0, abs_tol=_EPSILON):
            result["rotation"] = rotation_deg
        if not (
            math.isclose(scale_x, 1.0, abs_tol=_EPSILON)
            and math.isclose(scale_y, 1.0, abs_tol=_EPSILON)
        ):
            if math.isclose(scale_x, scale_y, abs_tol=_EPSILON):
                result["scale"] = scale_x
            else:
                result["scale"] = [scale_x, scale_y]
    return context.add_converted(result)


def _graphic_common(entity: Any, context: _ConversionContext) -> dict[str, Any]:
    layers = tuple(str(value).strip() for value in getattr(entity, "layers", ()))
    layers = tuple(value for value in layers if value)
    layer = layers[0] if layers else "0"
    if len(layers) > 1:
        context.preserved_semantics["multiple_layers"] += 1
    context.layers.setdefault(
        layer,
        {
            "plot": True,
            "metadata": {"mi": {"source_layer": layer}},
        },
    )

    color_code = int(getattr(entity, "color"))
    linetype_code = int(getattr(entity, "linetype"))
    linetype_name = _LINETYPE_NAMES.get(linetype_code)
    if linetype_name is not None:
        context.linetypes.setdefault(
            linetype_name,
            {
                "description": _LINETYPE_DESCRIPTIONS[linetype_code],
                "pattern_mm": [],
            },
        )
        if linetype_code != 0:
            context.preserved_semantics["linetype_pattern_lengths"] += 1

    lineweight = _finite(getattr(entity, "lineweight"), "MI lineweight")
    if lineweight != 0.0:
        context.preserved_semantics["lineweight_without_verified_mm_units"] += 1

    metadata = {
        "mi": {
            "part_index": getattr(entity, "part_index", None),
            "color_code": color_code,
            "color_name": getattr(entity, "color_name", None),
            "linetype_code": linetype_code,
            "linetype_name": getattr(entity, "linetype_name", None),
            "lineweight": lineweight,
            "visibility_value": getattr(entity, "visibility_value", None),
            "layers": list(layers),
            "property_ids": list(getattr(entity, "property_ids", ())),
        }
    }
    result: dict[str, Any] = {
        "id": context.allocate_id(),
        "kind": "LINE",
        "layer": layer,
        "source": _entity_source(entity),
        "metadata": metadata,
    }
    color = _COLOR_MAP.get(color_code)
    if color is not None:
        result["color"] = color
    if linetype_name is not None:
        result["linetype"] = linetype_name
    visibility = getattr(entity, "visibility", None)
    if isinstance(visibility, bool):
        result["visible"] = visibility
    elif getattr(entity, "visibility_value", None) is not None:
        context.preserved_semantics["unverified_visibility_code"] += 1
    return result


def _entity_source(entity: Any) -> dict[str, Any]:
    return {
        "format": "mi",
        "id": str(getattr(entity, "id")),
        "kind": _kind(entity),
        "metadata": _record_details(entity),
    }


def _record_details(entity: Any) -> dict[str, Any]:
    result: dict[str, Any] = {"part_index": getattr(entity, "part_index", None)}
    record = getattr(entity, "raw_record", None)
    if record is None:
        return result
    result.update(
        {
            "record_index": getattr(record, "index", None),
            "section_index": getattr(record, "section_index", None),
            "section_number": getattr(record, "section_number", None),
            "termination": getattr(record, "termination", None),
        }
    )
    span = getattr(record, "span", None)
    if span is not None:
        result["span"] = _span_dict(span)
    return result


def _tolerance_definition(entity: Any) -> dict[str, Any]:
    return {
        "source_id": int(getattr(entity, "id")),
        "definition_value": int(getattr(entity, "definition_value")),
        "upper_value": _finite(getattr(entity, "upper_value"), "upper tolerance"),
        "lower_value": _finite(getattr(entity, "lower_value"), "lower tolerance"),
        "format_value": int(getattr(entity, "format_value")),
        "upper_text": getattr(entity, "upper_text", None),
        "lower_text": getattr(entity, "lower_text", None),
        "text_style_id": int(getattr(entity, "text_style_id")),
        # The last field of a DTV points to the DTA text attribute of the tolerance
        # text. ezmi2d before 0.2.3 called it ``alignment``.
        "text_attribute_id": int(
            getattr(entity, "text_attribute_id", None)
            if getattr(entity, "text_attribute_id", None) is not None
            else getattr(entity, "alignment")
        ),
    }


def _sample_contour(contour: Any, curve_segments: int) -> list[list[float]]:
    if not bool(getattr(contour, "closed", False)):
        raise ValueError(f"contour {getattr(contour, 'id', '?')} is not closed")
    segments: list[list[list[float]]] = []
    for component in getattr(contour, "components", ()):
        if component is None:
            raise LookupError("contour component is unresolved")
        segments.append(_sample_component(component, curve_segments))
    if not segments:
        raise ValueError("contour has no components")

    vertices = list(segments[0])
    for segment in segments[1:]:
        if not segment:
            continue
        if _distance(vertices[-1], segment[-1]) < _distance(vertices[-1], segment[0]):
            segment = list(reversed(segment))
        if _points_close(vertices[-1], segment[0]):
            vertices.extend(segment[1:])
        else:
            vertices.extend(segment)
    vertices = _deduplicate_adjacent(vertices)
    if len(vertices) >= 2 and _points_close(vertices[0], vertices[-1]):
        vertices.pop()
    if len(vertices) < 3:
        raise ValueError("sampled contour has fewer than three vertices")
    return vertices


def _sample_component(entity: Any, curve_segments: int) -> list[list[float]]:
    kind = _kind(entity)
    if kind == "LIN":
        return [
            _point(getattr(entity, "start"), "line start"),
            _point(getattr(entity, "end"), "line end"),
        ]
    if kind in {"ARC", "FIL"}:
        center = _point(getattr(entity, "center"), "arc center")
        radius = _positive(getattr(entity, "radius"), "arc radius")
        start = _finite(getattr(entity, "start_angle"), "arc start angle")
        end = _finite(getattr(entity, "end_angle"), "arc end angle")
        ccw = getattr(entity, "ccw", None)
        if not isinstance(ccw, bool):
            raise ValueError("arc orientation is unresolved")
        if ccw:
            sweep = (end - start) % _TAU
        else:
            sweep = -((start - end) % _TAU)
        if math.isclose(sweep, 0.0, abs_tol=_EPSILON):
            sweep = _TAU if ccw else -_TAU
        count = max(2, math.ceil(curve_segments * abs(sweep) / _TAU))
        return [
            [
                center[0] + radius * math.cos(start + sweep * index / count),
                center[1] + radius * math.sin(start + sweep * index / count),
            ]
            for index in range(count + 1)
        ]
    if kind == "CIR":
        center = _point(getattr(entity, "center"), "circle center")
        radius = _positive(getattr(entity, "radius"), "circle radius")
        return [
            [
                center[0] + radius * math.cos(_TAU * index / curve_segments),
                center[1] + radius * math.sin(_TAU * index / curve_segments),
            ]
            for index in range(curve_segments)
        ]
    if kind == "BSPL":
        return _sample_spline(entity, curve_segments)
    raise ValueError(f"unsupported contour component {kind}")


def _sample_spline(entity: Any, curve_segments: int) -> list[list[float]]:
    domain = tuple(getattr(entity, "parameter_domain", ()))
    if len(domain) != 2:
        raise ValueError("spline parameter domain is missing")
    start = _finite(domain[0], "spline domain start")
    end = _finite(domain[1], "spline domain end")
    if end <= start:
        raise ValueError("spline parameter domain is empty")
    vertices = []
    for index in range(curve_segments + 1):
        parameter = (
            end
            if index == curve_segments
            else start + (end - start) * index / curve_segments
        )
        vertices.append(_point(entity.evaluate(parameter), "sampled spline point"))
    return _deduplicate_adjacent(vertices)


def _tile_overlapping_sheets(
    modelspace_entities: list[dict[str, Any]],
    blocks: Mapping[str, dict[str, Any]],
    context: _ConversionContext,
) -> None:
    """Lay the sheets side by side when the file draws them on top of each other.

    A drawing of several sheets keeps each sheet in the same place: the program shows
    one at a time. In one modelspace they would cover each other, so each sheet after
    the first is moved along +X behind the one before, with a gap of
    ``DEFAULT_SHEET_GAP_RATIO`` times the widest sheet. Sheets the file already keeps
    apart stay where they are.
    """
    sheets = [
        entity
        for entity in modelspace_entities
        if entity.get("kind") == "INSERT"
        and entity.get("metadata", {}).get("mi", {}).get("is_sheet")
    ]
    if len(sheets) < 2:
        return
    memo: dict[str, tuple[float, float, float, float] | None] = {}
    extents = [_entity_extent(entity, blocks, memo, _EXTENT_DEPTH) for entity in sheets]
    if any(extent is None for extent in extents):
        return
    boxes = [extent for extent in extents if extent is not None]
    overlapping = any(
        first[0] < second[2]
        and second[0] < first[2]
        and first[1] < second[3]
        and second[1] < first[3]
        for index, first in enumerate(boxes)
        for second in boxes[index + 1 :]
    )
    if not overlapping:
        return
    gap = max(box[2] - box[0] for box in boxes) * DEFAULT_SHEET_GAP_RATIO
    cursor = boxes[0][0]
    offsets: list[list[float]] = []
    for entity, box in zip(sheets, boxes):
        shift = cursor - box[0]
        if not math.isclose(shift, 0.0, abs_tol=_EPSILON):
            entity["insert"] = [
                float(entity["insert"][0]) + shift,
                float(entity["insert"][1]),
            ]
            entity["metadata"]["mi"]["sheet_offset"] = [shift, 0.0]
        offsets.append([shift, 0.0])
        cursor += (box[2] - box[0]) + gap
    context.diagnostics.append(
        ImportDiagnostic(
            code="MI_MULTISHEET_TILED",
            severity="warning",
            message=(
                f"Placed {len(sheets)} MI sheets side by side in one IR modelspace; "
                "sheet offsets remain in source metadata."
            ),
            action="flattened",
            details={"sheet_count": len(sheets), "gap": gap, "offsets": offsets},
        )
    )


def _entity_extent(
    entity: Mapping[str, Any],
    blocks: Mapping[str, dict[str, Any]],
    memo: dict[str, tuple[float, float, float, float] | None],
    depth: int,
) -> tuple[float, float, float, float] | None:
    """(min x, min y, max x, max y) of what the entity draws, from its defining points.

    Good enough to tell where a sheet ends: curves count by their centres, radii and
    control points, text by where it starts.
    """
    kind = entity.get("kind")
    points: list[tuple[float, float]] = []
    if kind == "LINE":
        points = [_xy(entity["p1"]), _xy(entity["p2"])]
    elif kind in {"CIRCLE", "ARC"}:
        cx, cy = _xy(entity["center"])
        radius = abs(float(entity["radius"]))
        points = [(cx - radius, cy - radius), (cx + radius, cy + radius)]
    elif kind == "LWPOLYLINE":
        points = [_xy(vertex) for vertex in entity.get("vertices", ())]
    elif kind == "SPLINE":
        points = [_xy(point) for point in entity.get("control_points", ())]
    elif kind in {"TEXT", "MTEXT"}:
        points = [_xy(entity["insert"])] if "insert" in entity else []
    elif kind == "HATCH":
        points = [
            _xy(vertex)
            for loop in entity.get("loops", ())
            for vertex in loop.get("vertices", ())
        ]
    elif kind == "INSERT":
        name = str(entity.get("block"))
        if depth <= 0 or name not in blocks:
            return None
        if name not in memo:
            memo[name] = None  # a block that names itself draws nothing here
            memo[name] = _merge_extents(
                _entity_extent(inner, blocks, memo, depth - 1)
                for inner in blocks[name].get("entities", ())
            )
        inner_extent = memo[name]
        if inner_extent is None:
            return None
        transform = _insert_affine(entity)
        x0, y0, x1, y1 = inner_extent
        points = [
            (
                transform.a * x + transform.c * y + transform.tx,
                transform.b * x + transform.d * y + transform.ty,
            )
            for x, y in ((x0, y0), (x1, y0), (x1, y1), (x0, y1))
        ]
    if not points:
        return None
    xs = [point[0] for point in points]
    ys = [point[1] for point in points]
    if not all(math.isfinite(value) for value in (*xs, *ys)):
        return None
    return (min(xs), min(ys), max(xs), max(ys))


def _merge_extents(
    extents: Iterable[tuple[float, float, float, float] | None],
) -> tuple[float, float, float, float] | None:
    found = [extent for extent in extents if extent is not None]
    if not found:
        return None
    return (
        min(extent[0] for extent in found),
        min(extent[1] for extent in found),
        max(extent[2] for extent in found),
        max(extent[3] for extent in found),
    )


def _xy(value: Any) -> tuple[float, float]:
    if isinstance(value, Mapping):
        return (float(value["x"]), float(value["y"]))
    return (float(value[0]), float(value[1]))


def _insert_affine(entity: Mapping[str, Any]) -> "_Affine":
    """The transform an INSERT of this module applies to its block."""
    tx, ty = _xy(entity.get("insert", (0.0, 0.0)))
    complete = entity.get("transform")
    if complete is not None:
        a, b, c, d = (float(value) for value in complete[:4])
        return _Affine(a, b, c, d, tx, ty)
    scale = entity.get("scale", 1.0)
    scale_x, scale_y = (
        (float(scale[0]), float(scale[1]))
        if isinstance(scale, (list, tuple))
        else (float(scale), float(scale))
    )
    angle = math.radians(float(entity.get("rotation", 0.0)))
    cos_angle, sin_angle = math.cos(angle), math.sin(angle)
    return _Affine(
        a=cos_angle * scale_x,
        b=sin_angle * scale_x,
        c=-sin_angle * scale_y,
        d=cos_angle * scale_y,
        tx=tx,
        ty=ty,
    )


@dataclass(frozen=True)
class _Affine:
    """x' = a*x + c*y + tx, y' = b*x + d*y + ty (the fields ezmi2d's Affine2D has)."""

    a: float
    b: float
    c: float
    d: float
    tx: float
    ty: float


_IDENTITY = _Affine(1.0, 0.0, 0.0, 1.0, 0.0, 0.0)


def _part_placement(
    part: Any, context: _ConversionContext
) -> tuple[float, float, float] | None:
    """Scale and offset the part's geometry is drawn with, when the file states them.

    A point ``p`` of the part lies at ``scale * p + offset`` on the sheet, whatever part it
    is placed in: a view drawn at 1:5 holds geometry five times the size it has there.
    ezmi2d 0.2.5 and later read both (``Document.part_placement``), with parts whose
    geometry is kept in sheet units (frames, title blocks, symbols) at full size.
    """
    reader = getattr(context.mi_document, "part_placement", None)
    placement = None if part is None or reader is None else reader(part)
    if placement is None:
        return None
    offset = getattr(placement, "offset")
    values = (
        float(getattr(placement, "scale")),
        float(getattr(offset, "x")),
        float(getattr(offset, "y")),
    )
    if not all(math.isfinite(value) for value in values) or values[0] <= 0.0:
        return None
    return values


def _placement_metadata(part: Any, context: _ConversionContext) -> dict[str, Any]:
    placement = _part_placement(part, context)
    if placement is None:
        return {"scale": None, "offset": None}
    return {"scale": placement[0], "offset": [placement[1], placement[2]]}


def _with_part_placements(
    transform: Any, parent: Any, child: Any, context: _ConversionContext
) -> _Affine:
    """The placement's transform with the two parts' scales and offsets taken in.

    Equating where a point lies on the sheet in the child's and in the parent's
    coordinates gives the child point in the parent's units; the placement's own matrix
    is applied after that. Parts that state no scale are taken as they are.
    """
    result = _Affine(
        float(transform.a),
        float(transform.b),
        float(transform.c),
        float(transform.d),
        float(transform.tx),
        float(transform.ty),
    )
    parent_placement = _part_placement(parent, context)
    child_placement = _part_placement(child, context)
    if parent_placement is None or child_placement is None:
        return result
    factor = child_placement[0] / parent_placement[0]
    dx = (child_placement[1] - parent_placement[1]) / parent_placement[0]
    dy = (child_placement[2] - parent_placement[2]) / parent_placement[0]
    return _Affine(
        a=result.a * factor,
        b=result.b * factor,
        c=result.c * factor,
        d=result.d * factor,
        tx=result.a * dx + result.c * dy + result.tx,
        ty=result.b * dx + result.d * dy + result.ty,
    )


def _pass_through_parts(
    parts_by_index: Mapping[int, Any],
    context: _ConversionContext,
    *,
    keep: set[int | None],
) -> dict[int, int]:
    """Parts that hold nothing of their own and place exactly one child at the origin.

    Some drawings nest a part inside dozens of such parts. Written as blocks they give
    block references as many levels deep, more than drawing programs and viewers follow,
    while the geometry sits where the innermost part puts it. A placement of such a part
    names the part behind it instead (``_behind_pass_through``) and the part is not
    written as a block. A part whose placement of the child moves, turns or scales it is
    kept: skipping it would need the two placements combined. The scale the empty part is
    drawn with does not matter: each part states where it lies on the sheet by itself
    (``_with_part_placements``). The top part and the sheets are kept too.
    """
    result: dict[int, int] = {}
    for part_index, part in parts_by_index.items():
        if part_index in keep:
            continue
        if getattr(part, "entities", ()) or getattr(part, "annotations", ()):
            continue
        instances = tuple(getattr(getattr(part, "assembly", None), "instances", ()))
        if len(instances) != 1:
            continue
        edge = (part_index, 0)
        if edge in context.invalid_instance_edges:
            continue
        transform = context.instance_transforms.get(edge)
        target_index = getattr(instances[0], "target_part_index", None)
        if transform is None or target_index is None or not _is_identity(transform):
            continue
        result[part_index] = int(target_index)
    return result


def _behind_pass_through(
    part_index: int, context: _ConversionContext
) -> tuple[int, list[int]]:
    """The part a placement of ``part_index`` draws, and the pass-through parts skipped."""
    skipped: list[int] = []
    while part_index in context.pass_through and part_index not in skipped:
        skipped.append(part_index)
        part_index = context.pass_through[part_index]
    return part_index, skipped


def _is_identity(transform: Any) -> bool:
    return all(
        math.isclose(float(actual), expected, abs_tol=_EPSILON)
        for actual, expected in (
            (transform.a, 1.0),
            (transform.b, 0.0),
            (transform.c, 0.0),
            (transform.d, 1.0),
            (transform.tx, 0.0),
            (transform.ty, 0.0),
        )
    )


def _classify_instance_edges(
    parts_by_index: Mapping[int, Any], context: _ConversionContext
) -> None:
    state: dict[int, int] = {}

    def reject(
        part: Any,
        assembly: Any,
        instance_index: int,
        reason: str,
    ) -> None:
        edge = (int(getattr(part, "index")), instance_index)
        if context.options.strict:
            raise ImporterError(
                f"Invalid MI assembly instance {getattr(assembly, 'id', '?')}:"
                f"{instance_index}: {reason}"
            )
        context.invalid_instance_edges.add(edge)
        context.skipped_counts["ASSE_INSTANCE"] += 1
        context.diagnostics.append(
            ImportDiagnostic(
                code="MI_INSTANCE_SKIPPED",
                severity="error",
                message=(
                    f"Skipped MI assembly instance {getattr(assembly, 'id', '?')}:"
                    f"{instance_index}: {reason}."
                ),
                source_id=f"{getattr(assembly, 'id', '?')}:{instance_index}",
                source_kind="ASSE_INSTANCE",
                action="skipped",
                details=_record_details(assembly),
            )
        )

    def visit(part_index: int) -> None:
        state[part_index] = 1
        part = parts_by_index[part_index]
        assembly = getattr(part, "assembly", None)
        if assembly is not None:
            for instance_index, instance in enumerate(
                getattr(assembly, "instances", ())
            ):
                edge = (part_index, instance_index)
                target_index = getattr(instance, "target_part_index", None)
                if target_index is None or target_index not in parts_by_index:
                    reject(part, assembly, instance_index, "target part is unresolved")
                    continue
                if state.get(target_index) == 1:
                    reject(part, assembly, instance_index, "assembly cycle detected")
                    continue
                try:
                    transform = instance.to_affine2d()
                except (AttributeError, TypeError, ValueError) as exc:
                    reject(
                        part,
                        assembly,
                        instance_index,
                        f"invalid affine transform ({exc})",
                    )
                    continue
                context.instance_transforms[edge] = transform
                if state.get(target_index, 0) == 0:
                    visit(int(target_index))
        state[part_index] = 2

    for part_index in sorted(parts_by_index):
        if state.get(part_index, 0) == 0:
            visit(part_index)


def _decompose_insert_transform(transform: Any) -> tuple[float, float, float] | None:
    a = _finite(transform.a, "transform a")
    b = _finite(transform.b, "transform b")
    c = _finite(transform.c, "transform c")
    d = _finite(transform.d, "transform d")
    scale_x = math.hypot(a, b)
    if scale_x <= _EPSILON:
        return None
    angle = math.atan2(b, a)
    scale_y = (a * d - b * c) / scale_x
    if abs(scale_y) <= _EPSILON:
        return None
    expected_c = -math.sin(angle) * scale_y
    expected_d = math.cos(angle) * scale_y
    if not (
        math.isclose(c, expected_c, rel_tol=1e-9, abs_tol=_EPSILON)
        and math.isclose(d, expected_d, rel_tol=1e-9, abs_tol=_EPSILON)
    ):
        return None
    return math.degrees(angle), scale_x, scale_y


def _build_hatch_loop_roles(mi_document: Any) -> dict[int, dict[int, bool]]:
    result: dict[int, dict[int, bool]] = {}
    for association in getattr(mi_document, "hatch_associations", ()):
        hatch_id = int(getattr(association, "hatch_id"))
        roles = result.setdefault(hatch_id, {})
        roles[int(getattr(association, "outer_loop_id"))] = True
        roles.update(
            {
                int(loop_id): False
                for loop_id in getattr(association, "inner_loop_ids", ())
            }
        )
    return result


def _forward_parser_diagnostics(mi_document: Any, context: _ConversionContext) -> None:
    for diagnostic in getattr(mi_document, "diagnostics", ()):
        span = getattr(diagnostic, "span", None)
        details: dict[str, Any] = {
            "upstream_code": str(getattr(diagnostic, "code", "unknown")),
        }
        if span is not None:
            details["span"] = _span_dict(span)
        upstream_action = getattr(diagnostic, "action", None)
        if upstream_action is not None:
            details["upstream_action"] = str(upstream_action)
        severity = str(getattr(diagnostic, "severity", "warning"))
        if severity not in {"info", "warning", "error"}:
            severity = "warning"
        context.diagnostics.append(
            ImportDiagnostic(
                code="MI_PARSER_DIAGNOSTIC",
                severity=severity,  # type: ignore[arg-type]
                message=(
                    f"ezmi2d [{details['upstream_code']}]: "
                    f"{getattr(diagnostic, 'message', '')}"
                ),
                action="forwarded",
                details=details,
            )
        )


def _report_unsupported_entities(mi_document: Any, context: _ConversionContext) -> None:
    for entity in getattr(mi_document, "unsupported_entities", ()):
        source_kind = _kind(entity)
        source_id = str(getattr(entity, "id", "?"))
        context.skipped_counts[source_kind] += 1
        context.diagnostics.append(
            ImportDiagnostic(
                code="MI_UNSUPPORTED_ENTITY",
                severity="warning",
                message=(
                    f"MI {source_kind} entity {source_id} has no typed ezmi2d "
                    "mapping and was skipped."
                ),
                source_id=source_id,
                source_kind=source_kind,
                action="skipped",
                details=_record_details(entity),
            )
        )


def _report_unhandled_annotations(
    mi_document: Any, context: _ConversionContext
) -> None:
    for annotation in getattr(mi_document, "annotations", ()):
        source_id = int(getattr(annotation, "id"))
        if source_id in context.handled_annotation_ids:
            continue
        if _kind(annotation) == "DTV":
            context.skipped_counts["DTV"] += 1
            context.diagnostics.append(
                ImportDiagnostic(
                    code="MI_UNSUPPORTED_ANNOTATION",
                    severity="warning",
                    message=(
                        f"Standalone MI tolerance {source_id} was not referenced by "
                        "a converted dimension and was skipped."
                    ),
                    source_id=str(source_id),
                    source_kind="DTV",
                    action="skipped",
                    details=_record_details(annotation),
                )
            )
        else:
            _diagnose_unsupported_annotation(annotation, context)


def _diagnose_unsupported_annotation(entity: Any, context: _ConversionContext) -> None:
    source_id = int(getattr(entity, "id"))
    context.handled_annotation_ids.add(source_id)
    source_kind = _kind(entity)
    context.skipped_counts[source_kind] += 1
    context.diagnostics.append(
        ImportDiagnostic(
            code="MI_UNSUPPORTED_ANNOTATION",
            severity="warning",
            message=(
                f"MI {source_kind} annotation {source_id} has no lossless IR "
                "equivalent and was skipped; referenced graphics remain in their parts."
            ),
            source_id=str(source_id),
            source_kind=source_kind,
            action="skipped",
            details=_record_details(entity),
        )
    )


def _append_preserved_semantics_diagnostic(context: _ConversionContext) -> None:
    if not context.preserved_semantics:
        return
    context.diagnostics.append(
        ImportDiagnostic(
            code="MI_SOURCE_SEMANTICS_PRESERVED",
            severity="warning",
            message=(
                "MI source semantics without an exact operational IR field were "
                "preserved in metadata."
            ),
            action="preserved_metadata",
            details={"counts": dict(sorted(context.preserved_semantics.items()))},
        )
    )


def _resolve_units(
    mi_document: Any, context: _ConversionContext
) -> tuple[str, str | None]:
    global_info = getattr(mi_document, "global_info", None)
    source_unit = None if global_info is None else getattr(global_info, "unit", None)
    if source_unit is None or not str(source_unit).strip():
        return "unknown", None
    normalized = str(source_unit).strip().upper()
    units = _UNIT_MAP.get(normalized)
    if units is not None:
        return units, str(source_unit)
    context.diagnostics.append(
        ImportDiagnostic(
            code="MI_UNKNOWN_UNITS",
            severity="warning",
            message=f"MI unit {source_unit!r} has no CAD 2D IR unit mapping.",
            action="preserved_metadata",
            details={"source_unit": str(source_unit)},
        )
    )
    return "unknown", str(source_unit)


def _build_header(
    mi_document: Any,
    *,
    units: str,
    source_unit: str | None,
) -> dict[str, Any]:
    global_info = getattr(mi_document, "global_info", None)
    header: dict[str, Any] = {
        "units": units,
        "angle_unit": "deg",
        "coord_space": "world",
        "metadata": {
            "mi": {
                "source_unit": source_unit,
                "source_angle_unit": None
                if global_info is None
                else getattr(global_info, "angle_unit", None),
                "angles_normalized_to_degrees": True,
                "drawing_scale": None
                if global_info is None
                else getattr(global_info, "drawing_scale", None),
                "global_transform": None
                if global_info is None
                else list(getattr(global_info, "transform_values", ()) or ()),
            }
        },
    }
    extents = None if global_info is None else getattr(global_info, "extents", None)
    if extents is not None:
        minimum = _point(getattr(extents, "min"), "drawing extent minimum")
        maximum = _point(getattr(extents, "max"), "drawing extent maximum")
        if minimum[0] <= maximum[0] and minimum[1] <= maximum[1]:
            header["bbox"] = {"min": minimum, "max": maximum}
    return header


def _build_source(
    mi_document: Any,
    *,
    source_name: str | None,
    source_sha256: str | None,
    parser_version: str | None,
    requested_encoding: str,
) -> dict[str, Any]:
    global_info = getattr(mi_document, "global_info", None)
    encoding_info = getattr(mi_document, "encoding_info", None)
    raw = getattr(mi_document, "raw", None)
    format_info = None if raw is None else getattr(raw, "format", None)
    newlines = None if raw is None else getattr(raw, "newlines", None)
    source: dict[str, Any] = {
        "format": "mi",
        "version": str(
            "unknown"
            if global_info is None or getattr(global_info, "version", None) is None
            else getattr(global_info, "version")
        ),
        "metadata": {
            "mi": {
                "parser": "ezmi2d",
                "parser_version": parser_version,
                "requested_encoding": requested_encoding,
                "encoding": None
                if encoding_info is None
                else {
                    "name": getattr(encoding_info, "name", None),
                    "source": getattr(encoding_info, "source", None),
                    "declared_name": getattr(encoding_info, "declared_name", None),
                },
                "container": {
                    "format_kind": None
                    if format_info is None
                    else getattr(format_info, "kind", None),
                    "compression": None
                    if format_info is None
                    else getattr(format_info, "compression", None),
                    "first_section": None
                    if format_info is None
                    else getattr(format_info, "first_section", None),
                    "utf8_bom": False
                    if format_info is None
                    else bool(getattr(format_info, "utf8_bom", False)),
                    "container_size": None
                    if raw is None
                    else getattr(raw, "container_size", None),
                    "logical_size": None
                    if raw is None
                    else getattr(raw, "source_size", None),
                    "trailing_bytes": None
                    if raw is None
                    else getattr(raw, "trailing_bytes", None),
                    "termination": None
                    if raw is None
                    else getattr(raw, "termination", None),
                    "newlines": None
                    if newlines is None
                    else {
                        "lf": getattr(newlines, "lf", 0),
                        "crlf": getattr(newlines, "crlf", 0),
                        "cr": getattr(newlines, "cr", 0),
                        "unterminated": getattr(newlines, "unterminated", 0),
                    },
                },
                "drawing": _global_metadata(global_info),
                "toc_last_entity": getattr(mi_document, "toc_last_entity", None),
                "top_part_index": getattr(mi_document, "top_part_index", None),
                "root_part_indices": list(
                    getattr(mi_document, "root_part_indices", ())
                ),
                "sheet_part_indices": list(
                    getattr(mi_document, "sheet_part_indices", ())
                ),
                "part_count": len(tuple(getattr(mi_document, "parts", ()))),
                "record_count": 0
                if raw is None
                else len(tuple(getattr(raw, "records", ()))),
            }
        },
    }
    if source_name is not None:
        source["name"] = source_name
    if source_sha256 is not None:
        source["sha256"] = source_sha256
    return source


def _global_metadata(global_info: Any) -> dict[str, Any] | None:
    if global_info is None:
        return None
    extents = getattr(global_info, "extents", None)
    return {
        "section_index": getattr(global_info, "section_index", None),
        "name": getattr(global_info, "drawing_name", None),
        "creation_date": getattr(global_info, "creation_date", None),
        "creation_time": getattr(global_info, "creation_time", None),
        "producer": getattr(global_info, "producer", None),
        "version": getattr(global_info, "version", None),
        "dimension": getattr(global_info, "dimension", None),
        "extents": None
        if extents is None
        else {
            "min": _point(getattr(extents, "min"), "drawing extent minimum"),
            "max": _point(getattr(extents, "max"), "drawing extent maximum"),
        },
        "paper_size": getattr(global_info, "paper_size", None),
        "drawing_scale": getattr(global_info, "drawing_scale", None),
        "unit": getattr(global_info, "unit", None),
        "angle_unit": getattr(global_info, "angle_unit", None),
        "transform_values": list(getattr(global_info, "transform_values", ()) or ()),
    }


def _parts_by_index(parts: Sequence[Any]) -> dict[int, Any]:
    result: dict[int, Any] = {}
    for part in parts:
        part_index = int(getattr(part, "index"))
        if part_index in result:
            raise ImporterError(
                f"MI document contains duplicate part index {part_index}"
            )
        result[part_index] = part
    return result


def _resolve_top_part_index(
    mi_document: Any, parts_by_index: Mapping[int, Any]
) -> int | None:
    top_part_index = getattr(mi_document, "top_part_index", None)
    if top_part_index in parts_by_index:
        return int(top_part_index)
    root_indices = tuple(getattr(mi_document, "root_part_indices", ()))
    if len(root_indices) == 1 and root_indices[0] in parts_by_index:
        return int(root_indices[0])
    return None


def _build_block_names(parts: Sequence[Any]) -> dict[int, str]:
    result: dict[int, str] = {}
    used: set[str] = set()
    for part in sorted(parts, key=lambda item: int(getattr(item, "index"))):
        part_index = int(getattr(part, "index"))
        raw_name = str(getattr(part, "name", "") or "").strip()
        safe_name = re.sub(r"[^\w.\-]+", "_", raw_name, flags=re.UNICODE).strip("_")
        candidate = f"MI_PART_{part_index}"
        if safe_name:
            candidate += f"_{safe_name}"
        unique = candidate
        suffix = 2
        while unique.casefold() in used:
            unique = f"{candidate}_{suffix}"
            suffix += 1
        used.add(unique.casefold())
        result[part_index] = unique
    return result


def _text_style_name(font: str, context: _ConversionContext) -> str:
    safe_font = re.sub(r"[^\w.\-]+", "_", font, flags=re.UNICODE).strip("_")
    candidate = f"MI_{safe_font}"
    if candidate == "MI_":
        candidate = "MI_DEFAULT"
    name = candidate
    suffix = 2
    while name in context.text_styles and context.text_styles[name].get("font") != font:
        name = f"{candidate}_{suffix}"
        suffix += 1
    context.text_styles.setdefault(name, {"font": font})
    return name


def _span_dict(span: Any) -> dict[str, int]:
    return {
        "offset": int(getattr(span, "offset")),
        "length": int(getattr(span, "length")),
        "start_line": int(getattr(span, "start_line")),
        "end_line": int(getattr(span, "end_line")),
    }


def _point(value: Any, label: str) -> list[float]:
    if value is None:
        raise LookupError(f"{label} is unresolved")
    return [
        _finite(getattr(value, "x"), f"{label} x"),
        _finite(getattr(value, "y"), f"{label} y"),
    ]


def _finite(value: Any, label: str) -> float:
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise TypeError(f"{label} must be numeric")
    result = float(value)
    if not math.isfinite(result):
        raise ValueError(f"{label} must be finite")
    return result


def _positive(value: Any, label: str) -> float:
    result = _finite(value, label)
    if result <= 0.0:
        raise ValueError(f"{label} must be positive")
    return result


def _optional_positive(value: Any) -> float | None:
    """``value`` as a positive finite number, or ``None`` when it is not one."""
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        return None
    result = float(value)
    return result if math.isfinite(result) and result > 0.0 else None


def _kind(entity: Any) -> str:
    return str(getattr(entity, "mi_type", type(entity).__name__)).upper()


def _distance(first: Sequence[float], second: Sequence[float]) -> float:
    return math.hypot(
        float(second[0]) - float(first[0]), float(second[1]) - float(first[1])
    )


def _points_close(first: Sequence[float], second: Sequence[float]) -> bool:
    return _distance(first, second) <= _EPSILON


def _deduplicate_adjacent(vertices: Iterable[Sequence[float]]) -> list[list[float]]:
    result: list[list[float]] = []
    for vertex in vertices:
        point = [float(vertex[0]), float(vertex[1])]
        if not result or not _points_close(result[-1], point):
            result.append(point)
    return result


def _optional_hex(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, bytes):
        raise TypeError("MI relation value must be bytes")
    return value.hex()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


__all__ = ["convert_mi_file_to_ir", "mi_document_to_ir"]
