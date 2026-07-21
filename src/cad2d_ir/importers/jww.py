"""Native JWW to CAD 2D IR importer."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
import hashlib
import math
from pathlib import Path
from typing import Any, Mapping, Sequence

from cad2d_ir.importers.base import (
    ImportDiagnostic,
    ImporterError,
    ImportOptions,
    ImportResult,
    MissingOptionalDependencyError,
)
from cad2d_ir.schema import validate_ir

_TAU = 2.0 * math.pi
_EPSILON = 1e-12

_LINE_TYPES = {
    0: "CONTINUOUS",
    1: "CONTINUOUS",
    2: "DASHED",
    3: "DASHDOT",
    4: "CENTER",
    5: "DOT",
    6: "DASHED2",
    7: "DASHDOT2",
    8: "CENTER2",
    9: "DOT2",
}

_LINETYPE_TABLE: dict[str, dict[str, Any]] = {
    "CONTINUOUS": {"description": "Continuous line", "pattern_mm": []},
    "DASHED": {"description": "Dashed line", "pattern_mm": [0.6, -0.3]},
    "DASHDOT": {"description": "Dash-dot line", "pattern_mm": [0.6, -0.2, 0.1, -0.2]},
    "CENTER": {"description": "Center line", "pattern_mm": [1.25, -0.25, 0.25, -0.25]},
    "DOT": {"description": "Dotted line", "pattern_mm": [0.1, -0.1]},
    "DASHED2": {"description": "Dashed line x2", "pattern_mm": [1.2, -0.6]},
    "DASHDOT2": {
        "description": "Dash-dot line x2",
        "pattern_mm": [1.2, -0.4, 0.2, -0.4],
    },
    "CENTER2": {"description": "Center line x2", "pattern_mm": [2.5, -0.5, 0.5, -0.5]},
    "DOT2": {"description": "Dotted line x2", "pattern_mm": [0.2, -0.2]},
    "BYLAYER": {"description": "Use the layer linetype", "pattern_mm": []},
}


@dataclass(slots=True)
class _ConversionContext:
    options: ImportOptions
    layers: dict[str, dict[str, Any]]
    layer_names: dict[tuple[int, int], str]
    block_names: dict[int, str]
    diagnostics: list[ImportDiagnostic] = field(default_factory=list)
    converted_counts: Counter[str] = field(default_factory=Counter)
    skipped_counts: Counter[str] = field(default_factory=Counter)
    approximation_counts: Counter[str] = field(default_factory=Counter)
    preserved_dimensions: int = 0
    next_entity_number: int = 1

    def allocate_id(self) -> str:
        entity_id = f"JWW_E{self.next_entity_number:08d}"
        self.next_entity_number += 1
        return entity_id


def convert_jww_file_to_ir(
    path: str | Path,
    *,
    options: ImportOptions | None = None,
) -> ImportResult:
    """Read a JWW file with ``ezjww`` and convert it directly to IR.

    Install the ``jww`` extra to enable this importer::

        pip install "cad2d-ir[jww]"
    """
    try:
        from ezjww import read_document
    except ImportError as exc:
        raise MissingOptionalDependencyError(
            "JWW support requires the optional dependency ezjww; "
            'install it with `pip install "cad2d-ir[jww]"`.'
        ) from exc

    source_path = Path(path)
    jww_document = read_document(str(source_path))
    return jww_document_to_ir(
        jww_document,
        source_name=source_path.name,
        source_sha256=_sha256_file(source_path),
        options=options,
    )


def jww_document_to_ir(
    jww_document: Mapping[str, Any],
    *,
    source_name: str | None = None,
    source_sha256: str | None = None,
    options: ImportOptions | None = None,
) -> ImportResult:
    """Convert an ``ezjww.read_document`` result to CAD 2D IR."""
    import_options = options or ImportOptions()
    header = _mapping(jww_document.get("header"), "header")
    layers, layer_names = _build_layers(header)
    block_defs = _mapping_sequence(jww_document.get("block_defs", []), "block_defs")
    block_names = _build_block_names(block_defs)
    context = _ConversionContext(
        options=import_options,
        layers=layers,
        layer_names=layer_names,
        block_names=block_names,
    )

    entities = _convert_entity_sequence(
        _mapping_sequence(jww_document.get("entities", []), "entities"),
        context,
        source_prefix="entities",
    )
    blocks = _convert_blocks(block_defs, context)
    text_styles = _collect_text_styles(jww_document)

    tables: dict[str, Any] = {
        "layers": context.layers,
        "linetypes": _LINETYPE_TABLE,
        "text_styles": text_styles,
    }
    if blocks:
        tables["blocks"] = blocks

    raw_version = int(header.get("version", 0))
    source: dict[str, Any] = {"format": "jww", "version": str(raw_version)}
    if source_name is not None:
        source["name"] = source_name
    if source_sha256 is not None:
        source["sha256"] = source_sha256

    document: dict[str, Any] = {
        "format": "cad2d-ir",
        "version": import_options.ir_version,
        "header": {
            "units": "mm",
            "angle_unit": "deg",
            "coord_space": "world",
            "metadata": {
                "jww": {
                    "version": raw_version,
                    "memo": str(header.get("memo", "")),
                    "paper_size": int(header.get("paper_size", 0)),
                    "write_layer_group": int(header.get("write_layer_group", 0)),
                }
            },
        },
        "source": source,
        "tables": tables,
        "entities": entities,
    }

    _append_summary_diagnostics(jww_document, context)
    if import_options.validate:
        validate_ir(document)

    source_counts = {
        str(key): int(value)
        for key, value in _mapping(
            jww_document.get("entity_counts", {}), "entity_counts"
        ).items()
    }
    block_entity_count = sum(len(block["entities"]) for block in blocks.values())
    statistics: dict[str, Any] = {
        "source_format": "jww",
        "source_entities": len(
            _mapping_sequence(jww_document.get("entities", []), "entities")
        ),
        "source_entity_counts": dict(sorted(source_counts.items())),
        "source_block_definitions": len(block_defs),
        "converted_entities": len(entities),
        "converted_block_entities": block_entity_count,
        "converted_entity_counts": dict(sorted(context.converted_counts.items())),
        "skipped_entities": sum(context.skipped_counts.values()),
        "skipped_entity_counts": dict(sorted(context.skipped_counts.items())),
        "approximated_entities": sum(context.approximation_counts.values()),
        "preserved_dimensions": context.preserved_dimensions,
    }
    return ImportResult(
        document=document, diagnostics=context.diagnostics, statistics=statistics
    )


def _build_layers(
    header: Mapping[str, Any],
) -> tuple[dict[str, dict[str, Any]], dict[tuple[int, int], str]]:
    layers: dict[str, dict[str, Any]] = {}
    layer_names: dict[tuple[int, int], str] = {}
    groups = _mapping_sequence(header.get("layer_groups", []), "header.layer_groups")

    for group_index, group in enumerate(groups):
        group_layers = _mapping_sequence(
            group.get("layers", []),
            f"header.layer_groups[{group_index}].layers",
        )
        for layer_index, layer in enumerate(group_layers):
            fallback = f"{group_index:X}-{layer_index:X}"
            raw_name = str(layer.get("name", ""))
            candidate = raw_name.strip() or fallback
            unique_name = _unique_name(candidate, fallback, layers)
            layer_names[(group_index, layer_index)] = unique_name
            layers[unique_name] = {
                "color": (group_index * 16 + layer_index) % 255 + 1,
                "linetype": "CONTINUOUS",
                "plot": int(layer.get("state", 0)) != 0,
                "metadata": {
                    "jww": {
                        "layer_group": group_index,
                        "layer": layer_index,
                        "original_name": raw_name,
                        "state": int(layer.get("state", 0)),
                        "protect": int(layer.get("protect", 0)),
                        "group_name": str(group.get("name", "")),
                        "group_state": int(group.get("state", 0)),
                        "group_protect": int(group.get("protect", 0)),
                        "group_scale": float(group.get("scale", 1.0)),
                    }
                },
            }
    return layers, layer_names


def _build_block_names(block_defs: Sequence[Mapping[str, Any]]) -> dict[int, str]:
    result: dict[int, str] = {}
    used_names: dict[str, Any] = {}
    for block_def in block_defs:
        number = int(block_def.get("number", 0))
        fallback = f"BLOCK_{number}"
        candidate = str(block_def.get("name", "")).strip() or fallback
        result[number] = _unique_name(candidate, fallback, used_names)
        used_names[result[number]] = True
    return result


def _convert_blocks(
    block_defs: Sequence[Mapping[str, Any]],
    context: _ConversionContext,
) -> dict[str, dict[str, Any]]:
    blocks: dict[str, dict[str, Any]] = {}
    for block_index, block_def in enumerate(block_defs):
        number = int(block_def.get("number", 0))
        name = context.block_names[number]
        block_entities = _convert_entity_sequence(
            _mapping_sequence(
                block_def.get("entities", []),
                f"block_defs[{block_index}].entities",
            ),
            context,
            source_prefix=f"block_defs[{block_index}].entities",
        )
        blocks[name] = {
            "base_point": [0.0, 0.0],
            "entities": block_entities,
            "metadata": {
                "jww": {
                    "number": number,
                    "is_referenced": bool(block_def.get("is_referenced", False)),
                    "base": dict(_mapping(block_def.get("base", {}), "block base")),
                }
            },
        }
    return blocks


def _convert_entity_sequence(
    source_entities: Sequence[Mapping[str, Any]],
    context: _ConversionContext,
    *,
    source_prefix: str,
) -> list[dict[str, Any]]:
    converted: list[dict[str, Any]] = []
    for index, source_entity in enumerate(source_entities):
        source_id = f"{source_prefix}[{index}]"
        entity = _convert_entity_safe(source_entity, context, source_id=source_id)
        if entity is not None:
            converted.append(entity)
            context.converted_counts[str(entity["kind"])] += 1
    return converted


def _convert_entity_safe(
    source_entity: Mapping[str, Any],
    context: _ConversionContext,
    *,
    source_id: str,
) -> dict[str, Any] | None:
    source_kind = str(source_entity.get("type", "UNKNOWN")).upper()
    try:
        return _convert_entity(
            source_entity, context, source_id=source_id, source_kind=source_kind
        )
    except (KeyError, TypeError, ValueError) as exc:
        if context.options.strict:
            raise ImporterError(
                f"Failed to convert JWW {source_kind} at {source_id}: {exc}"
            ) from exc
        context.skipped_counts[source_kind] += 1
        context.diagnostics.append(
            ImportDiagnostic(
                code="JWW_ENTITY_CONVERSION_FAILED",
                severity="error",
                message=f"Failed to convert JWW {source_kind}: {exc}",
                source_id=source_id,
                source_kind=source_kind,
                action="skipped",
            )
        )
        return None


def _convert_entity(
    source_entity: Mapping[str, Any],
    context: _ConversionContext,
    *,
    source_id: str,
    source_kind: str,
) -> dict[str, Any] | None:
    common = _entity_common(
        source_entity, context, source_id=source_id, source_kind=source_kind
    )

    if source_kind == "LINE":
        return {
            **common,
            "kind": "LINE",
            "p1": [
                _number(source_entity, "start_x"),
                _number(source_entity, "start_y"),
            ],
            "p2": [_number(source_entity, "end_x"), _number(source_entity, "end_y")],
        }
    if source_kind in {"ARC", "CIRCLE"}:
        return _convert_arc(source_entity, common, context, source_id=source_id)
    if source_kind == "POINT":
        result = {
            **common,
            "kind": "POINT",
            "position": [_number(source_entity, "x"), _number(source_entity, "y")],
            "marker_code": int(source_entity.get("code", 0)),
        }
        if bool(source_entity.get("is_temporary", False)):
            result["temporary"] = True
        scale = float(source_entity.get("scale", 0.0))
        if scale > 0.0:
            result["scale"] = scale
        angle = float(source_entity.get("angle", 0.0))
        if angle != 0.0:
            result["rotation"] = angle
        return result
    if source_kind == "TEXT":
        return _convert_text(source_entity, common, context, source_id=source_id)
    if source_kind == "SOLID":
        return _convert_solid(source_entity, common)
    if source_kind == "CIRCLE_SOLID":
        return _convert_circle_solid(
            source_entity, common, context, source_id=source_id
        )
    if source_kind == "BLOCK":
        return _convert_block_reference(
            source_entity, common, context, source_id=source_id
        )
    if source_kind == "DIMENSION":
        context.preserved_dimensions += 1
        return _convert_dimension(source_entity, common)

    context.skipped_counts[source_kind] += 1
    return None


def _entity_common(
    source_entity: Mapping[str, Any],
    context: _ConversionContext,
    *,
    source_id: str,
    source_kind: str,
) -> dict[str, Any]:
    base = _mapping(source_entity.get("base", {}), f"{source_id}.base")
    layer_group = int(base.get("layer_group", 0))
    layer = int(base.get("layer", 0))
    layer_name = context.layer_names.get((layer_group, layer))
    if layer_name is None:
        fallback = f"{layer_group:X}-{layer:X}"
        layer_name = _unique_name(fallback, fallback, context.layers)
        context.layer_names[(layer_group, layer)] = layer_name
        context.layers[layer_name] = {
            "color": (layer_group * 16 + layer) % 255 + 1,
            "linetype": "CONTINUOUS",
            "metadata": {"jww": {"layer_group": layer_group, "layer": layer}},
        }

    pen_style = int(base.get("pen_style", 0))
    pen_width = int(base.get("pen_width", 0))
    common: dict[str, Any] = {
        "id": context.allocate_id(),
        "layer": layer_name,
        "linetype": _LINE_TYPES.get(pen_style, "BYLAYER"),
        "color": _map_color(int(base.get("pen_color", 0))),
        "source": {"format": "jww", "id": source_id, "kind": source_kind},
        "metadata": {
            "jww": {
                "group": int(base.get("group", 0)),
                "pen_style": pen_style,
                "pen_color": int(base.get("pen_color", 0)),
                "pen_width": pen_width,
                "layer": layer,
                "layer_group": layer_group,
                "flag": int(base.get("flag", 0)),
            }
        },
    }
    if pen_width > 0:
        common["lineweight_mm"] = min(pen_width, 211) / 100.0
    custom_color = source_entity.get("color")
    if isinstance(custom_color, int):
        common["color"] = f"#{custom_color & 0xFFFFFF:06X}"
        common["metadata"]["jww"]["custom_color"] = custom_color
    return common


def _convert_arc(
    source: Mapping[str, Any],
    common: dict[str, Any],
    context: _ConversionContext,
    *,
    source_id: str,
) -> dict[str, Any]:
    radius = abs(_number(source, "radius"))
    if radius <= _EPSILON:
        raise ValueError("radius must be non-zero")
    center = [_number(source, "center_x"), _number(source, "center_y")]
    flatness = abs(float(source.get("flatness", 1.0)))
    if flatness <= _EPSILON:
        flatness = 1.0
        context.diagnostics.append(
            ImportDiagnostic(
                code="JWW_ZERO_FLATNESS_NORMALIZED",
                severity="warning",
                message="Zero arc flatness was normalized to 1.0.",
                source_id=source_id,
                source_kind=str(source.get("type", "ARC")),
                action="normalized",
            )
        )
    is_full = (
        bool(source.get("is_full_circle", False))
        or str(source.get("type", "")).upper() == "CIRCLE"
    )
    start = float(source.get("start_angle", 0.0))
    span = float(source.get("arc_angle", _TAU if is_full else 0.0))

    if math.isclose(flatness, 1.0, rel_tol=1e-12, abs_tol=1e-12):
        if is_full:
            return {**common, "kind": "CIRCLE", "center": center, "radius": radius}
        span_start, span_end = _ccw_span(start, span)
        return {
            **common,
            "kind": "ARC",
            "center": center,
            "radius": radius,
            "start_angle": _normalize_degrees(math.degrees(span_start)),
            "end_angle": _normalize_degrees(math.degrees(span_end)),
            "ccw": True,
        }

    major_radius = radius
    ratio = flatness
    tilt = float(source.get("tilt_angle", 0.0))
    if ratio > 1.0:
        major_radius *= ratio
        ratio = 1.0 / ratio
        tilt += math.pi / 2.0
    span_start, span_end = _ccw_span(start, span)
    return {
        **common,
        "kind": "ELLIPSE",
        "center": center,
        "major_axis": [major_radius * math.cos(tilt), major_radius * math.sin(tilt)],
        "ratio": ratio,
        "start_param": 0.0 if is_full else span_start,
        "end_param": _TAU if is_full else span_end,
        "ccw": True,
    }


def _convert_text(
    source: Mapping[str, Any],
    common: dict[str, Any],
    context: _ConversionContext,
    *,
    source_id: str,
) -> dict[str, Any]:
    height = float(source.get("size_y", 0.0))
    if height <= 0.0:
        height = 2.5
        context.diagnostics.append(
            ImportDiagnostic(
                code="JWW_TEXT_HEIGHT_DEFAULTED",
                severity="warning",
                message="Non-positive JWW text height was replaced with 2.5 mm.",
                source_id=source_id,
                source_kind="TEXT",
                action="normalized",
            )
        )
    font_name = str(source.get("font_name", "")).strip()
    result: dict[str, Any] = {
        **common,
        "kind": "TEXT",
        "insert": [_number(source, "start_x"), _number(source, "start_y")],
        "height": height,
        "text": str(source.get("content", "")),
        "style": font_name or "STANDARD",
    }
    rotation = float(source.get("angle", 0.0))
    if rotation != 0.0:
        result["rotation"] = rotation
    size_x = float(source.get("size_x", 0.0))
    if size_x > 0.0:
        result["width_factor"] = size_x / height
    result["metadata"]["jww"].update(
        {
            "end": [_number(source, "end_x"), _number(source, "end_y")],
            "text_type": int(source.get("text_type", 0)),
            "size_x": size_x,
            "size_y": float(source.get("size_y", 0.0)),
            "spacing": float(source.get("spacing", 0.0)),
            "font_name": str(source.get("font_name", "")),
        }
    )
    return result


def _convert_solid(source: Mapping[str, Any], common: dict[str, Any]) -> dict[str, Any]:
    points = [
        [_number(source, f"point{index}_x"), _number(source, f"point{index}_y")]
        for index in range(1, 5)
    ]
    return {
        **common,
        "kind": "HATCH",
        "solid": True,
        "loops": [{"vertices": _order_solid_vertices(points), "is_outer": True}],
    }


def _convert_circle_solid(
    source: Mapping[str, Any],
    common: dict[str, Any],
    context: _ConversionContext,
    *,
    source_id: str,
) -> dict[str, Any]:
    radius = abs(_number(source, "radius"))
    if radius <= _EPSILON:
        raise ValueError("circle-solid radius must be non-zero")
    start = float(source.get("start_angle", 0.0))
    span = float(source.get("arc_angle", _TAU))
    mode = round(float(source.get("solid_mode", 0.0)))
    is_full = mode == 100 or math.isclose(abs(span), _TAU, rel_tol=0.0, abs_tol=1e-6)
    if is_full:
        start = 0.0
        span = _TAU
    steps = _arc_segment_count(span, is_full, context.options.curve_segments)

    outer = _ellipse_arc_points(source, radius, start, span, is_full, steps)
    pen_style = int(
        _mapping(source.get("base", {}), f"{source_id}.base").get("pen_style", 0)
    )
    loops: list[dict[str, Any]]
    if pen_style in {105, 106}:
        inner_radius = abs(float(source.get("solid_mode", 0.0)))
        if _EPSILON < inner_radius < radius:
            inner = _ellipse_arc_points(
                source, inner_radius, start, span, is_full, steps
            )
            if is_full:
                loops = [
                    {"vertices": outer, "is_outer": True},
                    {"vertices": list(reversed(inner)), "is_outer": False},
                ]
            else:
                loops = [{"vertices": outer + list(reversed(inner)), "is_outer": True}]
        else:
            loops = [{"vertices": outer, "is_outer": True}]
    elif mode in {-1, 5, 100} or is_full:
        loops = [{"vertices": outer, "is_outer": True}]
    else:
        center = [_number(source, "center_x"), _number(source, "center_y")]
        loops = [{"vertices": [center, *outer], "is_outer": True}]

    if any(len(loop["vertices"]) < 3 for loop in loops):
        raise ValueError(
            "circle-solid approximation produced fewer than three vertices"
        )
    context.approximation_counts["CIRCLE_SOLID"] += 1
    return {
        **common,
        "kind": "HATCH",
        "solid": True,
        "loops": loops,
        "approximation": {
            "method": "polyline",
            "source_kind": "CIRCLE_SOLID",
            "segments": steps,
            "metadata": {"jww_solid_mode": float(source.get("solid_mode", 0.0))},
        },
    }


def _convert_block_reference(
    source: Mapping[str, Any],
    common: dict[str, Any],
    context: _ConversionContext,
    *,
    source_id: str,
) -> dict[str, Any]:
    number = int(source.get("def_number", 0))
    block_name = context.block_names.get(number)
    if block_name is None:
        block_name = str(source.get("block_name") or f"BLOCK_{number}")
    scale_x = float(source.get("scale_x", 1.0))
    scale_y = float(source.get("scale_y", 1.0))
    if scale_x == 0.0 or scale_y == 0.0:
        if context.options.strict:
            raise ValueError("block reference scale must be non-zero")
        scale_x = 1.0 if scale_x == 0.0 else scale_x
        scale_y = 1.0 if scale_y == 0.0 else scale_y
        context.diagnostics.append(
            ImportDiagnostic(
                code="JWW_ZERO_BLOCK_SCALE_NORMALIZED",
                severity="warning",
                message="Zero JWW block scale was replaced with 1.0.",
                source_id=source_id,
                source_kind="BLOCK",
                action="normalized",
            )
        )
    result: dict[str, Any] = {
        **common,
        "kind": "INSERT",
        "block": block_name,
        "insert": [_number(source, "ref_x"), _number(source, "ref_y")],
    }
    if not (math.isclose(scale_x, 1.0) and math.isclose(scale_y, 1.0)):
        result["scale"] = (
            scale_x if math.isclose(scale_x, scale_y) else [scale_x, scale_y]
        )
    rotation = float(source.get("rotation", 0.0))
    if rotation != 0.0:
        result["rotation"] = math.degrees(rotation)
    result["metadata"]["jww"]["def_number"] = number
    return result


def _convert_dimension(
    source: Mapping[str, Any], common: dict[str, Any]
) -> dict[str, Any]:
    line = dict(_mapping(source.get("line", {}), "dimension.line"))
    text = dict(_mapping(source.get("text", {}), "dimension.text"))
    aux_lines = [
        dict(value)
        for value in _mapping_sequence(
            source.get("aux_lines", []), "dimension.aux_lines"
        )
    ]
    aux_points = [
        dict(value)
        for value in _mapping_sequence(
            source.get("aux_points", []), "dimension.aux_points"
        )
    ]

    line_start = [_number(line, "start_x"), _number(line, "start_y")]
    line_end = [_number(line, "end_x"), _number(line, "end_y")]
    text_start = [_number(text, "start_x"), _number(text, "start_y")]
    text_end = [_number(text, "end_x"), _number(text, "end_y")]
    sxf_mode = source.get("sxf_mode")
    common["metadata"]["jww"]["sxf_mode"] = sxf_mode
    return {
        **common,
        "kind": "DIMENSION",
        "dim_kind": "GENERIC",
        "definition": {
            "points": {
                "location": line_start,
                "p1": line_start,
                "p2": line_end,
                "text_midpoint": [
                    (text_start[0] + text_end[0]) / 2.0,
                    (text_start[1] + text_end[1]) / 2.0,
                ],
            },
            "text": str(text.get("content", "")),
            "source_geometry": {
                "line": line,
                "text": text,
                "sxf_mode": sxf_mode,
                "aux_lines": aux_lines,
                "aux_points": aux_points,
            },
        },
    }


def _collect_text_styles(jww_document: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    fonts: set[str] = {"STANDARD"}

    def collect(entities: Sequence[Mapping[str, Any]]) -> None:
        for entity in entities:
            source_kind = str(entity.get("type", "")).upper()
            if source_kind == "TEXT":
                fonts.add(str(entity.get("font_name", "")).strip() or "STANDARD")
            elif source_kind == "DIMENSION":
                text = _mapping(entity.get("text", {}), "dimension.text")
                fonts.add(str(text.get("font_name", "")).strip() or "STANDARD")

    collect(_mapping_sequence(jww_document.get("entities", []), "entities"))
    for block_def in _mapping_sequence(
        jww_document.get("block_defs", []), "block_defs"
    ):
        collect(_mapping_sequence(block_def.get("entities", []), "block entities"))
    return {font: {"font": font} for font in sorted(fonts)}


def _append_summary_diagnostics(
    jww_document: Mapping[str, Any],
    context: _ConversionContext,
) -> None:
    for source_kind, count in sorted(context.skipped_counts.items()):
        if count == 0:
            continue
        if not any(
            diagnostic.code == "JWW_ENTITY_CONVERSION_FAILED"
            and diagnostic.source_kind == source_kind
            for diagnostic in context.diagnostics
        ):
            context.diagnostics.append(
                ImportDiagnostic(
                    code="JWW_UNSUPPORTED_ENTITY",
                    severity="warning",
                    message=f"Skipped {count} unsupported JWW {source_kind} entities.",
                    source_kind=source_kind,
                    action="skipped",
                )
            )
    for source_kind, count in sorted(context.approximation_counts.items()):
        context.diagnostics.append(
            ImportDiagnostic(
                code="JWW_CURVE_APPROXIMATED",
                severity="warning",
                message=f"Approximated {count} JWW {source_kind} entities with polyline hatch boundaries.",
                source_kind=source_kind,
                action="approximated",
            )
        )

    validation = _mapping(jww_document.get("validation", {}), "validation")
    unresolved = [int(value) for value in validation.get("unresolved_def_numbers", [])]
    if unresolved:
        context.diagnostics.append(
            ImportDiagnostic(
                code="JWW_UNRESOLVED_BLOCK_REFERENCE",
                severity="warning",
                message=f"JWW contains unresolved block definition numbers: {unresolved}.",
                source_kind="BLOCK",
                action="preserved_reference",
            )
        )


def _ellipse_arc_points(
    source: Mapping[str, Any],
    radius: float,
    start: float,
    span: float,
    is_full: bool,
    steps: int,
) -> list[list[float]]:
    end_index = steps if is_full else steps + 1
    return [
        _ellipse_point(source, radius, start + span * (index / steps))
        for index in range(end_index)
    ]


def _ellipse_point(
    source: Mapping[str, Any], radius: float, angle: float
) -> list[float]:
    flatness = abs(float(source.get("flatness", 1.0)))
    if flatness <= _EPSILON:
        flatness = 1.0
    major_radius = abs(radius)
    ratio = flatness
    tilt = float(source.get("tilt_angle", 0.0))
    if ratio > 1.0:
        major_radius *= ratio
        ratio = 1.0 / ratio
        tilt += math.pi / 2.0
    minor_radius = major_radius * ratio
    local_x = major_radius * math.cos(angle)
    local_y = minor_radius * math.sin(angle)
    cos_tilt = math.cos(tilt)
    sin_tilt = math.sin(tilt)
    return [
        _number(source, "center_x") + local_x * cos_tilt - local_y * sin_tilt,
        _number(source, "center_y") + local_x * sin_tilt + local_y * cos_tilt,
    ]


def _arc_segment_count(span: float, is_full: bool, full_segments: int) -> int:
    effective_span = _TAU if is_full else max(abs(span), math.pi / 32.0)
    return max(8, min(full_segments, math.ceil(effective_span / _TAU * full_segments)))


def _order_solid_vertices(points: list[list[float]]) -> list[list[float]]:
    if not _solid_vertices_cross(points):
        return points
    center_x = sum(point[0] for point in points) / len(points)
    center_y = sum(point[1] for point in points) / len(points)
    ordered = sorted(
        points, key=lambda point: math.atan2(point[1] - center_y, point[0] - center_x)
    )
    first_index = next(
        (index for index, point in enumerate(ordered) if _same_point(point, points[0])),
        0,
    )
    return ordered[first_index:] + ordered[:first_index]


def _solid_vertices_cross(points: list[list[float]]) -> bool:
    return _segments_intersect(
        points[0], points[1], points[2], points[3]
    ) or _segments_intersect(points[1], points[2], points[3], points[0])


def _segments_intersect(
    a: list[float],
    b: list[float],
    c: list[float],
    d: list[float],
) -> bool:
    return (
        _orientation(a, b, c) * _orientation(a, b, d) < 0.0
        and _orientation(c, d, a) * _orientation(c, d, b) < 0.0
    )


def _orientation(a: list[float], b: list[float], c: list[float]) -> float:
    return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])


def _same_point(a: list[float], b: list[float]) -> bool:
    return math.isclose(a[0], b[0], abs_tol=1e-9) and math.isclose(
        a[1], b[1], abs_tol=1e-9
    )


def _ccw_span(start: float, span: float) -> tuple[float, float]:
    return (start + span, start) if span < 0.0 else (start, start + span)


def _normalize_degrees(value: float) -> float:
    normalized = value % 360.0
    return 0.0 if math.isclose(normalized, 360.0, abs_tol=1e-12) else normalized


def _map_color(pen_color: int) -> int:
    return {
        1: 7,
        8: 7,
        2: 5,
        3: 1,
        4: 6,
        5: 3,
        6: 4,
        7: 2,
        9: 8,
    }.get(pen_color, max(pen_color % 255, 1))


def _unique_name(candidate: str, fallback: str, used: Mapping[str, Any]) -> str:
    if candidate not in used:
        return candidate
    suffixed = f"{candidate} [{fallback}]"
    serial = 2
    while suffixed in used:
        suffixed = f"{candidate} [{fallback}-{serial}]"
        serial += 1
    return suffixed


def _number(mapping: Mapping[str, Any], key: str) -> float:
    value = mapping[key]
    if not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        raise ValueError(f"{key} must be a finite number")
    return float(value)


def _mapping(value: Any, path: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{path} must be a mapping")
    return value


def _mapping_sequence(value: Any, path: str) -> list[Mapping[str, Any]]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        raise TypeError(f"{path} must be a sequence")
    result: list[Mapping[str, Any]] = []
    for index, item in enumerate(value):
        result.append(_mapping(item, f"{path}[{index}]"))
    return result


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


__all__ = ["convert_jww_file_to_ir", "jww_document_to_ir"]
