from __future__ import annotations

import codecs
import copy
from dataclasses import dataclass
import math
from pathlib import Path
import re
from typing import TYPE_CHECKING, Any, Literal

from cad2d_ir.constants import CURRENT_IR_VERSION
from cad2d_ir.diagnostics import ExportAction, ExportDiagnostic
from cad2d_ir.schema import validate_ir

if TYPE_CHECKING:
    from cad2d_ir.importers.base import ImportDiagnostic

DXFPair = tuple[int, str]
EntityMapEntry = dict[str, Any]
TargetVersion = Literal["AC1009", "AC1024"]
GenericDimensionMode = Literal["explode", "skip"]

SUPPORTED_DXF_TARGET_VERSIONS: tuple[TargetVersion, ...] = ("AC1009", "AC1024")
_HANDLE_START = 0x100
_HANDLE_EXCLUDED_RECORDS = {"SECTION", "ENDSEC", "ENDTAB", "EOF"}
_CODEPAGE_ENCODINGS = {
    "ANSI_932": "cp932",
    "ANSI_1252": "cp1252",
    "DOS932": "cp932",
    "UTF-8": "utf-8",
}

_ENTITY_COMMON_CODES = {6, 8, 48, 60, 62, 370, 420}
_ENTITY_SUBCLASSES: dict[str, tuple[str, ...]] = {
    "LINE": ("AcDbLine",),
    "CIRCLE": ("AcDbCircle",),
    "ARC": ("AcDbCircle", "AcDbArc"),
    "POINT": ("AcDbPoint",),
    "ELLIPSE": ("AcDbEllipse",),
    "LWPOLYLINE": ("AcDbPolyline",),
    "TEXT": ("AcDbText",),
    "MTEXT": ("AcDbMText",),
    "INSERT": ("AcDbBlockReference",),
    "HATCH": ("AcDbHatch",),
    "SPLINE": ("AcDbSpline",),
    "BLOCK": ("AcDbBlockBegin",),
    "ENDBLK": ("AcDbBlockEnd",),
    "ATTRIB": ("AcDbText", "AcDbAttribute"),
    "SEQEND": ("AcDbSequenceEnd",),
}
_TABLE_RECORD_SUBCLASSES = {
    "LTYPE": "AcDbLinetypeTableRecord",
    "LAYER": "AcDbLayerTableRecord",
    "STYLE": "AcDbTextStyleTableRecord",
}
_DIMENSION_SUBCLASSES = {
    0: "AcDbRotatedDimension",
    1: "AcDbAlignedDimension",
    2: "AcDb2LineAngularDimension",
    3: "AcDbDiametricDimension",
    4: "AcDbRadialDimension",
    5: "AcDb3PointAngularDimension",
    6: "AcDbOrdinateDimension",
}


@dataclass(slots=True)
class _RenderedEntity:
    dxf_type: str
    pairs: list[DXFPair]


@dataclass(slots=True)
class _HandleAllocator:
    next_value: int = _HANDLE_START

    def allocate(self) -> str:
        handle = f"{self.next_value:X}"
        self.next_value += 1
        return handle

    @property
    def handseed(self) -> str:
        return f"{self.next_value:X}"


_INSUNITS_TO_IR = {
    0: "unitless",
    1: "inch",
    2: "ft",
    4: "mm",
    5: "cm",
    6: "m",
}

_IR_TO_INSUNITS = {
    "unitless": 0,
    "unknown": 0,
    "inch": 1,
    "ft": 2,
    "mm": 4,
    "cm": 5,
    "m": 6,
}

_TEXT_HALIGN_TO_DXF = {"left": 0, "center": 1, "right": 2}
_TEXT_VALIGN_TO_DXF = {"baseline": 0, "bottom": 1, "middle": 2, "top": 3}
_TEXT_HALIGN_FROM_DXF = {value: key for key, value in _TEXT_HALIGN_TO_DXF.items()}
_TEXT_VALIGN_FROM_DXF = {value: key for key, value in _TEXT_VALIGN_TO_DXF.items()}

_MTEXT_ATTACH_TO_DXF = {
    "top_left": 1,
    "top_center": 2,
    "top_right": 3,
    "middle_left": 4,
    "middle_center": 5,
    "middle_right": 6,
    "bottom_left": 7,
    "bottom_center": 8,
    "bottom_right": 9,
}
_MTEXT_ATTACH_FROM_DXF = {value: key for key, value in _MTEXT_ATTACH_TO_DXF.items()}

_DIM_KIND_TO_DXF = {
    "LINEAR": 0,
    "ALIGNED": 1,
    "ANGULAR": 2,
    "DIAMETER": 3,
    "RADIAL": 4,
    "ORDINATE": 6,
}

_DIM_KIND_FROM_DXF = {
    0: "LINEAR",
    1: "ALIGNED",
    2: "ANGULAR",
    3: "DIAMETER",
    4: "RADIAL",
    5: "ANGULAR",
    6: "ORDINATE",
}


def _warn(warnings: list[str] | None, message: str) -> None:
    if warnings is not None:
        warnings.append(message)


def _diagnose(
    diagnostics: list[ExportDiagnostic],
    warnings: list[str] | None,
    *,
    code: str,
    severity: Literal["info", "warning", "error"],
    message: str,
    entity_id: str | None = None,
    action: ExportAction | None = None,
) -> None:
    diagnostic = ExportDiagnostic(
        code=code,
        severity=severity,
        message=message,
        entity_id=entity_id,
        action=action,
    )
    diagnostics.append(diagnostic)
    if severity in {"warning", "error"}:
        _warn(warnings, message)


def read_dxf_file(
    path: str | Path,
    *,
    ir_version: str = CURRENT_IR_VERSION,
    validate: bool = True,
    warnings: list[str] | None = None,
    diagnostics: list[ImportDiagnostic] | None = None,
    encoding: str = "auto",
) -> dict[str, Any]:
    from cad2d_ir.importers.base import ImportDiagnostic

    source_path = Path(path)
    raw = source_path.read_bytes()
    selected_encoding, encoding_source = _select_dxf_encoding(raw, encoding)
    dxf_text = raw.decode(selected_encoding, errors="replace")
    replacement_characters = dxf_text.count("�")
    affected_lines = sum("�" in line for line in dxf_text.splitlines())

    import_diagnostics = diagnostics if diagnostics is not None else []
    import_diagnostics.append(
        ImportDiagnostic(
            code="DXF_ENCODING_DETECTED",
            severity="info",
            message=(f"DXF input decoded as {selected_encoding} ({encoding_source})."),
            action="detected",
            details={
                "encoding": selected_encoding,
                "source": encoding_source,
            },
        )
    )
    if replacement_characters:
        message = (
            f"DXF decoding replaced {replacement_characters} undecodable "
            f"character(s) on {affected_lines} line(s)."
        )
        import_diagnostics.append(
            ImportDiagnostic(
                code="DXF_DECODE_REPLACED",
                severity="warning",
                message=message,
                action="normalized",
                details={
                    "encoding": selected_encoding,
                    "replacement_characters": replacement_characters,
                    "affected_lines": affected_lines,
                },
            )
        )
        _warn(warnings, message)

    document = dxf_to_ir(
        dxf_text,
        ir_version=ir_version,
        validate=validate,
        warnings=warnings,
    )
    document["source"] = {
        "format": "dxf",
        "name": source_path.name,
        "metadata": {
            "encoding": selected_encoding,
            "encoding_source": encoding_source,
            "decode_replacement_characters": replacement_characters,
            "decode_replacement_lines": affected_lines,
        },
    }
    return document


def write_dxf_file(
    ir_document: dict[str, Any],
    path: str | Path,
    *,
    validate: bool = True,
    warnings: list[str] | None = None,
    diagnostics: list[ExportDiagnostic] | None = None,
    entity_map: list[EntityMapEntry] | None = None,
    target_version: TargetVersion = "AC1024",
    curve_segments: int = 96,
    generic_dimensions: GenericDimensionMode = "explode",
) -> None:
    Path(path).write_text(
        ir_to_dxf(
            ir_document,
            validate=validate,
            warnings=warnings,
            diagnostics=diagnostics,
            entity_map=entity_map,
            target_version=target_version,
            curve_segments=curve_segments,
            generic_dimensions=generic_dimensions,
        ),
        encoding="utf-8",
    )


def dxf_to_ir(
    dxf_text: str,
    *,
    ir_version: str = CURRENT_IR_VERSION,
    validate: bool = True,
    warnings: list[str] | None = None,
) -> dict[str, Any]:
    pairs = _parse_pairs(dxf_text)
    sections = _split_sections(pairs)

    units = _header_units(sections.get("HEADER", []))
    entities = _entities_to_ir(sections.get("ENTITIES", []), warnings=warnings)
    blocks = _blocks_to_ir(sections.get("BLOCKS", []), warnings=warnings)
    tables = _tables_to_ir(sections.get("TABLES", []))
    if blocks:
        tables["blocks"] = blocks

    ir_document: dict[str, Any] = {
        "format": "cad2d-ir",
        "version": ir_version,
        "header": {
            "units": units,
            "angle_unit": "deg",
            "coord_space": "world",
        },
        "source": {"format": "dxf"},
        "entities": entities,
    }
    if tables:
        ir_document["tables"] = tables

    if validate:
        validate_ir(ir_document)

    return ir_document


def ir_to_dxf(
    ir_document: dict[str, Any],
    *,
    validate: bool = True,
    warnings: list[str] | None = None,
    diagnostics: list[ExportDiagnostic] | None = None,
    entity_map: list[EntityMapEntry] | None = None,
    target_version: TargetVersion = "AC1024",
    curve_segments: int = 96,
    generic_dimensions: GenericDimensionMode = "explode",
) -> str:
    """Serialize an IR document deterministically.

    entity_map and diagnostics are optional output collectors. The string
    return type is retained for compatibility with the original API.
    """
    if target_version not in SUPPORTED_DXF_TARGET_VERSIONS:
        choices = ", ".join(SUPPORTED_DXF_TARGET_VERSIONS)
        raise ValueError(f"target_version must be one of: {choices}")
    if not 8 <= curve_segments <= 4096:
        raise ValueError("curve_segments must be in [8, 4096]")
    if generic_dimensions not in {"explode", "skip"}:
        raise ValueError("generic_dimensions must be 'explode' or 'skip'")
    if validate:
        validate_ir(ir_document)

    export_diagnostics: list[ExportDiagnostic] = []
    generated_map: list[EntityMapEntry] = []
    allocator = _HandleAllocator() if target_version == "AC1024" else None
    export_document = ir_document
    if _dimension_geometry_blocks_are_missing(ir_document):
        export_document = copy.deepcopy(ir_document)
        _ensure_dimension_geometry_blocks(
            export_document,
            diagnostics=export_diagnostics,
            warnings=warnings,
        )

    normalized_tables = _normalized_tables(
        export_document,
        diagnostics=export_diagnostics,
        warnings=warnings,
    )
    table_pairs = _tables_to_pairs(
        normalized_tables,
        target_version=target_version,
        diagnostics=export_diagnostics,
        warnings=warnings,
    )
    if allocator is not None:
        table_pairs = _add_r2010_subclasses(table_pairs)
        table_pairs, _ = _add_record_handles(table_pairs, allocator)

    block_pairs = _blocks_to_pairs(
        export_document.get("tables", {}).get("blocks", {}),
        target_version=target_version,
        curve_segments=curve_segments,
        generic_dimensions=generic_dimensions,
        allocator=allocator,
        diagnostics=export_diagnostics,
        warnings=warnings,
        entity_map=generated_map,
    )

    constraints = export_document.get("constraints")
    if isinstance(constraints, list) and constraints:
        _diagnose(
            export_diagnostics,
            warnings,
            code="DXF_CONSTRAINTS_OMITTED",
            severity="warning",
            message="IR constraints are not representable in plain DXF and were omitted.",
            action="skipped",
        )

    entity_pairs: list[DXFPair] = [(0, "SECTION"), (2, "ENTITIES")]
    for entity in export_document.get("entities", []):
        _append_entity_output(
            entity_pairs,
            entity,
            scope="modelspace",
            target_version=target_version,
            curve_segments=curve_segments,
            generic_dimensions=generic_dimensions,
            allocator=allocator,
            diagnostics=export_diagnostics,
            warnings=warnings,
            entity_map=generated_map,
        )
    entity_pairs.append((0, "ENDSEC"))

    header_pairs = _header_to_pairs(
        export_document,
        target_version=target_version,
        handseed=allocator.handseed if allocator is not None else None,
        diagnostics=export_diagnostics,
        warnings=warnings,
    )

    pairs = header_pairs + table_pairs + block_pairs + entity_pairs + [(0, "EOF")]
    if diagnostics is not None:
        diagnostics.extend(export_diagnostics)
    if entity_map is not None:
        entity_map.extend(generated_map)
    return _format_pairs(pairs)


def _select_dxf_encoding(raw: bytes, requested: str) -> tuple[str, str]:
    normalized = requested.strip().lower()
    if normalized != "auto":
        try:
            codec = codecs.lookup(normalized)
        except LookupError as exc:
            raise ValueError(f"Unknown DXF encoding: {requested!r}") from exc
        return codec.name, "explicit"

    if raw.startswith(codecs.BOM_UTF8):
        return "utf-8-sig", "bom"
    if raw.startswith(codecs.BOM_UTF16_LE) or raw.startswith(codecs.BOM_UTF16_BE):
        return "utf-16", "bom"

    codepage = _raw_dwg_codepage(raw)
    if codepage is not None:
        mapped = _CODEPAGE_ENCODINGS.get(codepage.upper())
        if mapped is not None:
            return mapped, f"$DWGCODEPAGE={codepage}"

    try:
        raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        return "cp932", "cp932-fallback"
    return "utf-8", "utf-8-probe"


def _raw_dwg_codepage(raw: bytes) -> str | None:
    text = raw.decode("latin-1")
    lines = text.splitlines()
    for index in range(0, len(lines) - 3, 2):
        if lines[index].strip() != "9":
            continue
        if lines[index + 1].strip().upper() != "$DWGCODEPAGE":
            continue
        return lines[index + 3].strip()
    return None


def _header_to_pairs(
    ir_document: dict[str, Any],
    *,
    target_version: TargetVersion,
    handseed: str | None,
    diagnostics: list[ExportDiagnostic],
    warnings: list[str] | None,
) -> list[DXFPair]:
    header = ir_document.get("header", {})
    units_name = str(header.get("units", "mm"))
    units = _IR_TO_INSUNITS.get(units_name, 4)
    pairs: list[DXFPair] = [
        (0, "SECTION"),
        (2, "HEADER"),
        (9, "$ACADVER"),
        (1, target_version),
    ]
    if target_version == "AC1024":
        pairs.extend([(9, "$INSUNITS"), (70, str(units))])
        if handseed is not None:
            pairs.extend([(9, "$HANDSEED"), (5, handseed)])
    else:
        pairs.extend(
            [
                (9, "$MEASUREMENT"),
                (70, "0" if units_name in {"inch", "ft"} else "1"),
            ]
        )
        if units_name not in {"mm", "unknown", "unitless"}:
            _diagnose(
                diagnostics,
                warnings,
                code="DXF_R12_INSUNITS_OMITTED",
                severity="warning",
                message=(
                    f"DXF R12 has no $INSUNITS header; IR units {units_name!r} "
                    "were reduced to $MEASUREMENT."
                ),
                action="skipped",
            )
    pairs.append((0, "ENDSEC"))
    return pairs


def _add_r2010_subclasses(pairs: list[DXFPair]) -> list[DXFPair]:
    result: list[DXFPair] = []
    index = 0
    while index < len(pairs):
        if pairs[index][0] != 0:
            result.append(pairs[index])
            index += 1
            continue

        kind = pairs[index][1].strip().upper()
        end = index + 1
        while end < len(pairs) and pairs[end][0] != 0:
            end += 1
        body = pairs[index + 1 : end]
        result.append(pairs[index])
        result.extend(_r2010_record_body(kind, body))
        index = end
    return result


def _r2010_record_body(kind: str, body: list[DXFPair]) -> list[DXFPair]:
    if kind in _HANDLE_EXCLUDED_RECORDS or kind == "ENDTAB":
        return body
    if kind == "TABLE":
        if body and body[0][0] == 2:
            return [body[0], (100, "AcDbSymbolTable"), *body[1:]]
        return [(100, "AcDbSymbolTable"), *body]
    if kind in _TABLE_RECORD_SUBCLASSES:
        return [
            (100, "AcDbSymbolTableRecord"),
            (100, _TABLE_RECORD_SUBCLASSES[kind]),
            *body,
        ]

    split = 0
    while split < len(body) and body[split][0] in _ENTITY_COMMON_CODES:
        split += 1
    common = body[:split]
    payload = body[split:]

    if kind == "ARC":
        circle_codes = {10, 20, 30, 39, 40, 210, 220, 230}
        circle = [pair for pair in payload if pair[0] in circle_codes]
        arc = [pair for pair in payload if pair[0] not in circle_codes]
        return [
            (100, "AcDbEntity"),
            *common,
            (100, "AcDbCircle"),
            *circle,
            (100, "AcDbArc"),
            *arc,
        ]

    if kind == "TEXT":
        alignment_codes = {11, 21, 31, 72, 73}
        text_data = [pair for pair in payload if pair[0] not in alignment_codes]
        alignment = [pair for pair in payload if pair[0] in alignment_codes]
        result = [
            (100, "AcDbEntity"),
            *common,
            (100, "AcDbText"),
            *text_data,
        ]
        if alignment:
            result.extend([(100, "AcDbText"), *alignment])
        return result

    if kind == "ATTRIB":
        attribute_codes = {2, 70, 73, 74, 280}
        text_data = [pair for pair in payload if pair[0] not in attribute_codes]
        attribute = [pair for pair in payload if pair[0] in attribute_codes]
        return [
            (100, "AcDbEntity"),
            *common,
            (100, "AcDbText"),
            *text_data,
            (100, "AcDbAttribute"),
            *attribute,
        ]

    if kind == "DIMENSION":
        dim_type = next(
            (int(float(value)) & 7 for code, value in payload if code == 70),
            0,
        )
        dimension_codes = {
            1,
            2,
            3,
            10,
            11,
            20,
            21,
            30,
            31,
            41,
            42,
            51,
            53,
            70,
            71,
            72,
        }
        dimension = [pair for pair in payload if pair[0] in dimension_codes]
        subtype = [pair for pair in payload if pair[0] not in dimension_codes]
        return [
            (100, "AcDbEntity"),
            *common,
            (100, "AcDbDimension"),
            *dimension,
            (100, _DIMENSION_SUBCLASSES.get(dim_type, "AcDbRotatedDimension")),
            *subtype,
        ]

    subclasses = _ENTITY_SUBCLASSES.get(kind)
    if subclasses is None:
        return body
    return [
        (100, "AcDbEntity"),
        *common,
        *((100, subclass) for subclass in subclasses),
        *payload,
    ]


def _add_record_handles(
    pairs: list[DXFPair], allocator: _HandleAllocator
) -> tuple[list[DXFPair], str | None]:
    result: list[DXFPair] = []
    primary_handle: str | None = None
    pending_table_handle: str | None = None

    for code, value in pairs:
        result.append((code, value))
        if pending_table_handle is not None and code == 2:
            result.append((5, pending_table_handle))
            if primary_handle is None:
                primary_handle = pending_table_handle
            pending_table_handle = None
            continue
        if code != 0 or value.strip().upper() in _HANDLE_EXCLUDED_RECORDS:
            continue

        handle = allocator.allocate()
        if value.strip().upper() == "TABLE":
            pending_table_handle = handle
            continue
        result.append((5, handle))
        if primary_handle is None:
            primary_handle = handle
    return result, primary_handle


def _parse_pairs(dxf_text: str) -> list[DXFPair]:
    raw_lines = dxf_text.splitlines()
    if len(raw_lines) % 2 != 0:
        raise ValueError("DXF text must contain an even number of lines")

    pairs: list[DXFPair] = []
    for index in range(0, len(raw_lines), 2):
        code_text = raw_lines[index].strip()
        value = raw_lines[index + 1].rstrip("\r\n")
        try:
            code = int(code_text)
        except ValueError as exc:
            raise ValueError(
                f"Invalid DXF group code at line {index + 1}: {code_text}"
            ) from exc
        pairs.append((code, value))
    return pairs


def _split_sections(pairs: list[DXFPair]) -> dict[str, list[DXFPair]]:
    sections: dict[str, list[DXFPair]] = {}
    index = 0
    while index < len(pairs):
        code, value = pairs[index]
        if code == 0 and value.strip().upper() == "SECTION":
            if index + 1 >= len(pairs) or pairs[index + 1][0] != 2:
                raise ValueError("Malformed DXF SECTION record")
            name = pairs[index + 1][1].strip().upper()
            index += 2
            content: list[DXFPair] = []
            while index < len(pairs):
                if pairs[index][0] == 0 and pairs[index][1].strip().upper() == "ENDSEC":
                    index += 1
                    break
                content.append(pairs[index])
                index += 1
            sections[name] = content
            continue
        index += 1
    return sections


def _header_units(header_pairs: list[DXFPair]) -> str:
    current_name: str | None = None
    variables: dict[str, list[DXFPair]] = {}

    for code, value in header_pairs:
        if code == 9:
            current_name = value.strip().upper()
            variables[current_name] = []
            continue
        if current_name is not None:
            variables[current_name].append((code, value))

    if "$INSUNITS" not in variables:
        return "mm"
    insunits = _first_int(variables["$INSUNITS"], 70, default=4)
    return _INSUNITS_TO_IR.get(insunits, "mm")


def _tables_to_ir(table_pairs: list[DXFPair]) -> dict[str, Any]:
    records = _pairs_to_records(table_pairs)
    layers: dict[str, dict[str, Any]] = {}
    linetypes: dict[str, dict[str, Any]] = {}
    text_styles: dict[str, dict[str, Any]] = {}
    current_table: str | None = None

    for kind, pairs in records:
        if kind == "TABLE":
            current_table = (_first_string(pairs, 2) or "").upper()
            continue
        if kind == "ENDTAB":
            current_table = None
            continue
        if current_table == "LAYER" and kind == "LAYER":
            name = _first_string(pairs, 2)
            if name:
                layers[name] = _layer_from_table_pairs(pairs)
        elif current_table == "LTYPE" and kind == "LTYPE":
            name = _first_string(pairs, 2)
            if name:
                linetypes[name] = _linetype_from_table_pairs(pairs)
        elif current_table == "STYLE" and kind == "STYLE":
            name = _first_string(pairs, 2)
            if name:
                text_styles[name] = _style_from_table_pairs(pairs)

    tables: dict[str, Any] = {}
    if layers:
        tables["layers"] = layers
    if linetypes:
        tables["linetypes"] = linetypes
    if text_styles:
        tables["text_styles"] = text_styles
    return tables


def _layer_from_table_pairs(pairs: list[DXFPair]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    true_color = _first_int(pairs, 420, default=None)
    color = _first_int(pairs, 62, default=None)
    if true_color is not None:
        result["color"] = f"#{true_color:06X}"
    elif color is not None:
        result["color"] = abs(color)

    linetype = _first_string(pairs, 6)
    if linetype:
        result["linetype"] = linetype

    lineweight = _first_int(pairs, 370, default=None)
    if lineweight is not None and lineweight >= 0:
        result["lineweight_mm"] = round(lineweight / 100.0, 6)

    plot = _first_int(pairs, 290, default=None)
    if plot is not None:
        result["plot"] = bool(plot)
    return result


def _linetype_from_table_pairs(pairs: list[DXFPair]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    description = _first_string(pairs, 3)
    if description:
        result["description"] = description
    pattern = _all_floats(pairs, 49)
    if pattern:
        result["pattern_mm"] = pattern
    else:
        result["pattern_mm"] = []
    return result


def _style_from_table_pairs(pairs: list[DXFPair]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    font = _first_string(pairs, 3)
    if font:
        result["font"] = font
    height = _first_float(pairs, 40, default=None)
    if height is not None:
        result["height"] = height
    width_factor = _first_float(pairs, 41, default=None)
    if width_factor is not None and width_factor > 0:
        result["width_factor"] = width_factor
    oblique = _first_float(pairs, 50, default=None)
    if oblique is not None:
        result["oblique_deg"] = oblique
    return result


def _entities_to_ir(
    entity_pairs: list[DXFPair], *, warnings: list[str] | None = None
) -> list[dict[str, Any]]:
    records = _pairs_to_records(entity_pairs)
    return _records_to_entities(records, warnings=warnings, context="ENTITIES")


def _blocks_to_ir(
    block_pairs: list[DXFPair],
    *,
    warnings: list[str] | None = None,
) -> dict[str, dict[str, Any]]:
    records = _pairs_to_records(block_pairs)
    blocks: dict[str, dict[str, Any]] = {}
    index = 0
    while index < len(records):
        kind, pairs = records[index]
        if kind != "BLOCK":
            index += 1
            continue

        name = _first_string(pairs, 2) or _first_string(pairs, 3)
        if not name:
            index += 1
            continue

        base_point = [
            _first_float(pairs, 10, default=0.0) or 0.0,
            _first_float(pairs, 20, default=0.0) or 0.0,
        ]

        index += 1
        entity_records: list[tuple[str, list[DXFPair]]] = []
        while index < len(records):
            inner_kind, inner_pairs = records[index]
            if inner_kind == "ENDBLK":
                index += 1
                break
            entity_records.append((inner_kind, inner_pairs))
            index += 1

        blocks[name] = {
            "base_point": base_point,
            "entities": _records_to_entities(
                entity_records,
                warnings=warnings,
                context=f"BLOCK:{name}",
            ),
        }
    return blocks


def _pairs_to_records(pairs: list[DXFPair]) -> list[tuple[str, list[DXFPair]]]:
    records: list[tuple[str, list[DXFPair]]] = []
    current_kind: str | None = None
    current_pairs: list[DXFPair] = []

    for code, value in pairs:
        if code == 0:
            if current_kind is not None:
                records.append((current_kind, current_pairs))
            current_kind = value.strip().upper()
            current_pairs = []
        elif current_kind is not None:
            current_pairs.append((code, value))

    if current_kind is not None:
        records.append((current_kind, current_pairs))
    return records


def _records_to_entities(
    records: list[tuple[str, list[DXFPair]]],
    *,
    warnings: list[str] | None = None,
    context: str = "ENTITIES",
) -> list[dict[str, Any]]:
    entities: list[dict[str, Any]] = []
    used_ids: set[str] = set()
    record_index = 0
    serial = 1

    while record_index < len(records):
        kind, pairs = records[record_index]
        consumed = 1
        entity: dict[str, Any] | None = None

        if kind == "LINE":
            entity = _line_from_pairs(pairs, serial)
        elif kind == "CIRCLE":
            entity = _circle_from_pairs(pairs, serial)
        elif kind == "ARC":
            entity = _arc_from_pairs(pairs, serial)
        elif kind == "POINT":
            entity = _point_from_pairs(pairs, serial)
        elif kind == "ELLIPSE":
            entity = _ellipse_from_pairs(pairs, serial)
        elif kind == "LWPOLYLINE":
            entity = _lwpolyline_from_pairs(pairs, serial)
        elif kind == "POLYLINE":
            vertex_pairs: list[list[DXFPair]] = []
            lookahead = record_index + 1
            while lookahead < len(records) and records[lookahead][0] == "VERTEX":
                vertex_pairs.append(records[lookahead][1])
                lookahead += 1
            if lookahead < len(records) and records[lookahead][0] == "SEQEND":
                lookahead += 1
            consumed = max(1, lookahead - record_index)
            entity = _polyline_from_pairs(pairs, vertex_pairs, serial)
        elif kind == "TEXT":
            entity = _text_from_pairs(pairs, serial)
        elif kind == "MTEXT":
            entity = _mtext_from_pairs(pairs, serial)
        elif kind == "INSERT":
            attributes: dict[str, str] = {}
            lookahead = record_index + 1
            while lookahead < len(records) and records[lookahead][0] == "ATTRIB":
                attrib_pairs = records[lookahead][1]
                tag = _first_string(attrib_pairs, 2)
                if tag:
                    attributes[tag] = _first_string(attrib_pairs, 1) or ""
                lookahead += 1
            if lookahead < len(records) and records[lookahead][0] == "SEQEND":
                lookahead += 1
            consumed = max(1, lookahead - record_index)
            entity = _insert_from_pairs(
                pairs, serial, attributes if attributes else None
            )
        elif kind == "HATCH":
            entity = _hatch_from_pairs(pairs, serial)
        elif kind == "SPLINE":
            entity = _spline_from_pairs(pairs, serial)
        elif kind == "DIMENSION":
            entity = _dimension_from_pairs(pairs, serial, warnings=warnings)

        if entity is not None:
            entity.setdefault("source", {"format": "dxf"})["kind"] = kind
            _make_entity_id_unique(entity, used_ids)
            entities.append(entity)
            serial += 1
        else:
            _warn(warnings, f"[{context}] Unsupported DXF entity skipped: {kind}")
        record_index += consumed

    return entities


def _make_entity_id_unique(entity: dict[str, Any], used_ids: set[str]) -> None:
    base = str(entity["id"])
    if base not in used_ids:
        used_ids.add(base)
        return

    suffix_number = 2
    while True:
        suffix = f"_{suffix_number}"
        candidate = f"{base[: 64 - len(suffix)]}{suffix}"
        if candidate not in used_ids:
            entity["id"] = candidate
            used_ids.add(candidate)
            return
        suffix_number += 1


def _common_from_pairs(pairs: list[DXFPair], idx: int) -> dict[str, Any]:
    handle = _first_string(pairs, 5)
    base = f"E{handle}" if handle else f"E{idx}"
    clean_id = re.sub(r"[^A-Za-z0-9_.:\-]", "_", base)[:64]
    if not clean_id or not clean_id[0].isalpha():
        clean_id = f"E{clean_id}"

    entity: dict[str, Any] = {"id": clean_id, "source": {"format": "dxf"}}
    if handle:
        entity["source"]["id"] = handle
    layer = _first_string(pairs, 8)
    if layer:
        entity["layer"] = layer

    linetype = _first_string(pairs, 6)
    if linetype:
        entity["linetype"] = linetype

    true_color = _first_int(pairs, 420, default=None)
    if true_color is not None:
        entity["color"] = f"#{true_color:06X}"
    else:
        aci = _first_int(pairs, 62, default=None)
        if aci is not None:
            entity["color"] = abs(aci)
            if aci < 0:
                entity["visible"] = False

    invisible = _first_int(pairs, 60, default=None)
    if invisible == 1:
        entity["visible"] = False

    lineweight = _first_int(pairs, 370, default=None)
    if lineweight is not None and lineweight >= 0:
        entity["lineweight_mm"] = round(lineweight / 100.0, 6)
    return entity


def _line_from_pairs(pairs: list[DXFPair], idx: int) -> dict[str, Any]:
    entity = _common_from_pairs(pairs, idx)
    entity["kind"] = "LINE"
    entity["p1"] = [_required_float(pairs, 10), _required_float(pairs, 20)]
    entity["p2"] = [_required_float(pairs, 11), _required_float(pairs, 21)]
    return entity


def _circle_from_pairs(pairs: list[DXFPair], idx: int) -> dict[str, Any]:
    entity = _common_from_pairs(pairs, idx)
    entity["kind"] = "CIRCLE"
    entity["center"] = [_required_float(pairs, 10), _required_float(pairs, 20)]
    entity["radius"] = _required_float(pairs, 40)
    return entity


def _arc_from_pairs(pairs: list[DXFPair], idx: int) -> dict[str, Any]:
    entity = _common_from_pairs(pairs, idx)
    entity["kind"] = "ARC"
    entity["center"] = [_required_float(pairs, 10), _required_float(pairs, 20)]
    entity["radius"] = _required_float(pairs, 40)
    entity["start_angle"] = _required_float(pairs, 50)
    entity["end_angle"] = _required_float(pairs, 51)
    return entity


def _point_from_pairs(pairs: list[DXFPair], idx: int) -> dict[str, Any]:
    entity = _common_from_pairs(pairs, idx)
    entity["kind"] = "POINT"
    entity["position"] = [_required_float(pairs, 10), _required_float(pairs, 20)]
    return entity


def _ellipse_from_pairs(pairs: list[DXFPair], idx: int) -> dict[str, Any]:
    entity = _common_from_pairs(pairs, idx)
    entity["kind"] = "ELLIPSE"
    entity["center"] = [_required_float(pairs, 10), _required_float(pairs, 20)]
    entity["major_axis"] = [_required_float(pairs, 11), _required_float(pairs, 21)]
    entity["ratio"] = _required_float(pairs, 40)
    start_param = _first_float(pairs, 41, default=0.0)
    end_param = _first_float(pairs, 42, default=6.283185307179586)
    entity["start_param"] = 0.0 if start_param is None else start_param
    entity["end_param"] = 6.283185307179586 if end_param is None else end_param
    return entity


def _lwpolyline_from_pairs(pairs: list[DXFPair], idx: int) -> dict[str, Any]:
    entity = _common_from_pairs(pairs, idx)
    entity["kind"] = "LWPOLYLINE"

    vertices: list[list[float]] = []
    current: list[float] | None = None
    for code, value in pairs:
        if code == 10:
            if current is not None:
                vertices.append(current)
            current = [_to_float(value), 0.0]
        elif code == 20 and current is not None:
            current[1] = _to_float(value)
        elif code == 42 and current is not None:
            bulge = _to_float(value)
            if len(current) == 2:
                current.append(bulge)
            else:
                current[2] = bulge
    if current is not None:
        vertices.append(current)

    if len(vertices) < 2:
        raise ValueError("LWPOLYLINE requires at least 2 vertices")

    flags = _first_int(pairs, 70, default=0)
    entity["vertices"] = vertices
    if flags & 1:
        entity["closed"] = True
    return entity


def _polyline_from_pairs(
    pairs: list[DXFPair],
    vertex_pairs: list[list[DXFPair]],
    idx: int,
) -> dict[str, Any]:
    entity = _common_from_pairs(pairs, idx)
    entity["kind"] = "LWPOLYLINE"
    vertices: list[list[float]] = []
    for vertex in vertex_pairs:
        point = [_required_float(vertex, 10), _required_float(vertex, 20)]
        bulge = _first_float(vertex, 42, default=None)
        if bulge is not None:
            point.append(bulge)
        vertices.append(point)
    if len(vertices) < 2:
        raise ValueError("POLYLINE requires at least 2 VERTEX records")
    entity["vertices"] = vertices
    flags = _first_int(pairs, 70, default=0) or 0
    if flags & 1:
        entity["closed"] = True
    return entity


def _text_from_pairs(pairs: list[DXFPair], idx: int) -> dict[str, Any]:
    entity = _common_from_pairs(pairs, idx)
    entity["kind"] = "TEXT"
    entity["insert"] = [_required_float(pairs, 10), _required_float(pairs, 20)]
    entity["height"] = _required_float(pairs, 40)
    entity["text"] = _first_string(pairs, 1) or ""

    rotation = _first_float(pairs, 50, default=0.0)
    if rotation != 0.0:
        entity["rotation"] = rotation

    style = _first_string(pairs, 7)
    if style:
        entity["style"] = style

    halign = _TEXT_HALIGN_FROM_DXF.get(_first_int(pairs, 72, default=0), "left")
    valign = _TEXT_VALIGN_FROM_DXF.get(_first_int(pairs, 73, default=0), "baseline")
    if halign != "left":
        entity["halign"] = halign
    if valign != "baseline":
        entity["valign"] = valign

    width_factor = _first_float(pairs, 41, default=None)
    if width_factor is not None:
        entity["width_factor"] = width_factor

    oblique = _first_float(pairs, 51, default=None)
    if oblique is not None:
        entity["oblique_deg"] = oblique
    return entity


def _mtext_from_pairs(pairs: list[DXFPair], idx: int) -> dict[str, Any]:
    entity = _common_from_pairs(pairs, idx)
    entity["kind"] = "MTEXT"
    entity["insert"] = [_required_float(pairs, 10), _required_float(pairs, 20)]
    entity["height"] = _required_float(pairs, 40)

    chunks: list[str] = []
    for code, value in pairs:
        if code in (3, 1):
            chunks.append(value)
    entity["text"] = "".join(chunks)

    rotation = _first_float(pairs, 50, default=0.0)
    if rotation != 0.0:
        entity["rotation"] = rotation

    width = _first_float(pairs, 41, default=None)
    if width is not None:
        entity["width"] = width

    style = _first_string(pairs, 7)
    if style:
        entity["style"] = style

    attach = _MTEXT_ATTACH_FROM_DXF.get(_first_int(pairs, 71, default=1), "top_left")
    if attach != "top_left":
        entity["attach"] = attach
    return entity


def _insert_from_pairs(
    pairs: list[DXFPair], idx: int, attributes: dict[str, str] | None
) -> dict[str, Any]:
    entity = _common_from_pairs(pairs, idx)
    entity["kind"] = "INSERT"
    block_name = _first_string(pairs, 2)
    if not block_name:
        raise ValueError("INSERT requires block name (group code 2)")
    entity["block"] = block_name
    entity["insert"] = [_required_float(pairs, 10), _required_float(pairs, 20)]

    rotation = _first_float(pairs, 50, default=0.0)
    if rotation != 0.0:
        entity["rotation"] = rotation

    sx = _first_float(pairs, 41, default=1.0) or 1.0
    sy = _first_float(pairs, 42, default=sx) or sx
    if abs(sx - sy) < 1e-12:
        if abs(sx - 1.0) > 1e-12:
            entity["scale"] = sx
    else:
        entity["scale"] = [sx, sy]

    if attributes:
        entity["attributes"] = attributes
    return entity


def _hatch_from_pairs(pairs: list[DXFPair], idx: int) -> dict[str, Any]:
    entity = _common_from_pairs(pairs, idx)
    entity["kind"] = "HATCH"

    solid = (_first_int(pairs, 70, default=1) or 1) == 1
    if not solid:
        entity["solid"] = False

    pattern = _first_string(pairs, 2)
    if pattern and pattern.upper() != "SOLID":
        entity["pattern"] = pattern

    loops = _parse_hatch_loops(pairs)
    if not loops:
        raise ValueError("HATCH requires at least one polyline loop")
    entity["loops"] = loops
    return entity


def _spline_from_pairs(pairs: list[DXFPair], idx: int) -> dict[str, Any]:
    entity = _common_from_pairs(pairs, idx)
    entity["kind"] = "SPLINE"

    degree = _first_int(pairs, 71, default=None)
    if degree is None:
        raise ValueError("SPLINE requires degree (group code 71)")
    entity["degree"] = degree

    control_points: list[list[float]] = []
    current: list[float] | None = None
    for code, value in pairs:
        if code == 10:
            if current is not None:
                control_points.append(current)
            current = [_to_float(value), 0.0]
        elif code == 20 and current is not None:
            current[1] = _to_float(value)
    if current is not None:
        control_points.append(current)
    if not control_points:
        raise ValueError("SPLINE requires at least one control point")
    entity["control_points"] = control_points

    knots = _all_floats(pairs, 40)
    if knots:
        entity["knots"] = knots

    weights = _all_floats(pairs, 41)
    if weights:
        entity["weights"] = weights

    flags = _first_int(pairs, 70, default=0) or 0
    if flags & 1:
        entity["closed"] = True
    return entity


def _dimension_from_pairs(
    pairs: list[DXFPair],
    idx: int,
    *,
    warnings: list[str] | None = None,
) -> dict[str, Any]:
    entity = _common_from_pairs(pairs, idx)
    entity["kind"] = "DIMENSION"

    raw_type = _first_int(pairs, 70, default=0) or 0
    dim_type = raw_type & 7
    dim_kind = _DIM_KIND_FROM_DXF.get(dim_type)
    if dim_kind is None:
        dim_kind = "LINEAR"
        _warn(
            warnings,
            f"[DIMENSION] Unsupported dimension type {dim_type}; mapped to LINEAR.",
        )
    entity["dim_kind"] = dim_kind

    style = _first_string(pairs, 3)
    if style:
        entity["style"] = style

    definition: dict[str, Any] = {}
    block_name = _first_string(pairs, 2)
    if block_name:
        definition["block"] = block_name

    text = _first_string(pairs, 1)
    if text is not None and text != "":
        definition["text"] = text

    points: dict[str, list[float]] = {}
    for key, x_code, y_code in (
        ("location", 10, 20),
        ("text_midpoint", 11, 21),
        ("p1", 13, 23),
        ("p2", 14, 24),
        ("p3", 15, 25),
        ("p4", 16, 26),
    ):
        x = _first_float(pairs, x_code, default=None)
        y = _first_float(pairs, y_code, default=None)
        if x is not None and y is not None:
            points[key] = [x, y]
    if points:
        definition["points"] = points

    rotation = _first_float(pairs, 50, default=None)
    if rotation is not None:
        definition["rotation"] = rotation

    text_rotation = _first_float(pairs, 53, default=None)
    if text_rotation is not None:
        definition["text_rotation"] = text_rotation

    measurement = _first_float(pairs, 42, default=None)
    if measurement is not None:
        definition["measurement"] = measurement

    entity["definition"] = definition
    return entity


def _parse_hatch_loops(pairs: list[DXFPair]) -> list[dict[str, Any]]:
    loop_count_index = next(
        (i for i, (code, _) in enumerate(pairs) if code == 91), None
    )
    if loop_count_index is None:
        return []

    num_loops = int(float(pairs[loop_count_index][1]))
    cursor = loop_count_index + 1
    loops: list[dict[str, Any]] = []

    for loop_index in range(num_loops):
        while cursor < len(pairs) and pairs[cursor][0] != 92:
            cursor += 1
        if cursor >= len(pairs):
            break

        path_flags = int(float(pairs[cursor][1]))
        cursor += 1
        if not (path_flags & 2):
            while cursor < len(pairs) and pairs[cursor][0] not in {92, 75, 76, 98}:
                cursor += 1
            continue

        has_bulge = 0
        while cursor < len(pairs):
            code, value = pairs[cursor]
            if code == 72:
                has_bulge = int(float(value))
                cursor += 1
            elif code == 73:
                cursor += 1
            elif code == 93:
                vertex_count = int(float(value))
                cursor += 1
                break
            else:
                cursor += 1
        else:
            break

        vertices: list[list[float]] = []
        for _ in range(vertex_count):
            x: float | None = None
            y: float | None = None
            bulge: float | None = None

            while cursor < len(pairs):
                code, value = pairs[cursor]
                if code == 10 and x is None:
                    x = _to_float(value)
                    cursor += 1
                    continue
                if code == 20 and y is None:
                    y = _to_float(value)
                    cursor += 1
                    continue
                if code == 42 and has_bulge:
                    bulge = _to_float(value)
                    cursor += 1
                    continue
                if x is not None and y is not None:
                    break
                cursor += 1

            if x is None or y is None:
                break
            if has_bulge and bulge is not None:
                vertices.append([x, y, bulge])
            else:
                vertices.append([x, y])

        if len(vertices) >= 3:
            loops.append({"vertices": vertices, "is_outer": loop_index == 0})
    return loops


def _line_to_pairs(entity: dict[str, Any]) -> list[DXFPair]:
    p1 = entity["p1"]
    p2 = entity["p2"]
    pairs = [(0, "LINE")]
    pairs.extend(_common_to_pairs(entity))
    pairs.extend(
        [(10, _num(p1[0])), (20, _num(p1[1])), (11, _num(p2[0])), (21, _num(p2[1]))]
    )
    return pairs


def _circle_to_pairs(entity: dict[str, Any]) -> list[DXFPair]:
    center = entity["center"]
    pairs = [(0, "CIRCLE")]
    pairs.extend(_common_to_pairs(entity))
    pairs.extend(
        [(10, _num(center[0])), (20, _num(center[1])), (40, _num(entity["radius"]))]
    )
    return pairs


def _arc_to_pairs(entity: dict[str, Any]) -> list[DXFPair]:
    center = entity["center"]
    pairs = [(0, "ARC")]
    pairs.extend(_common_to_pairs(entity))
    pairs.extend(
        [
            (10, _num(center[0])),
            (20, _num(center[1])),
            (40, _num(entity["radius"])),
            (50, _num(entity["start_angle"])),
            (51, _num(entity["end_angle"])),
        ]
    )
    return pairs


def _point_to_pairs(entity: dict[str, Any]) -> list[DXFPair]:
    position = entity["position"]
    pairs = [(0, "POINT")]
    pairs.extend(_common_to_pairs(entity))
    pairs.extend([(10, _num(position[0])), (20, _num(position[1]))])
    return pairs


def _ellipse_to_pairs(entity: dict[str, Any]) -> list[DXFPair]:
    center = entity["center"]
    major_axis = entity["major_axis"]
    start_param = entity["start_param"]
    end_param = entity["end_param"]
    if entity.get("ccw") is False:
        start_param, end_param = end_param, start_param

    pairs = [(0, "ELLIPSE")]
    pairs.extend(_common_to_pairs(entity))
    pairs.extend(
        [
            (10, _num(center[0])),
            (20, _num(center[1])),
            (11, _num(major_axis[0])),
            (21, _num(major_axis[1])),
            (40, _num(entity["ratio"])),
            (41, _num(start_param)),
            (42, _num(end_param)),
        ]
    )
    return pairs


def _lwpolyline_to_pairs(entity: dict[str, Any]) -> list[DXFPair]:
    vertices = entity["vertices"]
    pairs = [(0, "LWPOLYLINE")]
    pairs.extend(_common_to_pairs(entity))
    pairs.append((90, str(len(vertices))))
    pairs.append((70, str(1 if entity.get("closed") else 0)))
    for vertex in vertices:
        pairs.append((10, _num(vertex[0])))
        pairs.append((20, _num(vertex[1])))
        if len(vertex) >= 3:
            pairs.append((42, _num(vertex[2])))
    return pairs


def _text_to_pairs(entity: dict[str, Any]) -> list[DXFPair]:
    insert = entity["insert"]
    pairs = [(0, "TEXT")]
    pairs.extend(_common_to_pairs(entity))
    pairs.extend(
        [
            (10, _num(insert[0])),
            (20, _num(insert[1])),
            (40, _num(entity["height"])),
            (1, str(entity["text"])),
        ]
    )

    rotation = float(entity.get("rotation", 0.0))
    if rotation != 0.0:
        pairs.append((50, _num(rotation)))

    style = entity.get("style")
    if style:
        pairs.append((7, str(style)))

    halign = str(entity.get("halign", "left"))
    valign = str(entity.get("valign", "baseline"))
    halign_code = _TEXT_HALIGN_TO_DXF.get(halign, 0)
    valign_code = _TEXT_VALIGN_TO_DXF.get(valign, 0)
    if halign_code != 0:
        pairs.append((72, str(halign_code)))
    if valign_code != 0:
        pairs.append((73, str(valign_code)))
    if halign_code != 0 or valign_code != 0:
        pairs.append((11, _num(insert[0])))
        pairs.append((21, _num(insert[1])))

    width_factor = entity.get("width_factor")
    if width_factor is not None:
        pairs.append((41, _num(width_factor)))

    oblique = entity.get("oblique_deg")
    if oblique is not None:
        pairs.append((51, _num(oblique)))
    return pairs


def _mtext_to_pairs(entity: dict[str, Any]) -> list[DXFPair]:
    insert = entity["insert"]
    pairs = [(0, "MTEXT")]
    pairs.extend(_common_to_pairs(entity))
    pairs.extend(
        [
            (10, _num(insert[0])),
            (20, _num(insert[1])),
            (40, _num(entity["height"])),
            (1, str(entity["text"])),
        ]
    )

    rotation = float(entity.get("rotation", 0.0))
    if rotation != 0.0:
        pairs.append((50, _num(rotation)))

    width = entity.get("width")
    if width is not None:
        pairs.append((41, _num(width)))

    style = entity.get("style")
    if style:
        pairs.append((7, str(style)))

    attach = entity.get("attach")
    if isinstance(attach, str):
        attach_code = _MTEXT_ATTACH_TO_DXF.get(attach)
        if attach_code is not None and attach_code != 1:
            pairs.append((71, str(attach_code)))
    return pairs


def _insert_to_pairs(entity: dict[str, Any]) -> list[DXFPair]:
    insert = entity["insert"]
    pairs = [(0, "INSERT")]
    pairs.extend(_common_to_pairs(entity))
    pairs.extend(
        [
            (2, str(entity["block"])),
            (10, _num(insert[0])),
            (20, _num(insert[1])),
        ]
    )

    rotation = float(entity.get("rotation", 0.0))
    if rotation != 0.0:
        pairs.append((50, _num(rotation)))

    scale = entity.get("scale", 1)
    if isinstance(scale, (int, float)):
        scale_value = float(scale)
        if abs(scale_value - 1.0) > 1e-12:
            pairs.append((41, _num(scale_value)))
            pairs.append((42, _num(scale_value)))
    elif isinstance(scale, list) and len(scale) == 2:
        pairs.append((41, _num(scale[0])))
        pairs.append((42, _num(scale[1])))

    attributes = entity.get("attributes")
    if isinstance(attributes, dict) and attributes:
        pairs.append((66, "1"))
        for tag, value in attributes.items():
            pairs.extend(
                [
                    (0, "ATTRIB"),
                    (8, str(entity.get("layer", "0"))),
                    (10, _num(insert[0])),
                    (20, _num(insert[1])),
                    (40, "1"),
                    (2, str(tag)),
                    (1, str(value)),
                    (70, "0"),
                ]
            )
        pairs.extend([(0, "SEQEND"), (8, str(entity.get("layer", "0")))])
    return pairs


def _hatch_to_pairs(entity: dict[str, Any]) -> list[DXFPair]:
    loops = entity["loops"]
    if not isinstance(loops, list) or not loops:
        raise ValueError("HATCH requires non-empty loops")

    solid = bool(entity.get("solid", True))
    pattern = str(entity.get("pattern", "SOLID")) if not solid else "SOLID"

    pairs = [(0, "HATCH")]
    pairs.extend(_common_to_pairs(entity))
    pairs.extend(
        [
            (10, "0"),
            (20, "0"),
            (30, "0"),
            (210, "0"),
            (220, "0"),
            (230, "1"),
            (2, pattern),
            (70, "1" if solid else "0"),
            (71, "0"),
            (91, str(len(loops))),
        ]
    )

    for loop in loops:
        vertices = loop["vertices"]
        has_bulge = any(len(vertex) >= 3 for vertex in vertices)
        pairs.extend(
            [
                (92, "2"),
                (72, "1" if has_bulge else "0"),
                (73, "1"),
                (93, str(len(vertices))),
            ]
        )
        for vertex in vertices:
            pairs.append((10, _num(vertex[0])))
            pairs.append((20, _num(vertex[1])))
            if has_bulge:
                pairs.append((42, _num(vertex[2] if len(vertex) >= 3 else 0.0)))

    pairs.extend([(75, "0"), (76, "1")])
    if not solid:
        pairs.append((78, "0"))
    pairs.append((98, "0"))
    return pairs


def _spline_to_pairs(entity: dict[str, Any]) -> list[DXFPair]:
    control_points = entity["control_points"]
    degree = int(entity["degree"])
    knots = entity.get("knots")
    if not isinstance(knots, list) or not knots:
        knots = _default_spline_knots(len(control_points), degree)

    pairs = [(0, "SPLINE")]
    pairs.extend(_common_to_pairs(entity))
    flags = 1 if entity.get("closed") else 0
    pairs.extend(
        [
            (70, str(flags)),
            (71, str(degree)),
            (72, str(len(knots))),
            (73, str(len(control_points))),
            (74, "0"),
        ]
    )

    for knot in knots:
        pairs.append((40, _num(knot)))

    for point in control_points:
        pairs.append((10, _num(point[0])))
        pairs.append((20, _num(point[1])))

    weights = entity.get("weights")
    if isinstance(weights, list):
        for weight in weights:
            pairs.append((41, _num(weight)))
    return pairs


def _dimension_to_pairs(entity: dict[str, Any]) -> list[DXFPair]:
    definition = entity.get("definition")
    if not isinstance(definition, dict):
        definition = {}

    dim_kind = str(entity.get("dim_kind", "LINEAR"))
    if dim_kind == "GENERIC":
        raise ValueError(
            "GENERIC dimensions must be exploded or skipped before writing"
        )

    pairs = [(0, "DIMENSION")]
    pairs.extend(_common_to_pairs(entity))

    block_name = definition.get("block")
    if block_name:
        pairs.append((2, str(block_name)))

    style = entity.get("style", "Standard")
    pairs.append((3, str(style)))

    dim_code = _DIM_KIND_TO_DXF.get(dim_kind, 0)
    pairs.append((70, str(dim_code)))

    points = definition.get("points")
    if not isinstance(points, dict):
        points = {}

    location = _dimension_point(points, definition, "location", default=[0.0, 0.0])
    pairs.append((10, _num(location[0])))
    pairs.append((20, _num(location[1])))
    pairs.append((30, "0"))

    text_midpoint = _dimension_point(points, definition, "text_midpoint", default=None)
    if text_midpoint is not None:
        pairs.append((11, _num(text_midpoint[0])))
        pairs.append((21, _num(text_midpoint[1])))
        pairs.append((31, "0"))

    for key, x_code, y_code in (
        ("p1", 13, 23),
        ("p2", 14, 24),
        ("p3", 15, 25),
        ("p4", 16, 26),
    ):
        point = _dimension_point(points, definition, key, default=None)
        if point is not None:
            pairs.append((x_code, _num(point[0])))
            pairs.append((y_code, _num(point[1])))

    text_value = definition.get("text")
    if text_value is not None:
        pairs.append((1, str(text_value)))

    rotation = definition.get("rotation")
    if isinstance(rotation, (int, float)):
        pairs.append((50, _num(rotation)))

    text_rotation = definition.get("text_rotation")
    if isinstance(text_rotation, (int, float)):
        pairs.append((53, _num(text_rotation)))

    measurement = definition.get("measurement")
    if isinstance(measurement, (int, float)):
        pairs.append((42, _num(measurement)))
    return pairs


def _entity_to_pairs(entity: dict[str, Any]) -> list[DXFPair]:
    kind = entity.get("kind")
    if kind == "LINE":
        return _line_to_pairs(entity)
    if kind == "CIRCLE":
        return _circle_to_pairs(entity)
    if kind == "ARC":
        return _arc_to_pairs(entity)
    if kind == "POINT":
        return _point_to_pairs(entity)
    if kind == "ELLIPSE":
        return _ellipse_to_pairs(entity)
    if kind == "LWPOLYLINE":
        return _lwpolyline_to_pairs(entity)
    if kind == "TEXT":
        return _text_to_pairs(entity)
    if kind == "MTEXT":
        return _mtext_to_pairs(entity)
    if kind == "INSERT":
        return _insert_to_pairs(entity)
    if kind == "HATCH":
        return _hatch_to_pairs(entity)
    if kind == "SPLINE":
        return _spline_to_pairs(entity)
    if kind == "DIMENSION":
        return _dimension_to_pairs(entity)
    raise ValueError(f"Unsupported IR entity kind for DXF writer: {kind}")


def _dimension_geometry_blocks_are_missing(document: dict[str, Any]) -> bool:
    tables = document.get("tables")
    blocks = tables.get("blocks") if isinstance(tables, dict) else None
    if not isinstance(blocks, dict):
        blocks = {}
    block_names = {str(name).upper() for name in blocks}

    scopes: list[Any] = [document.get("entities")]
    scopes.extend(
        block.get("entities") for block in blocks.values() if isinstance(block, dict)
    )
    for entities in scopes:
        if not isinstance(entities, list):
            continue
        for entity in entities:
            if (
                not isinstance(entity, dict)
                or entity.get("kind") != "DIMENSION"
                or entity.get("dim_kind") == "GENERIC"
            ):
                continue
            definition = entity.get("definition")
            block_name = (
                str(definition.get("block", "")) if isinstance(definition, dict) else ""
            )
            if not block_name or block_name.upper() not in block_names:
                return True
    return False


def _ensure_dimension_geometry_blocks(
    document: dict[str, Any],
    *,
    diagnostics: list[ExportDiagnostic],
    warnings: list[str] | None,
) -> None:
    tables = document.setdefault("tables", {})
    if not isinstance(tables, dict):
        return
    blocks = tables.setdefault("blocks", {})
    if not isinstance(blocks, dict):
        return

    scope_entities: list[list[dict[str, Any]]] = []
    modelspace = document.get("entities")
    if isinstance(modelspace, list):
        scope_entities.append(modelspace)
    for block in list(blocks.values()):
        if not isinstance(block, dict):
            continue
        entities = block.get("entities")
        if isinstance(entities, list):
            scope_entities.append(entities)

    used_names = {str(name).upper() for name in blocks}
    next_number = 0
    for entities in scope_entities:
        for entity in entities:
            if (
                not isinstance(entity, dict)
                or entity.get("kind") != "DIMENSION"
                or entity.get("dim_kind") == "GENERIC"
            ):
                continue
            definition = entity.get("definition")
            if not isinstance(definition, dict):
                definition = {}
                entity["definition"] = definition

            raw_name = definition.get("block")
            block_name = str(raw_name) if raw_name else ""
            if block_name and block_name.upper() in used_names:
                continue
            if not block_name:
                while f"*D{next_number}".upper() in used_names:
                    next_number += 1
                block_name = f"*D{next_number}"
                next_number += 1

            definition["block"] = block_name
            entity_id = str(entity.get("id", "")) or None
            blocks[block_name] = {
                "base_point": [0.0, 0.0],
                "entities": _dimension_geometry_entities(
                    entity,
                    serial=next_number,
                ),
                "metadata": {
                    "cad2d_ir": {
                        "generated_for_entity_id": entity_id,
                    }
                },
            }
            used_names.add(block_name.upper())
            _diagnose(
                diagnostics,
                warnings,
                code="DXF_DIMENSION_BLOCK_GENERATED",
                severity="info",
                message=(
                    f"[DIMENSION:{entity_id or '?'}] geometry block "
                    f"{block_name!r} was generated for DXF export."
                ),
                entity_id=entity_id,
                action="normalized",
            )


def _dimension_geometry_entities(
    dimension: dict[str, Any],
    *,
    serial: int,
) -> list[dict[str, Any]]:
    definition = dimension.get("definition")
    if not isinstance(definition, dict):
        definition = {}
    points = definition.get("points")
    if not isinstance(points, dict):
        points = {}

    common = _entity_common_values(dimension)
    common.pop("id", None)
    entities: list[dict[str, Any]] = []
    p1 = points.get("p1")
    p2 = points.get("p2")
    if _point_like(p1) and _point_like(p2):
        entities.append(
            {
                **common,
                "id": f"DGEOM_{serial}_L",
                "kind": "LINE",
                "p1": [float(p1[0]), float(p1[1])],
                "p2": [float(p2[0]), float(p2[1])],
            }
        )

    text_value = definition.get("text")
    if text_value is not None:
        insert = points.get("text_midpoint", points.get("location"))
        if not _point_like(insert) and _point_like(p1) and _point_like(p2):
            insert = [
                (float(p1[0]) + float(p2[0])) / 2.0,
                (float(p1[1]) + float(p2[1])) / 2.0,
            ]
        if not _point_like(insert):
            insert = [0.0, 0.0]
        height = definition.get("text_height", 2.5)
        if not isinstance(height, (int, float)) or float(height) <= 0.0:
            height = 2.5
        text_entity: dict[str, Any] = {
            **common,
            "id": f"DGEOM_{serial}_T",
            "kind": "TEXT",
            "insert": [float(insert[0]), float(insert[1])],
            "height": float(height),
            "text": str(text_value),
        }
        style = dimension.get("style")
        if isinstance(style, str):
            text_entity["style"] = style
        entities.append(text_entity)

    if not entities:
        location = points.get("location", definition.get("location"))
        if not _point_like(location):
            location = [0.0, 0.0]
        entities.append(
            {
                **common,
                "id": f"DGEOM_{serial}_P",
                "kind": "POINT",
                "position": [float(location[0]), float(location[1])],
            }
        )
    return entities


def _normalized_tables(
    ir_document: dict[str, Any],
    *,
    diagnostics: list[ExportDiagnostic],
    warnings: list[str] | None,
) -> dict[str, dict[str, dict[str, Any]]]:
    raw_tables = ir_document.get("tables")
    if not isinstance(raw_tables, dict):
        raw_tables = {}

    def definitions(name: str) -> dict[str, dict[str, Any]]:
        raw = raw_tables.get(name)
        if not isinstance(raw, dict):
            return {}
        return {
            str(key): dict(value) if isinstance(value, dict) else {}
            for key, value in raw.items()
        }

    layers = definitions("layers")
    linetypes = definitions("linetypes")
    text_styles = definitions("text_styles")

    entities: list[dict[str, Any]] = [
        entity for entity in ir_document.get("entities", []) if isinstance(entity, dict)
    ]
    blocks = raw_tables.get("blocks")
    if isinstance(blocks, dict):
        for definition in blocks.values():
            if not isinstance(definition, dict):
                continue
            entities.extend(
                entity
                for entity in definition.get("entities", [])
                if isinstance(entity, dict)
            )

    referenced_layers = {str(entity.get("layer", "0")) for entity in entities}
    referenced_linetypes = {
        str(entity["linetype"])
        for entity in entities
        if isinstance(entity.get("linetype"), str)
    }
    referenced_styles = {
        str(entity["style"])
        for entity in entities
        if entity.get("kind") in {"TEXT", "MTEXT", "DIMENSION"}
        and isinstance(entity.get("style"), str)
    }
    for entity in entities:
        if entity.get("kind") != "DIMENSION":
            continue
        definition = entity.get("definition")
        if not isinstance(definition, dict):
            continue
        source_geometry = definition.get("source_geometry")
        if not isinstance(source_geometry, dict):
            continue
        text_geometry = source_geometry.get("text")
        if not isinstance(text_geometry, dict):
            continue
        font_name = str(text_geometry.get("font_name", "")).strip()
        if font_name:
            referenced_styles.add(font_name)
    referenced_linetypes.update(
        str(definition["linetype"])
        for definition in layers.values()
        if isinstance(definition.get("linetype"), str)
    )

    _ensure_table_record(
        layers,
        "0",
        {"color": 7, "linetype": "CONTINUOUS", "plot": True},
        table_name="LAYER",
        diagnostics=diagnostics,
        warnings=warnings,
    )
    for name in sorted(referenced_layers, key=str.casefold):
        _ensure_table_record(
            layers,
            name,
            {"color": 7, "linetype": "CONTINUOUS", "plot": True},
            table_name="LAYER",
            diagnostics=diagnostics,
            warnings=warnings,
        )

    _ensure_table_record(
        linetypes,
        "CONTINUOUS",
        {"description": "Continuous line", "pattern_mm": []},
        table_name="LTYPE",
        diagnostics=diagnostics,
        warnings=warnings,
    )
    for name in sorted(referenced_linetypes, key=str.casefold):
        _ensure_table_record(
            linetypes,
            name,
            {"description": f"Defaulted linetype {name}", "pattern_mm": []},
            table_name="LTYPE",
            diagnostics=diagnostics,
            warnings=warnings,
        )

    _ensure_table_record(
        text_styles,
        "STANDARD",
        {"font": "txt"},
        table_name="STYLE",
        diagnostics=diagnostics,
        warnings=warnings,
    )
    for name in sorted(referenced_styles, key=str.casefold):
        _ensure_table_record(
            text_styles,
            name,
            {"font": str(name)},
            table_name="STYLE",
            diagnostics=diagnostics,
            warnings=warnings,
        )

    return {
        "layers": layers,
        "linetypes": linetypes,
        "text_styles": text_styles,
    }


def _ensure_table_record(
    table: dict[str, dict[str, Any]],
    name: str,
    default: dict[str, Any],
    *,
    table_name: str,
    diagnostics: list[ExportDiagnostic],
    warnings: list[str] | None,
) -> None:
    if any(existing.upper() == name.upper() for existing in table):
        return
    table[name] = default
    _diagnose(
        diagnostics,
        warnings,
        code="DXF_TABLE_DEFAULTED",
        severity="info",
        message=f"DXF {table_name} record {name!r} was synthesized from defaults.",
        action="normalized",
    )


def _tables_to_pairs(
    tables: dict[str, dict[str, dict[str, Any]]],
    *,
    target_version: TargetVersion,
    diagnostics: list[ExportDiagnostic],
    warnings: list[str] | None,
) -> list[DXFPair]:
    pairs: list[DXFPair] = [(0, "SECTION"), (2, "TABLES")]

    linetypes = tables["linetypes"]
    pairs.extend([(0, "TABLE"), (2, "LTYPE"), (70, str(len(linetypes)))])
    for name in sorted(linetypes, key=str.casefold):
        definition = linetypes[name]
        pattern = definition.get("pattern_mm", [])
        if not isinstance(pattern, list):
            pattern = []
        numeric_pattern = [
            float(value) for value in pattern if isinstance(value, (int, float))
        ]
        pairs.extend(
            [
                (0, "LTYPE"),
                (2, name),
                (70, "0"),
                (3, str(definition.get("description", ""))),
                (72, "65"),
                (73, str(len(numeric_pattern))),
                (40, _num(sum(abs(value) for value in numeric_pattern))),
            ]
        )
        for value in numeric_pattern:
            pairs.extend([(49, _num(value)), (74, "0")])
    pairs.append((0, "ENDTAB"))

    layers = tables["layers"]
    pairs.extend([(0, "TABLE"), (2, "LAYER"), (70, str(len(layers)))])
    for name in sorted(layers, key=str.casefold):
        definition = layers[name]
        pairs.extend([(0, "LAYER"), (2, name), (70, "0")])
        color = definition.get("color", 7)
        if isinstance(color, int):
            pairs.append((62, str(max(0, min(256, color)))))
        elif isinstance(color, str) and re.fullmatch(
            r"#([A-Fa-f0-9]{6}|[A-Fa-f0-9]{8})", color
        ):
            if target_version == "AC1009":
                aci = _nearest_aci(color)
                pairs.append((62, str(aci)))
                _diagnose(
                    diagnostics,
                    warnings,
                    code="DXF_R12_TRUE_COLOR_APPROXIMATED",
                    severity="warning",
                    message=(
                        f"Layer {name!r} true color {color} was approximated "
                        f"as ACI {aci} for DXF R12."
                    ),
                    action="approximated",
                )
            else:
                pairs.extend([(62, "7"), (420, str(int(color[1:7], 16)))])
        else:
            pairs.append((62, "7"))

        pairs.append((6, str(definition.get("linetype", "CONTINUOUS"))))
        lineweight = definition.get("lineweight_mm")
        if isinstance(lineweight, (int, float)):
            if target_version == "AC1024":
                pairs.append((370, str(int(round(float(lineweight) * 100.0)))))
            else:
                _diagnose(
                    diagnostics,
                    warnings,
                    code="DXF_R12_LINEWEIGHT_OMITTED",
                    severity="warning",
                    message=f"Layer {name!r} lineweight was omitted from DXF R12.",
                    action="skipped",
                )
        if target_version == "AC1024":
            pairs.append((290, "1" if definition.get("plot", True) else "0"))
    pairs.append((0, "ENDTAB"))

    styles = tables["text_styles"]
    pairs.extend([(0, "TABLE"), (2, "STYLE"), (70, str(len(styles)))])
    for name in sorted(styles, key=str.casefold):
        definition = styles[name]
        pairs.extend(
            [
                (0, "STYLE"),
                (2, name),
                (70, "0"),
                (40, _num(definition.get("height", 0.0))),
                (41, _num(definition.get("width_factor", 1.0))),
                (50, _num(definition.get("oblique_deg", 0.0))),
                (71, "0"),
                (42, "0"),
                (3, str(definition.get("font", "txt"))),
                (4, ""),
            ]
        )
    pairs.extend([(0, "ENDTAB"), (0, "ENDSEC")])
    return pairs


def _blocks_to_pairs(
    blocks: Any,
    *,
    target_version: TargetVersion,
    curve_segments: int,
    generic_dimensions: GenericDimensionMode,
    allocator: _HandleAllocator | None,
    diagnostics: list[ExportDiagnostic],
    warnings: list[str] | None,
    entity_map: list[EntityMapEntry],
) -> list[DXFPair]:
    pairs: list[DXFPair] = [(0, "SECTION"), (2, "BLOCKS")]
    if not isinstance(blocks, dict):
        blocks = {}

    for name in sorted(blocks, key=str.casefold):
        definition = blocks[name]
        if not isinstance(definition, dict):
            continue
        base = definition.get("base_point", [0.0, 0.0])
        block_start: list[DXFPair] = [
            (0, "BLOCK"),
            (8, "0"),
            (2, str(name)),
            (70, "0"),
            (10, _num(base[0])),
            (20, _num(base[1])),
            (30, "0"),
            (3, str(name)),
            (1, ""),
        ]
        if allocator is not None:
            block_start = _add_r2010_subclasses(block_start)
            block_start, _ = _add_record_handles(block_start, allocator)
        pairs.extend(block_start)

        metadata = definition.get("metadata")
        generated_for: str | None = None
        if isinstance(metadata, dict):
            cad2d_metadata = metadata.get("cad2d_ir")
            if isinstance(cad2d_metadata, dict):
                value = cad2d_metadata.get("generated_for_entity_id")
                if isinstance(value, str):
                    generated_for = value

        for entity in definition.get("entities", []):
            _append_entity_output(
                pairs,
                entity,
                scope=f"block:{name}",
                ir_id_override=generated_for,
                target_version=target_version,
                curve_segments=curve_segments,
                generic_dimensions=generic_dimensions,
                allocator=allocator,
                diagnostics=diagnostics,
                warnings=warnings,
                entity_map=entity_map,
            )

        block_end: list[DXFPair] = [(0, "ENDBLK"), (8, "0")]
        if allocator is not None:
            block_end = _add_r2010_subclasses(block_end)
            block_end, _ = _add_record_handles(block_end, allocator)
        pairs.extend(block_end)

    pairs.append((0, "ENDSEC"))
    return pairs


def _append_entity_output(
    output_pairs: list[DXFPair],
    entity: dict[str, Any],
    *,
    scope: str,
    target_version: TargetVersion,
    curve_segments: int,
    generic_dimensions: GenericDimensionMode,
    allocator: _HandleAllocator | None,
    diagnostics: list[ExportDiagnostic],
    warnings: list[str] | None,
    entity_map: list[EntityMapEntry],
    ir_id_override: str | None = None,
) -> None:
    rendered, reason_code = _render_entity(
        entity,
        target_version=target_version,
        curve_segments=curve_segments,
        generic_dimensions=generic_dimensions,
        diagnostics=diagnostics,
        warnings=warnings,
    )
    ir_id = ir_id_override if ir_id_override is not None else str(entity.get("id", ""))
    if not rendered:
        entity_map.append(
            {
                "ir_id": ir_id,
                "handle": None,
                "dxf_type": None,
                "index": _next_output_index(entity_map),
                "scope": scope,
                "reason_code": reason_code,
            }
        )
        return

    for item in rendered:
        pairs = item.pairs
        handle: str | None = None
        if allocator is not None:
            pairs = _add_r2010_subclasses(pairs)
            pairs, handle = _add_record_handles(pairs, allocator)
        output_pairs.extend(pairs)
        entity_map.append(
            {
                "ir_id": ir_id,
                "handle": handle,
                "dxf_type": item.dxf_type,
                "index": _next_output_index(entity_map),
                "scope": scope,
            }
        )


def _next_output_index(entity_map: list[EntityMapEntry]) -> int:
    return sum(entry.get("dxf_type") is not None for entry in entity_map)


def _render_entity(
    entity: dict[str, Any],
    *,
    target_version: TargetVersion,
    curve_segments: int,
    generic_dimensions: GenericDimensionMode,
    diagnostics: list[ExportDiagnostic],
    warnings: list[str] | None,
) -> tuple[list[_RenderedEntity], str | None]:
    entity_id = str(entity.get("id", ""))
    kind = str(entity.get("kind", ""))

    if kind == "DIMENSION" and entity.get("dim_kind") == "GENERIC":
        if generic_dimensions == "skip":
            message = (
                f"[DIMENSION:{entity_id or '?'}] GENERIC dimension was omitted "
                "by generic_dimensions='skip'."
            )
            _diagnose(
                diagnostics,
                warnings,
                code="DXF_GENERIC_DIMENSION_SKIPPED",
                severity="warning",
                message=message,
                entity_id=entity_id or None,
                action="skipped",
            )
            return [], "DXF_GENERIC_DIMENSION_SKIPPED"

        components = _generic_dimension_components(
            entity,
            diagnostics=diagnostics,
            warnings=warnings,
        )
        if not components:
            message = (
                f"[DIMENSION:{entity_id or '?'}] GENERIC dimension had no "
                "renderable primitive geometry and was omitted."
            )
            _diagnose(
                diagnostics,
                warnings,
                code="DXF_GENERIC_DIMENSION_GEOMETRY_MISSING",
                severity="warning",
                message=message,
                entity_id=entity_id or None,
                action="skipped",
            )
            return [], "DXF_GENERIC_DIMENSION_GEOMETRY_MISSING"

        _diagnose(
            diagnostics,
            warnings,
            code="DXF_GENERIC_DIMENSION_EXPLODED",
            severity="info",
            message=(
                f"[DIMENSION:{entity_id or '?'}] GENERIC dimension was "
                f"expanded into {len(components)} primitive(s)."
            ),
            entity_id=entity_id or None,
            action="exploded",
        )
        rendered: list[_RenderedEntity] = []
        for component in components:
            child_rendered, _ = _render_entity(
                component,
                target_version=target_version,
                curve_segments=curve_segments,
                generic_dimensions=generic_dimensions,
                diagnostics=diagnostics,
                warnings=warnings,
            )
            rendered.extend(child_rendered)
        return rendered, None

    prepared = entity
    if target_version == "AC1009":
        prepared = _prepare_r12_entity(
            entity,
            diagnostics=diagnostics,
            warnings=warnings,
        )

        if kind == "LWPOLYLINE":
            _diagnose(
                diagnostics,
                warnings,
                code="DXF_R12_LWPOLYLINE_EXPLODED",
                severity="info",
                message=f"[{kind}:{entity_id or '?'}] emitted as POLYLINE/VERTEX.",
                entity_id=entity_id or None,
                action="exploded",
            )
            return [_RenderedEntity("POLYLINE", _polyline_r12_to_pairs(prepared))], None

        if kind == "MTEXT":
            text_entities, formatting_changed = _explode_mtext(prepared)
            _diagnose(
                diagnostics,
                warnings,
                code="DXF_R12_MTEXT_EXPLODED",
                severity="warning",
                message=(
                    f"[MTEXT:{entity_id or '?'}] expanded into "
                    f"{len(text_entities)} TEXT entity/entities for DXF R12."
                ),
                entity_id=entity_id or None,
                action="exploded",
            )
            if formatting_changed:
                _diagnose(
                    diagnostics,
                    warnings,
                    code="DXF_R12_MTEXT_FORMATTING_NORMALIZED",
                    severity="warning",
                    message=(
                        f"[MTEXT:{entity_id or '?'}] formatting codes were "
                        "removed for DXF R12."
                    ),
                    entity_id=entity_id or None,
                    action="normalized",
                )
            return [
                _RenderedEntity("TEXT", _text_to_pairs(text_entity))
                for text_entity in text_entities
            ], None

        if kind == "ELLIPSE":
            polyline = {
                **_entity_common_values(prepared),
                "kind": "LWPOLYLINE",
                "vertices": _sample_ellipse(prepared, curve_segments),
                "closed": _ellipse_is_closed(prepared),
            }
            _diagnose(
                diagnostics,
                warnings,
                code="DXF_R12_ELLIPSE_APPROXIMATED",
                severity="warning",
                message=(
                    f"[ELLIPSE:{entity_id or '?'}] approximated as a "
                    f"{len(polyline['vertices'])}-vertex POLYLINE for DXF R12."
                ),
                entity_id=entity_id or None,
                action="approximated",
            )
            return [_RenderedEntity("POLYLINE", _polyline_r12_to_pairs(polyline))], None

        if kind == "SPLINE":
            vertices = _sample_spline(prepared, curve_segments)
            polyline = {
                **_entity_common_values(prepared),
                "kind": "LWPOLYLINE",
                "vertices": vertices,
                "closed": bool(prepared.get("closed")),
            }
            _diagnose(
                diagnostics,
                warnings,
                code="DXF_R12_SPLINE_APPROXIMATED",
                severity="warning",
                message=(
                    f"[SPLINE:{entity_id or '?'}] approximated as a "
                    f"{len(vertices)}-vertex POLYLINE for DXF R12."
                ),
                entity_id=entity_id or None,
                action="approximated",
            )
            return [_RenderedEntity("POLYLINE", _polyline_r12_to_pairs(polyline))], None

        if kind == "HATCH":
            rendered = []
            for loop in prepared.get("loops", []):
                polyline = {
                    **_entity_common_values(prepared),
                    "kind": "LWPOLYLINE",
                    "vertices": loop["vertices"],
                    "closed": True,
                }
                rendered.append(
                    _RenderedEntity("POLYLINE", _polyline_r12_to_pairs(polyline))
                )
            _diagnose(
                diagnostics,
                warnings,
                code="DXF_R12_HATCH_EXPLODED",
                severity="warning",
                message=(
                    f"[HATCH:{entity_id or '?'}] fill was replaced by "
                    f"{len(rendered)} boundary POLYLINE(s) for DXF R12."
                ),
                entity_id=entity_id or None,
                action="exploded",
            )
            return rendered, None

    supported = {
        "LINE",
        "CIRCLE",
        "ARC",
        "POINT",
        "ELLIPSE",
        "LWPOLYLINE",
        "TEXT",
        "MTEXT",
        "INSERT",
        "HATCH",
        "SPLINE",
        "DIMENSION",
    }
    if kind not in supported:
        _diagnose(
            diagnostics,
            warnings,
            code="DXF_UNKNOWN_ENTITY_SKIPPED",
            severity="warning",
            message=(
                f"[{kind or 'UNKNOWN'}:{entity_id or '?'}] unsupported IR "
                "entity kind was skipped during DXF export."
            ),
            entity_id=entity_id or None,
            action="skipped",
        )
        return [], "DXF_UNKNOWN_ENTITY_SKIPPED"

    if kind == "INSERT" and "transform" in entity:
        _diagnose(
            diagnostics,
            warnings,
            code="DXF_INSERT_TRANSFORM_OMITTED",
            severity="warning",
            message=(
                f"[INSERT:{entity_id or '?'}] affine transform is not "
                "representable by plain DXF INSERT and was omitted."
            ),
            entity_id=entity_id or None,
            action="skipped",
        )

    return [_RenderedEntity(kind, _entity_to_pairs(prepared))], None


def _entity_common_values(entity: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "id",
        "layer",
        "linetype",
        "color",
        "lineweight_mm",
        "visible",
    )
    return {key: entity[key] for key in keys if key in entity}


def _prepare_r12_entity(
    entity: dict[str, Any],
    *,
    diagnostics: list[ExportDiagnostic],
    warnings: list[str] | None,
) -> dict[str, Any]:
    prepared = dict(entity)
    entity_id = str(entity.get("id", "")) or None
    color = prepared.get("color")
    if isinstance(color, str) and re.fullmatch(
        r"#([A-Fa-f0-9]{6}|[A-Fa-f0-9]{8})", color
    ):
        aci = _nearest_aci(color)
        prepared["color"] = aci
        _diagnose(
            diagnostics,
            warnings,
            code="DXF_R12_TRUE_COLOR_APPROXIMATED",
            severity="warning",
            message=(
                f"[{entity.get('kind', 'ENTITY')}:{entity_id or '?'}] true "
                f"color {color} was approximated as ACI {aci} for DXF R12."
            ),
            entity_id=entity_id,
            action="approximated",
        )
    if "lineweight_mm" in prepared:
        prepared.pop("lineweight_mm")
        _diagnose(
            diagnostics,
            warnings,
            code="DXF_R12_LINEWEIGHT_OMITTED",
            severity="warning",
            message=(
                f"[{entity.get('kind', 'ENTITY')}:{entity_id or '?'}] "
                "lineweight was omitted from DXF R12."
            ),
            entity_id=entity_id,
            action="skipped",
        )
    return prepared


def _nearest_aci(color: str) -> int:
    rgb = tuple(int(color[index : index + 2], 16) for index in (1, 3, 5))
    palette = {
        1: (255, 0, 0),
        2: (255, 255, 0),
        3: (0, 255, 0),
        4: (0, 255, 255),
        5: (0, 0, 255),
        6: (255, 0, 255),
        7: (255, 255, 255),
    }
    return min(
        palette,
        key=lambda index: sum(
            (rgb[channel] - palette[index][channel]) ** 2 for channel in range(3)
        ),
    )


def _polyline_r12_to_pairs(entity: dict[str, Any]) -> list[DXFPair]:
    layer = str(entity.get("layer", "0"))
    pairs: list[DXFPair] = [(0, "POLYLINE")]
    pairs.extend(_common_to_pairs(entity))
    pairs.extend(
        [
            (66, "1"),
            (10, "0"),
            (20, "0"),
            (30, "0"),
            (70, "1" if entity.get("closed") else "0"),
        ]
    )
    for vertex in entity["vertices"]:
        pairs.extend(
            [
                (0, "VERTEX"),
                (8, layer),
                (10, _num(vertex[0])),
                (20, _num(vertex[1])),
                (30, "0"),
            ]
        )
        if len(vertex) >= 3:
            pairs.append((42, _num(vertex[2])))
    pairs.extend([(0, "SEQEND"), (8, layer)])
    return pairs


def _explode_mtext(entity: dict[str, Any]) -> tuple[list[dict[str, Any]], bool]:
    plain, formatting_changed = _plain_mtext(str(entity.get("text", "")))
    lines = plain.splitlines() or [""]
    insert = entity["insert"]
    height = float(entity["height"])
    rotation = float(entity.get("rotation", 0.0))
    angle = math.radians(rotation)
    step = height * 1.2
    result: list[dict[str, Any]] = []
    for index, line in enumerate(lines):
        text_entity: dict[str, Any] = {
            **_entity_common_values(entity),
            "kind": "TEXT",
            "insert": [
                float(insert[0]) + math.sin(angle) * step * index,
                float(insert[1]) - math.cos(angle) * step * index,
            ],
            "height": height,
            "text": line,
        }
        for key in ("rotation", "style"):
            if key in entity:
                text_entity[key] = entity[key]
        result.append(text_entity)
    return result, formatting_changed or len(lines) > 1


def _plain_mtext(text: str) -> tuple[str, bool]:
    original = text
    plain = text.replace("\\P", "\n").replace("\\~", " ")
    plain = re.sub(
        r"\\U\+([0-9A-Fa-f]{4})",
        lambda match: chr(int(match.group(1), 16)),
        plain,
    )
    plain = re.sub(r"\\[ACFHQTW][^;]*;", "", plain)
    plain = re.sub(r"\\[LlOoKk]", "", plain)
    plain = plain.replace("\\{", "{").replace("\\}", "}").replace("\\\\", "\\")
    plain = plain.replace("{", "").replace("}", "")
    return plain, plain != original


def _ellipse_is_closed(entity: dict[str, Any]) -> bool:
    start = float(entity["start_param"])
    end = float(entity["end_param"])
    span = abs(end - start)
    return math.isclose(span, 2.0 * math.pi, rel_tol=0.0, abs_tol=1e-9)


def _sample_ellipse(entity: dict[str, Any], curve_segments: int) -> list[list[float]]:
    center = entity["center"]
    major = entity["major_axis"]
    ratio = float(entity["ratio"])
    start = float(entity["start_param"])
    end = float(entity["end_param"])
    if entity.get("ccw") is False:
        start, end = end, start
    span = end - start
    while span <= 0.0:
        span += 2.0 * math.pi
    closed = math.isclose(span, 2.0 * math.pi, rel_tol=0.0, abs_tol=1e-9)
    count = max(2, math.ceil(curve_segments * span / (2.0 * math.pi)))
    sample_count = count if closed else count + 1
    minor = [-float(major[1]) * ratio, float(major[0]) * ratio]
    vertices: list[list[float]] = []
    for index in range(sample_count):
        parameter = start + span * index / count
        vertices.append(
            [
                float(center[0])
                + float(major[0]) * math.cos(parameter)
                + minor[0] * math.sin(parameter),
                float(center[1])
                + float(major[1]) * math.cos(parameter)
                + minor[1] * math.sin(parameter),
            ]
        )
    return vertices


def _sample_spline(entity: dict[str, Any], curve_segments: int) -> list[list[float]]:
    control_points = entity["control_points"]
    degree = min(int(entity["degree"]), len(control_points) - 1)
    knots = entity.get("knots")
    expected = len(control_points) + degree + 1
    if (
        not isinstance(knots, list)
        or len(knots) != expected
        or any(
            float(knots[index]) > float(knots[index + 1])
            for index in range(len(knots) - 1)
        )
    ):
        knots = _default_spline_knots(len(control_points), degree)
    numeric_knots = [float(value) for value in knots]
    weights = entity.get("weights")
    if not isinstance(weights, list) or len(weights) != len(control_points):
        weights = [1.0] * len(control_points)
    numeric_weights = [float(value) for value in weights]

    start = numeric_knots[degree]
    end = numeric_knots[len(control_points)]
    if end <= start:
        raise ValueError("Invalid SPLINE knot domain")

    vertices = []
    for sample in range(curve_segments + 1):
        parameter = (
            end
            if sample == curve_segments
            else (start + (end - start) * sample / curve_segments)
        )
        basis = _bspline_basis(
            parameter,
            degree,
            numeric_knots,
            len(control_points),
            end,
        )
        denominator = sum(
            basis[index] * numeric_weights[index]
            for index in range(len(control_points))
        )
        if abs(denominator) < 1e-15:
            continue
        vertices.append(
            [
                sum(
                    basis[index]
                    * numeric_weights[index]
                    * float(control_points[index][axis])
                    for index in range(len(control_points))
                )
                / denominator
                for axis in range(2)
            ]
        )
    if len(vertices) < 2:
        raise ValueError("SPLINE approximation produced fewer than two vertices")
    return vertices


def _bspline_basis(
    parameter: float,
    degree: int,
    knots: list[float],
    control_point_count: int,
    domain_end: float,
) -> list[float]:
    basis = [0.0] * control_point_count
    for index in range(control_point_count):
        if knots[index] <= parameter < knots[index + 1]:
            basis[index] = 1.0
        elif parameter == domain_end and index == control_point_count - 1:
            basis[index] = 1.0

    for order in range(1, degree + 1):
        next_basis = [0.0] * control_point_count
        for index in range(control_point_count):
            left_denominator = knots[index + order] - knots[index]
            if left_denominator:
                next_basis[index] += (
                    (parameter - knots[index]) / left_denominator * basis[index]
                )
            if index + 1 < control_point_count:
                right_denominator = knots[index + order + 1] - knots[index + 1]
                if right_denominator:
                    next_basis[index] += (
                        (knots[index + order + 1] - parameter)
                        / right_denominator
                        * basis[index + 1]
                    )
        basis = next_basis
    return basis


def _generic_dimension_components(
    entity: dict[str, Any],
    *,
    diagnostics: list[ExportDiagnostic],
    warnings: list[str] | None,
) -> list[dict[str, Any]]:
    definition = entity.get("definition")
    if not isinstance(definition, dict):
        definition = {}
    common = _entity_common_values(entity)
    entity_id = str(entity.get("id", "")) or None
    components: list[dict[str, Any]] = []
    line_keys: set[tuple[float, float, float, float]] = set()

    def add_line(start: Any, end: Any) -> None:
        if not (_point_like(start) and _point_like(end)):
            return
        key = (
            float(start[0]),
            float(start[1]),
            float(end[0]),
            float(end[1]),
        )
        reverse = (key[2], key[3], key[0], key[1])
        if key in line_keys or reverse in line_keys:
            return
        line_keys.add(key)
        components.append(
            {
                **common,
                "kind": "LINE",
                "p1": [key[0], key[1]],
                "p2": [key[2], key[3]],
            }
        )

    def add_source_line(value: Any) -> None:
        if not isinstance(value, dict):
            return
        if all(
            isinstance(value.get(key), (int, float))
            for key in ("start_x", "start_y", "end_x", "end_y")
        ):
            add_line(
                [value["start_x"], value["start_y"]],
                [value["end_x"], value["end_y"]],
            )
        else:
            add_line(value.get("p1"), value.get("p2"))

    source_geometry = definition.get("source_geometry")
    if isinstance(source_geometry, dict):
        add_source_line(source_geometry.get("line"))
        aux_lines = source_geometry.get("aux_lines")
        if isinstance(aux_lines, list):
            for value in aux_lines:
                add_source_line(value)

        text_geometry = source_geometry.get("text")
        if isinstance(text_geometry, dict):
            text_value = str(text_geometry.get("content", definition.get("text", "")))
            insert = [
                float(text_geometry.get("start_x", 0.0)),
                float(text_geometry.get("start_y", 0.0)),
            ]
            height = float(text_geometry.get("size_y", 2.5))
            if height <= 0.0:
                height = 2.5
            text_entity: dict[str, Any] = {
                **common,
                "kind": "TEXT",
                "insert": insert,
                "height": height,
                "text": text_value,
            }
            angle = text_geometry.get("angle")
            if isinstance(angle, (int, float)) and float(angle) != 0.0:
                text_entity["rotation"] = math.degrees(float(angle))
            font = str(text_geometry.get("font_name", "")).strip()
            if font:
                text_entity["style"] = font
            size_x = text_geometry.get("size_x")
            if isinstance(size_x, (int, float)) and float(size_x) > 0.0:
                text_entity["width_factor"] = float(size_x) / height
            components.append(text_entity)

        aux_points = source_geometry.get("aux_points")
        if isinstance(aux_points, list):
            for point in aux_points:
                if (
                    isinstance(point, dict)
                    and isinstance(point.get("x"), (int, float))
                    and isinstance(point.get("y"), (int, float))
                ):
                    components.append(
                        {
                            **common,
                            "kind": "POINT",
                            "position": [float(point["x"]), float(point["y"])],
                        }
                    )

    rendered_paths = definition.get("rendered_paths")
    if isinstance(rendered_paths, list):
        for path in rendered_paths:
            if not isinstance(path, dict):
                continue
            points = path.get("points")
            if not isinstance(points, list) or len(points) < 2:
                continue
            if len(points) == 2:
                add_line(points[0], points[1])
            elif all(_point_like(point) for point in points):
                components.append(
                    {
                        **common,
                        "kind": "LWPOLYLINE",
                        "vertices": [
                            [float(point[0]), float(point[1])] for point in points
                        ],
                        "closed": bool(path.get("closed")),
                    }
                )

    rendered_texts = definition.get("rendered_texts")
    if isinstance(rendered_texts, list):
        for rendered_text in rendered_texts:
            if not isinstance(rendered_text, dict):
                continue
            anchor = rendered_text.get("anchor")
            height = rendered_text.get("height")
            if not _point_like(anchor) or not isinstance(height, (int, float)):
                continue
            text_entity = {
                **common,
                "kind": "TEXT",
                "insert": [float(anchor[0]), float(anchor[1])],
                "height": max(float(height), 1e-9),
                "text": str(rendered_text.get("text", "")),
            }
            angle = rendered_text.get("angle_deg")
            if isinstance(angle, (int, float)) and float(angle) != 0.0:
                text_entity["rotation"] = float(angle)
            components.append(text_entity)

    points = definition.get("points")
    if not isinstance(points, dict):
        points = {}
    if not line_keys:
        add_line(points.get("p1"), points.get("p2"))

    text_value = definition.get("text")
    has_text = any(component.get("kind") == "TEXT" for component in components)
    if text_value is not None and not has_text:
        insert = points.get("text_midpoint", points.get("location"))
        if not _point_like(insert):
            insert = definition.get("location")
        if not _point_like(insert):
            insert = [0.0, 0.0]
            _diagnose(
                diagnostics,
                warnings,
                code="DXF_GENERIC_DIMENSION_DEFAULTED",
                severity="warning",
                message=(
                    f"[DIMENSION:{entity_id or '?'}] GENERIC dimension missing "
                    "text placement was defaulted to [0, 0]."
                ),
                entity_id=entity_id,
                action="normalized",
            )
        height = definition.get("text_height", 2.5)
        if not isinstance(height, (int, float)) or float(height) <= 0.0:
            height = 2.5
        components.append(
            {
                **common,
                "kind": "TEXT",
                "insert": [float(insert[0]), float(insert[1])],
                "height": float(height),
                "text": str(text_value),
            }
        )
    return components


def _point_like(value: Any) -> bool:
    return (
        isinstance(value, list)
        and len(value) >= 2
        and isinstance(value[0], (int, float))
        and isinstance(value[1], (int, float))
    )


def _common_to_pairs(entity: dict[str, Any]) -> list[DXFPair]:
    pairs: list[DXFPair] = []
    layer = entity.get("layer", "0")
    pairs.append((8, str(layer)))

    linetype = entity.get("linetype")
    if linetype:
        pairs.append((6, str(linetype)))

    color = entity.get("color")
    if isinstance(color, int):
        pairs.append((62, str(color)))
    elif isinstance(color, str) and re.fullmatch(
        r"#([A-Fa-f0-9]{6}|[A-Fa-f0-9]{8})", color
    ):
        pairs.append((420, str(int(color[1:7], 16))))

    lineweight = entity.get("lineweight_mm")
    if isinstance(lineweight, (int, float)):
        pairs.append((370, str(int(round(float(lineweight) * 100.0)))))

    if entity.get("visible") is False:
        pairs.append((60, "1"))
    return pairs


def _dimension_point(
    points: dict[str, Any],
    definition: dict[str, Any],
    key: str,
    *,
    default: list[float] | None,
) -> list[float] | None:
    candidate = points.get(key, definition.get(key))
    if isinstance(candidate, list) and len(candidate) >= 2:
        return [float(candidate[0]), float(candidate[1])]
    return default


def _default_spline_knots(control_point_count: int, degree: int) -> list[float]:
    total = control_point_count + degree + 1
    edge = degree + 1
    interior = total - 2 * edge
    if interior < 0:
        raise ValueError(
            "Invalid SPLINE definition: too few control points for the degree"
        )

    knots = [0.0] * edge
    if interior > 0:
        step = 1.0 / (interior + 1)
        knots.extend(step * (index + 1) for index in range(interior))
    knots.extend([1.0] * edge)
    return knots


def _required_float(pairs: list[DXFPair], code: int) -> float:
    value = _first_float(pairs, code, default=None)
    if value is None:
        raise ValueError(f"Missing required DXF group code: {code}")
    return value


def _first_string(pairs: list[DXFPair], code: int) -> str | None:
    for pair_code, value in pairs:
        if pair_code == code:
            return value.strip()
    return None


def _first_int(pairs: list[DXFPair], code: int, default: int | None) -> int | None:
    value = _first_string(pairs, code)
    if value is None:
        return default
    return int(float(value))


def _first_float(
    pairs: list[DXFPair], code: int, default: float | None
) -> float | None:
    value = _first_string(pairs, code)
    if value is None:
        return default
    return _to_float(value)


def _all_floats(pairs: list[DXFPair], code: int) -> list[float]:
    values: list[float] = []
    for pair_code, value in pairs:
        if pair_code == code:
            values.append(_to_float(value))
    return values


def _to_float(value: str) -> float:
    return float(value.strip())


def _num(value: Any) -> str:
    as_float = float(value)
    if as_float.is_integer():
        return str(int(as_float))
    text = f"{as_float:.12g}"
    return "0" if text == "-0" else text


def _format_pairs(pairs: list[DXFPair]) -> str:
    lines: list[str] = []
    for code, value in pairs:
        lines.append(str(code))
        lines.append(str(value))
    return "\n".join(lines) + "\n"
