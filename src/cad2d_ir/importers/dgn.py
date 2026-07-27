"""Native MicroStation V7 DGN to CAD 2D IR importer backed by :mod:`ezdgn`."""

from __future__ import annotations

import codecs
from collections import Counter
from dataclasses import dataclass, field
import hashlib
import math
from pathlib import Path
import re
from typing import Any, Iterable, Sequence

from cad2d_ir.importers.base import (
    ImportDiagnostic,
    ImporterError,
    ImportOptions,
    ImportResult,
    MissingOptionalDependencyError,
)
from cad2d_ir.schema import validate_ir

_EPSILON = 1.0e-12
_COMPLEX_KINDS = {"COMPLEX_CHAIN", "COMPLEX_SHAPE", "TEXT_NODE"}
_AUTO_TEXT_ENCODINGS = ("ascii", "cp932", "latin-1")
# V7 control, group-data, tag, and application records are not drawables.
_NON_GRAPHIC_ELEMENT_TYPES = frozenset({5, 8, 9, 10, 37, 66})
_UNIT_MAP = {
    '"': "inch",
    "'": "ft",
    "mm": "mm",
    "millimeter": "mm",
    "millimeters": "mm",
    "cm": "cm",
    "centimeter": "cm",
    "centimeters": "cm",
    "m": "m",
    "meter": "m",
    "meters": "m",
    "in": "inch",
    "inch": "inch",
    "inches": "inch",
    "ft": "ft",
    "foot": "ft",
    "feet": "ft",
}


@dataclass(slots=True)
class _ConversionContext:
    drawing: Any
    options: ImportOptions
    text_encoding: str
    text_encoding_source: str
    diagnostics: list[ImportDiagnostic] = field(default_factory=list)
    layers: dict[str, dict[str, Any]] = field(default_factory=dict)
    linetypes: dict[str, dict[str, Any]] = field(default_factory=dict)
    text_styles: dict[str, dict[str, Any]] = field(default_factory=dict)
    blocks: dict[str, dict[str, Any]] = field(default_factory=dict)
    skipped_counts: Counter[str] = field(default_factory=Counter)
    approximation_counts: Counter[str] = field(default_factory=Counter)
    flattened_counts: Counter[str] = field(default_factory=Counter)
    next_entity_number: int = 1

    def allocate_id(self) -> str:
        entity_id = f"DGN_E{self.next_entity_number:08d}"
        self.next_entity_number += 1
        return entity_id


def convert_dgn_file_to_ir(
    path: str | Path,
    *,
    options: ImportOptions | None = None,
) -> ImportResult:
    """Read a V7 2D DGN file with ``ezdgn`` and convert it directly to IR."""
    try:
        import ezdgn
    except ImportError as exc:
        raise MissingOptionalDependencyError(
            "DGN support requires the optional dependency ezdgn; "
            'install it with `pip install "cad2d-ir[dgn]"`.'
        ) from exc

    source_path = Path(path)
    try:
        drawing = ezdgn.readfile(source_path)
    except Exception as exc:
        raise ImporterError(f"Failed to read DGN file {source_path}: {exc}") from exc
    return dgn_drawing_to_ir(
        drawing,
        source_name=source_path.name,
        source_sha256=_sha256_file(source_path),
        parser_version=str(getattr(ezdgn, "__version__", "unknown")),
        options=options,
    )


def dgn_drawing_to_ir(
    drawing: Any,
    *,
    source_name: str | None = None,
    source_sha256: str | None = None,
    parser_version: str | None = None,
    options: ImportOptions | None = None,
) -> ImportResult:
    """Convert an ``ezdgn.Drawing``-compatible object directly to IR."""
    import_options = options or ImportOptions()

    try:
        source_entities = tuple(drawing.entities)
        all_source_entities = _all_entities(drawing)
    except Exception as exc:
        raise ImporterError(f"Failed to enumerate DGN entities: {exc}") from exc

    text_samples = _dgn_text_samples(all_source_entities)
    try:
        selected_encoding, encoding_source = _select_dgn_text_encoding(
            text_samples, import_options.encoding
        )
    except LookupError as exc:
        raise ImporterError(
            f"Unknown DGN text encoding: {import_options.encoding!r}"
        ) from exc
    context = _ConversionContext(
        drawing=drawing,
        options=import_options,
        text_encoding=selected_encoding,
        text_encoding_source=encoding_source,
    )
    if import_options.encoding.strip().lower() == "auto" and text_samples:
        context.diagnostics.append(
            ImportDiagnostic(
                code="DGN_ENCODING_DETECTED",
                severity="info",
                message=(
                    f"DGN text encoding selected as {selected_encoding} "
                    f"({encoding_source})."
                ),
                action="detected",
                details={
                    "encoding": selected_encoding,
                    "source": encoding_source,
                    "text_elements_probed": len(text_samples),
                },
            )
        )

    raw_scan = drawing.raw_scan
    format_info = raw_scan.format
    dimension = getattr(format_info, "dimension", None)
    if _is_three_dimensional(dimension):
        context.diagnostics.append(
            ImportDiagnostic(
                code="DGN_3D_FLATTENED",
                severity="warning",
                message=(
                    "Projected 3D DGN geometry to the IR XY plane; "
                    "Z coordinates are not represented."
                ),
                action="projected",
                details={
                    "source_dimension": dimension,
                    "target_dimension": 2,
                    "projection_plane": "xy",
                },
            )
        )

    entities = _convert_sequence(source_entities, context)
    _collect_unsupported_graphics(context)
    _append_summary_diagnostics(context)

    settings = drawing.design_settings
    unit_name = getattr(settings, "master_unit_name", None)
    units = _UNIT_MAP.get(str(unit_name).strip().casefold(), "unknown")
    if units == "unknown":
        context.diagnostics.append(
            ImportDiagnostic(
                code="DGN_UNKNOWN_UNITS",
                severity="warning",
                message=(
                    f"DGN master unit {unit_name!r} has no IR units mapping; "
                    "header units remain 'unknown'."
                ),
                action="preserved_metadata",
            )
        )

    source: dict[str, Any] = {
        "format": "dgn",
        "version": str(getattr(format_info, "kind", "V7")),
        "metadata": {
            "encoding": selected_encoding,
            "encoding_source": encoding_source,
            "dgn": {
                "parser_version": parser_version,
                "dimension": dimension,
                "master_unit": unit_name,
                "sub_unit": getattr(settings, "sub_unit_name", None),
                "uor_per_master": getattr(settings, "uor_per_master", None),
                "subunits_per_master": getattr(settings, "subunits_per_master", None),
                "uor_per_subunit": getattr(settings, "uor_per_subunit", None),
                "record_count": len(getattr(raw_scan, "records", ())),
                "termination": getattr(raw_scan, "termination", None),
                "trailing_bytes": getattr(raw_scan, "trailing_bytes", None),
                "active_color_table_index": getattr(
                    drawing, "active_color_table_index", None
                ),
            },
        },
    }
    if source_name is not None:
        source["name"] = source_name
    if source_sha256 is not None:
        source["sha256"] = source_sha256

    tables: dict[str, Any] = {
        "layers": context.layers,
        "linetypes": context.linetypes,
        "text_styles": context.text_styles,
    }
    if context.blocks:
        tables["blocks"] = context.blocks

    document: dict[str, Any] = {
        "format": "cad2d-ir",
        "version": import_options.ir_version,
        "header": {
            "units": units,
            "angle_unit": "deg",
            "coord_space": "world",
            "metadata": {
                "dgn": {
                    "coordinate_source": (
                        "master" if getattr(settings, "scale", None) else "uor"
                    ),
                    "master_unit": unit_name,
                }
            },
        },
        "source": source,
        "tables": tables,
        "entities": entities,
    }
    if import_options.validate:
        validate_ir(document)

    block_entities = [
        entity
        for block in context.blocks.values()
        for entity in block.get("entities", [])
    ]
    converted = [*entities, *block_entities]
    source_kinds = Counter(_kind(entity) for entity in _all_entities(drawing))
    converted_kinds = Counter(str(entity["kind"]) for entity in converted)
    statistics: dict[str, Any] = {
        "source_format": "dgn",
        "source_version": str(getattr(format_info, "kind", "V7")),
        "encoding": selected_encoding,
        "encoding_source": encoding_source,
        "source_elements": len(getattr(drawing, "elements", ())),
        "source_entities": len(_all_entities(drawing)),
        "source_entity_counts": dict(sorted(source_kinds.items())),
        "converted_entities": len(entities),
        "converted_block_entities": len(block_entities),
        "converted_entity_counts": dict(sorted(converted_kinds.items())),
        "converted_blocks": len(context.blocks),
        "skipped_entities": sum(context.skipped_counts.values()),
        "skipped_entity_counts": dict(sorted(context.skipped_counts.items())),
        "approximated_entities": sum(context.approximation_counts.values()),
        "flattened_complex_elements": sum(context.flattened_counts.values()),
    }
    return ImportResult(
        document=document,
        diagnostics=context.diagnostics,
        statistics=statistics,
    )


def _convert_sequence(
    source_entities: Iterable[Any], context: _ConversionContext
) -> list[dict[str, Any]]:
    converted: list[dict[str, Any]] = []
    for source_entity in source_entities:
        converted.extend(_convert_entity_safe(source_entity, context))
    return converted


def _convert_entity_safe(
    source_entity: Any, context: _ConversionContext
) -> list[dict[str, Any]]:
    source_kind = _kind(source_entity)
    source_id = _source_id(source_entity)
    if bool(getattr(getattr(source_entity, "record", None), "deleted", False)):
        context.skipped_counts[source_kind] += 1
        return []
    try:
        return _convert_entity(source_entity, context)
    except (AttributeError, KeyError, TypeError, ValueError, OverflowError) as exc:
        if context.options.strict:
            raise ImporterError(
                f"Failed to convert DGN {source_kind} at record {source_id}: {exc}"
            ) from exc
        context.skipped_counts[source_kind] += 1
        context.diagnostics.append(
            ImportDiagnostic(
                code="DGN_ENTITY_CONVERSION_FAILED",
                severity="error",
                message=f"Failed to convert DGN {source_kind}: {exc}",
                source_id=source_id,
                source_kind=source_kind,
                action="skipped",
            )
        )
        return []


def _convert_entity(
    source_entity: Any, context: _ConversionContext
) -> list[dict[str, Any]]:
    kind = _kind(source_entity)
    common = _entity_common(source_entity, context)

    if kind == "LINE":
        return [
            {
                **common,
                "kind": "LINE",
                "p1": _point_from_attrs(
                    source_entity, "start_master", "start_uor_precise", "start_uor"
                ),
                "p2": _point_from_attrs(
                    source_entity, "end_master", "end_uor_precise", "end_uor"
                ),
            }
        ]

    if kind in {"LINE_STRING", "SHAPE"}:
        points = _vertices(source_entity)
        closed = kind == "SHAPE"
        if closed:
            points = _without_repeated_closure(points)
        if len(points) < (3 if closed else 2):
            raise ValueError(f"{kind} has too few vertices")
        if closed and _has_fill(source_entity):
            common = _with_fill_color(common, source_entity)
            return [
                {
                    **common,
                    "kind": "HATCH",
                    "solid": True,
                    "loops": [{"vertices": points, "is_outer": True}],
                }
            ]
        return [
            {
                **common,
                "kind": "LWPOLYLINE",
                "vertices": points,
                "closed": closed,
            }
        ]

    if kind in {"ELLIPSE", "ARC"}:
        return [_convert_ellipse(source_entity, common, kind)]

    if kind == "CURVE":
        points = _vertices(source_entity)
        if len(points) < 2:
            raise ValueError("CURVE has too few control points")
        context.approximation_counts[kind] += 1
        context.diagnostics.append(
            ImportDiagnostic(
                code="DGN_CURVE_APPROXIMATED",
                severity="warning",
                message="DGN type-11 curve was represented by its control polyline.",
                source_id=_source_id(source_entity),
                source_kind=kind,
                action="approximated",
                details={"control_points": len(points)},
            )
        )
        return [
            {
                **common,
                "kind": "LWPOLYLINE",
                "vertices": points,
                "closed": False,
                "approximation": {
                    "method": "polyline",
                    "source_kind": "DGN_CURVE",
                    "segments": len(points) - 1,
                },
            }
        ]

    if kind == "TEXT":
        return [_convert_text(source_entity, common, context)]

    if kind == "CELL":
        return [_convert_cell(source_entity, common, context)]

    if kind == "BSPLINE_CURVE":
        return [_convert_bspline(source_entity, common, context)]

    if kind in _COMPLEX_KINDS:
        context.flattened_counts[kind] += 1
        return _convert_sequence(_children(context, source_entity), context)

    context.skipped_counts[kind] += 1
    return []


def _convert_ellipse(
    source_entity: Any, common: dict[str, Any], source_kind: str
) -> dict[str, Any]:
    center = _point_from_attrs(source_entity, "center_master", "center_uor")
    primary = _positive_axis(source_entity, "primary_axis_master", "primary_axis_uor")
    secondary = _positive_axis(
        source_entity, "secondary_axis_master", "secondary_axis_uor"
    )
    rotation = float(getattr(source_entity, "rotation_degrees", 0.0))
    start = float(getattr(source_entity, "start_angle_degrees", 0.0))
    sweep = (
        float(getattr(source_entity, "sweep_angle_degrees"))
        if source_kind == "ARC"
        else 360.0
    )

    if source_kind == "ARC" and math.isclose(
        primary, secondary, rel_tol=1.0e-10, abs_tol=_EPSILON
    ):
        return {
            **common,
            "kind": "ARC",
            "center": center,
            "radius": primary,
            "start_angle": rotation + start,
            "end_angle": rotation + start + sweep,
            "ccw": sweep >= 0.0,
        }

    if source_kind == "ELLIPSE" and math.isclose(
        primary, secondary, rel_tol=1.0e-10, abs_tol=_EPSILON
    ):
        return {**common, "kind": "CIRCLE", "center": center, "radius": primary}

    if primary >= secondary:
        major_length = primary
        minor_length = secondary
        major_angle = rotation
        parameter_start = math.radians(start)
    else:
        major_length = secondary
        minor_length = primary
        major_angle = rotation + 90.0
        parameter_start = math.radians(start - 90.0)
    radians = math.radians(major_angle)
    parameter_sweep = math.radians(sweep)
    return {
        **common,
        "kind": "ELLIPSE",
        "center": center,
        "major_axis": [
            major_length * math.cos(radians),
            major_length * math.sin(radians),
        ],
        "ratio": minor_length / major_length,
        "start_param": parameter_start,
        "end_param": parameter_start + parameter_sweep,
        "ccw": sweep >= 0.0,
    }


def _convert_text(
    source_entity: Any,
    common: dict[str, Any],
    context: _ConversionContext,
) -> dict[str, Any]:
    raw = bytes(getattr(source_entity, "text_bytes"))
    selected_encoding = context.text_encoding
    try:
        text = raw.decode(selected_encoding)
    except (LookupError, UnicodeDecodeError) as exc:
        if context.options.strict:
            raise ValueError(
                f"text cannot be decoded as {selected_encoding}: {exc}"
            ) from exc
        try:
            text = raw.decode(selected_encoding, errors="replace")
        except LookupError as lookup_error:
            raise ValueError(
                f"unknown text encoding {selected_encoding!r}"
            ) from lookup_error
        context.diagnostics.append(
            ImportDiagnostic(
                code="DGN_TEXT_DECODE_REPLACED",
                severity="warning",
                message=(
                    f"DGN text bytes were not valid {selected_encoding}; "
                    "undecodable bytes were replaced."
                ),
                source_id=_source_id(source_entity),
                source_kind="TEXT",
                action="normalized",
                details={"encoding": selected_encoding, "raw_hex": raw.hex()},
            )
        )
    height = _positive_text_size(
        source_entity, "height_multiplier_master", "height_multiplier_raw"
    )
    width = _positive_text_size(
        source_entity, "length_multiplier_master", "length_multiplier_raw"
    )
    font_id = int(getattr(source_entity, "font_id", 0))
    justification = int(getattr(source_entity, "justification", 0))
    style_name = f"DGN_FONT_{font_id}"
    context.text_styles.setdefault(style_name, {"font": style_name, "height": height})
    metadata = common["metadata"]["dgn"]
    metadata.update(
        {
            "font_id": font_id,
            "justification": justification,
            "editable_fields": int(getattr(source_entity, "editable_fields", 0)),
            "text_encoding": selected_encoding,
            "text_raw_hex": raw.hex(),
        }
    )
    result: dict[str, Any] = {
        **common,
        "kind": "TEXT",
        "insert": _point_from_attrs(source_entity, "origin_master", "origin_uor"),
        "height": height,
        "rotation": float(getattr(source_entity, "rotation_degrees", 0.0)),
        "text": text,
        "style": style_name,
    }
    halign = _dgn_text_halign(justification)
    if halign is not None:
        result["halign"] = halign
    if height > _EPSILON and width > _EPSILON:
        result["width_factor"] = width / height
    return result


def _convert_cell(
    source_entity: Any,
    common: dict[str, Any],
    context: _ConversionContext,
) -> dict[str, Any]:
    source_id = _source_id(source_entity)
    raw_name = str(getattr(source_entity, "name", "CELL")) or "CELL"
    block_name = _unique_block_name(raw_name, source_id, context.blocks)
    origin = _point_from_attrs(source_entity, "origin_master", "origin_uor")
    transform = getattr(source_entity, "transform", ((1.0, 0.0), (0.0, 1.0)))
    placement_transform = [
        [float(transform[0][0]), float(transform[0][1])],
        [float(transform[1][0]), float(transform[1][1])],
    ]
    children = _convert_sequence(_children(context, source_entity), context)
    context.blocks[block_name] = {
        "base_point": origin,
        "entities": children,
        "metadata": {
            "dgn": {
                "cell_name": raw_name,
                "cell_class": int(getattr(source_entity, "cell_class", 0)),
                "source_record": source_id,
                "placement_transform": placement_transform,
                "component_coordinate_space": "design",
            }
        },
    }
    common["metadata"]["dgn"].update(
        {
            "cell_name": raw_name,
            "cell_class": int(getattr(source_entity, "cell_class", 0)),
            "placement_transform": placement_transform,
            "component_coordinate_space": "design",
        }
    )
    return {
        **common,
        "kind": "INSERT",
        "block": block_name,
        "insert": origin,
    }


def _convert_bspline(
    source_entity: Any,
    common: dict[str, Any],
    context: _ConversionContext,
) -> dict[str, Any]:
    children = tuple(_children(context, source_entity))
    pole = next((child for child in children if _kind(child) == "BSPLINE_POLE"), None)
    if pole is None:
        raise ValueError("B-spline curve has no pole record")
    points = _vertices(pole)
    if len(points) < 2:
        raise ValueError("B-spline curve has too few poles")
    knot = next((child for child in children if _kind(child) == "BSPLINE_KNOT"), None)
    weight = next(
        (child for child in children if _kind(child) == "BSPLINE_WEIGHT"), None
    )
    order = int(getattr(source_entity, "order", 2))
    degree = order - 1
    if not 1 <= degree <= 7:
        raise ValueError(f"B-spline degree {degree} is outside the IR range 1..7")
    closed = bool(getattr(source_entity, "is_closed", False))
    native_knots = (
        [float(value) for value in knot.values]
        if knot is not None and getattr(knot, "values", None)
        else []
    )
    native_weights = (
        [float(value) for value in weight.values]
        if weight is not None and getattr(weight, "values", None)
        else []
    )
    common["metadata"]["dgn"].update(
        {
            "order": order,
            "curve_type": int(getattr(source_entity, "curve_type", 0)),
            "properties": int(getattr(source_entity, "properties", 0)),
            "native_knots": native_knots,
            "native_weights": native_weights,
        }
    )
    result: dict[str, Any] = {
        **common,
        "kind": "SPLINE",
        "degree": degree,
        "control_points": points,
        "closed": closed,
    }
    if native_knots and not closed:
        expected_interior = len(points) - order
        if len(native_knots) != expected_interior:
            raise ValueError(
                "B-spline interior-knot count does not match poles and order"
            )
        result["knots"] = [0.0] * order + native_knots + [1.0] * order
    elif native_knots:
        context.approximation_counts["BSPLINE_CURVE"] += 1
        context.diagnostics.append(
            ImportDiagnostic(
                code="DGN_CURVE_APPROXIMATED",
                severity="warning",
                message=(
                    "Closed non-uniform DGN B-spline knots were retained in "
                    "metadata; the IR spline uses its closed uniform form."
                ),
                source_id=_source_id(source_entity),
                source_kind="BSPLINE_CURVE",
                action="approximated",
            )
        )
        result["approximation"] = {
            "method": "spline",
            "source_kind": "DGN_BSPLINE_CURVE",
            "metadata": {"reason": "closed_non_uniform_knots"},
        }
    if native_weights:
        if len(native_weights) != len(points) or not all(
            value > 0.0 for value in native_weights
        ):
            raise ValueError("B-spline weights must be positive and match the poles")
        result["weights"] = native_weights
    return result


def _entity_common(source_entity: Any, context: _ConversionContext) -> dict[str, Any]:
    level = int(getattr(source_entity, "level", 0))
    layer_name = f"DGN_LEVEL_{level}"
    style = getattr(source_entity, "style", None)
    color = _rgb_hex(getattr(style, "rgb", None)) if style is not None else None
    line_style = int(getattr(style, "line_style", 0)) if style is not None else 0
    linetype = f"DGN_STYLE_{line_style}"
    context.linetypes.setdefault(
        linetype,
        {
            "description": f"MicroStation V7 line-style index {line_style}",
            "pattern_mm": [],
        },
    )
    layer = context.layers.setdefault(
        layer_name,
        {
            "linetype": linetype,
            "metadata": {"dgn": {"level": level}},
        },
    )
    if color is not None:
        layer.setdefault("color", color)

    record = getattr(source_entity, "record", None)
    parent_index = getattr(source_entity, "parent_index", None)
    metadata: dict[str, Any] = {
        "record_index": getattr(record, "index", None),
        "record_offset": getattr(record, "offset", None),
        "element_type": getattr(record, "element_type", None),
        "level": level,
        "parent_record_index": parent_index,
        "line_style": line_style,
        "line_weight": int(getattr(style, "line_weight", 0)) if style else None,
        "color_index": int(getattr(style, "color_index", 0)) if style else None,
        "fill_color_index": getattr(style, "fill_color_index", None),
        "association_ids": list(getattr(source_entity, "association_ids", ())),
        "linkage_types": [
            str(
                getattr(linkage, "linkage_type_name", None)
                or getattr(linkage, "kind", "unknown")
            )
            for linkage in getattr(source_entity, "linkages", ())
        ],
    }
    result: dict[str, Any] = {
        "id": context.allocate_id(),
        "layer": layer_name,
        "linetype": linetype,
        "source": {
            "format": "dgn",
            "id": _source_id(source_entity),
            "kind": _kind(source_entity),
            "metadata": {
                "record_offset": getattr(record, "offset", None),
                "element_type": getattr(record, "element_type", None),
            },
        },
        "metadata": {"dgn": metadata},
    }
    if color is not None:
        result["color"] = color
    return result


def _collect_unsupported_graphics(context: _ConversionContext) -> None:
    for element in getattr(context.drawing, "unsupported_elements", ()):
        header = getattr(element, "common_header", None)
        record = getattr(element, "record", None)
        element_type = getattr(record, "element_type", None)
        if (
            header is None
            or bool(getattr(record, "deleted", False))
            or element_type in _NON_GRAPHIC_ELEMENT_TYPES
        ):
            continue
        context.skipped_counts[f"TYPE_{element_type or 'UNKNOWN'}"] += 1


def _append_summary_diagnostics(context: _ConversionContext) -> None:
    for kind, count in sorted(context.skipped_counts.items()):
        context.diagnostics.append(
            ImportDiagnostic(
                code="DGN_UNSUPPORTED_ENTITY",
                severity="warning",
                message=f"Skipped {count} unsupported DGN {kind} element(s).",
                source_kind=kind,
                action="skipped",
                details={"count": count},
            )
        )
    for kind, count in sorted(context.flattened_counts.items()):
        context.diagnostics.append(
            ImportDiagnostic(
                code="DGN_COMPLEX_FLATTENED",
                severity="warning",
                message=(
                    f"Flattened {count} DGN {kind} container(s) while retaining "
                    "parent record indexes on their children."
                ),
                source_kind=kind,
                action="flattened",
                details={"count": count},
            )
        )


def _all_entities(drawing: Any) -> tuple[Any, ...]:
    return tuple(getattr(drawing, "all_entities", getattr(drawing, "entities", ())))


def _dgn_text_samples(source_entities: Iterable[Any]) -> tuple[bytes, ...]:
    samples: list[bytes] = []
    for source_entity in source_entities:
        if _kind(source_entity) != "TEXT":
            continue
        try:
            samples.append(bytes(getattr(source_entity, "text_bytes")))
        except (AttributeError, TypeError, ValueError):
            # Leave malformed text handling to the normal per-entity conversion path.
            continue
    return tuple(samples)


def _select_dgn_text_encoding(
    samples: Sequence[bytes], requested: str
) -> tuple[str, str]:
    normalized = requested.strip().lower()
    if normalized != "auto":
        return codecs.lookup(normalized).name, "explicit"

    for candidate in _AUTO_TEXT_ENCODINGS:
        try:
            for sample in samples:
                sample.decode(candidate, errors="strict")
        except UnicodeDecodeError:
            continue
        if candidate == "latin-1":
            return candidate, "latin-1-fallback"
        return candidate, f"{candidate}-probe"
    raise AssertionError("latin-1 must decode every byte sequence")


def _is_three_dimensional(dimension: Any) -> bool:
    if dimension == 3:
        return True
    return str(dimension).strip().lower() in {"3", "3d"}


def _dgn_text_halign(justification: int) -> str | None:
    if 0 <= justification <= 5:
        return "left"
    if 6 <= justification <= 8:
        return "center"
    if 9 <= justification <= 14:
        return "right"
    return None


def _children(context: _ConversionContext, source_entity: Any) -> tuple[Any, ...]:
    children = getattr(context.drawing, "children", None)
    if not callable(children):
        return tuple(getattr(source_entity, "children", ()))
    return tuple(children(source_entity))


def _vertices(source_entity: Any) -> list[list[float]]:
    for name in ("vertices_master", "vertices_uor_precise", "vertices_uor"):
        values = getattr(source_entity, name, None)
        if values is not None:
            return [_point(value) for value in values]
    raise ValueError("entity has no vertex coordinates")


def _point_from_attrs(source_entity: Any, *names: str) -> list[float]:
    for name in names:
        value = getattr(source_entity, name, None)
        if value is not None:
            return _point(value)
    raise ValueError(f"entity has no coordinates in {names!r}")


def _point(value: Sequence[Any]) -> list[float]:
    if len(value) < 2:
        raise ValueError("point requires two coordinates")
    x, y = float(value[0]), float(value[1])
    if not math.isfinite(x) or not math.isfinite(y):
        raise ValueError("point coordinates must be finite")
    return [x, y]


def _positive_axis(source_entity: Any, master_name: str, raw_name: str) -> float:
    value = getattr(source_entity, master_name, None)
    if value is None:
        value = getattr(source_entity, raw_name, None)
    result = float(value)
    if not math.isfinite(result) or result <= 0.0:
        raise ValueError(f"{master_name} must be positive")
    return result


def _positive_text_size(source_entity: Any, master_name: str, raw_name: str) -> float:
    value = getattr(source_entity, master_name, None)
    if value is None:
        value = getattr(source_entity, raw_name, None)
    result = float(value or 0.0)
    return result if math.isfinite(result) and result > _EPSILON else 1.0


def _without_repeated_closure(points: list[list[float]]) -> list[list[float]]:
    if len(points) > 1 and points[0] == points[-1]:
        return points[:-1]
    return points


def _has_fill(source_entity: Any) -> bool:
    style = getattr(source_entity, "style", None)
    return style is not None and getattr(style, "fill_color_index", None) is not None


def _with_fill_color(common: dict[str, Any], source_entity: Any) -> dict[str, Any]:
    result = dict(common)
    style = getattr(source_entity, "style", None)
    fill = _rgb_hex(getattr(style, "fill_rgb", None)) if style is not None else None
    if fill is not None:
        result["color"] = fill
    return result


def _rgb_hex(value: Any) -> str | None:
    if value is None:
        return None
    red, green, blue = (max(0, min(255, int(component))) for component in value[:3])
    return f"#{red:02X}{green:02X}{blue:02X}"


def _kind(source_entity: Any) -> str:
    value = getattr(source_entity, "kind", None)
    if value is None:
        dxftype = getattr(source_entity, "dxftype", None)
        value = dxftype() if callable(dxftype) else type(source_entity).__name__
    return str(value).strip().upper().replace("-", "_").replace(" ", "_")


def _source_id(source_entity: Any) -> str:
    record = getattr(source_entity, "record", None)
    return str(getattr(record, "index", getattr(source_entity, "index", "unknown")))


def _unique_block_name(
    raw_name: str, source_id: str, blocks: dict[str, dict[str, Any]]
) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_.:\-]", "_", raw_name).strip("_") or "CELL"
    candidate = f"DGN_{cleaned}_{source_id}"
    suffix = 2
    while candidate in blocks:
        candidate = f"DGN_{cleaned}_{source_id}_{suffix}"
        suffix += 1
    return candidate


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
