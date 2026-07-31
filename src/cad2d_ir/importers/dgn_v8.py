"""Native MicroStation V8 DGN to CAD 2D IR importer backed by :mod:`ezdgn`."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
import math
from types import SimpleNamespace
from typing import Any, Iterable

from cad2d_ir.importers.base import (
    ImportDiagnostic,
    ImporterError,
    ImportOptions,
    ImportResult,
)
from cad2d_ir.importers.dgn import (
    _UNIT_MAP,
    _convert_ellipse,
    _unique_block_name,
    _without_repeated_closure,
)
from cad2d_ir.schema import validate_ir

_EPSILON = 1.0e-12
_COMPLEX_KINDS = {"COMPLEX_CHAIN", "COMPLEX_SHAPE"}
# V8 keeps the V7-compatible justification table (left/center/right columns
# including the margin variants, by top/center/bottom rows) and appends the
# descender codes 15..17. The stored V8 text origin is the justification-
# dependent user origin (unlike V7's fixed bottom-left corner), so both IR
# alignments derive from this code; confirmed against the ODA-authored GDAL
# fixture and the GDAL DGNv8 driver anchor mapping (kLeftTop=0).
_TEXT_ALIGNMENTS: dict[int, tuple[str, str]] = {
    0: ("left", "top"),
    1: ("left", "middle"),
    2: ("left", "bottom"),
    3: ("left", "top"),
    4: ("left", "middle"),
    5: ("left", "bottom"),
    6: ("center", "top"),
    7: ("center", "middle"),
    8: ("center", "bottom"),
    9: ("right", "top"),
    10: ("right", "middle"),
    11: ("right", "bottom"),
    12: ("right", "top"),
    13: ("right", "middle"),
    14: ("right", "bottom"),
    15: ("left", "bottom"),
    16: ("center", "bottom"),
    17: ("right", "bottom"),
}


@dataclass(slots=True)
class _V8ConversionContext:
    model: Any
    options: ImportOptions
    diagnostics: list[ImportDiagnostic] = field(default_factory=list)
    layers: dict[str, dict[str, Any]] = field(default_factory=dict)
    linetypes: dict[str, dict[str, Any]] = field(default_factory=dict)
    text_styles: dict[str, dict[str, Any]] = field(default_factory=dict)
    blocks: dict[str, dict[str, Any]] = field(default_factory=dict)
    skipped_counts: Counter[str] = field(default_factory=Counter)
    shared_cell_counts: Counter[str] = field(default_factory=Counter)
    approximation_counts: Counter[str] = field(default_factory=Counter)
    flattened_counts: Counter[str] = field(default_factory=Counter)
    text_encodings: set[str] = field(default_factory=set)
    next_entity_number: int = 1

    def allocate_id(self) -> str:
        entity_id = f"DGN_E{self.next_entity_number:08d}"
        self.next_entity_number += 1
        return entity_id


def dgn_v8_document_to_ir(
    document: Any,
    *,
    source_name: str | None = None,
    source_sha256: str | None = None,
    parser_version: str | None = None,
    options: ImportOptions | None = None,
) -> ImportResult:
    """Convert an ``ezdgn.V8Document``-compatible object directly to IR."""
    import_options = options or ImportOptions()

    models = tuple(getattr(document, "models", ()))
    if not models:
        raise ImporterError("V8 DGN document has no models")
    model = next(
        (candidate for candidate in models if tuple(candidate.entities)), models[0]
    )
    context = _V8ConversionContext(model=model, options=import_options)
    metadata_obj = model.metadata

    if len(models) > 1:
        skipped_models = [
            str(candidate.metadata.name)
            for candidate in models
            if candidate is not model
        ]
        context.diagnostics.append(
            ImportDiagnostic(
                code="DGN_V8_EXTRA_MODELS_SKIPPED",
                severity="warning",
                message=(
                    f"Converted V8 model {str(metadata_obj.name)!r} only; "
                    f"skipped {len(skipped_models)} other model(s)."
                ),
                action="skipped",
                details={
                    "converted_model": str(metadata_obj.name),
                    "model_count": len(models),
                    "skipped_models": skipped_models,
                },
            )
        )

    dimension = int(getattr(metadata_obj, "dimension", 2))
    if dimension == 3:
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

    entities = _convert_sequence(model.entities, context)
    _collect_unknown_elements(context)
    _append_summary_diagnostics(context)

    unit_name = getattr(metadata_obj, "master_unit", None)
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
        "version": "V8",
        "metadata": {
            "dgn": {
                "parser_version": parser_version,
                "dimension": dimension,
                "model_name": str(metadata_obj.name),
                "model_index": int(getattr(metadata_obj, "index", 0)),
                "model_count": len(models),
                "master_unit": unit_name,
                "sub_unit": getattr(metadata_obj, "sub_unit", None),
                "uor_per_master": float(getattr(metadata_obj, "uor_per_master", 0.0)),
                "global_origin_uor": [
                    float(value)
                    for value in getattr(metadata_obj, "global_origin_uor", ())
                ],
                "storage_path": getattr(metadata_obj, "storage_path", None),
            }
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

    document_ir: dict[str, Any] = {
        "format": "cad2d-ir",
        "version": import_options.ir_version,
        "header": {
            "units": units,
            "angle_unit": "deg",
            "coord_space": "world",
            "metadata": {
                "dgn": {"coordinate_source": "master", "master_unit": unit_name}
            },
        },
        "source": source,
        "tables": tables,
        "entities": entities,
    }
    if import_options.validate:
        validate_ir(document_ir)

    block_entities = [
        entity
        for block in context.blocks.values()
        for entity in block.get("entities", [])
    ]
    converted = [*entities, *block_entities]
    source_kinds = Counter(str(element.kind) for element in model.all_entities)
    converted_kinds = Counter(str(entity["kind"]) for entity in converted)
    skipped_counts = dict(context.skipped_counts)
    shared_cell_total = sum(context.shared_cell_counts.values())
    if shared_cell_total:
        skipped_counts["SHARED_CELL_INSTANCE"] = (
            skipped_counts.get("SHARED_CELL_INSTANCE", 0) + shared_cell_total
        )
    statistics: dict[str, Any] = {
        "source_format": "dgn",
        "source_version": "V8",
        "source_models": len(models),
        "converted_model": str(metadata_obj.name),
        "source_elements": len(getattr(model, "elements", ())),
        "source_entities": len(model.all_entities),
        "source_entity_counts": dict(sorted(source_kinds.items())),
        "converted_entities": len(entities),
        "converted_block_entities": len(block_entities),
        "converted_entity_counts": dict(sorted(converted_kinds.items())),
        "converted_blocks": len(context.blocks),
        "skipped_entities": sum(skipped_counts.values()),
        "skipped_entity_counts": dict(sorted(skipped_counts.items())),
        "approximated_entities": sum(context.approximation_counts.values()),
        "flattened_complex_elements": sum(context.flattened_counts.values()),
    }
    if context.text_encodings:
        statistics["text_encodings"] = sorted(context.text_encodings)
    return ImportResult(
        document=document_ir,
        diagnostics=context.diagnostics,
        statistics=statistics,
    )


def _convert_sequence(
    elements: Iterable[Any], context: _V8ConversionContext
) -> list[dict[str, Any]]:
    converted: list[dict[str, Any]] = []
    for element in elements:
        converted.extend(_convert_element_safe(element, context))
    return converted


def _convert_element_safe(
    element: Any, context: _V8ConversionContext
) -> list[dict[str, Any]]:
    source_kind = str(element.kind)
    source_id = _source_id(element)
    try:
        return _convert_element(element, context)
    except (AttributeError, KeyError, TypeError, ValueError, OverflowError) as exc:
        if context.options.strict:
            raise ImporterError(
                f"Failed to convert DGN {source_kind} at element {source_id}: {exc}"
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


def _convert_element(
    element: Any, context: _V8ConversionContext
) -> list[dict[str, Any]]:
    kind = str(element.kind)
    data = element.data

    if kind == "LINE":
        points = _vertices(data)
        if len(points) < 2:
            raise ValueError("LINE has too few vertices")
        common = _element_common(element, context)
        return [{**common, "kind": "LINE", "p1": points[0], "p2": points[1]}]

    if kind in {"LINE_STRING", "SHAPE"}:
        points = _vertices(data)
        closed = kind == "SHAPE"
        if closed:
            points = _without_repeated_closure(points)
        if len(points) < (3 if closed else 2):
            raise ValueError(f"{kind} has too few vertices")
        common = _element_common(element, context)
        # Fill and color-table linkages are not semantically decoded for V8
        # yet, so a SHAPE stays a closed polyline instead of a solid HATCH.
        return [{**common, "kind": "LWPOLYLINE", "vertices": points, "closed": closed}]

    if kind in {"ELLIPSE", "ARC"}:
        common = _element_common(element, context)
        return [_convert_ellipse(_ellipse_adapter(data, kind), common, kind)]

    if kind == "CURVE":
        points = _vertices(data)
        if len(points) < 2:
            raise ValueError("CURVE has too few control points")
        common = _element_common(element, context)
        context.approximation_counts[kind] += 1
        context.diagnostics.append(
            ImportDiagnostic(
                code="DGN_CURVE_APPROXIMATED",
                severity="warning",
                message="DGN type-11 curve was represented by its control polyline.",
                source_id=_source_id(element),
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
        return [_convert_text(element, context)]

    if kind == "POINT_STRING":
        return _convert_point_string(element, context)

    if kind == "CELL":
        return [_convert_cell(element, context)]

    if kind == "SHARED_CELL_INSTANCE":
        context.shared_cell_counts[str(data.name or "")] += 1
        return []

    if kind == "BSPLINE_CURVE":
        return [_convert_bspline(element, context)]

    if kind in _COMPLEX_KINDS:
        context.flattened_counts[kind] += 1
        return _convert_sequence(context.model.children(element), context)

    context.skipped_counts[kind] += 1
    return []


def _convert_text(element: Any, context: _V8ConversionContext) -> dict[str, Any]:
    data = element.data
    text = data.text
    if text is None:
        raise ValueError("TEXT has no decoded string")
    if data.origin is None:
        raise ValueError("TEXT has no origin")
    height = _positive_size(data.height_master)
    width = _positive_size(data.width_master)
    font_id = int(data.font_id or 0)
    justification = int(data.justification or 0)
    halign, valign = _TEXT_ALIGNMENTS.get(justification, ("left", "baseline"))
    style_name = f"DGN_FONT_{font_id}"
    context.text_styles.setdefault(style_name, {"font": style_name, "height": height})
    if data.encoding:
        context.text_encodings.add(str(data.encoding))

    common = _element_common(element, context)
    metadata = common["metadata"]["dgn"]
    metadata.update(
        {
            "font_id": font_id,
            "justification": justification,
            "editable_fields": int(data.editable_fields or 0),
            "text_encoding": data.encoding,
        }
    )
    if data.text_bytes is not None:
        metadata["text_raw_hex"] = bytes(data.text_bytes).hex()
    if data.orientation:
        metadata["orientation"] = [float(value) for value in data.orientation]

    result: dict[str, Any] = {
        **common,
        "kind": "TEXT",
        "insert": _xy(data.origin),
        "height": height,
        "rotation": float(data.rotation_degrees or 0.0),
        "text": str(text),
        "style": style_name,
        "halign": halign,
        "valign": valign,
    }
    if height > _EPSILON and width > _EPSILON:
        result["width_factor"] = width / height
    return result


def _convert_point_string(
    element: Any, context: _V8ConversionContext
) -> list[dict[str, Any]]:
    data = element.data
    if not data.vertices:
        raise ValueError("POINT_STRING has no vertices")
    orientations = tuple(data.orientations or ())
    results: list[dict[str, Any]] = []
    for index, vertex in enumerate(data.vertices):
        common = _element_common(element, context)
        metadata = common["metadata"]["dgn"]
        metadata["point_index"] = index
        if index < len(orientations):
            metadata["orientation"] = [float(value) for value in orientations[index]]
        results.append({**common, "kind": "POINT", "position": _xy(vertex)})
    return results


def _convert_cell(element: Any, context: _V8ConversionContext) -> dict[str, Any]:
    data = element.data
    if data.origin is None:
        raise ValueError("CELL has no origin")
    source_id = _source_id(element)
    raw_name = str(data.name or "CELL") or "CELL"
    block_name = _unique_block_name(raw_name, source_id, context.blocks)
    origin = _xy(data.origin)
    matrix, translation = _placement_transform(data.transform)

    common = _element_common(element, context)
    children = _convert_sequence(context.model.children(element), context)
    context.blocks[block_name] = {
        "base_point": origin,
        "entities": children,
        "metadata": {
            "dgn": {
                "cell_name": raw_name,
                "source_element_id": source_id,
                "placement_transform": matrix,
                "placement_translation": translation,
                "component_coordinate_space": "design",
            }
        },
    }
    common["metadata"]["dgn"].update(
        {
            "cell_name": raw_name,
            "placement_transform": matrix,
            "placement_translation": translation,
            "component_coordinate_space": "design",
        }
    )
    return {**common, "kind": "INSERT", "block": block_name, "insert": origin}


def _convert_bspline(element: Any, context: _V8ConversionContext) -> dict[str, Any]:
    data = element.data
    pole = next(
        (
            child
            for child in context.model.children(element)
            if str(child.kind) == "BSPLINE_POLE"
        ),
        None,
    )
    if pole is None:
        raise ValueError("B-spline curve has no pole record")
    points = _vertices(pole.data)
    if len(points) < 2:
        raise ValueError("B-spline curve has too few poles")
    common = _element_common(element, context)
    common["metadata"]["dgn"].update(
        {
            "properties_raw": data.properties_raw,
            "declared_poles": data.declared_poles,
        }
    )
    # The V8 stream does not expose the curve order or knots yet, so the pole
    # control polyline stands in for the exact spline.
    context.approximation_counts["BSPLINE_CURVE"] += 1
    context.diagnostics.append(
        ImportDiagnostic(
            code="DGN_CURVE_APPROXIMATED",
            severity="warning",
            message=(
                "V8 B-spline curve was represented by its pole control "
                "polyline; the order and knots are not decoded."
            ),
            source_id=_source_id(element),
            source_kind="BSPLINE_CURVE",
            action="approximated",
            details={"control_points": len(points)},
        )
    )
    return {
        **common,
        "kind": "LWPOLYLINE",
        "vertices": points,
        "closed": False,
        "approximation": {
            "method": "polyline",
            "source_kind": "DGN_V8_BSPLINE_CURVE",
            "segments": len(points) - 1,
        },
    }


def _element_common(element: Any, context: _V8ConversionContext) -> dict[str, Any]:
    header = element.common
    level = int(header.level)
    layer_name = f"DGN_LEVEL_{level}"
    line_style = int(header.line_style)
    linetype = f"DGN_STYLE_{line_style}"
    context.linetypes.setdefault(
        linetype,
        {
            "description": f"MicroStation V8 line-style index {line_style}",
            "pattern_mm": [],
        },
    )
    # The active V8 color table lives in an undecoded control object, so
    # layers and entities carry the color index as metadata without an RGB.
    context.layers.setdefault(
        layer_name,
        {
            "linetype": linetype,
            "metadata": {"dgn": {"level": level}},
        },
    )

    raw = getattr(element, "raw", None)
    element_type = getattr(raw, "element_type", None)
    metadata: dict[str, Any] = {
        "element_index": int(getattr(element, "index", 0)),
        "element_id": int(header.element_id),
        "element_type": element_type,
        "model_id": int(getattr(header, "model_id", 0)),
        "level": level,
        "parent_element_index": element.parent_index,
        "graphic_group": int(getattr(header, "graphic_group", 0)),
        "line_style": line_style,
        "line_weight": int(getattr(header, "line_weight", 0)),
        "color_index": int(getattr(header, "color_index", 0)),
        "stored_dimension": getattr(header, "stored_dimension", None),
        "linkage_kind_codes": [
            linkage.kind_code
            for linkage in getattr(element, "linkages", ())
            if getattr(linkage, "kind_code", None) is not None
        ],
    }
    return {
        "id": context.allocate_id(),
        "layer": layer_name,
        "linetype": linetype,
        "source": {
            "format": "dgn",
            "id": _source_id(element),
            "kind": str(element.kind),
            "metadata": {
                "element_type": element_type,
                "stream_offset": getattr(raw, "inflated_offset", None),
            },
        },
        "metadata": {"dgn": metadata},
    }


def _collect_unknown_elements(context: _V8ConversionContext) -> None:
    for element in getattr(context.model, "unknown_elements", ()):
        element_type = getattr(getattr(element, "raw", None), "element_type", None)
        context.skipped_counts[f"TYPE_{element_type or 'UNKNOWN'}"] += 1


def _append_summary_diagnostics(context: _V8ConversionContext) -> None:
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
    shared_total = sum(context.shared_cell_counts.values())
    if shared_total:
        names = sorted(name for name in context.shared_cell_counts if name)
        context.diagnostics.append(
            ImportDiagnostic(
                code="DGN_SHARED_CELL_UNRESOLVED",
                severity="warning",
                message=(
                    f"Skipped {shared_total} V8 shared-cell instance(s); "
                    "shared-cell definitions are not decoded yet."
                ),
                source_kind="SHARED_CELL_INSTANCE",
                action="skipped",
                details={"count": shared_total, "definition_names": names},
            )
        )
    for kind, count in sorted(context.flattened_counts.items()):
        context.diagnostics.append(
            ImportDiagnostic(
                code="DGN_COMPLEX_FLATTENED",
                severity="warning",
                message=(
                    f"Flattened {count} DGN {kind} container(s) while retaining "
                    "parent element indexes on their children."
                ),
                source_kind=kind,
                action="flattened",
                details={"count": count},
            )
        )


def _ellipse_adapter(data: Any, kind: str) -> SimpleNamespace:
    if data.center is None:
        raise ValueError(f"{kind} has no center")
    sweep = data.sweep_angle_degrees
    if kind == "ARC" and sweep is None:
        raise ValueError("ARC has no sweep angle")
    return SimpleNamespace(
        center_master=_xy(data.center),
        primary_axis_master=data.primary_axis_master,
        primary_axis_uor=data.primary_axis_uor,
        secondary_axis_master=data.secondary_axis_master,
        secondary_axis_uor=data.secondary_axis_uor,
        rotation_degrees=float(data.rotation_degrees or 0.0),
        start_angle_degrees=float(data.start_angle_degrees or 0.0),
        sweep_angle_degrees=float(sweep) if sweep is not None else 360.0,
    )


def _vertices(data: Any) -> list[list[float]]:
    return [_xy(vertex) for vertex in data.vertices]


def _xy(point: Any) -> list[float]:
    master = getattr(point, "master", point)
    if len(master) < 2:
        raise ValueError("point requires two coordinates")
    x, y = float(master[0]), float(master[1])
    if not math.isfinite(x) or not math.isfinite(y):
        raise ValueError("point coordinates must be finite")
    return [x, y]


def _positive_size(value: Any) -> float:
    result = float(value or 0.0)
    return result if math.isfinite(result) and result > _EPSILON else 1.0


def _placement_transform(
    transform: Any,
) -> tuple[list[list[float]], list[float]]:
    values = tuple(float(value) for value in transform or ())
    if len(values) == 6:
        return [[values[0], values[1]], [values[2], values[3]]], list(values[4:6])
    if len(values) == 12:
        return [[values[0], values[1]], [values[3], values[4]]], list(values[9:12])
    return [[1.0, 0.0], [0.0, 1.0]], []


def _source_id(element: Any) -> str:
    header = getattr(element, "common", None)
    return str(getattr(header, "element_id", getattr(element, "index", "unknown")))
