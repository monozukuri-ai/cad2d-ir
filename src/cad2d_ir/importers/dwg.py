"""Native DWG to CAD 2D IR importer backed by :mod:`ezdwg`."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
import hashlib
import math
from pathlib import Path
import re
from typing import Any, Iterable, Mapping, Sequence

from cad2d_ir.codecs.dxf import INSUNITS_TO_IR_UNITS, text_alignment_from_dxf
from cad2d_ir.importers.base import (
    ImportDiagnostic,
    ImporterError,
    ImportOptions,
    ImportResult,
    MissingOptionalDependencyError,
)
from cad2d_ir.layouts import (
    layout_entity_count,
    layout_paper,
    order_layouts,
    promote_layout_when_model_is_empty,
    shows_the_sheet_itself,
    unnamed_layout_name,
    viewport_from_dxf_values,
)
from cad2d_ir.schema import validate_ir
from cad2d_ir.tolerance import explode_tolerance

_EPSILON = 1.0e-12
_DEFAULT_LAYER = "0"
# "*D12", "*U3", "*T": blocks that the application numbers when it loads a drawing.
_ANONYMOUS_BLOCK_NAME = re.compile(r"^(\*[A-Za-z])\d*$")

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
    linetypes: dict[str, dict[str, Any]] = field(default_factory=dict)
    diagnostics: list[ImportDiagnostic] = field(default_factory=list)
    converted_counts: Counter[str] = field(default_factory=Counter)
    skipped_counts: Counter[str] = field(default_factory=Counter)
    approximation_counts: Counter[str] = field(default_factory=Counter)
    projected_counts: Counter[str] = field(default_factory=Counter)
    projected_handles: set[int] = field(default_factory=set)
    preserved_dimensions: int = 0
    exploded_tolerances: int = 0
    renamed_blocks: int = 0
    hidden_attributes: int = 0
    attached_attributes: int = 0
    inserts_by_handle: dict[int, dict[str, Any]] = field(default_factory=dict)
    next_entity_number: int = 1

    def allocate_id(self) -> str:
        entity_id = f"DWG_E{self.next_entity_number:08d}"
        self.next_entity_number += 1
        return entity_id


@dataclass(slots=True)
class _PaperSheet:
    """What one paper-space block holds: converted entities and its viewports."""

    entities: list[dict[str, Any]] = field(default_factory=list)
    viewports: list[Any] = field(default_factory=list)


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
    try:
        decode_path = str(getattr(document, "decode_path", None) or source_path)
        raw = getattr(document, "raw", getattr(ezdwg, "raw", None))
        layer_names, layer_colors, block_names = _read_dwg_tables(raw, decode_path)
        linetypes, layer_linetypes = _read_dwg_linetypes(raw, decode_path)
        return dwg_document_to_ir(
            document,
            source_name=source_path.name,
            source_sha256=_sha256_file(source_path),
            layer_names_by_handle=layer_names,
            layer_colors_by_handle=layer_colors,
            block_names_by_handle=block_names,
            linetypes_by_handle=linetypes,
            layer_linetypes_by_handle=layer_linetypes,
            layer_states_by_handle=_read_dwg_layer_states(raw, decode_path),
            options=options,
        )
    finally:
        # Native decode helpers cache large per-file tables. ezdwg >= 0.12.5
        # exposes an explicit lifecycle hook so batch conversion can release them.
        clear_decode_caches = getattr(ezdwg, "clear_decode_caches", None)
        if callable(clear_decode_caches):
            try:
                clear_decode_caches()
            except Exception:
                # Cache cleanup must not replace a successful conversion or hide
                # the original importer exception.
                pass


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


def _read_dwg_linetypes(
    raw: Any, decode_path: str
) -> tuple[dict[int, tuple[str, str, list[float]]], dict[int, int]]:
    """Linetype table and the linetype of every layer (``ezdwg`` >= 0.12.10).

    Returns ``({linetype handle: (name, description, dashes)}, {layer handle:
    linetype handle})``; both are empty with an older ``ezdwg``.
    """
    linetypes: dict[int, tuple[str, str, list[float]]] = {}
    layer_linetypes: dict[int, int] = {}
    decode_linetypes = getattr(raw, "decode_linetypes", None)
    if callable(decode_linetypes):
        try:
            for handle, name, description, _length, dashes in decode_linetypes(
                decode_path
            ):
                linetypes[int(handle)] = (
                    str(name),
                    str(description),
                    [float(dash) for dash in dashes],
                )
        except Exception:
            linetypes = {}
    decode_layer_linetypes = getattr(raw, "decode_layer_linetypes", None)
    if callable(decode_layer_linetypes):
        try:
            layer_linetypes = {
                int(layer): int(linetype)
                for layer, linetype in decode_layer_linetypes(decode_path)
            }
        except Exception:
            layer_linetypes = {}
    return linetypes, layer_linetypes


def _read_dwg_layer_states(raw: Any, decode_path: str) -> dict[int, dict[str, Any]]:
    """State of every layer by handle (``ezdwg`` >= 0.12.11); empty with an older one.

    Each state holds ``frozen``, ``off``, ``locked``, ``plot`` and ``lineweight``
    (hundredths of a millimetre, negative for the default weight).
    """
    decode_layer_states = getattr(raw, "decode_layer_states", None)
    if not callable(decode_layer_states):
        return {}
    states: dict[int, dict[str, Any]] = {}
    try:
        for (
            handle,
            frozen,
            off,
            _frozen_in_new,
            locked,
            plot,
            lineweight,
        ) in decode_layer_states(decode_path):
            states[int(handle)] = {
                "frozen": bool(frozen),
                "off": bool(off),
                "locked": bool(locked),
                "plot": bool(plot),
                "lineweight": int(lineweight),
            }
    except Exception:
        return {}
    return states


def _resolve_linetype_scale(dwg_document: Any) -> float | None:
    """``$LTSCALE`` from the DWG header when it is a usable value other than 1.

    Entity and layer linetypes need ``ezdwg`` >= 0.12.10; older versions only record the
    global scale; failures are silent because the units lookup already reports an
    unreadable header.
    """
    header_variables = getattr(dwg_document, "header_variables", None)
    if not callable(header_variables):
        return None
    try:
        value = header_variables().get("ltscale")
    except Exception:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    scale = float(value)
    if not math.isfinite(scale) or scale <= 0.0 or abs(scale - 1.0) < 1e-12:
        return None
    return scale


def dwg_document_to_ir(
    dwg_document: Any,
    *,
    source_name: str | None = None,
    source_sha256: str | None = None,
    layer_names_by_handle: Mapping[int, str] | None = None,
    layer_colors_by_handle: Mapping[int, tuple[int | None, int | None]] | None = None,
    block_names_by_handle: Mapping[int, str] | None = None,
    linetypes_by_handle: Mapping[int, tuple[str, str, Sequence[float]]] | None = None,
    layer_linetypes_by_handle: Mapping[int, int] | None = None,
    layer_states_by_handle: Mapping[int, Mapping[str, Any]] | None = None,
    options: ImportOptions | None = None,
) -> ImportResult:
    """Convert an ``ezdwg.Document``-compatible object directly to IR.

    The extra table arguments are public primarily for adapters and tests. The file
    entry point obtains them from ``ezdwg.raw`` so layer and block names are retained.
    ``linetypes_by_handle`` maps a linetype handle to ``(name, description,
    dashes)`` and ``layer_linetypes_by_handle`` a layer handle to its linetype handle.
    ``layer_states_by_handle`` maps a layer handle to its state: ``frozen``,
    ``off``, ``locked``, ``plot`` (booleans) and ``lineweight`` (hundredths of a
    millimetre, negative for the default weight).
    """
    import_options = options or ImportOptions()
    layer_names, layers = _build_layers(
        layer_names_by_handle or {},
        layer_colors_by_handle or {},
        layer_states_by_handle or {},
    )
    linetype_names, linetypes = _build_linetypes(linetypes_by_handle or {})
    for layer_handle, linetype_handle in (layer_linetypes_by_handle or {}).items():
        layer_name = layer_names.get(int(layer_handle))
        linetype_name = linetype_names.get(int(linetype_handle))
        if layer_name is not None and linetype_name not in (None, "BYLAYER", "BYBLOCK"):
            layers[layer_name]["linetype"] = linetype_name
    context = _ConversionContext(
        options=import_options,
        layer_names=layer_names,
        layers=layers,
        linetypes=linetypes,
    )

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
    entities: list[dict[str, Any]] = []
    block_entities_by_owner: dict[int, list[dict[str, Any]]] = {
        handle: [] for handle in block_names
    }
    # An ATTRIB belongs to a block reference, which can come later in the file.
    attributes: list[tuple[Any, int | None]] = []
    # Paper-space entities by the block record of their sheet. The sheet that
    # was current when the file was saved stores no owner: its key is None.
    paper_sheets: dict[int | None, _PaperSheet] = {}
    source_entity_count = 0
    source_entity_counts: Counter[str] = Counter()
    try:
        source_iterator = iter(_enumerate_source_entities(dwg_document))
    except Exception as exc:
        raise ImporterError(f"Failed to enumerate DWG entities: {exc}") from exc
    while True:
        try:
            source_entity = next(source_iterator)
        except StopIteration:
            break
        except Exception as exc:
            raise ImporterError(f"Failed to enumerate DWG entities: {exc}") from exc

        source_entity_count += 1
        source_kind = _source_kind(source_entity)
        source_entity_counts[source_kind] += 1
        owner_handle, in_paperspace = _entity_owner(
            source_entity, paperspace_handles, placement_of
        )
        if in_paperspace:
            sheet = paper_sheets.setdefault(owner_handle, _PaperSheet())
            if source_kind == "VIEWPORT":
                # Converted once the sheet is known: one of them is the sheet itself.
                sheet.viewports.append(source_entity)
                continue
            in_block = False
            destination = sheet.entities
        elif source_kind == "ATTRIB":
            attributes.append((source_entity, owner_handle))
            continue
        else:
            in_block = owner_handle is not None and owner_handle in block_names
            destination = (
                block_entities_by_owner[owner_handle] if in_block else entities
            )
        if _is_hidden_attribute(source_entity, in_block):
            context.hidden_attributes += 1
            continue
        converted = _convert_entity_sequence((source_entity,), context)
        if source_kind == "ATTDEF" and not in_block:
            # Outside of a block the definition itself is displayed, by its tag.
            for entity in converted:
                tag = entity["metadata"]["dwg"].get("tag")
                if tag:
                    entity["text"] = str(tag)
        destination.extend(converted)

    _attach_attributes(attributes, block_entities_by_owner, entities, context)
    blocks, block_name_by_handle = _build_blocks(
        block_names, block_entities_by_owner, context
    )
    layouts = _build_layouts(paper_sheets, _read_dwg_layouts(dwg_document), context)
    layout_scopes = [layout["entities"] for layout in layouts]
    _link_dimension_blocks(entities, blocks, block_name_by_handle, layout_scopes)

    next_entity_number = 1
    for entity in entities:
        entity["id"] = f"DWG_E{next_entity_number:08d}"
        next_entity_number += 1
    for block in blocks.values():
        for entity in block["entities"]:
            entity["id"] = f"DWG_E{next_entity_number:08d}"
            next_entity_number += 1
    for scope in layout_scopes:
        for entity in scope:
            entity["id"] = f"DWG_E{next_entity_number:08d}"
            next_entity_number += 1

    _append_unresolved_block_diagnostics(
        [*entities, *(entity for scope in layout_scopes for entity in scope)],
        blocks,
        context,
    )
    _append_summary_diagnostics(context)

    tables: dict[str, Any] = {
        "layers": context.layers,
        "linetypes": context.linetypes,
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
    linetype_scale = _resolve_linetype_scale(dwg_document)
    if linetype_scale is not None:
        document["header"]["linetype_scale"] = linetype_scale
    if layouts:
        document["layouts"] = layouts
        context.diagnostics.append(
            ImportDiagnostic(
                code="DWG_PAPERSPACE_LAYOUT_PRESERVED",
                severity="info",
                message=(
                    f"Kept {layout_entity_count(document)} paper-space DWG entities "
                    f"in {len(layouts)} layout(s), apart from model space."
                ),
                action="preserved_layout",
                details={"layouts": [layout["name"] for layout in layouts]},
            )
        )
        promoted = promote_layout_when_model_is_empty(document, metadata_key="dwg")
        if promoted is not None:
            entities = document["entities"]
            context.diagnostics.append(
                ImportDiagnostic(
                    code="DWG_LAYOUT_PROMOTED",
                    severity="info",
                    message=(
                        f"Model space is empty; layout {promoted['name']!r} is "
                        "the drawing."
                    ),
                    action="promoted",
                    details={"layout": promoted["name"]},
                )
            )

    if import_options.validate:
        validate_ir(document)

    block_entity_count = sum(len(block["entities"]) for block in blocks.values())
    statistics: dict[str, Any] = {
        "source_format": "dwg",
        "source_entities": source_entity_count,
        "source_entity_counts": dict(sorted(source_entity_counts.items())),
        "source_block_definitions": len(block_names),
        "converted_entities": len(entities),
        "converted_block_entities": block_entity_count,
        "converted_layout_entities": layout_entity_count(document),
        "layouts": len(document.get("layouts", [])),
        "converted_entity_counts": dict(sorted(context.converted_counts.items())),
        "skipped_entities": sum(context.skipped_counts.values()),
        "skipped_entity_counts": dict(sorted(context.skipped_counts.items())),
        "attached_attributes": context.attached_attributes,
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
        # The model-space and paper-space block records stay in the table: the
        # entities of every sheet but the current one name their paper-space
        # block record as owner, and that is how they are told from model space.
        for handle, name in raw.decode_block_header_names(decode_path):
            name_text = str(name)
            if name_text:
                block_names[int(handle)] = name_text
    except Exception:
        pass
    return layer_names, layer_colors, block_names


_BUILTIN_LINETYPES = ("BYLAYER", "BYBLOCK", "CONTINUOUS")


def _linetype_name(raw_name: Any) -> str | None:
    """IR name of a DWG linetype: the stored name, with the built-in ones in upper case."""
    name = str(raw_name).strip()
    if not name:
        return None
    return name.upper() if name.upper() in _BUILTIN_LINETYPES else name


def _build_linetypes(
    linetypes_by_handle: Mapping[int, tuple[str, str, Sequence[float]]],
) -> tuple[dict[int, str], dict[str, dict[str, Any]]]:
    """Linetype names by handle and the IR linetype table.

    ``pattern_mm`` takes the DWG dash lengths unchanged: both are drawing units at
    linetype scale 1 with the DXF sign convention.
    """
    names: dict[int, str] = {}
    table: dict[str, dict[str, Any]] = {
        "BYLAYER": {"description": "Use the DWG layer linetype", "pattern_mm": []},
        "CONTINUOUS": {"description": "Continuous line", "pattern_mm": []},
    }
    for handle, (raw_name, description, dashes) in sorted(linetypes_by_handle.items()):
        name = _linetype_name(raw_name)
        if name is None:
            continue
        names[int(handle)] = name
        if name in _BUILTIN_LINETYPES:
            continue
        pattern = [float(dash) for dash in dashes]
        if not all(math.isfinite(dash) for dash in pattern):
            pattern = []
        definition: dict[str, Any] = {"pattern_mm": pattern}
        if str(description).strip():
            definition["description"] = str(description).strip()
        table.setdefault(name, definition)
    return names, table


def _apply_layer_state(layer: dict[str, Any], state: Mapping[str, Any] | None) -> None:
    """Plot flag, lineweight and visibility of a layer.

    A layer that is off or frozen shows none of its entities; the IR does not
    tell the two apart, the DWG metadata does.
    """
    if not state:
        return
    if state.get("plot") is False:
        layer["plot"] = False
    lineweight = _optional_int(state.get("lineweight"))
    if lineweight is not None and lineweight >= 0:
        layer["lineweight_mm"] = round(lineweight / 100.0, 6)
    if state.get("off") or state.get("frozen"):
        layer["visible"] = False
    for key in ("off", "frozen", "locked"):
        if state.get(key):
            layer["metadata"]["dwg"][key] = True


def _build_layers(
    names: Mapping[int, str],
    colors: Mapping[int, tuple[int | None, int | None]],
    states: Mapping[int, Mapping[str, Any]],
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
            _apply_layer_state(layers[_DEFAULT_LAYER], states.get(int(handle)))
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
        _apply_layer_state(layer, states.get(int(handle)))
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
    if kind == "POLYLINE_3D":
        polyline = _convert_polyline_3d(dxf, common, handle, kind, context)
        return [] if polyline is None else [polyline]
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
        # Only R13/R14 store the text height of a feature control frame; ezdwg
        # resolves it through the dimension style otherwise. Without a height
        # there is nothing to size the text with.
        height = _finite_number(dxf.get("height"))
        if height is None or height <= 0.0:
            return []
        return _convert_tolerance(dxf, common, handle, kind, context, height)
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
        "linetype": _entity_linetype(dxf, context),
        "source": {"format": "dwg", "id": _handle_text(handle), "kind": kind},
        "metadata": {
            "dwg": {
                "handle": _handle_text(handle),
                "layer_handle": _handle_text(layer_handle),
            }
        },
    }
    linetype_scale = dxf.get("linetype_scale")
    if (
        isinstance(linetype_scale, (int, float))
        and not isinstance(linetype_scale, bool)
        and math.isfinite(linetype_scale)
        and linetype_scale > 0
        and linetype_scale != 1
    ):
        result["linetype_scale"] = float(linetype_scale)
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
    # ezdwg >= 0.12.11: lineweight in hundredths of a millimetre (negative for
    # BYLAYER, BYBLOCK and the default weight) and the invisibility flag.
    lineweight = dxf.get("lineweight")
    if (
        isinstance(lineweight, int)
        and not isinstance(lineweight, bool)
        and lineweight >= 0
    ):
        result["lineweight_mm"] = round(lineweight / 100.0, 6)
    if dxf.get("invisible") is True:
        result["visible"] = False
    owner = _optional_int(dxf.get("owner_handle"))
    if owner is not None:
        result["metadata"]["dwg"]["owner_handle"] = _handle_text(owner)
    return result


def _entity_linetype(dxf: Mapping[str, Any], context: _ConversionContext) -> str:
    """Linetype name of an entity (``ezdwg`` >= 0.12.10 reports it as ``linetype``).

    A name the linetype table does not hold gets an entry without a pattern, so the
    reference stays valid; the entity then draws as a continuous line.
    """
    name = _linetype_name(dxf.get("linetype") or "")
    if name is None:
        return "BYLAYER"
    if name not in context.linetypes and name != "BYBLOCK":
        context.linetypes[name] = {"pattern_mm": []}
    return name


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


def _convert_polyline_3d(
    dxf: Mapping[str, Any],
    common: dict[str, Any],
    handle: int,
    kind: str,
    context: _ConversionContext,
) -> dict[str, Any] | None:
    """3D polyline as its projection to XY, the way the DXF importer reads it.

    The vertices are joined by straight segments (a 3D polyline has no bulges).
    A polyline that leaves the XY plane is counted as projected. ``None`` for a
    polyline whose vertices could not be read: it is skipped as unsupported,
    like every 3D polyline was before.
    """
    raw_points = dxf.get("points", [])
    if not isinstance(raw_points, Sequence) or len(raw_points) < 2:
        return None
    points = [_point(value, handle, kind, context) for value in raw_points]
    closed = bool(dxf.get("closed", False))
    if closed and len(points) > 2 and _near(points[0], points[-1]):
        points.pop()
    if len(points) < 2:
        return None
    result: dict[str, Any] = {
        **common,
        "kind": "LWPOLYLINE",
        "vertices": points,
        "closed": closed,
    }
    result["metadata"]["dwg"]["flags"] = int(dxf.get("flags", 0))
    return result


def _convert_text(
    dxf: Mapping[str, Any],
    common: dict[str, Any],
    handle: int,
    kind: str,
    context: _ConversionContext,
) -> dict[str, Any]:
    result: dict[str, Any] = {
        **common,
        "kind": "TEXT",
        **_text_fields(dxf, handle, kind, context),
    }
    result["metadata"]["dwg"].update(
        {
            key: _json_safe(dxf[key])
            for key in ("align_point", "style_handle", "text_generation_flag", "tag")
            if key in dxf
        }
    )
    return result


def _text_fields(
    dxf: Mapping[str, Any],
    handle: int,
    kind: str,
    context: _ConversionContext,
) -> dict[str, Any]:
    """The fields TEXT and attribute texts share.

    ``insert`` is the point the justification refers to: the alignment point of
    a justified text, the insertion point (left end of the baseline) otherwise.
    """
    halign, valign, anchored = text_alignment_from_dxf(
        _optional_int(dxf.get("halign")), _optional_int(dxf.get("valign"))
    )
    insert = _point(dxf["insert"], handle, kind, context)
    if anchored:
        anchor = _finite_pair(dxf.get("align_point"))
        if anchor is not None:
            insert = anchor
    fields: dict[str, Any] = {
        "insert": insert,
        "height": _positive(dxf.get("height", 0.0), "text height"),
        "rotation": float(dxf.get("rotation", 0.0)),
        "text": str(dxf.get("text", "")),
        "style": "STANDARD",
        "halign": halign,
        "valign": valign,
    }
    width_factor = float(dxf.get("width", 1.0))
    if width_factor > 0.0:
        fields["width_factor"] = width_factor
    oblique = float(dxf.get("oblique", 0.0))
    if oblique:
        fields["oblique_deg"] = oblique
    return fields


def _convert_attribute(
    source_entity: Any, context: _ConversionContext
) -> dict[str, Any]:
    """The text of one ATTRIB, for ``attribute_texts`` of its block reference."""
    dxf = _dxf(source_entity)
    handle = _entity_handle(source_entity)
    common = _entity_common(source_entity, context)
    attribute: dict[str, Any] = {
        "tag": str(dxf.get("tag") or ""),
        **_text_fields(dxf, handle, "ATTRIB", context),
        "layer": common["layer"],
    }
    if "color" in common:
        attribute["color"] = common["color"]
    flags = _optional_int(dxf.get("attribute_flags")) or 0
    if flags & 1 or common.get("visible") is False:
        attribute["visible"] = False
    return attribute


def _attach_attributes(
    attributes: Sequence[tuple[Any, int | None]],
    block_entities_by_owner: Mapping[int, list[dict[str, Any]]],
    entities: list[dict[str, Any]],
    context: _ConversionContext,
) -> None:
    """Give every block reference the texts of its attributes.

    ``attributes`` pairs each ATTRIB with the handle of its owner. An attribute
    whose block reference was not converted stays a TEXT where the file has it,
    as long as it is visible.
    """
    for source_entity, owner_handle in attributes:
        insert = (
            context.inserts_by_handle.get(owner_handle)
            if owner_handle is not None
            else None
        )
        if insert is None:
            if _is_hidden_attribute(source_entity, False):
                context.hidden_attributes += 1
            else:
                destination = block_entities_by_owner.get(owner_handle, entities)
                destination.extend(_convert_entity_sequence((source_entity,), context))
            continue
        try:
            attribute = _convert_attribute(source_entity, context)
        except (KeyError, TypeError, ValueError, OverflowError) as exc:
            handle = _entity_handle(source_entity)
            if context.options.strict:
                raise ImporterError(
                    f"Failed to convert DWG ATTRIB at {_handle_text(handle)}: {exc}"
                ) from exc
            context.skipped_counts["ATTRIB"] += 1
            context.diagnostics.append(
                ImportDiagnostic(
                    code="DWG_ENTITY_CONVERSION_FAILED",
                    severity="error",
                    message=f"Failed to convert DWG ATTRIB: {exc}",
                    source_id=_handle_text(handle),
                    source_kind="ATTRIB",
                    action="skipped",
                )
            )
            continue
        insert.setdefault("attribute_texts", []).append(attribute)
        if attribute["tag"]:
            insert.setdefault("attributes", {})[attribute["tag"]] = attribute["text"]
        context.attached_attributes += 1


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
    height: float,
) -> list[dict[str, Any]]:
    """Feature control frame as the lines of its frame and one TEXT per compartment."""
    common["metadata"]["dwg"]["tolerance_text"] = str(dxf.get("text", ""))
    gap = _finite_number(dxf.get("dimgap"))
    entities = explode_tolerance(
        text=str(dxf.get("text", "")),
        insert=_point(dxf["insert"], handle, kind, context),
        height=height,
        rotation_deg=float(dxf.get("rotation", 0.0)),
        gap=abs(gap) if gap else None,
        common=common,
    )
    if entities:
        context.exploded_tolerances += 1
    for entity in entities:
        if entity["kind"] == "TEXT":
            entity["style"] = "STANDARD"
    return entities


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
    if not result["solid"]:
        result.update(_hatch_pattern(dxf))
    result["metadata"]["dwg"].update(
        {
            "associative": bool(dxf.get("associative", False)),
            "elevation": _json_safe(dxf.get("elevation")),
            "extrusion": _json_safe(dxf.get("extrusion")),
        }
    )
    return result


def _hatch_pattern(dxf: Mapping[str, Any]) -> dict[str, Any]:
    """Pattern definition of a pattern fill (``ezdwg`` >= 0.12.10; angles in degrees).

    The definition lines are stored already rotated and scaled in the coordinate
    system of the boundary paths, exactly as in a DXF file.
    """
    pattern: dict[str, Any] = {}
    angle = _finite_number(dxf.get("pattern_angle"))
    if angle is not None and angle != 0.0:
        pattern["pattern_angle"] = angle
    scale = _finite_number(dxf.get("pattern_scale"))
    if scale is not None and scale > 0.0 and scale != 1.0:
        pattern["pattern_scale"] = scale

    source_lines = dxf.get("pattern_lines")
    if not isinstance(source_lines, Sequence) or isinstance(source_lines, (str, bytes)):
        return pattern
    lines: list[dict[str, Any]] = []
    for source in source_lines:
        if not isinstance(source, Mapping):
            continue
        line_angle = _finite_number(source.get("angle"))
        base = _finite_pair(source.get("base"))
        offset = _finite_pair(source.get("offset"))
        if line_angle is None or base is None or offset is None:
            continue
        direction = math.radians(line_angle)
        spacing = offset[1] * math.cos(direction) - offset[0] * math.sin(direction)
        if abs(spacing) < 1e-12:
            continue  # every line of the family would lie on the same line
        line: dict[str, Any] = {"angle": line_angle, "base": base, "offset": offset}
        dashes = source.get("dashes")
        if isinstance(dashes, Sequence) and not isinstance(dashes, (str, bytes)):
            values = [_finite_number(dash) for dash in dashes]
            if values and all(value is not None for value in values):
                line["dashes"] = values
        lines.append(line)
    if lines:
        pattern["pattern_lines"] = lines
    return pattern


def _finite_number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _finite_pair(value: Any) -> list[float] | None:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return None
    if len(value) < 2:
        return None
    x, y = _finite_number(value[0]), _finite_number(value[1])
    if x is None or y is None:
        return None
    return [x, y]


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
    context.inserts_by_handle[handle] = result
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


def _build_blocks(
    block_names: Mapping[int, str],
    block_entities_by_owner: Mapping[int, list[dict[str, Any]]],
    context: _ConversionContext,
) -> tuple[dict[str, dict[str, Any]], dict[int, str]]:
    """Block definitions by IR name, and the IR name of each block header handle.

    Two block headers can share a name: a DWG stores anonymous blocks without
    their number, and the numbers ezdwg gives them are not always unique. An
    ``INSERT`` names its block, so the last header keeps the name as before; the
    others get a free name and stay reachable through their handle (a
    ``DIMENSION`` refers to its block by handle).
    """
    emitted = [
        (handle, name)
        for handle, name in sorted(block_names.items())
        if block_entities_by_owner[handle]
    ]
    name_owner = {name: handle for handle, name in emitted}
    taken = {name.upper() for name in block_names.values()}
    next_number: dict[str, int] = {}
    blocks: dict[str, dict[str, Any]] = {}
    block_name_by_handle: dict[int, str] = {}
    for owner_handle, source_name in emitted:
        block_name = source_name
        dwg_metadata = {
            "block_header_handle": _handle_text(owner_handle),
            "base_point_status": "not exposed by ezdwg",
        }
        if name_owner[source_name] != owner_handle:
            block_name = _free_block_name(source_name, owner_handle, taken, next_number)
            taken.add(block_name.upper())
            dwg_metadata["source_name"] = source_name
            context.renamed_blocks += 1
        blocks[block_name] = {
            "base_point": [0.0, 0.0],
            "entities": block_entities_by_owner[owner_handle],
            "metadata": {"dwg": dwg_metadata},
        }
        block_name_by_handle[owner_handle] = block_name
    return blocks, block_name_by_handle


def _free_block_name(
    name: str, handle: int, taken: set[str], next_number: dict[str, int]
) -> str:
    anonymous = _ANONYMOUS_BLOCK_NAME.match(name)
    if anonymous:
        prefix = anonymous.group(1)
        number = next_number.get(prefix, 1)
        while f"{prefix}{number}".upper() in taken:
            number += 1
        next_number[prefix] = number + 1
        return f"{prefix}{number}"
    candidate = f"{name}_{handle:X}"
    while candidate.upper() in taken:
        candidate += "_"
    return candidate


def _link_dimension_blocks(
    entities: Sequence[dict[str, Any]],
    blocks: Mapping[str, dict[str, Any]],
    block_name_by_handle: Mapping[int, str],
    extra_scopes: Sequence[Sequence[dict[str, Any]]] = (),
) -> None:
    """Name the block that holds the saved graphics of each dimension.

    ``definition["block"]`` is the same reference the DXF codec keeps (group 2).
    """
    if not block_name_by_handle:
        return
    scopes = [
        entities,
        *(block["entities"] for block in blocks.values()),
        *extra_scopes,
    ]
    for scope in scopes:
        for entity in scope:
            if entity.get("kind") != "DIMENSION":
                continue
            definition = entity["definition"]
            block_handle = _optional_int(
                definition["source_geometry"].get("anonymous_block_handle")
            )
            block_name = block_name_by_handle.get(block_handle)
            if block_name is not None:
                definition["block"] = block_name


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


def _read_dwg_layouts(dwg_document: Any) -> dict[int, dict[str, Any]]:
    """Paper-space layouts by the handle of their block record.

    Uses ``Document.layouts()`` (``ezdwg`` >= 0.12.12). Without it the sheets
    are still told apart by their block record, but they have no name, paper or
    list of viewports.
    """
    factory = getattr(dwg_document, "layouts", None)
    if not callable(factory):
        return {}
    try:
        table = factory()
    except Exception:
        return {}
    if not isinstance(table, Mapping):
        return {}
    layouts: dict[int, dict[str, Any]] = {}
    for name, entry in table.items():
        if not isinstance(entry, Mapping) or entry.get("model"):
            continue
        block_record = _optional_int(entry.get("block_record_handle"))
        if block_record is None:
            continue
        layouts[block_record] = {**entry, "name": str(name)}
    return layouts


def _build_layouts(
    sheets: Mapping[int | None, _PaperSheet],
    layout_infos: Mapping[int, Mapping[str, Any]],
    context: _ConversionContext,
) -> list[dict[str, Any]]:
    """IR layouts of the paper-space sheets, in tab order.

    A sheet that holds neither entities nor a viewport onto model space is left
    out. Sheets without a layout object are named ``Layout1``, ``Layout2``, ...
    """
    active_block = next(
        (block for block, info in layout_infos.items() if info.get("active")), None
    )
    merged: dict[int | None, _PaperSheet] = {}
    for key, sheet in sheets.items():
        target_key = active_block if key is None else key
        target = merged.setdefault(target_key, _PaperSheet())
        target.entities.extend(sheet.entities)
        target.viewports.extend(sheet.viewports)

    def order(key: int | None) -> tuple[int, int, int]:
        tab_order = _optional_int(layout_infos.get(key, {}).get("tab_order"))
        return (
            0 if key is None or key == active_block else 1,
            tab_order if tab_order is not None else 1 << 30,
            key or 0,
        )

    layouts: list[dict[str, Any]] = []
    # A sheet without a layout object must not take the name of one that has.
    reserved_names = {
        str(info["name"]) for info in layout_infos.values() if info.get("name")
    }
    used_names: set[str] = set()
    for key in sorted(merged, key=order):
        sheet = merged[key]
        info = layout_infos.get(key, {})
        viewports = _convert_viewports(sheet.viewports, info, context)
        if not sheet.entities and not viewports:
            continue
        name = str(info.get("name") or unnamed_layout_name(reserved_names | used_names))
        while name in used_names:
            name += "_"
        used_names.add(name)
        layout: dict[str, Any] = {"name": name, "entities": sheet.entities}
        if viewports:
            layout["viewports"] = viewports
        tab_order = _optional_int(info.get("tab_order"))
        if tab_order is not None and tab_order >= 0:
            layout["tab_order"] = tab_order
        if key is None or key == active_block:
            layout["active"] = True
        paper = layout_paper(
            name=info.get("paper_size"),
            width_mm=info.get("paper_width"),
            height_mm=info.get("paper_height"),
            margins_mm=info.get("margins"),
            units_code=info.get("paper_units"),
            rotation_code=info.get("plot_rotation"),
        )
        if paper:
            layout["paper"] = paper
        dwg_metadata: dict[str, Any] = {}
        if key is not None:
            dwg_metadata["block_record_handle"] = _handle_text(key)
        layout_handle = _optional_int(info.get("handle"))
        if layout_handle is not None:
            dwg_metadata["layout_handle"] = _handle_text(layout_handle)
        if dwg_metadata:
            layout["metadata"] = {"dwg": dwg_metadata}
        layouts.append(layout)
    return order_layouts(layouts)


def _convert_viewports(
    viewport_entities: Sequence[Any],
    layout_info: Mapping[str, Any],
    context: _ConversionContext,
) -> list[dict[str, Any]]:
    """IR viewports of one sheet.

    Every sheet has one viewport that stands for the sheet itself: the first of
    the layout's viewport list (R2004+), otherwise the one with the lowest
    handle, as long as its view is its own window. It shows no model space and
    is left out. Viewports without geometry (``ezdwg`` < 0.12.12) are counted as
    unsupported.
    """
    if not viewport_entities:
        return []
    ordered = sorted(viewport_entities, key=_entity_handle)
    listed = [
        handle
        for handle in (
            _optional_int(value) for value in layout_info.get("viewport_handles") or []
        )
        if handle is not None
    ]
    sheet_viewport: int | None
    if listed:
        sheet_viewport = listed[0]
    else:
        first = _dxf(ordered[0])
        sheet_viewport = _entity_handle(ordered[0])
        if "center" in first and not shows_the_sheet_itself(
            center=first["center"],
            height=first.get("height"),
            view_center=first.get("view_center"),
            view_height=first.get("view_height"),
        ):
            sheet_viewport = None
    viewports: list[dict[str, Any]] = []
    for source_entity in ordered:
        handle = _entity_handle(source_entity)
        if handle == sheet_viewport:
            continue
        dxf = _dxf(source_entity)
        if "center" not in dxf:
            context.skipped_counts["VIEWPORT"] += 1
            continue
        common = _entity_common(source_entity, context)
        frozen_handles = dxf.get("frozen_layer_handles")
        if isinstance(frozen_handles, Sequence) and not isinstance(
            frozen_handles, (str, bytes)
        ):
            frozen_layers = [
                context.layer_names[layer_handle]
                for layer_handle in (_optional_int(value) for value in frozen_handles)
                if layer_handle in context.layer_names
            ]
        else:
            frozen_layers = [
                str(name)
                for name in dxf.get("frozen_layers") or []
                if str(name) in context.layers
            ]
        clip = _optional_int(dxf.get("clip_boundary_handle"))
        try:
            viewport = viewport_from_dxf_values(
                source_format="dwg",
                handle=_handle_text(handle),
                center=dxf["center"],
                width=dxf.get("width"),
                height=dxf.get("height"),
                view_center=dxf.get("view_center"),
                view_height=dxf.get("view_height"),
                twist_deg=dxf.get("view_twist_angle"),
                target=dxf.get("view_target"),
                direction=dxf.get("view_direction"),
                status_flags=dxf.get("status_flags"),
                layer=common["layer"],
                frozen_layers=frozen_layers,
                clip_boundary=_handle_text(clip) if clip is not None else None,
            )
        except (KeyError, TypeError, ValueError, IndexError, OverflowError):
            # A viewport without a usable window (zero size, as minimized or
            # never regenerated ones have) shows nothing. It is counted with the
            # unsupported entities, as every viewport was before, in strict mode
            # too.
            context.skipped_counts["VIEWPORT"] += 1
            continue
        viewports.append(viewport)
    return viewports


def _enumerate_source_entities(dwg_document: Any) -> Any:
    """Enumerate every entity of the drawing.

    ``ezdwg >= 0.12.1`` partitions ``Document.modelspace()`` by the stored entity
    placement, so block-definition contents are only reachable through
    ``Document.entities()``. This adapter consumes that complete sequence one
    entity at a time and partitions by owner handle; older ``ezdwg`` releases
    expose the same sequence through ``modelspace()``.
    """
    layout_factory = getattr(dwg_document, "entities", None)
    if callable(layout_factory):
        return layout_factory().query()
    return dwg_document.modelspace().query()


def _is_paperspace_block(name: str) -> bool:
    return name.strip().upper().startswith("*PAPER_SPACE")


def _entity_owner(
    source_entity: Any,
    paperspace_handles: set[int],
    placement_of: Any,
) -> tuple[int | None, bool]:
    """Owner block handle of an entity, and whether it lives in paper space.

    The placement stored in the common entity data decides when ``ezdwg`` exposes
    it (``Document.entity_placement``). The owner handle of the type-specific
    decoders is absent for several entity types (HATCH, SPLINE, SOLID, ...),
    which moved block contents into model space, and it can name another object
    for a model-space INSERT.

    Paper-space entities (layout frames, viewports, title blocks) belong to a
    sheet: the returned owner is the block record of that sheet, or ``None`` for
    the sheet that was current when the file was saved, whose entities store no
    owner.
    """
    owner_handle = _optional_int(_dxf(source_entity).get("owner_handle"))
    placement = _entity_placement(source_entity, placement_of)
    if placement is not None:
        mode, placement_owner = placement
        if mode == 1:
            return None, True
        if mode == 2:
            owner_handle = None
        elif mode == 0 and placement_owner is not None:
            owner_handle = placement_owner
    return owner_handle, owner_handle is not None and owner_handle in paperspace_handles


def _is_hidden_attribute(source_entity: Any, in_block: bool) -> bool:
    """Attribute text that a drawing does not show.

    An attribute definition inside a block definition is a template: a block
    reference shows the values of its own ``ATTRIB`` entities instead. Constant
    definitions (flag 2) have no ``ATTRIB`` and are part of the block. Invisible
    attributes (flag 1) are never shown.
    """
    kind = _source_kind(source_entity)
    if kind not in {"ATTRIB", "ATTDEF"}:
        return False
    flags = _optional_int(_dxf(source_entity).get("attribute_flags")) or 0
    if flags & 1:
        return True
    return kind == "ATTDEF" and in_block and not flags & 2


def _entity_placement(
    source_entity: Any, placement_of: Any
) -> tuple[int, int | None] | None:
    """``(entmode, owner_handle)``: 0 = owner stored, 1 = paper space, 2 = model space."""
    if not callable(placement_of):
        return None
    try:
        placement = placement_of(getattr(source_entity, "handle"))
    except Exception:
        return None
    if not isinstance(placement, tuple) or len(placement) < 2:
        return None
    mode = _optional_int(placement[0])
    if mode is None:
        return None
    return mode, _optional_int(placement[1])


def _append_summary_diagnostics(context: _ConversionContext) -> None:
    if context.hidden_attributes:
        context.diagnostics.append(
            ImportDiagnostic(
                code="DWG_HIDDEN_ATTRIBUTE_SKIPPED",
                severity="info",
                message=(
                    f"Skipped {context.hidden_attributes} DWG attribute definitions "
                    "inside blocks and invisible attributes without a block reference "
                    "(not shown in the drawing)."
                ),
                action="skipped",
            )
        )
    if context.renamed_blocks:
        context.diagnostics.append(
            ImportDiagnostic(
                code="DWG_DUPLICATE_BLOCK_NAME_RENAMED",
                severity="info",
                message=(
                    f"Renamed {context.renamed_blocks} DWG blocks that share their "
                    "name with another block."
                ),
                action="renamed",
            )
        )
    if context.exploded_tolerances:
        context.diagnostics.append(
            ImportDiagnostic(
                code="DWG_TOLERANCE_EXPLODED",
                severity="warning",
                message=(
                    f"Drew {context.exploded_tolerances} DWG feature control frames "
                    "as lines and texts; compartment widths are estimated."
                ),
                source_kind="TOLERANCE",
                action="exploded",
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
