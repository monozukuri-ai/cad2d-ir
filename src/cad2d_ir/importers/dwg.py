"""Native DWG to CAD 2D IR importer backed by :mod:`ezdwg`."""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass, field
import hashlib
import math
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from cad2d_ir.codecs.dxf import INSUNITS_TO_IR_UNITS
from cad2d_ir.importers.base import (
    ImportDiagnostic,
    ImporterError,
    ImportOptions,
    ImportResult,
    MissingOptionalDependencyError,
)
from cad2d_ir.schema import validate_ir

_EPSILON = 1.0e-12
_DEFAULT_LAYER = "0"

_DIMENSION_KINDS = {
    "LINEAR": "LINEAR",
    "ALIGNED": "ALIGNED",
    "ANG2LN": "ANGULAR",
    "ANG3PT": "ANGULAR",
    "ANGULAR": "ANGULAR",
    "RADIUS": "RADIAL",
    "RADIAL": "RADIAL",
    "DIAMETER": "DIAMETER",
    "ORDINATE": "ORDINATE",
}


@dataclass(slots=True)
class _ConversionContext:
    options: ImportOptions
    layer_names: dict[int, str]
    layers: dict[str, dict[str, Any]]
    diagnostics: list[ImportDiagnostic] = field(default_factory=list)
    converted_counts: Counter[str] = field(default_factory=Counter)
    skipped_counts: Counter[str] = field(default_factory=Counter)
    approximation_counts: Counter[str] = field(default_factory=Counter)
    projected_counts: Counter[str] = field(default_factory=Counter)
    projected_handles: set[int] = field(default_factory=set)
    preserved_dimensions: int = 0
    paperspace_skipped: int = 0
    next_entity_number: int = 1

    def allocate_id(self) -> str:
        entity_id = f"DWG_E{self.next_entity_number:08d}"
        self.next_entity_number += 1
        return entity_id


def convert_dwg_file_to_ir(
    path: str | Path,
    *,
    options: ImportOptions | None = None,
) -> ImportResult:
    """Read a DWG file with ``ezdwg`` and convert its native model to IR."""
    try:
        import ezdwg
    except ImportError as exc:
        raise MissingOptionalDependencyError(
            "DWG support requires the optional dependency ezdwg; "
            'install it with `pip install "cad2d-ir[dwg]"`.'
        ) from exc

    source_path = Path(path)
    document = ezdwg.read(str(source_path))
    decode_path = str(getattr(document, "decode_path", None) or source_path)
    raw = getattr(document, "raw", getattr(ezdwg, "raw", None))
    layer_names, layer_colors, block_names = _read_dwg_tables(raw, decode_path)
    return dwg_document_to_ir(
        document,
        source_name=source_path.name,
        source_sha256=_sha256_file(source_path),
        layer_names_by_handle=layer_names,
        layer_colors_by_handle=layer_colors,
        block_names_by_handle=block_names,
        options=options,
    )


def _resolve_header_units(
    dwg_document: Any, context: _ConversionContext
) -> tuple[str, dict[str, Any]]:
    """Resolve IR header units from the DWG ``$INSUNITS`` header variable.

    Uses ``Document.header_variables()`` (``ezdwg`` >= 0.11). Document objects
    without that API keep the previous behavior of reporting unknown units, so
    adapters and test doubles stay compatible.
    """
    header_variables = getattr(dwg_document, "header_variables", None)
    if not callable(header_variables):
        return "unknown", {"units_status": "not exposed by ezdwg"}
    try:
        insunits = header_variables().get("insunits")
    except Exception as exc:
        context.diagnostics.append(
            ImportDiagnostic(
                code="DWG_HEADER_UNITS_UNREADABLE",
                severity="warning",
                message=f"Failed to decode DWG header variables for units: {exc}",
            )
        )
        return "unknown", {"units_status": "header variables unreadable"}
    if insunits is None:
        return "unknown", {"units_status": "INSUNITS not present (R14)"}
    code = int(insunits)
    units = INSUNITS_TO_IR_UNITS.get(code)
    if units is None:
        context.diagnostics.append(
            ImportDiagnostic(
                code="DWG_UNSUPPORTED_INSUNITS",
                severity="warning",
                message=(
                    f"DWG $INSUNITS code {code} has no CAD 2D IR units mapping; "
                    "header units fall back to 'unknown'."
                ),
                action="normalized",
            )
        )
        return "unknown", {
            "insunits": code,
            "units_status": "unsupported INSUNITS code",
        }
    return units, {"insunits": code}


def dwg_document_to_ir(
    dwg_document: Any,
    *,
    source_name: str | None = None,
    source_sha256: str | None = None,
    layer_names_by_handle: Mapping[int, str] | None = None,
    layer_colors_by_handle: Mapping[int, tuple[int | None, int | None]] | None = None,
    block_names_by_handle: Mapping[int, str] | None = None,
    options: ImportOptions | None = None,
) -> ImportResult:
    """Convert an ``ezdwg.Document``-compatible object directly to IR.

    The extra table arguments are public primarily for adapters and tests. The file
    entry point obtains them from ``ezdwg.raw`` so layer and block names are retained.
    """
    import_options = options or ImportOptions()
    layer_names, layers = _build_layers(
        layer_names_by_handle or {}, layer_colors_by_handle or {}
    )
    context = _ConversionContext(
        options=import_options,
        layer_names=layer_names,
        layers=layers,
    )

    try:
        source_entities = list(_enumerate_source_entities(dwg_document))
    except Exception as exc:
        raise ImporterError(f"Failed to enumerate DWG entities: {exc}") from exc

    block_names = {
        int(handle): str(name)
        for handle, name in (block_names_by_handle or {}).items()
        if str(name) and not _is_space_block(str(name))
    }
    paperspace_handles = {
        int(handle)
        for handle, name in (block_names_by_handle or {}).items()
        if _is_paperspace_block(str(name))
    }
    placement_of = getattr(dwg_document, "entity_placement", None)
    top_level: list[Any] = []
    block_sources: dict[int, list[Any]] = defaultdict(list)
    for source_entity in source_entities:
        owner_handle = _optional_int(_dxf(source_entity).get("owner_handle"))
        if owner_handle is not None and owner_handle in block_names:
            block_sources[owner_handle].append(source_entity)
            continue
        if _is_paperspace_entity(
            source_entity, owner_handle, paperspace_handles, placement_of
        ):
            context.paperspace_skipped += 1
            continue
        top_level.append(source_entity)

    entities = _convert_entity_sequence(top_level, context)
    blocks: dict[str, dict[str, Any]] = {}
    for owner_handle, block_name in sorted(block_names.items()):
        block_entities = _convert_entity_sequence(
            block_sources.get(owner_handle, []), context
        )
        if block_entities:
            blocks[block_name] = {
                "base_point": [0.0, 0.0],
                "entities": block_entities,
                "metadata": {
                    "dwg": {
                        "block_header_handle": _handle_text(owner_handle),
                        "base_point_status": "not exposed by ezdwg",
                    }
                },
            }

    _append_unresolved_block_diagnostics(entities, blocks, context)
    _append_summary_diagnostics(context)

    tables: dict[str, Any] = {
        "layers": context.layers,
        "linetypes": {
            "BYLAYER": {
                "description": "Use the DWG layer linetype",
                "pattern_mm": [],
            },
            "CONTINUOUS": {"description": "Continuous line", "pattern_mm": []},
        },
        "text_styles": {"STANDARD": {"font": "STANDARD"}},
    }
    if blocks:
        tables["blocks"] = blocks

    source: dict[str, Any] = {
        "format": "dwg",
        "version": str(getattr(dwg_document, "version", "unknown")),
    }
    if source_name is not None:
        source["name"] = source_name
    if source_sha256 is not None:
        source["sha256"] = source_sha256

    header_units, units_metadata = _resolve_header_units(dwg_document, context)
    dwg_header_metadata: dict[str, Any] = {
        **units_metadata,
        "block_base_points_status": "not exposed by ezdwg",
    }
    document: dict[str, Any] = {
        "format": "cad2d-ir",
        "version": import_options.ir_version,
        "header": {
            "units": header_units,
            "angle_unit": "deg",
            "coord_space": "world",
            "metadata": {"dwg": dwg_header_metadata},
        },
        "source": source,
        "tables": tables,
        "entities": entities,
    }

    if import_options.validate:
        validate_ir(document)

    block_entity_count = sum(len(block["entities"]) for block in blocks.values())
    statistics: dict[str, Any] = {
        "source_format": "dwg",
        "source_entities": len(source_entities),
        "source_entity_counts": dict(
            sorted(Counter(_source_kind(entity) for entity in source_entities).items())
        ),
        "source_block_definitions": len(block_names),
        "converted_entities": len(entities),
        "converted_block_entities": block_entity_count,
        "converted_entity_counts": dict(sorted(context.converted_counts.items())),
        "skipped_entities": sum(context.skipped_counts.values()),
        "skipped_entity_counts": dict(sorted(context.skipped_counts.items())),
        "approximated_entities": sum(context.approximation_counts.values()),
        "projected_entities": len(context.projected_handles),
        "preserved_dimensions": context.preserved_dimensions,
    }
    return ImportResult(
        document=document,
        diagnostics=context.diagnostics,
        statistics=statistics,
    )


def _read_dwg_tables(
    raw: Any, decode_path: str
) -> tuple[dict[int, str], dict[int, tuple[int | None, int | None]], dict[int, str]]:
    if raw is None:
        return {}, {}, {}

    layer_names: dict[int, str] = {}
    layer_colors: dict[int, tuple[int | None, int | None]] = {}
    block_names: dict[int, str] = {}
    try:
        layer_names = {
            int(handle): str(name)
            for handle, name in raw.decode_layer_names(decode_path)
        }
    except Exception:
        pass
    try:
        layer_colors = {
            int(handle): (_optional_int(index), _optional_int(true_color))
            for handle, index, true_color in raw.decode_layer_colors(decode_path)
        }
    except Exception:
        pass
    try:
        for handle, name in raw.decode_block_header_names(decode_path):
            name_text = str(name)
            if name_text and not _is_space_block(name_text):
                block_names[int(handle)] = name_text
    except Exception:
        pass
    return layer_names, layer_colors, block_names


def _build_layers(
    names: Mapping[int, str],
    colors: Mapping[int, tuple[int | None, int | None]],
) -> tuple[dict[int, str], dict[str, dict[str, Any]]]:
    layer_names: dict[int, str] = {0: _DEFAULT_LAYER}
    layers: dict[str, dict[str, Any]] = {
        _DEFAULT_LAYER: {
            "linetype": "CONTINUOUS",
            "metadata": {"dwg": {"layer_handle": "0"}},
        }
    }
    used = {_DEFAULT_LAYER}
    for handle, raw_name in sorted(names.items()):
        candidate = str(raw_name).strip() or f"DWG_LAYER_{int(handle):X}"
        if candidate == _DEFAULT_LAYER:
            layer_names[int(handle)] = _DEFAULT_LAYER
            dwg_metadata = layers[_DEFAULT_LAYER]["metadata"]["dwg"]
            aliases = dwg_metadata.setdefault("decoded_handles", [])
            aliases.append(_handle_text(int(handle)))
            color = _dwg_color(*(colors.get(int(handle), (None, None))))
            if color is not None:
                layers[_DEFAULT_LAYER]["color"] = color
            continue
        name = _unique_name(candidate, int(handle), used)
        used.add(name)
        layer_names[int(handle)] = name
        layer: dict[str, Any] = {
            "linetype": "CONTINUOUS",
            "metadata": {
                "dwg": {
                    "layer_handle": _handle_text(int(handle)),
                    "original_name": str(raw_name),
                }
            },
        }
        color = _dwg_color(*(colors.get(int(handle), (None, None))))
        if color is not None:
            layer["color"] = color
        layers[name] = layer
    return layer_names, layers


def _convert_entity_sequence(
    source_entities: Iterable[Any], context: _ConversionContext
) -> list[dict[str, Any]]:
    converted: list[dict[str, Any]] = []
    for source_entity in source_entities:
        result = _convert_entity_safe(source_entity, context)
        converted.extend(result)
        for entity in result:
            context.converted_counts[str(entity["kind"])] += 1
    return converted


def _convert_entity_safe(
    source_entity: Any, context: _ConversionContext
) -> list[dict[str, Any]]:
    source_kind = _source_kind(source_entity)
    handle = _entity_handle(source_entity)
    try:
        result = _convert_entity(source_entity, context)
    except (KeyError, TypeError, ValueError, OverflowError) as exc:
        if context.options.strict:
            raise ImporterError(
                f"Failed to convert DWG {source_kind} at {_handle_text(handle)}: {exc}"
            ) from exc
        context.skipped_counts[source_kind] += 1
        context.diagnostics.append(
            ImportDiagnostic(
                code="DWG_ENTITY_CONVERSION_FAILED",
                severity="error",
                message=f"Failed to convert DWG {source_kind}: {exc}",
                source_id=_handle_text(handle),
                source_kind=source_kind,
                action="skipped",
            )
        )
        return []
    if not result:
        context.skipped_counts[source_kind] += 1
    return result


def _convert_entity(
    source_entity: Any, context: _ConversionContext
) -> list[dict[str, Any]]:
    kind = _source_kind(source_entity)
    dxf = _dxf(source_entity)
    handle = _entity_handle(source_entity)
    common = _entity_common(source_entity, context)

    if kind == "LINE":
        return [
            {
                **common,
                "kind": "LINE",
                "p1": _point(dxf["start"], handle, kind, context),
                "p2": _point(dxf["end"], handle, kind, context),
            }
        ]
    if kind == "CIRCLE":
        return [
            {
                **common,
                "kind": "CIRCLE",
                "center": _point(dxf["center"], handle, kind, context),
                "radius": _positive(dxf["radius"], "radius"),
            }
        ]
    if kind == "ARC":
        return [
            {
                **common,
                "kind": "ARC",
                "center": _point(dxf["center"], handle, kind, context),
                "radius": _positive(dxf["radius"], "radius"),
                "start_angle": float(dxf["start_angle"]),
                "end_angle": float(dxf["end_angle"]),
                "ccw": True,
            }
        ]
    if kind == "ELLIPSE":
        ratio = float(dxf["axis_ratio"])
        if not 0.0 < ratio <= 1.0:
            raise ValueError("ellipse axis_ratio must be in (0, 1]")
        _mark_nonplanar_extrusion(dxf, handle, kind, context)
        return [
            {
                **common,
                "kind": "ELLIPSE",
                "center": _point(dxf["center"], handle, kind, context),
                "major_axis": _vector(dxf["major_axis"], handle, kind, context),
                "ratio": ratio,
                "start_param": float(dxf["start_angle"]),
                "end_param": float(dxf["end_angle"]),
                "ccw": True,
            }
        ]
    if kind in {"LWPOLYLINE", "POLYLINE_2D"}:
        return [_convert_polyline(dxf, common, handle, kind, context)]
    if kind in {"LEADER", "MLINE"}:
        points = [
            _point(value, handle, kind, context) for value in dxf.get("points", [])
        ]
        if len(points) < 2:
            raise ValueError("path requires at least two points")
        common["metadata"]["dwg"].update(_json_safe(dxf))
        return [
            {
                **common,
                "kind": "LWPOLYLINE",
                "vertices": points,
                "closed": bool(dxf.get("closed", False)),
            }
        ]
    if kind == "POINT":
        result: dict[str, Any] = {
            **common,
            "kind": "POINT",
            "position": _point(dxf["location"], handle, kind, context),
        }
        angle = float(dxf.get("x_axis_angle", 0.0))
        if angle:
            result["rotation"] = math.degrees(angle)
        return [result]
    if kind in {"TEXT", "ATTRIB", "ATTDEF"}:
        return [_convert_text(dxf, common, handle, kind, context)]
    if kind == "MTEXT":
        return [_convert_mtext(dxf, common, handle, kind, context)]
    if kind == "TOLERANCE":
        return [_convert_tolerance(dxf, common, handle, kind, context)]
    if kind == "SPLINE":
        return [_convert_spline(dxf, common, handle, kind, context)]
    if kind == "HATCH":
        return [_convert_hatch(dxf, common, handle, kind, context)]
    if kind in {"SOLID", "TRACE", "3DFACE"}:
        return [_convert_solid(dxf, common, handle, kind, context)]
    if kind in {"INSERT", "MINSERT"}:
        return [_convert_insert(dxf, common, handle, kind, context)]
    if kind == "DIMENSION":
        context.preserved_dimensions += 1
        return [_convert_dimension(dxf, common, handle, kind, context)]
    return []


def _entity_common(source_entity: Any, context: _ConversionContext) -> dict[str, Any]:
    dxf = _dxf(source_entity)
    kind = _source_kind(source_entity)
    handle = _entity_handle(source_entity)
    layer_handle = _optional_int(dxf.get("layer_handle")) or 0
    layer = context.layer_names.get(layer_handle)
    if layer is None:
        layer = _unique_name(
            f"DWG_LAYER_{layer_handle:X}", layer_handle, set(context.layers)
        )
        context.layer_names[layer_handle] = layer
        context.layers[layer] = {
            "linetype": "CONTINUOUS",
            "metadata": {"dwg": {"layer_handle": _handle_text(layer_handle)}},
        }
    result: dict[str, Any] = {
        "id": context.allocate_id(),
        "layer": layer,
        "linetype": "BYLAYER",
        "source": {"format": "dwg", "id": _handle_text(handle), "kind": kind},
        "metadata": {
            "dwg": {
                "handle": _handle_text(handle),
                "layer_handle": _handle_text(layer_handle),
            }
        },
    }
    color = _dwg_color(
        _optional_int(dxf.get("resolved_color_index")),
        _optional_int(dxf.get("resolved_true_color")),
    )
    if color is None:
        color = _dwg_color(
            _optional_int(dxf.get("color_index")),
            _optional_int(dxf.get("true_color")),
        )
    if color is not None:
        result["color"] = color
    owner = _optional_int(dxf.get("owner_handle"))
    if owner is not None:
        result["metadata"]["dwg"]["owner_handle"] = _handle_text(owner)
    return result


def _convert_polyline(
    dxf: Mapping[str, Any],
    common: dict[str, Any],
    handle: int,
    kind: str,
    context: _ConversionContext,
) -> dict[str, Any]:
    interpolation_applied = bool(dxf.get("interpolation_applied", False))
    raw_points = (
        dxf.get("interpolated_points", [])
        if interpolation_applied
        else dxf.get("points", [])
    )
    points = [_point(value, handle, kind, context) for value in raw_points]
    closed = bool(dxf.get("closed", False))
    if closed and len(points) > 2 and _near(points[0], points[-1]):
        points.pop()
    if len(points) < 2:
        raise ValueError("polyline requires at least two points")
    bulges = [] if interpolation_applied else list(dxf.get("bulges", []))
    vertices: list[list[float]] = []
    for index, point in enumerate(points):
        bulge = float(bulges[index]) if index < len(bulges) else 0.0
        vertices.append([*point, bulge] if abs(bulge) > _EPSILON else point)
    result: dict[str, Any] = {
        **common,
        "kind": "LWPOLYLINE",
        "vertices": vertices,
        "closed": closed,
    }
    result["metadata"]["dwg"].update(
        {
            "flags": int(dxf.get("flags", 0)),
            "widths": _json_safe(dxf.get("widths", [])),
            "const_width": _json_safe(dxf.get("const_width")),
        }
    )
    if interpolation_applied:
        context.approximation_counts[f"{kind}_FIT"] += 1
        result["approximation"] = {
            "method": "polyline",
            "source_kind": f"{kind}_FIT",
            "segments": max(1, len(vertices) - (0 if closed else 1)),
        }
    return result


def _convert_text(
    dxf: Mapping[str, Any],
    common: dict[str, Any],
    handle: int,
    kind: str,
    context: _ConversionContext,
) -> dict[str, Any]:
    height = _positive(dxf.get("height", 0.0), "text height")
    result: dict[str, Any] = {
        **common,
        "kind": "TEXT",
        "insert": _point(dxf["insert"], handle, kind, context),
        "height": height,
        "rotation": float(dxf.get("rotation", 0.0)),
        "text": str(dxf.get("text", "")),
        "style": "STANDARD",
        "halign": _text_halign(int(dxf.get("halign", 0))),
        "valign": _text_valign(int(dxf.get("valign", 0))),
    }
    width_factor = float(dxf.get("width", 1.0))
    if width_factor > 0.0:
        result["width_factor"] = width_factor
    oblique = float(dxf.get("oblique", 0.0))
    if oblique:
        result["oblique_deg"] = oblique
    result["metadata"]["dwg"].update(
        {
            key: _json_safe(dxf[key])
            for key in ("align_point", "style_handle", "text_generation_flag", "tag")
            if key in dxf
        }
    )
    return result


def _convert_mtext(
    dxf: Mapping[str, Any],
    common: dict[str, Any],
    handle: int,
    kind: str,
    context: _ConversionContext,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        **common,
        "kind": "MTEXT",
        "insert": _point(dxf["insert"], handle, kind, context),
        "height": _positive(dxf.get("char_height", 0.0), "MTEXT height"),
        "rotation": float(dxf.get("rotation", 0.0)),
        "text": str(dxf.get("text", "")),
        "style": "STANDARD",
        "attach": _mtext_attachment(int(dxf.get("attachment_point", 1))),
    }
    width = float(dxf.get("rect_width", 0.0))
    if width >= 0.0:
        result["width"] = width
    result["metadata"]["dwg"].update(
        {
            key: _json_safe(dxf[key])
            for key in (
                "raw_text",
                "drawing_direction",
                "background_flags",
                "background_scale_factor",
                "background_color_index",
                "background_true_color",
                "background_transparency",
            )
            if key in dxf
        }
    )
    return result


def _convert_tolerance(
    dxf: Mapping[str, Any],
    common: dict[str, Any],
    handle: int,
    kind: str,
    context: _ConversionContext,
) -> dict[str, Any]:
    result = {
        **common,
        "kind": "MTEXT",
        "insert": _point(dxf["insert"], handle, kind, context),
        "height": _positive(dxf.get("height", 0.0), "tolerance text height"),
        "rotation": float(dxf.get("rotation", 0.0)),
        "text": str(dxf.get("text", "")),
        "style": "STANDARD",
        "attach": "middle_left",
    }
    result["metadata"]["dwg"].update(_json_safe(dxf))
    return result


def _convert_spline(
    dxf: Mapping[str, Any],
    common: dict[str, Any],
    handle: int,
    kind: str,
    context: _ConversionContext,
) -> dict[str, Any]:
    controls = [
        _point(value, handle, kind, context) for value in dxf.get("control_points", [])
    ]
    degree = int(dxf.get("degree", 3))
    if len(controls) >= 2 and 1 <= degree <= 7:
        result: dict[str, Any] = {
            **common,
            "kind": "SPLINE",
            "degree": degree,
            "control_points": controls,
            "closed": bool(dxf.get("closed", False)),
        }
        knots = [float(value) for value in dxf.get("knots", [])]
        if knots:
            result["knots"] = knots
        weights = [float(value) for value in dxf.get("weights", [])]
        if weights and all(value > 0.0 for value in weights):
            result["weights"] = weights
        result["metadata"]["dwg"].update(
            {
                "scenario": _json_safe(dxf.get("scenario")),
                "rational": bool(dxf.get("rational", False)),
                "periodic": bool(dxf.get("periodic", False)),
                "fit_points": _json_safe(dxf.get("fit_points", [])),
            }
        )
        return result

    points = [_point(value, handle, kind, context) for value in dxf.get("points", [])]
    if len(points) < 2:
        raise ValueError("spline has neither control points nor usable fit points")
    context.approximation_counts["SPLINE"] += 1
    return {
        **common,
        "kind": "LWPOLYLINE",
        "vertices": points,
        "closed": bool(dxf.get("closed", False)),
        "approximation": {
            "method": "polyline",
            "source_kind": "SPLINE",
            "segments": max(1, len(points) - 1),
        },
    }


def _convert_hatch(
    dxf: Mapping[str, Any],
    common: dict[str, Any],
    handle: int,
    kind: str,
    context: _ConversionContext,
) -> dict[str, Any]:
    loops: list[dict[str, Any]] = []
    for index, path in enumerate(dxf.get("paths", [])):
        path_map = _mapping(path, f"hatch path {index}")
        points = [
            _point(value, handle, kind, context) for value in path_map.get("points", [])
        ]
        if len(points) > 2 and _near(points[0], points[-1]):
            points.pop()
        if len(points) >= 3:
            loops.append({"vertices": points, "is_outer": index == 0})
    if not loops:
        raise ValueError("hatch has no boundary with at least three points")
    result = {
        **common,
        "kind": "HATCH",
        "solid": bool(dxf.get("solid_fill", False)),
        "pattern": str(dxf.get("pattern_name", "SOLID")),
        "loops": loops,
    }
    result["metadata"]["dwg"].update(
        {
            "associative": bool(dxf.get("associative", False)),
            "elevation": _json_safe(dxf.get("elevation")),
            "extrusion": _json_safe(dxf.get("extrusion")),
        }
    )
    return result


def _convert_solid(
    dxf: Mapping[str, Any],
    common: dict[str, Any],
    handle: int,
    kind: str,
    context: _ConversionContext,
) -> dict[str, Any]:
    points = [_point(value, handle, kind, context) for value in dxf.get("points", [])]
    points = _ordered_polygon(points)
    if len(points) < 3:
        raise ValueError("solid face requires at least three distinct points")
    result = {
        **common,
        "kind": "HATCH",
        "solid": True,
        "pattern": "SOLID",
        "loops": [{"vertices": points, "is_outer": True}],
    }
    result["metadata"]["dwg"].update(
        {
            key: _json_safe(dxf[key])
            for key in ("thickness", "extrusion", "invisible_edge_flags")
            if key in dxf
        }
    )
    return result


def _convert_insert(
    dxf: Mapping[str, Any],
    common: dict[str, Any],
    handle: int,
    kind: str,
    context: _ConversionContext,
) -> dict[str, Any]:
    scale_x = float(dxf.get("xscale", 1.0))
    scale_y = float(dxf.get("yscale", 1.0))
    if abs(scale_x) <= _EPSILON or abs(scale_y) <= _EPSILON:
        if context.options.strict:
            raise ValueError("insert scale must be non-zero")
        scale_x = 1.0 if abs(scale_x) <= _EPSILON else scale_x
        scale_y = 1.0 if abs(scale_y) <= _EPSILON else scale_y
        context.diagnostics.append(
            ImportDiagnostic(
                code="DWG_ZERO_INSERT_SCALE_NORMALIZED",
                severity="warning",
                message="A zero DWG insert scale was replaced with 1.0.",
                source_id=_handle_text(handle),
                source_kind=kind,
                action="normalized",
            )
        )
    block = str(dxf.get("name") or f"UNRESOLVED_BLOCK_{handle:X}")
    result: dict[str, Any] = {
        **common,
        "kind": "INSERT",
        "block": block,
        "insert": _point(dxf["insert"], handle, kind, context),
        "rotation": float(dxf.get("rotation", 0.0)),
    }
    if not (math.isclose(scale_x, 1.0) and math.isclose(scale_y, 1.0)):
        result["scale"] = (
            scale_x if math.isclose(scale_x, scale_y) else [scale_x, scale_y]
        )
    if kind == "MINSERT":
        result["metadata"]["dwg"]["array"] = {
            "column_count": int(dxf.get("column_count", 1)),
            "row_count": int(dxf.get("row_count", 1)),
            "column_spacing": float(dxf.get("column_spacing", 0.0)),
            "row_spacing": float(dxf.get("row_spacing", 0.0)),
        }
        context.diagnostics.append(
            ImportDiagnostic(
                code="DWG_MINSERT_ARRAY_PRESERVED",
                severity="warning",
                message="MINSERT array parameters were preserved on one IR INSERT.",
                source_id=_handle_text(handle),
                source_kind=kind,
                action="preserved_metadata",
            )
        )
    return result


def _convert_dimension(
    dxf: Mapping[str, Any],
    common: dict[str, Any],
    handle: int,
    kind: str,
    context: _ConversionContext,
) -> dict[str, Any]:
    source_kind = str(dxf.get("dimtype", "GENERIC")).upper()
    points = {
        key: _point(dxf[key], handle, kind, context)
        for key in (
            "defpoint",
            "defpoint2",
            "defpoint3",
            "defpoint4",
            "defpoint5",
            "text_midpoint",
            "insert",
        )
        if key in dxf
        and isinstance(dxf[key], Sequence)
        and not isinstance(dxf[key], (str, bytes))
        and len(dxf[key]) >= 2
    }
    definition: dict[str, Any] = {
        "points": points,
        "text": str(dxf.get("text", "")),
        "source_geometry": _json_safe(dxf),
    }
    measurement = dxf.get("actual_measurement")
    if isinstance(measurement, (int, float)) and math.isfinite(float(measurement)):
        definition["measurement"] = float(measurement)
    return {
        **common,
        "kind": "DIMENSION",
        "dim_kind": _DIMENSION_KINDS.get(source_kind, "GENERIC"),
        "definition": definition,
    }


def _append_unresolved_block_diagnostics(
    entities: Sequence[Mapping[str, Any]],
    blocks: Mapping[str, Any],
    context: _ConversionContext,
) -> None:
    unresolved = sorted(
        {
            str(entity["block"])
            for entity in entities
            if entity.get("kind") == "INSERT" and str(entity["block"]) not in blocks
        }
    )
    if unresolved:
        context.diagnostics.append(
            ImportDiagnostic(
                code="DWG_UNRESOLVED_BLOCK_REFERENCE",
                severity="warning",
                message=f"DWG INSERT references have no decoded block body: {unresolved}.",
                source_kind="INSERT",
                action="preserved_reference",
            )
        )


def _enumerate_source_entities(dwg_document: Any) -> Any:
    """Enumerate every entity of the drawing.

    ``ezdwg >= 0.12.1`` partitions ``Document.modelspace()`` by the stored entity
    placement, so block-definition contents are only reachable through
    ``Document.entities()``. This adapter partitions by owner handle itself and
    therefore needs the complete list; older ``ezdwg`` releases expose everything
    through ``modelspace()``.
    """
    layout_factory = getattr(dwg_document, "entities", None)
    if callable(layout_factory):
        return layout_factory().query()
    return dwg_document.modelspace().query()


def _is_paperspace_block(name: str) -> bool:
    return name.strip().upper().startswith("*PAPER_SPACE")


def _is_paperspace_entity(
    source_entity: Any,
    owner_handle: int | None,
    paperspace_handles: set[int],
    placement_of: Any,
) -> bool:
    """Paper-space entities (layout frames, viewports, title blocks) are not part
    of the model-space drawing the IR represents; they are skipped explicitly."""
    if owner_handle is not None and owner_handle in paperspace_handles:
        return True
    if not callable(placement_of):
        return False
    try:
        placement = placement_of(getattr(source_entity, "handle"))
    except Exception:
        return False
    if not isinstance(placement, tuple) or not placement:
        return False
    return placement[0] == 1


def _append_summary_diagnostics(context: _ConversionContext) -> None:
    if context.paperspace_skipped:
        context.diagnostics.append(
            ImportDiagnostic(
                code="DWG_PAPERSPACE_ENTITY_SKIPPED",
                severity="info",
                message=(
                    f"Skipped {context.paperspace_skipped} paper-space DWG entities "
                    "(layouts are not part of the model-space IR)."
                ),
                action="skipped",
            )
        )
    for source_kind, count in sorted(context.skipped_counts.items()):
        if count and not any(
            diagnostic.code == "DWG_ENTITY_CONVERSION_FAILED"
            and diagnostic.source_kind == source_kind
            for diagnostic in context.diagnostics
        ):
            context.diagnostics.append(
                ImportDiagnostic(
                    code="DWG_UNSUPPORTED_ENTITY",
                    severity="warning",
                    message=f"Skipped {count} unsupported DWG {source_kind} entities.",
                    source_kind=source_kind,
                    action="skipped",
                )
            )
    for source_kind, count in sorted(context.approximation_counts.items()):
        context.diagnostics.append(
            ImportDiagnostic(
                code="DWG_CURVE_APPROXIMATED",
                severity="warning",
                message=f"Approximated {count} DWG {source_kind} entities as polylines.",
                source_kind=source_kind,
                action="approximated",
            )
        )
    for source_kind, count in sorted(context.projected_counts.items()):
        context.diagnostics.append(
            ImportDiagnostic(
                code="DWG_NONPLANAR_PROJECTED",
                severity="warning",
                message=f"Projected {count} non-planar DWG {source_kind} entities to XY.",
                source_kind=source_kind,
                action="projected",
            )
        )


def _point(
    value: Any,
    handle: int,
    kind: str,
    context: _ConversionContext,
) -> list[float]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise TypeError("point must be a coordinate sequence")
    if len(value) < 2:
        raise ValueError("point must contain x and y")
    x, y = float(value[0]), float(value[1])
    if not math.isfinite(x) or not math.isfinite(y):
        raise ValueError("point coordinates must be finite")
    if len(value) >= 3 and abs(float(value[2])) > _EPSILON:
        _mark_projected(handle, kind, context)
    return [x, y]


def _vector(
    value: Any,
    handle: int,
    kind: str,
    context: _ConversionContext,
) -> list[float]:
    vector = _point(value, handle, kind, context)
    if math.hypot(*vector) <= _EPSILON:
        raise ValueError("ellipse major axis must be non-zero")
    return vector


def _mark_nonplanar_extrusion(
    dxf: Mapping[str, Any],
    handle: int,
    kind: str,
    context: _ConversionContext,
) -> None:
    extrusion = dxf.get("extrusion")
    if not isinstance(extrusion, Sequence) or len(extrusion) < 3:
        return
    x, y, z = map(float, extrusion[:3])
    if abs(x) > _EPSILON or abs(y) > _EPSILON or abs(abs(z) - 1.0) > _EPSILON:
        _mark_projected(handle, kind, context)


def _mark_projected(handle: int, kind: str, context: _ConversionContext) -> None:
    if handle not in context.projected_handles:
        context.projected_handles.add(handle)
        context.projected_counts[kind] += 1


def _positive(value: Any, label: str) -> float:
    number = float(value)
    if not math.isfinite(number) or number <= 0.0:
        raise ValueError(f"{label} must be positive and finite")
    return number


def _ordered_polygon(points: Sequence[list[float]]) -> list[list[float]]:
    unique: list[list[float]] = []
    for point in points:
        if not any(_near(point, existing) for existing in unique):
            unique.append(point)
    if len(unique) < 3:
        return unique
    center_x = sum(point[0] for point in unique) / len(unique)
    center_y = sum(point[1] for point in unique) / len(unique)
    return sorted(
        unique,
        key=lambda point: math.atan2(point[1] - center_y, point[0] - center_x),
    )


def _dwg_color(index: int | None, true_color: int | None) -> int | str | None:
    if true_color is not None:
        return f"#{true_color & 0xFFFFFF:06X}"
    if index is not None and 0 <= index <= 256:
        return index
    return None


def _text_halign(value: int) -> str:
    return {
        0: "left",
        1: "center",
        2: "right",
        3: "right",
        4: "center",
        5: "right",
    }.get(value, "left")


def _text_valign(value: int) -> str:
    return {0: "baseline", 1: "bottom", 2: "middle", 3: "top"}.get(value, "baseline")


def _mtext_attachment(value: int) -> str:
    return {
        1: "top_left",
        2: "top_center",
        3: "top_right",
        4: "middle_left",
        5: "middle_center",
        6: "middle_right",
        7: "bottom_left",
        8: "bottom_center",
        9: "bottom_right",
    }.get(value, "top_left")


def _source_kind(entity: Any) -> str:
    return str(getattr(entity, "dxftype", "UNKNOWN")).upper()


def _entity_handle(entity: Any) -> int:
    return int(getattr(entity, "handle", 0))


def _dxf(entity: Any) -> Mapping[str, Any]:
    return _mapping(getattr(entity, "dxf", {}), "entity.dxf")


def _mapping(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise TypeError(f"{label} must be a mapping")
    return value


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return None


def _handle_text(handle: int) -> str:
    return f"0x{int(handle):X}"


def _unique_name(candidate: str, handle: int, used: set[str]) -> str:
    if candidate not in used:
        return candidate
    return f"{candidate} [{handle:X}]"


def _is_space_block(name: str) -> bool:
    normalized = name.upper().replace("_", "")
    return normalized in {"*MODELSPACE", "*PAPERSPACE"}


def _near(left: Sequence[float], right: Sequence[float]) -> bool:
    return math.isclose(left[0], right[0], abs_tol=1.0e-9) and math.isclose(
        left[1], right[1], abs_tol=1.0e-9
    )


def _json_safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else str(value)
    if isinstance(value, Mapping):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return [_json_safe(item) for item in value]
    return str(value)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


__all__ = ["convert_dwg_file_to_ir", "dwg_document_to_ir"]
