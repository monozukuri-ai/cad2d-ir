"""DXF: the reader, and the entry points of the writer.

The reader (``dxf_to_ir``, ``read_dxf_file``) is this module. The writer
(``ir_to_dxf``, ``write_dxf_file``) is native code in the ``cad2d-ir-native``
package; what it asks of Python is in the section at the end of this module.
"""

from __future__ import annotations

import codecs
import math
import re
import struct
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterable, Literal

from cad2d_ir import model3d as _model3d
from cad2d_ir._native import native_core
from cad2d_ir.constants import CURRENT_IR_VERSION
from cad2d_ir.diagnostics import ExportDiagnostic
from cad2d_ir.layouts import (
    is_active_paper_space_block_name,
    is_paper_space_block_name,
    layout_entity_count,
    layout_paper,
    order_layouts,
    promote_layout_when_model_is_empty,
    shows_the_sheet_itself,
    unnamed_layout_name,
    viewport_from_dxf_values,
)
from cad2d_ir.mtext import (
    WIDTH_FACTOR_RANGE,
    mtext_dxf_codes,
    mtext_layout,
    mtext_lines,
    mtext_plain,
    mtext_source_text,
)
from cad2d_ir.schema import validate_ir
from cad2d_ir.tolerance import explode_tolerance

if TYPE_CHECKING:
    from cad2d_ir.importers.base import ImportDiagnostic

DXFPair = tuple[int, str]
EntityMapEntry = dict[str, Any]
TargetVersion = Literal["AC1009", "AC1024"]
GenericDimensionMode = Literal["explode", "skip"]

SUPPORTED_DXF_TARGET_VERSIONS: tuple[TargetVersion, ...] = ("AC1009", "AC1024")
# Windows code pages a DXF names in ``$DWGCODEPAGE``, and their codecs.
_CODEPAGE_ENCODINGS = {
    "ANSI_874": "cp874",
    "ANSI_932": "cp932",
    "ANSI_936": "gbk",
    "ANSI_949": "cp949",
    "ANSI_950": "cp950",
    "ANSI_1250": "cp1250",
    "ANSI_1251": "cp1251",
    "ANSI_1252": "cp1252",
    "ANSI_1253": "cp1253",
    "ANSI_1254": "cp1254",
    "ANSI_1255": "cp1255",
    "ANSI_1256": "cp1256",
    "ANSI_1257": "cp1257",
    "ANSI_1258": "cp1258",
    "DOS932": "cp932",
    "UTF-8": "utf-8",
}
# AutoCAD escapes for characters outside the file codepage: ``\U+XXXX`` (Unicode)
# and the older MIF form ``\M+nXXXX`` (a double-byte code in the CJK codepage
# selected by ``n``). Writers such as ezjww emit these in ANSI_1252 files.
_DXF_TEXT_ESCAPE = re.compile(r"\\(?:U\+([0-9A-Fa-f]{4})|M\+([1-5])([0-9A-Fa-f]{4}))")
_MIF_ENCODINGS = {"1": "cp932", "2": "cp950", "3": "cp949", "4": "cp1361", "5": "cp936"}


@dataclass(frozen=True, slots=True)
class _AxisProjection:
    """Lossless 2D projection for an axis-aligned DXF drawing plane."""

    source_plane: Literal["XZ", "YZ"]

    @property
    def normal(self) -> tuple[float, float, float]:
        # The normal follows the right-handed orientation of the output axes:
        # XZ -> (WCS X, WCS Z), YZ -> (WCS Y, WCS Z).
        if self.source_plane == "XZ":
            return (0.0, -1.0, 0.0)
        return (1.0, 0.0, 0.0)

    def project_wcs(self, point: tuple[float, float, float]) -> list[float]:
        if self.source_plane == "XZ":
            return [point[0], point[2]]
        return [point[1], point[2]]


_INSUNITS_TO_IR = {
    0: "unitless",
    1: "inch",
    2: "ft",
    4: "mm",
    5: "cm",
    6: "m",
}


# Public mapping used by native importers (DWG) so $INSUNITS handling stays
# consistent between the DXF codec and the other format adapters.
INSUNITS_TO_IR_UNITS = _INSUNITS_TO_IR

# Replacement height for TEXT/MTEXT records that carry a non-positive height
# (some exporters write 0 to mean "use the style height"), per drawing unit.
_DEFAULT_TEXT_HEIGHT_BY_UNIT = {
    "mm": 2.5,
    "cm": 0.25,
    "m": 0.0025,
    "inch": 0.1,
    "ft": 0.01,
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


def _import_diagnose(
    diagnostics: list[ImportDiagnostic] | None,
    warnings: list[str] | None,
    *,
    code: str,
    severity: Literal["info", "warning", "error"],
    message: str,
    source_id: str | None = None,
    source_kind: str | None = None,
    action: str | None = None,
    details: dict[str, Any] | None = None,
) -> None:
    """Record a structured import diagnostic (and mirror warnings/errors as text)."""
    if diagnostics is not None:
        from cad2d_ir.importers.base import ImportDiagnostic

        diagnostics.append(
            ImportDiagnostic(
                code=code,
                severity=severity,
                message=message,
                source_id=source_id,
                source_kind=source_kind,
                action=action,
                details=details,
            )
        )
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
    model3d: _model3d.Model3DCollector | None = None,
) -> dict[str, Any]:
    from cad2d_ir.importers.base import ImportDiagnostic

    source_path = Path(path)
    raw = source_path.read_bytes()
    import_diagnostics = diagnostics if diagnostics is not None else []
    if _is_binary_dxf(raw):
        return _read_binary_dxf(
            raw,
            source_name=source_path.name,
            ir_version=ir_version,
            validate=validate,
            warnings=warnings,
            diagnostics=import_diagnostics,
            encoding=encoding,
            model3d=model3d,
        )
    selected_encoding, encoding_source = _select_dxf_encoding(raw, encoding)
    dxf_text = raw.decode(selected_encoding, errors="replace")
    del raw
    replacement_characters = dxf_text.count("�")
    # Splitting the text into lines only to count them costs as much memory as the
    # text, so it is done only when something was replaced
    affected_lines = (
        sum("�" in line for line in dxf_text.splitlines())
        if replacement_characters
        else 0
    )

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

    # As dxf_to_ir, but the text is let go once it is split into pairs (the pairs
    # hold strings of their own), so it is not kept while the IR is built
    pairs = _parse_pairs(dxf_text, warnings=warnings, diagnostics=import_diagnostics)
    del dxf_text
    document = _pairs_to_ir(
        pairs,
        ir_version=ir_version,
        validate=validate,
        warnings=warnings,
        diagnostics=import_diagnostics,
        model3d=model3d,
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


def dxf_to_ir(
    dxf_text: str,
    *,
    ir_version: str = CURRENT_IR_VERSION,
    validate: bool = True,
    warnings: list[str] | None = None,
    diagnostics: list[ImportDiagnostic] | None = None,
    model3d: _model3d.Model3DCollector | None = None,
) -> dict[str, Any]:
    """Parse DXF text into an IR document.

    Parsing is best-effort: malformed entities are skipped with a structured
    diagnostic (``DXF_ENTITY_CONVERSION_FAILED``) instead of failing the whole
    file, and trailing garbage after ``EOF`` or an unpaired final line is ignored.
    """
    pairs = _parse_pairs(dxf_text, warnings=warnings, diagnostics=diagnostics)
    return _pairs_to_ir(
        pairs,
        ir_version=ir_version,
        validate=validate,
        warnings=warnings,
        diagnostics=diagnostics,
        model3d=model3d,
    )


def _decode_dxf_text_escapes(value: str) -> str:
    def replace(match: re.Match[str]) -> str:
        unicode_hex, mif_page, mif_hex = match.groups()
        if unicode_hex is not None:
            code_point = int(unicode_hex, 16)
            if 0xD800 <= code_point <= 0xDFFF:
                return match.group(0)
            return chr(code_point)
        return bytes.fromhex(mif_hex).decode(_MIF_ENCODINGS[mif_page], errors="replace")

    return _DXF_TEXT_ESCAPE.sub(replace, value)


def _pairs_to_ir(
    pairs: list[DXFPair],
    *,
    ir_version: str,
    validate: bool,
    warnings: list[str] | None,
    diagnostics: list[ImportDiagnostic] | None,
    model3d: _model3d.Model3DCollector | None = None,
) -> dict[str, Any]:
    """Build the IR from the pairs of a DXF.

    The list given is used up: escapes are decoded in place, and the list is emptied
    once its sections are split off, so that a large drawing does not hold the pairs
    two or three times over while its IR is built (a synthetic 72 MB DXF peaked at 2.2 GB).
    """
    for index, (code, value) in enumerate(pairs):
        if "\\U+" in value or "\\M+" in value:
            pairs[index] = (code, _decode_dxf_text_escapes(value))
    sections = _split_sections(pairs)
    pairs.clear()
    if not sections:
        # Lenient pairing must not turn arbitrary bytes into an empty drawing.
        raise ValueError("Not a DXF document: no SECTION record found")

    header_variables = _header_variables(sections.get("HEADER", []))
    units = _header_units(header_variables)
    linetype_scale = _header_linetype_scale(header_variables)
    default_text_height = _DEFAULT_TEXT_HEIGHT_BY_UNIT.get(units, 2.5)
    # Paper-space entities (group 67 = 1) are drawn on a sheet, in paper
    # coordinates. They are kept apart from model space, as layouts.
    model_records, paper_records = _split_paper_space_records(
        _pairs_to_records(sections.pop("ENTITIES", []))
    )
    dimstyles = _dimstyle_sizes(sections.get("TABLES", []))
    image_defs = _image_definitions(sections.get("OBJECTS", []))
    # The model space records are let go as they are converted
    entities = _entity_records_to_ir(
        model_records,
        warnings=warnings,
        diagnostics=diagnostics,
        default_text_height=default_text_height,
        dimstyles=dimstyles,
        image_defs=image_defs,
        model3d=model3d,
        release_records=True,
    )
    del model_records
    paper_space_blocks: dict[str, list[tuple[str, list[DXFPair]]]] = {}
    blocks = _blocks_to_ir(
        sections.get("BLOCKS", []),
        warnings=warnings,
        diagnostics=diagnostics,
        default_text_height=default_text_height,
        paper_space_blocks=paper_space_blocks,
        dimstyles=dimstyles,
        image_defs=image_defs,
        model3d=model3d,
    )
    tables = _tables_to_ir(sections.get("TABLES", []))
    if blocks:
        tables["blocks"] = blocks
    layouts = _layouts_to_ir(
        paper_records,
        paper_space_blocks,
        table_pairs=sections.get("TABLES", []),
        object_pairs=sections.get("OBJECTS", []),
        warnings=warnings,
        diagnostics=diagnostics,
        default_text_height=default_text_height,
        dimstyles=dimstyles,
        image_defs=image_defs,
    )

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
    if linetype_scale is not None:
        ir_document["header"]["linetype_scale"] = linetype_scale
    if tables:
        ir_document["tables"] = tables
    if layouts:
        ir_document["layouts"] = layouts
        _import_diagnose(
            diagnostics,
            warnings,
            code="DXF_PAPERSPACE_LAYOUT_PRESERVED",
            severity="info",
            message=(
                f"Kept {layout_entity_count(ir_document)} paper-space DXF entities "
                f"in {len(layouts)} layout(s), apart from model space."
            ),
            action="preserved_layout",
            details={"layouts": [layout["name"] for layout in layouts]},
        )
        promoted = promote_layout_when_model_is_empty(ir_document, metadata_key="dxf")
        if promoted is not None:
            _import_diagnose(
                diagnostics,
                warnings,
                code="DXF_LAYOUT_PROMOTED",
                severity="info",
                message=(
                    f"Model space is empty; layout {promoted['name']!r} is the drawing."
                ),
                action="promoted",
                details={"layout": promoted["name"]},
            )

    if validate:
        validate_ir(ir_document)

    return ir_document


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

    codepage = _raw_header_string(raw, "$DWGCODEPAGE")
    if codepage is not None:
        mapped = _CODEPAGE_ENCODINGS.get(codepage.upper())
        if mapped is not None:
            if _utf8_overrides_codepage(
                _raw_header_string(raw, "$ACADVER"), mapped, [raw]
            ):
                return "utf-8", f"utf-8-probe ($DWGCODEPAGE={codepage} ignored)"
            return mapped, f"$DWGCODEPAGE={codepage}"

    try:
        raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        return "cp932", "cp932-fallback"
    return "utf-8", "utf-8-probe"


def _raw_header_string(raw: bytes, name: str) -> str | None:
    """Value of a string header variable, read before the encoding is known.

    Only the HEADER section, which a DXF starts with, is split into lines: splitting
    a whole large file, once for every variable looked up, took more memory than
    reading it.
    """
    end = raw.find(b"ENDSEC")
    text = (raw if end < 0 else raw[:end]).decode("latin-1")
    lines = text.splitlines()
    for index in range(0, len(lines) - 3, 2):
        if lines[index].strip() != "9":
            continue
        if lines[index + 1].strip().upper() != name:
            continue
        return lines[index + 3].strip()
    return None


def _utf8_overrides_codepage(
    version: str | None, mapped: str, values: Iterable[bytes]
) -> bool:
    """Whether text is UTF-8 although ``$DWGCODEPAGE`` names another encoding.

    DXF text is UTF-8 from AC1021 (AutoCAD 2007) on, and writers keep declaring
    the system codepage (``ANSI_932`` on Japanese systems). Trusting the codepage
    turns every non-ASCII string of such a file into mojibake. Legacy-encoded
    Japanese text is practically never valid UTF-8, so UTF-8 wins when the
    non-ASCII bytes decode strictly as UTF-8 and either the file is AC1021 or
    later or the declared codepage cannot decode them.
    """
    if codecs.lookup(mapped).name == "utf-8":
        return False
    non_ascii = False
    declared_ok = True
    for value in values:
        if value.isascii():
            continue
        non_ascii = True
        try:
            value.decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            return False
        if declared_ok:
            try:
                value.decode(mapped, errors="strict")
            except UnicodeDecodeError:
                declared_ok = False
    if not non_ascii:
        return False
    modern = (
        version is not None
        and re.fullmatch(r"AC\d{4}", version.upper()) is not None
        and version.upper() >= "AC1021"
    )
    return modern or not declared_ok


_DXF_LINE_BREAK = re.compile(r"\r\n|\n|\r")
_DXF_RECORD_NAME = re.compile(r"[A-Z][A-Z0-9_]*")
_BINARY_DXF_SENTINEL = b"AutoCAD Binary DXF\r\n\x1a\x00"


def _is_binary_dxf(raw: bytes) -> bool:
    return raw.startswith(_BINARY_DXF_SENTINEL)


def _binary_group_kind(code: int) -> str:
    """Value type of a group code in binary DXF (DXF reference, "Group Code Value Types")."""
    if 0 <= code <= 9 or code in (100, 102, 105):
        return "str"
    if (
        10 <= code <= 59
        or 110 <= code <= 149
        or 210 <= code <= 239
        or 460 <= code <= 469
    ):
        return "f64"
    if 60 <= code <= 79 or 170 <= code <= 179 or 270 <= code <= 289:
        return "i16"
    if 370 <= code <= 389 or 400 <= code <= 409 or 1060 <= code <= 1070:
        return "i16"
    if 90 <= code <= 99 or 420 <= code <= 429 or 440 <= code <= 459 or code == 1071:
        return "i32"
    if 160 <= code <= 169:
        return "i64"
    if 290 <= code <= 299:
        return "bool"
    if 310 <= code <= 319 or code == 1004:
        return "bin"
    if 1010 <= code <= 1059:
        return "f64"
    return "str"


def _binary_dxf_pairs(raw: bytes) -> list[tuple[int, bytes | str]]:
    """Decode the binary DXF stream into (code, value) pairs; string values stay bytes."""

    pos = len(_BINARY_DXF_SENTINEL)
    # R13+ writes 2-byte group codes (the first code 0 is 00 00); R12 uses 1-byte
    # codes with 255 escaping to a 2-byte code.
    two_byte_codes = len(raw) > pos + 1 and raw[pos + 1] == 0
    pairs: list[tuple[int, bytes | str]] = []
    size = len(raw)
    while pos < size:
        if two_byte_codes:
            if pos + 2 > size:
                break
            code = struct.unpack_from("<H", raw, pos)[0]
            pos += 2
        else:
            code = raw[pos]
            pos += 1
            if code == 255:
                if pos + 2 > size:
                    break
                code = struct.unpack_from("<H", raw, pos)[0]
                pos += 2
        kind = _binary_group_kind(code)
        value: bytes | str
        if kind == "str":
            end = raw.find(b"\x00", pos)
            if end < 0:
                end = size
            value = raw[pos:end]
            pos = end + 1
        elif kind == "f64":
            if pos + 8 > size:
                break
            value = repr(struct.unpack_from("<d", raw, pos)[0])
            pos += 8
        elif kind == "i16":
            if pos + 2 > size:
                break
            value = str(struct.unpack_from("<h", raw, pos)[0])
            pos += 2
        elif kind == "i32":
            if pos + 4 > size:
                break
            value = str(struct.unpack_from("<i", raw, pos)[0])
            pos += 4
        elif kind == "i64":
            if pos + 8 > size:
                break
            value = str(struct.unpack_from("<q", raw, pos)[0])
            pos += 8
        elif kind == "bool":
            value = str(raw[pos])
            pos += 1
        else:  # binary chunk: 1-byte length + data, exposed as hex like ASCII DXF
            length = raw[pos]
            pos += 1
            value = raw[pos : pos + length].hex().upper()
            pos += length
        pairs.append((code, value))
        if code == 0 and value == b"EOF":
            break
    return pairs


def _read_binary_dxf(
    raw: bytes,
    *,
    source_name: str,
    ir_version: str,
    validate: bool,
    warnings: list[str] | None,
    diagnostics: list[ImportDiagnostic],
    encoding: str,
    model3d: _model3d.Model3DCollector | None = None,
) -> dict[str, Any]:
    """Import an ``AutoCAD Binary DXF`` file through the regular pair pipeline."""
    typed_pairs = _binary_dxf_pairs(raw)
    normalized = encoding.strip().lower()
    if normalized != "auto":
        selected_encoding, encoding_source = _select_dxf_encoding(b"", encoding)
    else:
        selected_encoding, encoding_source = "cp932", "cp932-fallback"
        header_strings: dict[bytes, str] = {}
        for index, (code, value) in enumerate(typed_pairs):
            if (
                code == 9
                and value in (b"$DWGCODEPAGE", b"$ACADVER")
                and index + 1 < len(typed_pairs)
            ):
                next_value = typed_pairs[index + 1][1]
                if isinstance(next_value, bytes):
                    header_strings[value] = next_value.decode("latin-1").strip()
                if len(header_strings) == 2:
                    break
        codepage = header_strings.get(b"$DWGCODEPAGE")
        mapped = _CODEPAGE_ENCODINGS.get(codepage.upper()) if codepage else None
        if mapped is not None and _utf8_overrides_codepage(
            header_strings.get(b"$ACADVER"),
            mapped,
            (value for _, value in typed_pairs if isinstance(value, bytes)),
        ):
            selected_encoding = "utf-8"
            encoding_source = f"utf-8-probe ($DWGCODEPAGE={codepage} ignored)"
        elif mapped is not None:
            selected_encoding, encoding_source = mapped, f"$DWGCODEPAGE={codepage}"
        else:
            try:
                for _, value in typed_pairs:
                    if isinstance(value, bytes):
                        value.decode("utf-8", errors="strict")
            except UnicodeDecodeError:
                pass
            else:
                selected_encoding, encoding_source = "utf-8", "utf-8-probe"

    replacement_characters = 0
    affected_values = 0
    pairs: list[DXFPair] = []
    for code, value in typed_pairs:
        if isinstance(value, bytes):
            decoded = value.decode(selected_encoding, errors="replace")
            replaced = decoded.count("\ufffd")
            if replaced:
                replacement_characters += replaced
                affected_values += 1
            if "^" in decoded:
                decoded = _caret_decode(decoded)
            pairs.append((code, decoded))
        else:
            pairs.append((code, value))

    _import_diagnose(
        diagnostics,
        warnings,
        code="DXF_BINARY_DETECTED",
        severity="info",
        message="Binary DXF input (AutoCAD Binary DXF sentinel) was decoded to group pairs.",
        action="detected",
        details={"group_code_bytes": 2 if len(raw) > 23 and raw[23] == 0 else 1},
    )
    _import_diagnose(
        diagnostics,
        warnings,
        code="DXF_ENCODING_DETECTED",
        severity="info",
        message=f"DXF input decoded as {selected_encoding} ({encoding_source}).",
        action="detected",
        details={"encoding": selected_encoding, "source": encoding_source},
    )
    if replacement_characters:
        _import_diagnose(
            diagnostics,
            warnings,
            code="DXF_DECODE_REPLACED",
            severity="warning",
            message=(
                f"DXF decoding replaced {replacement_characters} undecodable "
                f"character(s) in {affected_values} value(s)."
            ),
            action="normalized",
            details={
                "encoding": selected_encoding,
                "replacement_characters": replacement_characters,
                "affected_lines": affected_values,
            },
        )

    document = _pairs_to_ir(
        pairs,
        model3d=model3d,
        ir_version=ir_version,
        validate=validate,
        warnings=warnings,
        diagnostics=diagnostics,
    )
    document["source"] = {
        "format": "dxf",
        "name": source_name,
        "metadata": {
            "encoding": selected_encoding,
            "encoding_source": encoding_source,
            "decode_replacement_characters": replacement_characters,
            "decode_replacement_lines": affected_values,
            "binary": True,
        },
    }
    return document


def _find_dxf_resync_index(lines: list[str], start: int) -> int | None:
    """Index of the next ``0`` / RECORD-NAME line pair after a corrupted spot.

    A value line is always followed by a group-code line, so a ``0`` line
    followed by an upper-case record name can only be a genuine record start,
    whatever the pairing parity is in the damaged region.
    """
    for index in range(start + 1, len(lines) - 1):
        if lines[index].strip() == "0" and _DXF_RECORD_NAME.fullmatch(
            lines[index + 1].strip()
        ):
            return index
    return None


def _parse_pairs(
    dxf_text: str,
    *,
    warnings: list[str] | None = None,
    diagnostics: list[ImportDiagnostic] | None = None,
) -> list[DXFPair]:
    """Split DXF text into (group code, value) pairs.

    Only CR/LF line breaks are recognized (``str.splitlines`` would also break on
    form feeds and other separators that legitimately occur inside string values).
    Leading blank lines, anything after the ``0``/``EOF`` pair and a single unpaired
    trailing line (truncated upload, stray newline, padding) are ignored instead of
    rejecting the file.
    """
    raw_lines = _DXF_LINE_BREAK.split(dxf_text)
    while raw_lines and raw_lines[-1].strip() == "":
        raw_lines.pop()
    start = 0
    while start < len(raw_lines) and raw_lines[start].strip() == "":
        start += 1

    pairs: list[DXFPair] = []
    line_count = len(raw_lines)
    index = start
    while index + 1 < line_count:
        code_text = raw_lines[index].strip()
        value = raw_lines[index + 1].rstrip("\r\n")
        try:
            code = int(code_text)
        except ValueError as exc:
            resume = _find_dxf_resync_index(raw_lines, index)
            if resume is None:
                raise ValueError(
                    f"Invalid DXF group code at line {index + 1}: {code_text}"
                ) from exc
            _import_diagnose(
                diagnostics,
                warnings,
                code="DXF_STREAM_RESYNCED",
                severity="warning",
                message=(
                    f"Unparsable DXF data at line {index + 1} ({code_text[:24]!r}); "
                    f"skipped {resume - index} line(s) and resumed at the next record."
                ),
                action="skipped",
                details={"line": index + 1, "skipped_lines": resume - index},
            )
            index = resume
            continue
        if "^" in value:
            value = _caret_decode(value)
        pairs.append((code, value))
        index += 2
        if code == 0 and value.strip().upper() == "EOF":
            trailing = line_count - index
            if trailing:
                _import_diagnose(
                    diagnostics,
                    warnings,
                    code="DXF_TRAILING_DATA_IGNORED",
                    severity="info",
                    message=f"Ignored {trailing} line(s) after the DXF EOF marker.",
                    action="skipped",
                    details={"lines": trailing},
                )
            return pairs

    if index < line_count:
        dangling = raw_lines[index].strip()
        _import_diagnose(
            diagnostics,
            warnings,
            code="DXF_TRAILING_LINE_IGNORED",
            severity="warning",
            message=(
                "DXF text ends with an unpaired line (truncated file or stray data); "
                f"ignored line {index + 1}: {dangling[:40]!r}."
            ),
            action="skipped",
            details={"line": index + 1},
        )
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


def _header_variables(header_pairs: list[DXFPair]) -> dict[str, list[DXFPair]]:
    current_name: str | None = None
    variables: dict[str, list[DXFPair]] = {}

    for code, value in header_pairs:
        if code == 9:
            current_name = value.strip().upper()
            variables[current_name] = []
            continue
        if current_name is not None:
            variables[current_name].append((code, value))
    return variables


def _header_units(variables: dict[str, list[DXFPair]]) -> str:
    if "$INSUNITS" not in variables:
        return "mm"
    insunits = _first_int(variables["$INSUNITS"], 70, default=4)
    return _INSUNITS_TO_IR.get(insunits, "mm")


def _header_linetype_scale(variables: dict[str, list[DXFPair]]) -> float | None:
    """``$LTSCALE`` when it is a usable value other than the default 1."""
    if "$LTSCALE" not in variables:
        return None
    try:
        value = _first_float(variables["$LTSCALE"], 40, default=None)
    except ValueError:
        return None
    return _linetype_scale_or_none(value)


def _linetype_scale_or_none(value: float | None) -> float | None:
    """Keep a linetype scale only when it is finite, positive and not 1."""
    if value is None or not math.isfinite(value) or value <= 0.0:
        return None
    if abs(value - 1.0) < 1e-12:
        return None
    return value


def _entity_linetype_scale(pairs: list[DXFPair]) -> float | None:
    """Group 48 of the common entity data.

    The same code means other things after an entity-specific subclass marker
    (MTEXT stores its column width there), so the scan stops at the first
    ``100`` marker other than ``AcDbEntity``.
    """
    for code, value in pairs:
        if code == 100 and value.strip() != "AcDbEntity":
            return None
        if code == 48:
            try:
                return _linetype_scale_or_none(_to_float(value))
            except ValueError:
                return None
    return None


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

    # A negative color number turns the layer off; flag bit 1 freezes it.
    flags = _first_int(pairs, 70, default=0) or 0
    if (color is not None and color < 0) or flags & 1:
        result["visible"] = False
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
    big_font = _first_string(pairs, 4)
    if big_font:
        result["big_font"] = big_font
    family = _acad_xdata_string(pairs)
    if family:
        result["family"] = family
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


def _acad_xdata_string(pairs: list[DXFPair]) -> str | None:
    """The first string of a record's ``ACAD`` xdata (a text style's TrueType family)."""
    in_acad = False
    for code, value in pairs:
        if code == 1001:
            in_acad = value.strip().upper() == "ACAD"
        elif in_acad and code == 1000:
            return value.strip() or None
    return None


def _entity_records_to_ir(
    records: list[tuple[str, list[DXFPair]]],
    *,
    warnings: list[str] | None = None,
    diagnostics: list[ImportDiagnostic] | None = None,
    default_text_height: float = 2.5,
    dimstyles: dict[str, dict[str, float]] | None = None,
    image_defs: dict[str, dict[str, Any]] | None = None,
    model3d: _model3d.Model3DCollector | None = None,
    release_records: bool = False,
) -> list[dict[str, Any]]:
    projection = _detect_axis_projection(records)
    if projection is not None:
        _import_diagnose(
            diagnostics,
            warnings,
            code="DXF_NON_XY_PLANE_PROJECTED",
            severity="warning",
            message=(
                f"Projected axis-aligned DXF {projection.source_plane} geometry "
                "to the IR XY plane, including OCS circle/arc coordinates."
            ),
            action="projected",
            details={
                "source_plane": projection.source_plane,
                "target_plane": "XY",
                "method": "axis_aligned",
            },
        )
    return _records_to_entities(
        records,
        warnings=warnings,
        diagnostics=diagnostics,
        context="ENTITIES",
        default_text_height=default_text_height,
        projection=projection,
        dimstyles=dimstyles,
        image_defs=image_defs,
        model3d=model3d,
        model3d_scope=None,
        release_records=release_records,
    )


def _blocks_to_ir(
    block_pairs: list[DXFPair],
    *,
    warnings: list[str] | None = None,
    diagnostics: list[ImportDiagnostic] | None = None,
    default_text_height: float = 2.5,
    paper_space_blocks: dict[str, list[tuple[str, list[DXFPair]]]] | None = None,
    dimstyles: dict[str, dict[str, float]] | None = None,
    image_defs: dict[str, dict[str, Any]] | None = None,
    model3d: _model3d.Model3DCollector | None = None,
) -> dict[str, dict[str, Any]]:
    """Block definitions by name.

    The blocks of the paper-space layouts (``*Paper_Space``, ``*Paper_Space0``,
    ...) are sheets, not definitions a drawing refers to. With
    ``paper_space_blocks`` their records are handed back under their name
    instead of becoming blocks.
    """
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

        if paper_space_blocks is not None and is_paper_space_block_name(name):
            paper_space_blocks.setdefault(name, []).extend(entity_records)
            continue

        if model3d is not None:
            model3d.set_block_base_point(
                name,
                (
                    float(base_point[0]),
                    float(base_point[1]),
                    _first_float(pairs, 30, default=0.0) or 0.0,
                ),
            )
        blocks[name] = {
            "base_point": base_point,
            "entities": _records_to_entities(
                entity_records,
                warnings=warnings,
                diagnostics=diagnostics,
                context=f"BLOCK:{name}",
                default_text_height=default_text_height,
                dimstyles=dimstyles,
                image_defs=image_defs,
                model3d=model3d,
                model3d_scope=name,
            ),
        }
    return blocks


def _is_paper_space_record(pairs: list[DXFPair]) -> bool:
    """Group 67 = 1 in the common entity data: the entity is in paper space.

    The scan stops at the first subclass marker other than ``AcDbEntity``,
    behind which the code belongs to the entity type.
    """
    for code, value in pairs:
        if code == 100 and value.strip() != "AcDbEntity":
            return False
        if code == 67:
            try:
                return int(float(value)) == 1
            except ValueError:
                return False
    return False


def _split_paper_space_records(
    records: list[tuple[str, list[DXFPair]]],
) -> tuple[list[tuple[str, list[DXFPair]]], list[tuple[str, list[DXFPair]]]]:
    """Model-space and paper-space records of an ENTITIES section.

    A POLYLINE keeps its VERTEX and SEQEND records and an INSERT its ATTRIB and
    SEQEND records, whatever those say themselves.
    """
    model: list[tuple[str, list[DXFPair]]] = []
    paper: list[tuple[str, list[DXFPair]]] = []
    index = 0
    while index < len(records):
        kind, pairs = records[index]
        end = index + 1
        followers = {"POLYLINE": "VERTEX", "INSERT": "ATTRIB"}.get(kind)
        if followers is not None:
            while end < len(records) and records[end][0] == followers:
                end += 1
            if end < len(records) and records[end][0] == "SEQEND":
                end += 1
        destination = paper if _is_paper_space_record(pairs) else model
        destination.extend(records[index:end])
        index = end
    return model, paper


def _table_handles(table_pairs: list[DXFPair]) -> tuple[dict[str, str], dict[str, str]]:
    """Layer names and block record names by handle (upper-case hex)."""
    layers: dict[str, str] = {}
    block_records: dict[str, str] = {}
    for kind, pairs in _pairs_to_records(table_pairs):
        if kind not in {"LAYER", "BLOCK_RECORD"}:
            continue
        handle = _first_string(pairs, 5)
        name = _first_string(pairs, 2)
        if not handle or not name:
            continue
        target = layers if kind == "LAYER" else block_records
        target[handle.strip().upper()] = name
    return layers, block_records


def _layout_objects(object_pairs: list[DXFPair]) -> dict[str, dict[str, Any]]:
    """LAYOUT objects by the handle of their block record (upper-case hex).

    A LAYOUT object is the plot settings of a sheet (``AcDbPlotSettings``)
    followed by the layout itself (``AcDbLayout``): its name, its position among
    the tabs and the block record that holds its entities.
    """
    layouts: dict[str, dict[str, Any]] = {}
    for kind, pairs in _pairs_to_records(object_pairs):
        if kind != "LAYOUT":
            continue
        split = next(
            (
                index
                for index, (code, value) in enumerate(pairs)
                if code == 100 and value.strip() == "AcDbLayout"
            ),
            None,
        )
        if split is None:
            continue
        plot_pairs, layout_pairs = pairs[:split], pairs[split + 1 :]
        block_record = _first_string(layout_pairs, 330)
        name = _first_string(layout_pairs, 1)
        if not block_record or not name:
            continue
        try:
            info: dict[str, Any] = {
                "name": name,
                "tab_order": _first_int(layout_pairs, 71, default=None),
                "paper": layout_paper(
                    name=_first_string(plot_pairs, 4),
                    width_mm=_first_float(plot_pairs, 44, default=None),
                    height_mm=_first_float(plot_pairs, 45, default=None),
                    margins_mm=[
                        _first_float(plot_pairs, code, default=None)
                        for code in (40, 41, 42, 43)
                    ],
                    units_code=_first_int(plot_pairs, 72, default=None),
                    rotation_code=_first_int(plot_pairs, 73, default=None),
                ),
            }
        except ValueError:
            continue
        layouts[block_record.strip().upper()] = info
    return layouts


def _mview_extended_data(pairs: list[DXFPair]) -> dict[str, Any] | None:
    """The view of a viewport as R12-R14 files keep it: ``ACAD`` / ``MVIEW`` xdata.

    The list is positional: the view target and the view direction (two points,
    groups 1010/1020/1030), then reals (1040) for the twist angle in radians,
    the view height and the X and Y of the view center. The names of the layers
    that are frozen in the viewport follow as 1003 groups.
    """
    start = next(
        (
            index
            for index, (code, value) in enumerate(pairs)
            if code == 1000 and value.strip().upper() == "MVIEW"
        ),
        None,
    )
    if start is None:
        return None
    xdata = pairs[start + 1 :]
    try:
        xs = [float(value) for code, value in xdata if code == 1010]
        ys = [float(value) for code, value in xdata if code == 1020]
        zs = [float(value) for code, value in xdata if code == 1030]
        reals = [float(value) for code, value in xdata if code == 1040]
    except ValueError:
        return None
    if len(reals) < 4 or min(len(xs), len(ys), len(zs)) < 2:
        return None
    return {
        "target": [xs[0], ys[0], zs[0]],
        "direction": [xs[1], ys[1], zs[1]],
        "twist_deg": math.degrees(reals[0]),
        "view_height": reals[1],
        "view_center": [reals[2], reals[3]],
        "frozen_layers": [value for code, value in xdata if code == 1003 and value],
    }


def _viewport_from_pairs(
    pairs: list[DXFPair], layer_names_by_handle: dict[str, str]
) -> dict[str, Any]:
    """IR viewport of a VIEWPORT record.

    R2000 and later state the view in the record itself; R12-R14 files keep it
    in extended data.
    """
    handle = _first_string(pairs, 5)
    frozen_layers = [
        layer_names_by_handle[value.strip().upper()]
        for code, value in pairs
        if code == 331 and value.strip().upper() in layer_names_by_handle
    ]

    def point(codes: tuple[int, ...]) -> list[float] | None:
        values = [_first_float(pairs, code, default=None) for code in codes]
        if values[0] is None or values[1] is None:
            return None
        return [value if value is not None else 0.0 for value in values]

    view: dict[str, Any] = {
        "view_center": point((12, 22)),
        "view_height": _first_float(pairs, 45, default=None),
        "twist_deg": _first_float(pairs, 51, default=None),
        "target": point((17, 27, 37)),
        "direction": point((16, 26, 36)),
    }
    if view["view_height"] is None:
        mview = _mview_extended_data(pairs)
        if mview is not None:
            frozen_layers = mview.pop("frozen_layers")
            view = mview

    return viewport_from_dxf_values(
        source_format="dxf",
        handle=handle,
        center=[_required_float(pairs, 10), _required_float(pairs, 20)],
        width=_required_float(pairs, 40),
        height=_required_float(pairs, 41),
        status_flags=_first_int(pairs, 90, default=None),
        status=_first_int(pairs, 68, default=None),
        layer=_first_string(pairs, 8),
        frozen_layers=frozen_layers,
        clip_boundary=_first_string(pairs, 340),
        **view,
    )


def _viewport_has_no_window(pairs: list[DXFPair]) -> bool:
    try:
        width = _first_float(pairs, 40, default=None)
        height = _first_float(pairs, 41, default=None)
    except ValueError:
        return False
    return width is not None and height is not None and (width <= 0.0 or height <= 0.0)


def _sheet_viewport_index(viewport_records: list[list[DXFPair]]) -> int | None:
    """Index of the viewport that stands for the sheet itself, if there is one.

    Every layout has one such viewport. It shows no model space and is not part
    of the IR. In the sheet that was current when the file was written it has
    id 1 (group 69). The viewports of the other sheets all carry id 0; there it
    is the first one, as long as its view is its own window.
    """
    for index, pairs in enumerate(viewport_records):
        try:
            if _first_int(pairs, 69, default=None) == 1:
                return index
        except ValueError:
            continue
    if not viewport_records:
        return None
    first = viewport_records[0]
    try:
        center = [
            _first_float(first, 10, default=None),
            _first_float(first, 20, default=None),
        ]
        view_center = [
            _first_float(first, 12, default=None),
            _first_float(first, 22, default=None),
        ]
        view_height = _first_float(first, 45, default=None)
        if view_height is None:
            mview = _mview_extended_data(first)
            if mview is not None:
                view_center, view_height = mview["view_center"], mview["view_height"]
        looks_like_sheet = shows_the_sheet_itself(
            center=center,
            height=_first_float(first, 41, default=None),
            view_center=None if None in view_center else view_center,
            view_height=view_height,
        )
    except ValueError:
        return None
    return 0 if looks_like_sheet else None


def _paper_space_block_order(name: str) -> tuple[int, int, str]:
    """The active sheet first, then ``*Paper_Space0``, ``*Paper_Space1``, ..."""
    if is_active_paper_space_block_name(name):
        return (0, 0, "")
    suffix = name.strip().upper().removeprefix("*PAPER_SPACE")
    if suffix.isdigit():
        return (1, int(suffix), "")
    return (2, 0, name.upper())


def _layouts_to_ir(
    active_records: list[tuple[str, list[DXFPair]]],
    paper_space_blocks: dict[str, list[tuple[str, list[DXFPair]]]],
    *,
    table_pairs: list[DXFPair],
    object_pairs: list[DXFPair],
    warnings: list[str] | None,
    diagnostics: list[ImportDiagnostic] | None,
    default_text_height: float,
    dimstyles: dict[str, dict[str, float]] | None = None,
    image_defs: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Paper-space layouts of a DXF file, in tab order.

    The entities of the sheet that was current when the file was written are in
    the ENTITIES section with group 67 = 1; those of the other sheets are the
    bodies of the ``*Paper_Space<n>`` blocks. LAYOUT objects name the sheets and
    describe their paper; files without them (R12, minimal files) get
    ``Layout1``, ``Layout2``, ... A sheet that holds neither entities nor a
    viewport onto model space is left out.
    """
    sources: dict[str, list[tuple[str, list[DXFPair]]]] = {}
    block_names: dict[str, str] = {}
    if active_records:
        sources["*PAPER_SPACE"] = list(active_records)
        block_names["*PAPER_SPACE"] = "*Paper_Space"
    for name, records in paper_space_blocks.items():
        key = (
            "*PAPER_SPACE"
            if is_active_paper_space_block_name(name)
            else name.strip().upper()
        )
        sources.setdefault(key, []).extend(records)
        block_names.setdefault(key, name)
    if not any(sources.values()):
        return []

    layer_names_by_handle, block_record_names = _table_handles(table_pairs)
    layout_infos: dict[str, dict[str, Any]] = {}
    for block_record, info in _layout_objects(object_pairs).items():
        block_name = block_record_names.get(block_record)
        if block_name is None:
            continue
        key = (
            "*PAPER_SPACE"
            if is_active_paper_space_block_name(block_name)
            else block_name.strip().upper()
        )
        layout_infos[key] = info

    layouts: list[dict[str, Any]] = []
    # A sheet without a layout object must not take the name of one that has.
    reserved_names = {str(info["name"]) for info in layout_infos.values()}
    used_names: set[str] = set()
    ordered = sorted(
        sources, key=lambda key: _paper_space_block_order(block_names[key])
    )
    for key in ordered:
        records = sources[key]
        info = layout_infos.get(key, {})
        name = str(info.get("name") or unnamed_layout_name(reserved_names | used_names))
        while name in used_names:
            name += "_"
        entity_records = [record for record in records if record[0] != "VIEWPORT"]
        entities = _records_to_entities(
            entity_records,
            warnings=warnings,
            diagnostics=diagnostics,
            context=f"LAYOUT:{name}",
            default_text_height=default_text_height,
            in_block=False,
            dimstyles=dimstyles,
            image_defs=image_defs,
        )
        viewports: list[dict[str, Any]] = []
        viewport_records = [pairs for kind, pairs in records if kind == "VIEWPORT"]
        sheet_viewport = _sheet_viewport_index(viewport_records)
        for index, pairs in enumerate(viewport_records):
            if index == sheet_viewport:
                continue
            if _viewport_has_no_window(pairs):
                # A viewport of size zero (switched off and never regenerated)
                # shows nothing; it is no malformed record.
                _warn(
                    warnings, f"[LAYOUT:{name}] DXF VIEWPORT without a window skipped"
                )
                continue
            try:
                viewport = _viewport_from_pairs(pairs, layer_names_by_handle)
            except (ValueError, TypeError, IndexError, OverflowError) as exc:
                _import_diagnose(
                    diagnostics,
                    warnings,
                    code="DXF_ENTITY_CONVERSION_FAILED",
                    severity="error",
                    message=f"[LAYOUT:{name}] Skipped malformed DXF VIEWPORT: {exc}",
                    source_id=_first_string(pairs, 5),
                    source_kind="VIEWPORT",
                    action="skipped",
                )
                continue
            viewports.append(viewport)
        if not entities and not viewports:
            continue

        used_names.add(name)
        layout: dict[str, Any] = {"name": name, "entities": entities}
        if viewports:
            layout["viewports"] = viewports
        tab_order = info.get("tab_order")
        if isinstance(tab_order, int) and tab_order >= 0:
            layout["tab_order"] = tab_order
        if key == "*PAPER_SPACE":
            layout["active"] = True
        if info.get("paper"):
            layout["paper"] = info["paper"]
        layouts.append(layout)
    return order_layouts(layouts)


def _pairs_to_records(pairs: list[DXFPair]) -> list[tuple[str, list[DXFPair]]]:
    records: list[tuple[str, list[DXFPair]]] = []
    current_kind: str | None = None
    current_pairs: list[DXFPair] = []

    for pair in pairs:
        code, value = pair
        if code == 0:
            if current_kind is not None:
                records.append((current_kind, current_pairs))
            current_kind = value.strip().upper()
            current_pairs = []
        elif current_kind is not None:
            # the pair itself, not a copy: a large drawing has millions of them
            current_pairs.append(pair)

    if current_kind is not None:
        records.append((current_kind, current_pairs))
    return records


_AXIS_PROJECTABLE_DXF_KINDS = frozenset({"LINE", "POINT", "CIRCLE", "ARC", "IMAGE"})


def _detect_axis_projection(
    records: list[tuple[str, list[DXFPair]]],
) -> _AxisProjection | None:
    """Detect a lossless XZ/YZ drawing without guessing for mixed geometry.

    LINE and POINT coordinates are WCS, so they provide unambiguous plane
    evidence. CIRCLE and ARC centers are OCS and are verified against the
    detected plane before projection. More complex entity mixtures retain the
    historical XY behavior until their coordinate-system rules are supported.
    """
    convertible_kinds = {
        kind for kind, _pairs in records if kind in _CONVERTIBLE_DXF_KINDS
    }
    if not convertible_kinds or not convertible_kinds <= _AXIS_PROJECTABLE_DXF_KINDS:
        return None

    wcs_points: list[tuple[float, float, float]] = []
    try:
        for kind, pairs in records:
            if kind == "LINE":
                wcs_points.append(_point3_from_pairs(pairs, 10, 20, 30))
                wcs_points.append(_point3_from_pairs(pairs, 11, 21, 31))
            elif kind == "POINT":
                wcs_points.append(_point3_from_pairs(pairs, 10, 20, 30))
    except (ValueError, TypeError, OverflowError):
        # Plane detection must never bypass the per-entity lenient parser.
        return None

    if len(wcs_points) < 2:
        return None

    spans = tuple(
        max(point[axis] for point in wcs_points)
        - min(point[axis] for point in wcs_points)
        for axis in range(3)
    )
    scale = max(1.0, *spans)
    tolerance = scale * 1e-9
    x_span, y_span, z_span = spans
    if z_span <= tolerance:
        return None
    if y_span <= tolerance and x_span > tolerance:
        projection = _AxisProjection("XZ")
    elif x_span <= tolerance and y_span > tolerance:
        projection = _AxisProjection("YZ")
    else:
        return None

    try:
        for kind, pairs in records:
            if kind not in {"CIRCLE", "ARC"}:
                continue
            extrusion = _extrusion_from_pairs(pairs)
            if abs(abs(_dot3(extrusion, projection.normal)) - 1.0) > 1e-9:
                return None
    except (ValueError, TypeError, OverflowError):
        return None
    return projection


_CONVERTIBLE_DXF_KINDS = frozenset(
    {
        "LINE",
        "CIRCLE",
        "ARC",
        "POINT",
        "ELLIPSE",
        "LWPOLYLINE",
        "POLYLINE",
        "TEXT",
        "MTEXT",
        "ATTDEF",
        "INSERT",
        "HATCH",
        "SPLINE",
        "DIMENSION",
        "IMAGE",
        "SOLID",
        "TRACE",
        "LEADER",
        "TOLERANCE",
    }
)


def _records_to_entities(
    records: list[tuple[str, list[DXFPair]]],
    *,
    warnings: list[str] | None = None,
    diagnostics: list[ImportDiagnostic] | None = None,
    context: str = "ENTITIES",
    default_text_height: float = 2.5,
    projection: _AxisProjection | None = None,
    in_block: bool | None = None,
    dimstyles: dict[str, dict[str, float]] | None = None,
    image_defs: dict[str, dict[str, Any]] | None = None,
    model3d: _model3d.Model3DCollector | None = None,
    model3d_scope: Any = None,
    release_records: bool = False,
) -> list[dict[str, Any]]:
    """Convert entity records to IR entities.

    Conversion is best-effort per entity: a malformed record (missing group codes,
    unparsable numbers, degenerate geometry that fails IR validation, ...) is
    reported through ``DXF_ENTITY_CONVERSION_FAILED`` and skipped, so a single bad
    entity no longer rejects the whole drawing.

    With ``release_records`` the records are used up: each is replaced by an empty
    one once converted (the loop only looks ahead), and the list is emptied at the
    end, so the pairs of a large drawing are let go while its entities are built.
    """
    from cad2d_ir.schema import IRValidationError, validate_entity

    entities: list[dict[str, Any]] = []
    used_ids: set[str] = set()
    record_index = 0
    released = 0
    serial = 1
    if in_block is None:
        in_block = context != "ENTITIES"

    while record_index < len(records):
        if release_records:
            while released < record_index:
                records[released] = _RELEASED_RECORD
                released += 1
        kind, pairs = records[record_index]
        consumed = 1
        entity: dict[str, Any] | None = None
        # A record that is drawn as several entities (TOLERANCE).
        exploded: list[dict[str, Any]] = []

        # Compound records (POLYLINE/VERTEX/SEQEND, INSERT/ATTRIB/SEQEND) consume
        # their trailing records even when the head record turns out malformed.
        vertex_pairs: list[list[DXFPair]] = []
        attributes: dict[str, str] = {}
        attribute_texts: list[dict[str, Any]] = []
        if kind == "POLYLINE":
            lookahead = record_index + 1
            while lookahead < len(records) and records[lookahead][0] == "VERTEX":
                vertex_pairs.append(records[lookahead][1])
                lookahead += 1
            if lookahead < len(records) and records[lookahead][0] == "SEQEND":
                lookahead += 1
            consumed = max(1, lookahead - record_index)
        elif kind == "INSERT":
            lookahead = record_index + 1
            while lookahead < len(records) and records[lookahead][0] == "ATTRIB":
                attribute = _attribute_from_pairs(records[lookahead][1])
                if attribute is not None:
                    tag, value, attribute_text = attribute
                    attributes[tag] = value
                    if attribute_text is not None:
                        attribute_texts.append(attribute_text)
                lookahead += 1
            if lookahead < len(records) and records[lookahead][0] == "SEQEND":
                lookahead += 1
            consumed = max(1, lookahead - record_index)

        if kind not in _CONVERTIBLE_DXF_KINDS:
            if model3d is not None:
                _collect_dxf_model3d_unconverted(model3d, model3d_scope, kind, pairs)
            _warn(warnings, f"[{context}] Unsupported DXF entity skipped: {kind}")
            record_index += consumed
            continue

        try:
            if kind == "LINE":
                entity = _line_from_pairs(pairs, serial, projection=projection)
            elif kind == "CIRCLE":
                entity = _circle_from_pairs(pairs, serial, projection=projection)
            elif kind == "ARC":
                entity = _arc_from_pairs(pairs, serial, projection=projection)
            elif kind == "POINT":
                entity = _point_from_pairs(pairs, serial, projection=projection)
            elif kind == "ELLIPSE":
                entity = _ellipse_from_pairs(pairs, serial)
            elif kind == "LWPOLYLINE":
                entity = _lwpolyline_from_pairs(pairs, serial)
            elif kind == "POLYLINE":
                polyline_flags = _first_int(pairs, 70, default=0) or 0
                if polyline_flags & (_POLYLINE_POLYGON_MESH | _POLYLINE_POLYFACE_MESH):
                    # A mesh is a surface: its VERTEX records are a grid of points
                    # or face indices, not a path.
                    mesh = (
                        "polyface mesh"
                        if polyline_flags & _POLYLINE_POLYFACE_MESH
                        else "polygon mesh"
                    )
                    _import_diagnose(
                        diagnostics,
                        warnings,
                        code="DXF_MESH_SKIPPED",
                        severity="warning",
                        message=f"[{context}] Skipped DXF {mesh} (3D surface).",
                        source_id=_first_string(pairs, 5),
                        source_kind="POLYLINE",
                        action="skipped",
                    )
                    if model3d is not None:
                        _collect_dxf_polyline_mesh(
                            model3d, model3d_scope, pairs, vertex_pairs, polyline_flags
                        )
                    record_index += consumed
                    continue
                entity = _polyline_from_pairs(pairs, vertex_pairs, serial)
                if polyline_flags & _POLYLINE_3D and _vertices_leave_xy_plane(
                    vertex_pairs
                ):
                    _import_diagnose(
                        diagnostics,
                        warnings,
                        code="DXF_POLYLINE_3D_PROJECTED",
                        severity="warning",
                        message=(
                            f"[{context}] 3D POLYLINE {entity['id']} projected to XY."
                        ),
                        source_id=str(entity["id"]),
                        source_kind="POLYLINE",
                        action="projected",
                    )
            elif kind == "TEXT":
                entity = _text_from_pairs(pairs, serial)
            elif kind == "MTEXT":
                entity = _mtext_from_pairs(pairs, serial)
            elif kind == "ATTDEF":
                entity = _attdef_from_pairs(pairs, serial, in_block=in_block)
            elif kind == "INSERT":
                entity = _insert_from_pairs(
                    pairs, serial, attributes if attributes else None
                )
                if attribute_texts:
                    entity["attribute_texts"] = attribute_texts
            elif kind == "HATCH":
                entity = _hatch_from_pairs(
                    pairs, serial, warnings=warnings, diagnostics=diagnostics
                )
            elif kind == "IMAGE":
                entity = _image_from_pairs(
                    pairs,
                    serial,
                    image_defs=image_defs,
                    projection=projection,
                    context=context,
                    diagnostics=diagnostics,
                    warnings=warnings,
                )
            elif kind == "SPLINE":
                entity = _spline_from_pairs(pairs, serial)
            elif kind == "DIMENSION":
                entity = _dimension_from_pairs(pairs, serial, warnings=warnings)
            elif kind in {"SOLID", "TRACE"}:
                entity = _solid_from_pairs(pairs, serial)
            elif kind == "LEADER":
                entity = _leader_from_pairs(pairs, serial)
                _import_diagnose(
                    diagnostics,
                    warnings,
                    code="DXF_LEADER_APPROXIMATED",
                    severity="warning",
                    message=(
                        f"[{context}] LEADER {entity['id']} imported as its polyline "
                        "path; arrowhead and annotation link were omitted."
                    ),
                    source_id=str(entity["id"]),
                    source_kind="LEADER",
                    action="approximated",
                )
            elif kind == "TOLERANCE":
                exploded = _tolerance_from_pairs(
                    pairs,
                    serial,
                    dimstyles=dimstyles or {},
                    default_text_height=default_text_height,
                )
                for part in exploded:
                    part["source"]["kind"] = kind
                    validate_entity(part, f"{context}.{kind}")
                if exploded:
                    _import_diagnose(
                        diagnostics,
                        warnings,
                        code="DXF_TOLERANCE_EXPLODED",
                        severity="warning",
                        message=(
                            f"[{context}] TOLERANCE {_first_string(pairs, 5) or serial} "
                            "drawn as lines and texts; compartment widths are "
                            "estimated."
                        ),
                        source_id=_first_string(pairs, 5),
                        source_kind="TOLERANCE",
                        action="exploded",
                    )
            if entity is not None:
                entity.setdefault("source", {"format": "dxf"})["kind"] = kind
                if projection is not None:
                    entity.setdefault("metadata", {}).setdefault("dxf", {})[
                        "projected_from_plane"
                    ] = projection.source_plane
                if kind in {"TEXT", "MTEXT", "ATTDEF"}:
                    height = entity.get("height")
                    if not isinstance(height, (int, float)) or not height > 0:
                        entity["height"] = default_text_height
                        _import_diagnose(
                            diagnostics,
                            warnings,
                            code="DXF_TEXT_HEIGHT_DEFAULTED",
                            severity="warning",
                            message=(
                                f"[{context}] {kind} {entity['id']}: non-positive text "
                                f"height replaced with {default_text_height}."
                            ),
                            source_id=str(entity["id"]),
                            source_kind=kind,
                            action="normalized",
                        )
                validate_entity(entity, f"{context}.{kind}")
        except (
            IRValidationError,
            ValueError,
            KeyError,
            TypeError,
            IndexError,
            OverflowError,
            ZeroDivisionError,
        ) as exc:
            handle = _first_string(pairs, 5)
            _import_diagnose(
                diagnostics,
                warnings,
                code="DXF_ENTITY_CONVERSION_FAILED",
                severity="error",
                message=f"[{context}] Skipped malformed DXF {kind}: {exc}",
                source_id=handle,
                source_kind=kind,
                action="skipped",
            )
            record_index += consumed
            continue

        if entity is not None:
            exploded = [entity]
        for produced in exploded:
            _make_entity_id_unique(produced, used_ids)
            entities.append(produced)
        if model3d is not None and entity is not None:
            _collect_dxf_model3d(
                model3d, model3d_scope, kind, pairs, vertex_pairs, entity
            )
        if exploded:
            serial += 1
        record_index += consumed

    if release_records:
        records.clear()
    return entities


# What a converted record is replaced by when the records are used up
# (_records_to_entities with release_records)
_RELEASED_RECORD: tuple[str, list[DXFPair]] = ("", [])


def _points3_sequence(
    pairs: list[DXFPair], x_code: int = 10, y_code: int = 20, z_code: int = 30
) -> list[tuple[float, float, float]]:
    """Repeated ``(x, y[, z])`` groups in file order (polyline/spline vertices)."""
    points: list[list[float]] = []
    for code, value in pairs:
        if code == x_code:
            points.append([_to_float(value), 0.0, 0.0])
        elif code == y_code and points:
            points[-1][1] = _to_float(value)
        elif code == z_code and points:
            points[-1][2] = _to_float(value)
    return [(point[0], point[1], point[2]) for point in points]


def _bulges_sequence(pairs: list[DXFPair], x_code: int = 10) -> list[float]:
    """Bulge (42) of each vertex that starts with ``x_code``; 0 when absent."""
    bulges: list[float] = []
    for code, value in pairs:
        if code == x_code:
            bulges.append(0.0)
        elif code == 42 and bulges:
            bulges[-1] = _to_float(value)
    return bulges


def _collect_dxf_model3d(
    collector: _model3d.Model3DCollector,
    scope: Any,
    kind: str,
    pairs: list[DXFPair],
    vertex_pairs: list[list[DXFPair]],
    entity: dict[str, Any],
) -> None:
    """Side channel of ``keep_3d``: the geometry of a converted record before projection."""
    source_id = _first_string(pairs, 5)
    segments = collector.curve_segments
    try:
        if kind == "LINE":
            collector.add_curve(
                scope,
                entity=entity,
                source_id=source_id,
                kind=kind,
                points=[
                    _point3_from_pairs(pairs, 10, 20, 30),
                    _point3_from_pairs(pairs, 11, 21, 31),
                ],
            )
        elif kind in {"CIRCLE", "ARC"}:
            full = kind == "CIRCLE"
            points = _model3d.arc_points(
                _point3_from_pairs(pairs, 10, 20, 30),
                _required_float(pairs, 40),
                0.0 if full else _required_float(pairs, 50),
                360.0 if full else _required_float(pairs, 51),
                extrusion=_extrusion_from_pairs(pairs),
                curve_segments=segments,
                full=full,
            )
            collector.add_curve(
                scope,
                entity=entity,
                source_id=source_id,
                kind=kind,
                points=points,
                closed=full,
            )
        elif kind == "ELLIPSE":
            start = _first_float(pairs, 41, default=0.0) or 0.0
            end = _first_float(pairs, 42, default=None)
            if end is None:
                end = 2.0 * math.pi
            points = _model3d.ellipse_points(
                _point3_from_pairs(pairs, 10, 20, 30),
                _point3_from_pairs(pairs, 11, 21, 31),
                _required_float(pairs, 40),
                start,
                end,
                extrusion=_extrusion_from_pairs(pairs),
                curve_segments=segments,
            )
            full = abs((end - start) % (2.0 * math.pi)) <= 1e-9
            collector.add_curve(
                scope,
                entity=entity,
                source_id=source_id,
                kind=kind,
                points=points,
                closed=full,
            )
        elif kind == "LWPOLYLINE":
            elevation = _first_float(pairs, 38, default=0.0) or 0.0
            closed = bool((_first_int(pairs, 70, default=0) or 0) & 1)
            vertices = [
                (x, y, elevation) for x, y, _z in _points3_sequence(pairs, 10, 20, 30)
            ]
            points = _model3d.bulge_polyline_points(
                vertices,
                _bulges_sequence(pairs),
                closed=closed,
                extrusion=_extrusion_from_pairs(pairs),
                curve_segments=segments,
            )
            collector.add_curve(
                scope,
                entity=entity,
                source_id=source_id,
                kind=kind,
                points=points,
                closed=closed,
            )
        elif kind == "POLYLINE":
            flags = _first_int(pairs, 70, default=0) or 0
            closed = bool(flags & 1)
            path = _polyline_path_vertices(vertex_pairs)
            if flags & _POLYLINE_3D:
                points = [_point3_from_pairs(vertex, 10, 20, 30) for vertex in path]
            else:
                elevation = _first_float(pairs, 30, default=0.0) or 0.0
                vertices = [
                    (
                        _required_float(vertex, 10),
                        _required_float(vertex, 20),
                        elevation,
                    )
                    for vertex in path
                ]
                bulges = [
                    _first_float(vertex, 42, default=0.0) or 0.0 for vertex in path
                ]
                points = _model3d.bulge_polyline_points(
                    vertices,
                    bulges,
                    closed=closed,
                    extrusion=_extrusion_from_pairs(pairs),
                    curve_segments=segments,
                )
            collector.add_curve(
                scope,
                entity=entity,
                source_id=source_id,
                kind=kind,
                points=points,
                closed=closed,
            )
        elif kind == "SPLINE":
            controls = _points3_sequence(pairs, 10, 20, 30)
            closed = bool((_first_int(pairs, 70, default=0) or 0) & 1)
            if len(controls) >= 2:
                points = _model3d.spline_points(
                    controls,
                    _first_int(pairs, 71, default=3) or 3,
                    _all_floats(pairs, 40),
                    _all_floats(pairs, 41),
                    closed=closed,
                    curve_segments=segments,
                )
            else:
                points = _points3_sequence(pairs, 11, 21, 31)
            collector.add_curve(
                scope,
                entity=entity,
                source_id=source_id,
                kind=kind,
                points=points,
                closed=closed,
            )
        elif kind == "LEADER":
            collector.add_curve(
                scope,
                entity=entity,
                source_id=source_id,
                kind=kind,
                points=_points3_sequence(pairs, 10, 20, 30),
            )
        elif kind in {"SOLID", "TRACE"}:
            extrusion = _extrusion_from_pairs(pairs)
            corners = []
            for x_code, y_code, z_code in (
                (10, 20, 30),
                (11, 21, 31),
                (12, 22, 32),
                (13, 23, 33),
            ):
                if _first_float(pairs, x_code, default=None) is None:
                    continue
                corners.append(
                    _model3d.ocs_to_wcs(
                        _point3_from_pairs(pairs, x_code, y_code, z_code), extrusion
                    )
                )
            collector.add_faces(
                scope,
                entity=entity,
                source_id=source_id,
                kind=kind,
                triangles=_model3d.solid_triangles(corners),
            )
        elif kind == "INSERT":
            collector.add_insert(
                scope,
                entity=entity,
                source_id=source_id,
                block=str(entity.get("block") or _first_string(pairs, 2) or ""),
                insert=_point3_from_pairs(pairs, 10, 20, 30),
                extrusion=_extrusion_from_pairs(pairs),
                scale=(
                    _first_float(pairs, 41, default=1.0) or 1.0,
                    _first_float(pairs, 42, default=1.0) or 1.0,
                    _first_float(pairs, 43, default=1.0) or 1.0,
                ),
                rotation_deg=_first_float(pairs, 50, default=0.0) or 0.0,
                columns=_first_int(pairs, 70, default=1) or 1,
                rows=_first_int(pairs, 71, default=1) or 1,
                column_spacing=_first_float(pairs, 44, default=0.0) or 0.0,
                row_spacing=_first_float(pairs, 45, default=0.0) or 0.0,
            )
    except (KeyError, TypeError, ValueError, OverflowError, IndexError):
        return


def _collect_dxf_model3d_unconverted(
    collector: _model3d.Model3DCollector,
    scope: Any,
    kind: str,
    pairs: list[DXFPair],
) -> None:
    """3D records the IR has no entity for: 3DFACE, MESH, and the unreadable solids."""
    source_id = _first_string(pairs, 5)
    try:
        if kind == "3DFACE":
            corners = []
            for x_code, y_code, z_code in (
                (10, 20, 30),
                (11, 21, 31),
                (12, 22, 32),
                (13, 23, 33),
            ):
                if _first_float(pairs, x_code, default=None) is None:
                    continue
                corners.append(_point3_from_pairs(pairs, x_code, y_code, z_code))
            collector.add_faces(
                scope,
                entity=None,
                source_id=source_id,
                kind=kind,
                triangles=_model3d.face3d_triangles(corners),
            )
        elif kind == "MESH":
            collector.add_faces(
                scope,
                entity=None,
                source_id=source_id,
                kind=kind,
                triangles=_mesh_triangles_from_pairs(pairs),
            )
        elif kind in _model3d.UNSUPPORTED_3D_KINDS:
            collector.add_unsupported(kind)
    except (KeyError, TypeError, ValueError, OverflowError, IndexError):
        return


def _mesh_triangles_from_pairs(pairs: list[DXFPair]) -> list[Any]:
    """Faces of a MESH entity: 92 vertices, then 93 face-list values (90)."""
    vertices: list[tuple[float, float, float]] = []
    face_values: list[int] = []
    vertex_count: int | None = None
    face_list_size: int | None = None
    current: list[float] | None = None
    for code, value in pairs:
        if code == 92 and vertex_count is None:
            vertex_count = int(float(value))
        elif code == 10 and vertex_count is not None and len(vertices) < vertex_count:
            current = [_to_float(value), 0.0, 0.0]
            vertices.append((current[0], 0.0, 0.0))
        elif code == 20 and current is not None:
            current[1] = _to_float(value)
            vertices[-1] = (current[0], current[1], current[2])
        elif code == 30 and current is not None:
            current[2] = _to_float(value)
            vertices[-1] = (current[0], current[1], current[2])
        elif code == 93 and face_list_size is None:
            face_list_size = int(float(value))
        elif (
            code == 90
            and face_list_size is not None
            and len(face_values) < face_list_size
        ):
            face_values.append(int(float(value)))
    faces: list[list[int]] = []
    index = 0
    while index < len(face_values):
        count = face_values[index]
        index += 1
        if count <= 0:
            break
        faces.append([item + 1 for item in face_values[index : index + count]])
        index += count
    return _model3d.polyface_triangles(vertices, faces)


def _collect_dxf_polyline_mesh(
    collector: _model3d.Model3DCollector,
    scope: Any,
    pairs: list[DXFPair],
    vertex_pairs: list[list[DXFPair]],
    polyline_flags: int,
) -> None:
    """Polyface (flag 64) and polygon (flag 16) meshes stored as POLYLINE/VERTEX."""
    source_id = _first_string(pairs, 5)
    try:
        if polyline_flags & _POLYLINE_POLYFACE_MESH:
            vertices: list[tuple[float, float, float]] = []
            faces: list[list[int]] = []
            for vertex in vertex_pairs:
                flags = _first_int(vertex, 70, default=0) or 0
                if flags & 128 and not flags & 64:
                    faces.append(
                        [
                            _first_int(vertex, code, default=0) or 0
                            for code in (71, 72, 73, 74)
                            if (_first_int(vertex, code, default=0) or 0) != 0
                        ]
                    )
                else:
                    vertices.append(_point3_from_pairs(vertex, 10, 20, 30))
            triangles = _model3d.polyface_triangles(vertices, faces)
            kind = "POLYLINE_PFACE"
        else:
            points = [_point3_from_pairs(vertex, 10, 20, 30) for vertex in vertex_pairs]
            flags = _first_int(pairs, 70, default=0) or 0
            triangles = _model3d.grid_triangles(
                points,
                _first_int(pairs, 71, default=0) or 0,
                _first_int(pairs, 72, default=0) or 0,
                closed_m=bool(flags & 1),
                closed_n=bool(flags & 32),
            )
            kind = "POLYLINE_MESH"
        collector.add_faces(
            scope, entity=None, source_id=source_id, kind=kind, triangles=triangles
        )
    except (KeyError, TypeError, ValueError, OverflowError, IndexError):
        return


# Initial values of the dimension variables DIMTXT and DIMGAP.
_DIMTXT_INITIAL = 0.18
_DIMGAP_INITIAL = 0.09


def _dimstyle_sizes(table_pairs: list[DXFPair]) -> dict[str, dict[str, float]]:
    """Sizes of every dimension style by upper-case name.

    ``dimtxt`` (group 140) is the text height, ``dimgap`` (147) the distance
    between a text and its frame and ``dimscale`` (40) the overall scale.
    """
    styles: dict[str, dict[str, float]] = {}
    current_table: str | None = None
    for kind, pairs in _pairs_to_records(table_pairs):
        if kind == "TABLE":
            current_table = (_first_string(pairs, 2) or "").upper()
            continue
        if kind == "ENDTAB":
            current_table = None
            continue
        if current_table != "DIMSTYLE" or kind != "DIMSTYLE":
            continue
        name = _first_string(pairs, 2)
        if not name:
            continue
        sizes: dict[str, float] = {}
        for key, code in (("dimscale", 40), ("dimtxt", 140), ("dimgap", 147)):
            try:
                value = _first_float(pairs, code, default=None)
            except ValueError:
                value = None
            if value is not None and math.isfinite(value):
                sizes[key] = value
        styles[name.strip().upper()] = sizes
    return styles


def _dimstyle_overrides(pairs: list[DXFPair]) -> dict[int, float]:
    """Dimension variable overrides of an entity (``ACAD`` / ``DSTYLE`` xdata).

    The override list alternates the group code of a dimension variable (1070)
    with its value.
    """
    overrides: dict[int, float] = {}
    in_dstyle = False
    variable: int | None = None
    for code, value in pairs:
        if code == 1000:
            in_dstyle = value.strip().upper() == "DSTYLE"
            variable = None
        elif code == 1001:
            in_dstyle = False
        elif in_dstyle and code == 1070 and variable is None:
            try:
                variable = int(float(value))
            except ValueError:
                variable = None
        elif in_dstyle and variable is not None and code in (1040, 1070):
            try:
                overrides[variable] = float(value)
            except ValueError:
                pass
            variable = None
    return overrides


def _tolerance_from_pairs(
    pairs: list[DXFPair],
    idx: int,
    *,
    dimstyles: dict[str, dict[str, float]],
    default_text_height: float,
) -> list[dict[str, Any]]:
    """Feature control frame as the lines of its frame and one TEXT per compartment.

    The text height is the one of the dimension style (``DIMTXT`` times
    ``DIMSCALE``), which the entity can override.
    """
    common = _common_from_pairs(pairs, idx)
    text = _first_string(pairs, 1) or ""
    style = dimstyles.get((_first_string(pairs, 3) or "").strip().upper())
    overrides = _dimstyle_overrides(pairs)
    # A DIMSTYLE record leaves out the variables that have their initial value.
    defaults = (
        {} if style is None else {"dimtxt": _DIMTXT_INITIAL, "dimgap": _DIMGAP_INITIAL}
    )
    values = {**defaults, **(style or {})}
    dimtxt = overrides.get(140, values.get("dimtxt"))
    dimscale = overrides.get(40, values.get("dimscale")) or 1.0
    height = dimtxt * dimscale if dimtxt is not None and dimtxt > 0.0 else None
    if height is None or not height > 0.0:
        height = default_text_height
    dimgap = overrides.get(147, values.get("dimgap"))
    gap = abs(dimgap) * dimscale if dimgap else None
    direction_x = _first_float(pairs, 11, default=1.0)
    direction_y = _first_float(pairs, 21, default=0.0)
    rotation = math.degrees(math.atan2(direction_y or 0.0, direction_x or 0.0))
    common.setdefault("metadata", {}).setdefault("dxf", {})["tolerance_text"] = text
    return explode_tolerance(
        text=text,
        insert=[_required_float(pairs, 10), _required_float(pairs, 20)],
        height=height,
        rotation_deg=rotation,
        gap=gap,
        common=common,
    )


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
    linetype_scale = _entity_linetype_scale(pairs)
    if linetype_scale is not None:
        entity["linetype_scale"] = linetype_scale

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


def _line_from_pairs(
    pairs: list[DXFPair],
    idx: int,
    *,
    projection: _AxisProjection | None = None,
) -> dict[str, Any]:
    entity = _common_from_pairs(pairs, idx)
    entity["kind"] = "LINE"
    p1 = _point3_from_pairs(pairs, 10, 20, 30)
    p2 = _point3_from_pairs(pairs, 11, 21, 31)
    entity["p1"] = projection.project_wcs(p1) if projection else [p1[0], p1[1]]
    entity["p2"] = projection.project_wcs(p2) if projection else [p2[0], p2[1]]
    return entity


def _circle_from_pairs(
    pairs: list[DXFPair],
    idx: int,
    *,
    projection: _AxisProjection | None = None,
) -> dict[str, Any]:
    entity = _common_from_pairs(pairs, idx)
    entity["kind"] = "CIRCLE"
    center = _point3_from_pairs(pairs, 10, 20, 30)
    if projection is not None:
        center = _ocs_to_wcs(center, _extrusion_from_pairs(pairs))
        entity["center"] = projection.project_wcs(center)
    else:
        entity["center"] = [center[0], center[1]]
    entity["radius"] = _required_float(pairs, 40)
    return entity


def _arc_from_pairs(
    pairs: list[DXFPair],
    idx: int,
    *,
    projection: _AxisProjection | None = None,
) -> dict[str, Any]:
    entity = _common_from_pairs(pairs, idx)
    entity["kind"] = "ARC"
    center_ocs = _point3_from_pairs(pairs, 10, 20, 30)
    radius = _required_float(pairs, 40)
    start_angle = _required_float(pairs, 50)
    end_angle = _required_float(pairs, 51)
    entity["radius"] = radius
    if projection is None:
        entity["center"] = [center_ocs[0], center_ocs[1]]
        entity["start_angle"] = start_angle
        entity["end_angle"] = end_angle
        return entity

    extrusion = _extrusion_from_pairs(pairs)
    center_wcs = _ocs_to_wcs(center_ocs, extrusion)
    start_radians = math.radians(start_angle)
    end_radians = math.radians(end_angle)
    start_wcs = _ocs_to_wcs(
        (
            center_ocs[0] + radius * math.cos(start_radians),
            center_ocs[1] + radius * math.sin(start_radians),
            center_ocs[2],
        ),
        extrusion,
    )
    end_wcs = _ocs_to_wcs(
        (
            center_ocs[0] + radius * math.cos(end_radians),
            center_ocs[1] + radius * math.sin(end_radians),
            center_ocs[2],
        ),
        extrusion,
    )
    center_2d = projection.project_wcs(center_wcs)
    start_2d = projection.project_wcs(start_wcs)
    end_2d = projection.project_wcs(end_wcs)
    entity["center"] = center_2d
    entity["start_angle"] = _projected_angle(center_2d, start_2d)
    entity["end_angle"] = _projected_angle(center_2d, end_2d)
    if _dot3(extrusion, projection.normal) < 0.0:
        entity["ccw"] = False
    return entity


def _point_from_pairs(
    pairs: list[DXFPair],
    idx: int,
    *,
    projection: _AxisProjection | None = None,
) -> dict[str, Any]:
    entity = _common_from_pairs(pairs, idx)
    entity["kind"] = "POINT"
    point = _point3_from_pairs(pairs, 10, 20, 30)
    entity["position"] = (
        projection.project_wcs(point) if projection else [point[0], point[1]]
    )
    return entity


def _point3_from_pairs(
    pairs: list[DXFPair], x_code: int, y_code: int, z_code: int
) -> tuple[float, float, float]:
    return (
        _required_float(pairs, x_code),
        _required_float(pairs, y_code),
        _first_float(pairs, z_code, default=0.0) or 0.0,
    )


def _extrusion_from_pairs(pairs: list[DXFPair]) -> tuple[float, float, float]:
    extrusion = (
        _first_float(pairs, 210, default=0.0) or 0.0,
        _first_float(pairs, 220, default=0.0) or 0.0,
        _first_float(pairs, 230, default=1.0),
    )
    z = 1.0 if extrusion[2] is None else extrusion[2]
    return _normalize3((extrusion[0], extrusion[1], z))


def _ocs_to_wcs(
    point: tuple[float, float, float], extrusion: tuple[float, float, float]
) -> tuple[float, float, float]:
    # DXF arbitrary-axis algorithm. OCS Z is the normalized extrusion vector;
    # OCS X/Y are derived deterministically from it.
    if abs(extrusion[0]) < 1.0 / 64.0 and abs(extrusion[1]) < 1.0 / 64.0:
        x_axis = _normalize3(_cross3((0.0, 1.0, 0.0), extrusion))
    else:
        x_axis = _normalize3(_cross3((0.0, 0.0, 1.0), extrusion))
    y_axis = _cross3(extrusion, x_axis)
    return tuple(
        point[0] * x_axis[axis] + point[1] * y_axis[axis] + point[2] * extrusion[axis]
        for axis in range(3)
    )


def _projected_angle(center: list[float], point: list[float]) -> float:
    angle = math.degrees(math.atan2(point[1] - center[1], point[0] - center[0]))
    normalized = angle % 360.0
    if math.isclose(normalized, 360.0, abs_tol=1e-10) or math.isclose(
        normalized, 0.0, abs_tol=1e-10
    ):
        return 0.0
    return normalized


def _dot3(left: tuple[float, float, float], right: tuple[float, float, float]) -> float:
    return sum(a * b for a, b in zip(left, right, strict=True))


def _cross3(
    left: tuple[float, float, float], right: tuple[float, float, float]
) -> tuple[float, float, float]:
    return (
        left[1] * right[2] - left[2] * right[1],
        left[2] * right[0] - left[0] * right[2],
        left[0] * right[1] - left[1] * right[0],
    )


def _normalize3(vector: tuple[float, float, float]) -> tuple[float, float, float]:
    length = math.sqrt(_dot3(vector, vector))
    if length <= 1e-15:
        raise ValueError("DXF extrusion vector must be non-zero")
    return tuple(component / length for component in vector)


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


# POLYLINE flags (group 70).
_POLYLINE_3D = 8
_POLYLINE_POLYGON_MESH = 16
_POLYLINE_POLYFACE_MESH = 64
# VERTEX flag (group 70): control point of the frame of a spline-fit polyline.
_VERTEX_SPLINE_FRAME = 16


def _vertices_leave_xy_plane(vertex_pairs: list[list[DXFPair]]) -> bool:
    for vertex in vertex_pairs:
        try:
            z = _first_float(vertex, 30, default=0.0) or 0.0
        except ValueError:
            continue
        if abs(z) > 1e-12:
            return True
    return False


def _polyline_path_vertices(vertex_pairs: list[list[DXFPair]]) -> list[list[DXFPair]]:
    """The VERTEX records a polyline is drawn through.

    A spline-fit polyline also stores the control points of its frame. The
    curve runs through the fitted vertices; the frame is not displayed.
    """
    path = [
        vertex
        for vertex in vertex_pairs
        if not (_first_int(vertex, 70, default=0) or 0) & _VERTEX_SPLINE_FRAME
    ]
    return path if len(path) >= 2 else vertex_pairs


def _polyline_from_pairs(
    pairs: list[DXFPair],
    vertex_pairs: list[list[DXFPair]],
    idx: int,
) -> dict[str, Any]:
    entity = _common_from_pairs(pairs, idx)
    entity["kind"] = "LWPOLYLINE"
    vertices: list[list[float]] = []
    for vertex in _polyline_path_vertices(vertex_pairs):
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


def text_alignment_from_dxf(
    halign_code: int | None, valign_code: int | None
) -> tuple[str, str, bool]:
    """IR alignment of a DXF/DWG text, and whether its second point anchors it.

    A text keeps two points. The first one (group 10) is always the left end of
    the baseline. For every justification other than left/baseline, the point
    the text is justified at is the second one (group 11), and the first is
    derived from it. "Aligned" and "fit" (horizontal code 3 and 5) stretch the
    text between both points, so it starts at the first; "middle" (4) centers
    it on the second.
    """
    horizontal = halign_code or 0
    vertical = valign_code or 0
    if vertical == 0 and horizontal in (3, 5):
        return "left", "baseline", False
    if vertical == 0 and horizontal == 4:
        return "center", "middle", True
    halign = _TEXT_HALIGN_FROM_DXF.get(horizontal, "left")
    valign = _TEXT_VALIGN_FROM_DXF.get(vertical, "baseline")
    return halign, valign, (halign, valign) != ("left", "baseline")


def _text_fields_from_pairs(
    pairs: list[DXFPair], *, valign_code: int | None
) -> dict[str, Any]:
    """The fields TEXT and ATTRIB share: anchor point, size and justification.

    ``valign_code`` is the group holding the vertical justification (73 for
    TEXT, 74 for ATTRIB); None reads it as baseline.
    """
    fields: dict[str, Any] = {
        "insert": [_required_float(pairs, 10), _required_float(pairs, 20)],
        "height": _required_float(pairs, 40),
        "text": _first_string(pairs, 1) or "",
    }

    rotation = _first_float(pairs, 50, default=0.0)
    if rotation != 0.0:
        fields["rotation"] = rotation

    style = _first_string(pairs, 7)
    if style:
        fields["style"] = style

    halign, valign, anchored = text_alignment_from_dxf(
        _first_int(pairs, 72, default=0),
        None if valign_code is None else _first_int(pairs, valign_code, default=0),
    )
    if anchored:
        anchor_x = _first_float(pairs, 11, default=None)
        anchor_y = _first_float(pairs, 21, default=None)
        # Without a second point the first is all there is to anchor the text.
        if anchor_x is not None and anchor_y is not None:
            fields["insert"] = [anchor_x, anchor_y]
    if halign != "left":
        fields["halign"] = halign
    if valign != "baseline":
        fields["valign"] = valign

    width_factor = _first_float(pairs, 41, default=None)
    if width_factor is not None:
        fields["width_factor"] = width_factor

    oblique = _first_float(pairs, 51, default=None)
    if oblique is not None:
        fields["oblique_deg"] = oblique
    return fields


def _text_from_pairs(pairs: list[DXFPair], idx: int) -> dict[str, Any]:
    entity = _common_from_pairs(pairs, idx)
    entity["kind"] = "TEXT"
    entity.update(_text_fields_from_pairs(pairs, valign_code=73))
    return entity


def _attribute_record_parts(
    pairs: list[DXFPair],
) -> tuple[list[DXFPair], list[DXFPair], list[DXFPair]]:
    """Text part, attribute part and embedded MTEXT of an ATTRIB or ATTDEF.

    Groups 72 and 73 mean something else in the attribute part of the record
    than in its text part, so each part is read on its own when the record
    marks them. A record without subclass markers is both parts at once. A
    multi-line attribute carries its text in an MTEXT embedded at the end.
    """
    embedded_start = next(
        (index for index, (code, _) in enumerate(pairs) if code == 101), len(pairs)
    )
    embedded = pairs[embedded_start + 1 :]
    own = pairs[:embedded_start]
    attribute_start = next(
        (
            index
            for index, (code, value) in enumerate(own)
            if code == 100
            and value.strip() in ("AcDbAttribute", "AcDbAttributeDefinition")
        ),
        None,
    )
    if attribute_start is None:
        return own, own, embedded
    return own[:attribute_start], own[attribute_start:], embedded


def _attdef_from_pairs(
    pairs: list[DXFPair], idx: int, *, in_block: bool
) -> dict[str, Any] | None:
    """The text an attribute definition shows, or None when it shows none.

    Inside a block definition an ATTDEF is a template: a block reference shows
    the values of its own ATTRIB records instead. Only a constant definition
    (flag 2) has no ATTRIB and is part of the block, with its value. Outside of
    a block the definition itself is displayed, by its tag. Invisible
    definitions (flag 1) are never shown.
    """
    text_part, attribute_part, _embedded = _attribute_record_parts(pairs)
    flags = _first_int(attribute_part, 70, default=0) or 0
    if flags & 1 or (in_block and not flags & 2):
        return None
    entity = _common_from_pairs(text_part, idx)
    entity["kind"] = "TEXT"
    entity.update(
        _text_fields_from_pairs(
            [
                *text_part,
                *((code, text) for code, text in attribute_part if code == 74),
            ],
            valign_code=74,
        )
    )
    if not in_block:
        entity["text"] = _first_string(attribute_part, 2) or entity["text"]
    return entity


def _attribute_from_pairs(
    pairs: list[DXFPair],
) -> tuple[str, str, dict[str, Any] | None] | None:
    """Tag, value and text of one ATTRIB record; None when it has no tag.

    The text is None when the record has no usable geometry (the value is still
    kept in the INSERT's ``attributes``).
    """
    text_part, attribute_part, embedded = _attribute_record_parts(pairs)

    tag = _first_string(attribute_part, 2)
    if not tag:
        return None
    value = _first_string(text_part, 1) or ""
    if embedded:
        multiline = "".join(text for code, text in embedded if code in (3, 1))
        if multiline:
            value = mtext_plain(multiline)

    try:
        fields = _text_fields_from_pairs(
            [
                *text_part,
                *((code, text) for code, text in attribute_part if code == 74),
            ],
            valign_code=74,
        )
    except ValueError:
        return tag, value, None
    height = fields["height"]
    if not all(math.isfinite(number) for number in (*fields["insert"], height)):
        return tag, value, None
    if not height > 0:
        return tag, value, None

    attribute_text: dict[str, Any] = {"tag": tag, **fields, "text": value}
    common = _common_from_pairs(text_part, 0)
    for key in ("layer", "color"):
        if key in common:
            attribute_text[key] = common[key]
    flags = _first_int(attribute_part, 70, default=0) or 0
    if flags & 1 or common.get("visible") is False:
        attribute_text["visible"] = False
    return tag, value, attribute_text


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

    # AutoCAD and ODA give the direction of the text as a vector (11, 21, 31)
    # and no angle; group 50 (degrees) is the older form.
    rotation = _first_float(pairs, 50, default=0.0) or 0.0
    direction_x = _first_float(pairs, 11, default=None)
    direction_y = _first_float(pairs, 21, default=None)
    if (
        direction_x is not None
        and direction_y is not None
        and math.hypot(direction_x, direction_y) > 1e-12
    ):
        rotation = math.degrees(math.atan2(direction_y, direction_x))
        if abs(rotation) < 1e-9:
            rotation = 0.0
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

    # Each scale factor defaults to 1 on its own: AutoCAD and ODA leave out
    # a factor of 1, so a block mirrored in X comes with group 41 alone.
    sx = _first_float(pairs, 41, default=1.0) or 1.0
    sy = _first_float(pairs, 42, default=1.0) or 1.0
    if abs(sx - sy) < 1e-12:
        if abs(sx - 1.0) > 1e-12:
            entity["scale"] = sx
    else:
        entity["scale"] = [sx, sy]

    if attributes:
        entity["attributes"] = attributes
    return entity


def _normalize_handle(value: str | None) -> str | None:
    """Upper-case hex without leading zeros, so ``0A1`` and ``a1`` match."""
    if value is None or not value.strip():
        return None
    return value.strip().upper().lstrip("0") or "0"


def _image_base_name(path: str) -> str:
    """The file name of a Windows or POSIX path."""
    return re.split(r"[\\/]", path.strip())[-1].strip()


def _integral(value: float) -> float | int:
    return int(value) if float(value).is_integer() else value


def _image_definitions(object_pairs: list[DXFPair]) -> dict[str, dict[str, Any]]:
    """IMAGEDEF objects by normalized handle.

    An IMAGEDEF holds the path of the raster file and its size in pixels; its
    name is the key under which the ACAD_IMAGE_DICT dictionary lists it.
    """
    if not any(
        code == 0 and value.strip() == "IMAGEDEF" for code, value in object_pairs
    ):
        return {}
    definitions: dict[str, dict[str, Any]] = {}
    names: dict[str, str] = {}
    for kind, pairs in _pairs_to_records(object_pairs):
        if kind == "IMAGEDEF":
            handle = _normalize_handle(_first_string(pairs, 5))
            if handle is None:
                continue
            try:
                definitions[handle] = {
                    "file_path": _first_string(pairs, 1) or "",
                    "size": [
                        _first_float(pairs, 10, default=None),
                        _first_float(pairs, 20, default=None),
                    ],
                    "pixel_size": [
                        _first_float(pairs, 11, default=None),
                        _first_float(pairs, 21, default=None),
                    ],
                    "loaded": _first_int(pairs, 280, default=None),
                    "resolution_units": _first_int(pairs, 281, default=None),
                }
            except (ValueError, TypeError, OverflowError):
                continue
        elif kind == "DICTIONARY":
            key: str | None = None
            for code, value in pairs:
                if code == 3:
                    key = value.strip()
                elif code in {350, 360} and key is not None:
                    target = _normalize_handle(value)
                    if target is not None:
                        names[target] = key
                    key = None
    for handle, definition in definitions.items():
        if handle in names:
            definition["name"] = names[handle]
    return definitions


def _is_full_image_clip(
    clip_type: int | None,
    vertices: list[list[float]],
    columns: float,
    rows: float,
) -> bool:
    """A rectangular boundary that covers the whole image (the default writers emit)."""
    if not vertices:
        return True
    if clip_type == 2 or len(vertices) != 2:
        return False
    tolerance = 1e-6
    xs = sorted(vertex[0] for vertex in vertices)
    ys = sorted(vertex[1] for vertex in vertices)
    return (
        xs[0] <= tolerance
        and ys[0] <= tolerance
        and xs[1] >= columns - 0.5 - tolerance
        and ys[1] >= rows - 0.5 - tolerance
    )


def _image_from_pairs(
    pairs: list[DXFPair],
    idx: int,
    *,
    image_defs: dict[str, dict[str, Any]] | None,
    projection: _AxisProjection | None,
    context: str,
    diagnostics: list[ImportDiagnostic] | None,
    warnings: list[str] | None,
) -> dict[str, Any]:
    """An IMAGE entity as an IR image placement.

    The entity stores the lower-left corner, one pixel step along the columns
    (U) and along the rows (V) and the size in pixels; the IMAGEDEF it refers
    to (group 340) names the file. The IR keeps the drawn width and height
    (step × count), the rotation of the column axis and the path in ``href``;
    the DXF values stay in ``metadata.dxf.image``.
    """
    entity = _common_from_pairs(pairs, idx)
    entity["kind"] = "IMAGE"
    handle = _first_string(pairs, 5)
    label = f"IMAGE {handle or idx}"
    insert3 = _point3_from_pairs(pairs, 10, 20, 30)
    u3 = _point3_from_pairs(pairs, 11, 21, 31)
    v3 = _point3_from_pairs(pairs, 12, 22, 32)
    columns = _first_float(pairs, 13, default=None)
    rows = _first_float(pairs, 23, default=None)
    if columns is None or rows is None:
        raise ValueError("IMAGE requires its size in pixels (group codes 13 and 23)")
    if not (columns > 0.0 and rows > 0.0):
        raise ValueError("IMAGE size in pixels must be positive")
    if projection is not None:
        insert = projection.project_wcs(insert3)
        u_vector = projection.project_wcs(u3)
        v_vector = projection.project_wcs(v3)
    else:
        insert = [insert3[0], insert3[1]]
        u_vector = [u3[0], u3[1]]
        v_vector = [v3[0], v3[1]]
    u_length = math.hypot(*u_vector)
    v_length = math.hypot(*v_vector)
    if u_length <= 1e-12 or v_length <= 1e-12:
        raise ValueError("IMAGE pixel vectors must be non-zero")

    definition_handle = _first_string(pairs, 340)
    definition = (
        image_defs.get(_normalize_handle(definition_handle) or "")
        if image_defs and definition_handle
        else None
    )
    file_path = str((definition or {}).get("file_path") or "").strip()
    definition_name = (definition or {}).get("name")
    if not isinstance(definition_name, str) or not definition_name:
        definition_name = None
    pixel_size = (definition or {}).get("pixel_size")
    if not (
        isinstance(pixel_size, list)
        and len(pixel_size) == 2
        and all(isinstance(value, float) for value in pixel_size)
    ):
        pixel_size = None

    entity["insert"] = insert
    entity["width"] = u_length * columns
    entity["height"] = v_length * rows
    entity["name"] = (
        _image_base_name(file_path) or definition_name or f"IMAGE_{handle or idx}"
    )
    rotation = math.degrees(math.atan2(u_vector[1], u_vector[0]))
    if abs(rotation) > 1e-9:
        entity["rotation"] = rotation
    if file_path:
        entity["href"] = file_path

    clipping = _first_int(pairs, 280, default=None)
    clip_type = _first_int(pairs, 71, default=None)
    clip_vertices = [
        [x, y] for x, y in zip(_all_floats(pairs, 14), _all_floats(pairs, 24))
    ]
    image_metadata: dict[str, Any] = {
        "size": [_integral(columns), _integral(rows)],
        "u_vector": list(u3),
        "v_vector": list(v3),
        "display_props": _first_int(pairs, 70, default=None),
        "clipping": bool(clipping) if clipping is not None else None,
        "brightness": _first_int(pairs, 281, default=None),
        "contrast": _first_int(pairs, 282, default=None),
        "fade": _first_int(pairs, 283, default=None),
        "clip_type": clip_type,
        "clip_vertices": clip_vertices,
        "clip_mode": _first_int(pairs, 290, default=None),
        "file_path": file_path or None,
        "image_def_handle": definition_handle,
        "definition_name": definition_name,
        "pixel_size": pixel_size,
    }
    entity.setdefault("metadata", {}).setdefault("dxf", {})["image"] = {
        key: value for key, value in image_metadata.items() if value is not None
    }

    if definition is None:
        _import_diagnose(
            diagnostics,
            warnings,
            code="DXF_IMAGE_DEF_MISSING",
            severity="warning",
            message=(
                f"[{context}] {label}: its IMAGEDEF {definition_handle or '?'} is "
                "not in OBJECTS; the placement is kept without a file path."
            ),
            source_id=handle,
            source_kind="IMAGE",
        )
    else:
        _import_diagnose(
            diagnostics,
            warnings,
            code="DXF_IMAGE_LINKED",
            severity="info",
            message=(
                f"[{context}] {label} references {file_path!r} by path; a DXF "
                "never embeds the pixels."
            ),
            source_id=handle,
            source_kind="IMAGE",
        )
    cross = u_vector[0] * v_vector[1] - u_vector[1] * v_vector[0]
    dot = u_vector[0] * v_vector[0] + u_vector[1] * v_vector[1]
    if cross <= 0.0 or abs(dot) > 1e-9 * u_length * v_length:
        _import_diagnose(
            diagnostics,
            warnings,
            code="DXF_IMAGE_AXES_APPROXIMATED",
            severity="warning",
            message=(
                f"[{context}] {label}: the pixel axes are mirrored or not at a "
                "right angle; the IMAGE keeps its width, height and the rotation "
                "of the column axis only."
            ),
            source_id=handle,
            source_kind="IMAGE",
            action="approximated",
        )
    if clipping and not _is_full_image_clip(clip_type, clip_vertices, columns, rows):
        _import_diagnose(
            diagnostics,
            warnings,
            code="DXF_IMAGE_CLIP_IGNORED",
            severity="warning",
            message=(
                f"[{context}] {label}: the clip boundary was not applied; the "
                "IMAGE spans its full extent."
            ),
            source_id=handle,
            source_kind="IMAGE",
            action="approximated",
        )
    return entity


def _hatch_from_pairs(
    pairs: list[DXFPair],
    idx: int,
    *,
    warnings: list[str] | None = None,
    diagnostics: list[ImportDiagnostic] | None = None,
) -> dict[str, Any]:
    entity = _common_from_pairs(pairs, idx)
    entity["kind"] = "HATCH"

    # Group 70: 1 = solid fill, 0 = pattern fill (a bare ``or 1`` used to coerce 0
    # into "solid", which turned pattern hatches into solid fills on re-export).
    if _first_int(pairs, 70, default=1) == 0:
        entity["solid"] = False

    pattern = _first_string(pairs, 2)
    if pattern and pattern.upper() != "SOLID":
        entity["pattern"] = pattern
    if entity.get("solid") is False:
        _apply_hatch_pattern(entity, pairs)

    loops, notes = _parse_hatch_loops(pairs)
    if not loops:
        raise ValueError("HATCH has no usable boundary loop")
    entity["loops"] = loops
    entity_id = str(entity["id"])
    if notes.approximated_edges:
        _import_diagnose(
            diagnostics,
            warnings,
            code="DXF_HATCH_EDGE_APPROXIMATED",
            severity="warning",
            message=(
                f"HATCH {entity_id}: {notes.approximated_edges} elliptic/spline "
                "boundary edge(s) were approximated by polyline segments."
            ),
            source_id=entity_id,
            source_kind="HATCH",
            action="approximated",
            details={"edges": notes.approximated_edges},
        )
    if notes.skipped_loops:
        _import_diagnose(
            diagnostics,
            warnings,
            code="DXF_HATCH_LOOP_SKIPPED",
            severity="warning",
            message=(
                f"HATCH {entity_id}: {notes.skipped_loops} boundary loop(s) could not "
                "be reconstructed and were skipped."
            ),
            source_id=entity_id,
            source_kind="HATCH",
            action="skipped",
            details={"loops": notes.skipped_loops},
        )
    return entity


# Groups of one pattern definition line: angle, base point, offset, dash count, dashes.
_HATCH_PATTERN_LINE_CODES = frozenset({53, 43, 44, 45, 46, 79, 49})
# A pattern with more definition lines than this is not a plausible hatch pattern.
_HATCH_PATTERN_MAX_LINES = 512


def _apply_hatch_pattern(entity: dict[str, Any], pairs: list[DXFPair]) -> None:
    """Pattern angle, scale and definition lines of a pattern-filled HATCH.

    The definition lines in a DXF file are already rotated by the pattern angle
    and scaled by the pattern scale, and they share the coordinate system of the
    boundary paths, so they go to the IR unchanged.
    """
    try:
        angle = _first_float(pairs, 52, default=None)
        scale = _first_float(pairs, 41, default=None)
    except ValueError:
        angle = scale = None
    if angle is not None and math.isfinite(angle) and angle != 0.0:
        entity["pattern_angle"] = angle
    if scale is not None and math.isfinite(scale) and scale > 0.0 and scale != 1.0:
        entity["pattern_scale"] = scale
    lines = _parse_hatch_pattern_lines(pairs)
    if lines:
        entity["pattern_lines"] = lines


def _parse_hatch_pattern_lines(pairs: list[DXFPair]) -> list[dict[str, Any]]:
    """HATCH pattern definition lines: group 78 and the line data that follows it."""
    start = next((i for i, (code, _) in enumerate(pairs) if code == 78), None)
    if start is None:
        return []
    raw_lines: list[dict[int, list[float]]] = []
    for code, value in pairs[start + 1 :]:
        if code not in _HATCH_PATTERN_LINE_CODES:
            break
        if code == 53:
            if len(raw_lines) >= _HATCH_PATTERN_MAX_LINES:
                return []
            raw_lines.append({})
        if not raw_lines:
            return []  # line data must start with its angle
        try:
            number = _to_float(value)
        except ValueError:
            return []
        raw_lines[-1].setdefault(code, []).append(number)

    lines: list[dict[str, Any]] = []
    for raw in raw_lines:
        if any(code not in raw for code in (53, 43, 44, 45, 46)):
            continue
        angle, base_x, base_y, offset_x, offset_y = (
            raw[code][0] for code in (53, 43, 44, 45, 46)
        )
        if not all(
            math.isfinite(value)
            for value in (angle, base_x, base_y, offset_x, offset_y)
        ):
            continue
        direction = math.radians(angle)
        spacing = offset_y * math.cos(direction) - offset_x * math.sin(direction)
        if abs(spacing) < 1e-12:
            continue  # every line of the family would lie on the same line
        line: dict[str, Any] = {
            "angle": angle,
            "base": [base_x, base_y],
            "offset": [offset_x, offset_y],
        }
        dashes = raw.get(49, [])
        if dashes and all(math.isfinite(dash) for dash in dashes):
            line["dashes"] = dashes
        lines.append(line)
    return lines


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


def _solid_from_pairs(pairs: list[DXFPair], idx: int) -> dict[str, Any]:
    """SOLID/TRACE (filled 2D quad/triangle) as a solid HATCH loop.

    DXF stores the corners in bow-tie order (1, 2, 4, 3 around the outline).
    """
    entity = _common_from_pairs(pairs, idx)
    entity["kind"] = "HATCH"
    corners = [
        [_required_float(pairs, 10), _required_float(pairs, 20)],
        [_required_float(pairs, 11), _required_float(pairs, 21)],
        [_first_float(pairs, 13, default=None), _first_float(pairs, 23, default=None)],
        [_first_float(pairs, 12, default=None), _first_float(pairs, 22, default=None)],
    ]
    vertices: list[list[float]] = []
    for corner in corners:
        if corner[0] is None or corner[1] is None:
            continue
        point = [float(corner[0]), float(corner[1])]
        if (
            vertices
            and abs(point[0] - vertices[-1][0]) < 1e-12
            and abs(point[1] - vertices[-1][1]) < 1e-12
        ):
            continue
        vertices.append(point)
    if (
        len(vertices) > 1
        and abs(vertices[0][0] - vertices[-1][0]) < 1e-12
        and abs(vertices[0][1] - vertices[-1][1]) < 1e-12
    ):
        vertices.pop()
    if len(vertices) < 3:
        raise ValueError("SOLID needs at least three distinct corners")
    entity["loops"] = [{"vertices": vertices, "is_outer": True}]
    return entity


def _leader_from_pairs(pairs: list[DXFPair], idx: int) -> dict[str, Any]:
    """LEADER as the open polyline through its vertices (arrowhead omitted)."""
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
    if current is not None:
        vertices.append(current)
    if len(vertices) < 2:
        raise ValueError("LEADER requires at least 2 vertices")
    entity["vertices"] = vertices
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


# Segments used to approximate one full ellipse/spline edge of a HATCH boundary.
_HATCH_CURVE_SEGMENTS = 32
# Codes that terminate the search for the next boundary edge / loop.
_HATCH_LOOP_END_CODES = frozenset({92, 97, 75, 76, 98})


@dataclass
class _HatchLoopNotes:
    approximated_edges: int = 0
    skipped_loops: int = 0


# One boundary edge, normalized to polyline form: ``points`` are the vertices
# from the edge start onwards ([x, y] or [x, y, bulge], bulge applying to the
# segment towards the next vertex) and ``end`` is the edge end point.
_HatchEdge = tuple[list[list[float]], list[float]]


def _parse_hatch_loops(
    pairs: list[DXFPair],
) -> tuple[list[dict[str, Any]], _HatchLoopNotes]:
    """Parse HATCH boundary paths (group 91 onwards) into IR loops.

    Both polyline paths (flag bit 2) and edge paths (line/arc/ellipse/spline
    edges, the form AutoCAD writes for most hatches) are supported. Arcs keep
    exact geometry through bulges; ellipses and splines are approximated.
    Loops that cannot be reconstructed are skipped and counted in the notes.
    """
    notes = _HatchLoopNotes()
    loop_count_index = next(
        (i for i, (code, _) in enumerate(pairs) if code == 91), None
    )
    if loop_count_index is None:
        return [], notes

    num_loops = int(float(pairs[loop_count_index][1]))
    cursor = loop_count_index + 1
    loops: list[dict[str, Any]] = []

    for _ in range(num_loops):
        while cursor < len(pairs) and pairs[cursor][0] != 92:
            cursor += 1
        if cursor >= len(pairs):
            break

        path_flags = int(float(pairs[cursor][1]))
        cursor += 1
        try:
            if path_flags & 2:
                vertices, cursor = _parse_hatch_polyline_path(pairs, cursor)
            else:
                vertices, cursor, approximated = _parse_hatch_edge_path(pairs, cursor)
                notes.approximated_edges += approximated
        except (ValueError, IndexError, KeyError, ZeroDivisionError, OverflowError):
            vertices = []

        vertices = _ensure_hatch_loop_vertices(vertices)
        if len(vertices) >= 3:
            loops.append({"vertices": vertices, "is_outer": not loops})
        else:
            notes.skipped_loops += 1
    return loops, notes


def _ensure_hatch_loop_vertices(vertices: list[list[float]]) -> list[list[float]]:
    """Give arc-only loops enough vertices for the IR (>= 3).

    AutoCAD writes circular hatches as two vertices with bulge 1 (two half
    circles) and semicircular ones as one arc plus the closing line. Both are
    valid closed loops with 2 vertices; split each arc segment in half so the
    loop keeps its exact geometry with >= 3 vertices.
    """
    if len(vertices) != 2:
        return vertices
    if not any(len(vertex) > 2 and vertex[2] != 0.0 for vertex in vertices):
        return vertices  # two straight segments: a degenerate (zero-area) loop
    result: list[list[float]] = []
    for index, vertex in enumerate(vertices):
        result.append(vertex)
        bulge = vertex[2] if len(vertex) > 2 else 0.0
        if bulge == 0.0:
            continue
        nxt = vertices[(index + 1) % 2]
        chord = [nxt[0] - vertex[0], nxt[1] - vertex[1]]
        # Sagitta of a bulge arc is bulge * chord / 2, to the left of the chord
        # for positive (counter-clockwise) bulges.
        mid = [
            (vertex[0] + nxt[0]) / 2.0 - chord[1] * bulge / 2.0,
            (vertex[1] + nxt[1]) / 2.0 + chord[0] * bulge / 2.0,
        ]
        half_bulge = math.tan(math.atan(bulge) / 2.0)
        result[-1] = [vertex[0], vertex[1], half_bulge]
        result.append([mid[0], mid[1], half_bulge])
    return result


def _parse_hatch_polyline_path(
    pairs: list[DXFPair], cursor: int
) -> tuple[list[list[float]], int]:
    """Parse a polyline boundary path (cursor is right after its 92 flag)."""
    has_bulge = 0
    vertex_count: int | None = None
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
        elif code in _HATCH_LOOP_END_CODES:
            break
        else:
            cursor += 1
    if vertex_count is None:
        return [], cursor

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
            if code in _HATCH_LOOP_END_CODES:
                break
            cursor += 1

        if x is None or y is None:
            break
        if has_bulge and bulge is not None and bulge != 0.0:
            vertices.append([x, y, bulge])
        else:
            vertices.append([x, y])
    return vertices, cursor


def _parse_hatch_edge_path(
    pairs: list[DXFPair], cursor: int
) -> tuple[list[list[float]], int, int]:
    """Parse an edge boundary path (cursor is right after its 92 flag).

    Returns the loop vertices, the new cursor and the number of edges that had
    to be approximated (ellipse/spline edges).
    """
    edge_count: int | None = None
    while cursor < len(pairs):
        code, value = pairs[cursor]
        if code == 93:
            edge_count = int(float(value))
            cursor += 1
            break
        if code in _HATCH_LOOP_END_CODES:
            break
        cursor += 1
    if edge_count is None:
        return [], cursor, 0

    edges: list[_HatchEdge] = []
    approximated = 0
    for _ in range(edge_count):
        while cursor < len(pairs) and pairs[cursor][0] != 72:
            if pairs[cursor][0] in _HATCH_LOOP_END_CODES:
                return _assemble_hatch_edges(edges), cursor, approximated
            cursor += 1
        if cursor >= len(pairs):
            break
        edge_type = int(float(pairs[cursor][1]))
        cursor += 1
        edge: _HatchEdge | None
        if edge_type == 1:
            edge, cursor = _parse_hatch_line_edge(pairs, cursor)
        elif edge_type == 2:
            edge, cursor = _parse_hatch_arc_edge(pairs, cursor)
        elif edge_type == 3:
            edge, cursor = _parse_hatch_ellipse_edge(pairs, cursor)
            approximated += 1
        elif edge_type == 4:
            edge, cursor = _parse_hatch_spline_edge(pairs, cursor)
            approximated += 1
        else:
            # Unknown edge type: the remaining edge data cannot be delimited safely.
            return [], cursor, approximated
        if edge is not None:
            edges.append(edge)
    return _assemble_hatch_edges(edges), cursor, approximated


def _take_hatch_edge_fields(
    pairs: list[DXFPair], cursor: int, allowed: frozenset[int]
) -> tuple[dict[int, float], int]:
    """Consume the fixed group codes of a line/arc/ellipse edge (each at most once)."""
    fields: dict[int, float] = {}
    while cursor < len(pairs):
        code, value = pairs[cursor]
        if code not in allowed or code in fields:
            break
        fields[code] = _to_float(value)
        cursor += 1
    return fields, cursor


_HATCH_LINE_EDGE_CODES = frozenset({10, 20, 30, 11, 21, 31})
_HATCH_ARC_EDGE_CODES = frozenset({10, 20, 30, 40, 50, 51, 73})
_HATCH_ELLIPSE_EDGE_CODES = frozenset({10, 20, 30, 11, 21, 31, 40, 50, 51, 73})


def _parse_hatch_line_edge(
    pairs: list[DXFPair], cursor: int
) -> tuple[_HatchEdge | None, int]:
    fields, cursor = _take_hatch_edge_fields(pairs, cursor, _HATCH_LINE_EDGE_CODES)
    if not all(code in fields for code in (10, 20, 11, 21)):
        return None, cursor
    return ([[fields[10], fields[20]]], [fields[11], fields[21]]), cursor


def _hatch_arc_point(
    cx: float, cy: float, radius: float, angle_deg: float
) -> list[float]:
    angle = math.radians(angle_deg)
    return [cx + radius * math.cos(angle), cy + radius * math.sin(angle)]


def _parse_hatch_arc_edge(
    pairs: list[DXFPair], cursor: int
) -> tuple[_HatchEdge | None, int]:
    fields, cursor = _take_hatch_edge_fields(pairs, cursor, _HATCH_ARC_EDGE_CODES)
    if not all(code in fields for code in (10, 20, 40)):
        return None, cursor
    cx, cy, radius = fields[10], fields[20], fields[40]
    if radius <= 0.0:
        return None, cursor
    start = fields.get(50, 0.0)
    end = fields.get(51, 360.0)
    ccw = int(fields.get(73, 1.0)) != 0
    # DXF stores clockwise hatch arc edges with complementary angles (360 - angle),
    # see ezdxf ArcEdge.load_tags; the arc then runs clockwise from -start to -end.
    if ccw:
        first, last, sign = start, end, 1.0
    else:
        first, last, sign = -start, -end, -1.0
    sweep = (end - start) % 360.0
    if sweep < 1e-9:
        # Full circle: four quarter arcs (a loop needs at least 3 vertices).
        quarter_bulge = sign * math.tan(math.radians(90.0) / 4.0)
        points = [
            _hatch_arc_point(cx, cy, radius, first + sign * 90.0 * step)
            + [quarter_bulge]
            for step in range(4)
        ]
        return (points, _hatch_arc_point(cx, cy, radius, first)), cursor
    bulge = sign * math.tan(math.radians(sweep) / 4.0)
    start_point = _hatch_arc_point(cx, cy, radius, first) + [bulge]
    return ([start_point], _hatch_arc_point(cx, cy, radius, last)), cursor


def _parse_hatch_ellipse_edge(
    pairs: list[DXFPair], cursor: int
) -> tuple[_HatchEdge | None, int]:
    fields, cursor = _take_hatch_edge_fields(pairs, cursor, _HATCH_ELLIPSE_EDGE_CODES)
    if not all(code in fields for code in (10, 20, 11, 21, 40)):
        return None, cursor
    major = [fields[11], fields[21]]
    ratio = fields[40]
    if (major[0] == 0.0 and major[1] == 0.0) or ratio <= 0.0:
        return None, cursor
    start = fields.get(50, 0.0)
    end = fields.get(51, 360.0)
    ccw = int(fields.get(73, 1.0)) != 0
    # Same complementary-angle convention as arc edges (parameters in degrees).
    if ccw:
        first, last = start, end
    else:
        first, last = 360.0 - end, 360.0 - start
    sampled = native_core().sample_ellipse(
        [fields[10], fields[20]],
        major,
        ratio,
        math.radians(first),
        math.radians(last),
        _HATCH_CURVE_SEGMENTS,
    )
    if not ccw:
        sampled.reverse()
    closed = (end - start) % 360.0 < 1e-9
    if closed:
        return ([list(point) for point in sampled], list(sampled[0])), cursor
    if len(sampled) < 2:
        return None, cursor
    return ([list(point) for point in sampled[:-1]], list(sampled[-1])), cursor


def _parse_hatch_spline_edge(
    pairs: list[DXFPair], cursor: int
) -> tuple[_HatchEdge | None, int]:
    degree = 3
    knots: list[float] = []
    control_points: list[list[float]] = []
    weights: list[float] = []
    fit_points: list[list[float]] = []

    # Header (94/73/74/95/96) in any order until knots (40) or control points (10).
    while cursor < len(pairs):
        code, value = pairs[cursor]
        if code == 94:
            degree = int(float(value))
        elif code in {73, 74, 95, 96}:
            pass
        else:
            break
        cursor += 1
    while cursor < len(pairs) and pairs[cursor][0] == 40:
        knots.append(_to_float(pairs[cursor][1]))
        cursor += 1
    while cursor < len(pairs) and pairs[cursor][0] in {10, 20, 30, 42}:
        code, value = pairs[cursor]
        if code == 10:
            control_points.append([_to_float(value), 0.0])
        elif code == 20 and control_points:
            control_points[-1][1] = _to_float(value)
        elif code == 42 and control_points:
            while len(weights) < len(control_points) - 1:
                weights.append(1.0)
            weights.append(_to_float(value))
        cursor += 1
    if cursor < len(pairs) and pairs[cursor][0] == 97:
        cursor += 1
        while cursor < len(pairs) and pairs[cursor][0] in {11, 21, 31}:
            code, value = pairs[cursor]
            if code == 11:
                fit_points.append([_to_float(value), 0.0])
            elif code == 21 and fit_points:
                fit_points[-1][1] = _to_float(value)
            cursor += 1
    while cursor < len(pairs) and pairs[cursor][0] in {12, 22, 32, 13, 23, 33}:
        cursor += 1

    sampled: list[list[float]] | None = None
    if len(control_points) >= 2 and degree >= 1:
        usable_weights = (
            weights
            and len(weights) == len(control_points)
            and all(weight > 0 for weight in weights)
        )
        try:
            # (knots and weights that do not fit are replaced by the defaults)
            sampled = native_core().sample_spline(
                control_points,
                degree,
                knots or None,
                weights if usable_weights else None,
                _HATCH_CURVE_SEGMENTS,
            )
        except (ValueError, OverflowError):
            sampled = None
    if sampled is None:
        if len(fit_points) >= 2:
            sampled = fit_points
        elif len(control_points) >= 2:
            sampled = control_points
        else:
            return None, cursor
    return ([list(point) for point in sampled[:-1]], list(sampled[-1])), cursor


def _hatch_points_close(a: list[float], b: list[float], tolerance: float) -> bool:
    return abs(a[0] - b[0]) <= tolerance and abs(a[1] - b[1]) <= tolerance


def _reverse_hatch_edge(edge: _HatchEdge) -> _HatchEdge:
    points, end = edge
    bulges = [point[2] if len(point) > 2 else 0.0 for point in points]
    reversed_points: list[list[float]] = []
    previous = end
    for index in range(len(points) - 1, -1, -1):
        bulge = -bulges[index]
        reversed_points.append(
            [previous[0], previous[1], bulge]
            if bulge != 0.0
            else [previous[0], previous[1]]
        )
        previous = points[index]
    return reversed_points, [previous[0], previous[1]]


def _assemble_hatch_edges(edges: list[_HatchEdge]) -> list[list[float]]:
    """Chain edges into one closed vertex loop, reversing edges when needed."""
    if not edges:
        return []
    extent = 0.0
    for points, end in edges:
        for point in [*points, end]:
            extent = max(extent, abs(point[0]), abs(point[1]))
    tolerance = 1e-6 * max(1.0, extent)

    vertices: list[list[float]] = []
    last_end: list[float] | None = None
    for edge in edges:
        points, end = edge
        if last_end is not None and not _hatch_points_close(
            points[0], last_end, tolerance
        ):
            if _hatch_points_close(end, last_end, tolerance):
                points, end = _reverse_hatch_edge(edge)
        vertices.extend([list(point) for point in points])
        last_end = end
    if last_end is not None and not _hatch_points_close(
        vertices[0], last_end, tolerance
    ):
        vertices.append([last_end[0], last_end[1]])
    return vertices


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


# The caret notation a reader undoes: ``^ `` is a caret, ``^J``, ``^M`` and ``^I``
# a line feed, carriage return and tab. Any other ``^`` stays as it is, since
# writers that do not escape the caret put stacks such as ``\S1^2;`` into text.
_DXF_CARET_RE = re.compile(r"\^([ JMI])")
_DXF_CARET_CODES = {" ": "^", "J": "\n", "M": "\r", "I": "\t"}


def _caret_decode(value: str) -> str:
    return _DXF_CARET_RE.sub(lambda match: _DXF_CARET_CODES[match.group(1)], value)


# ---------------------------------------------------------------------------
# The DXF writer
# ---------------------------------------------------------------------------
#
# The writer is native code (the ``cad2d-ir-native`` package, in ``native/`` of
# the repository): ``ir_to_dxf`` validates the document and hands it over.
#
# What is decided with the codecs and the tables of Python stays here, and the
# native writer asks for it: the code page of the text
# (``_select_r12_codepage``, ``_r12_text_plan``) and the inline codes of MTEXT
# (``_mtext_dxf_text``, ``_explode_mtext``). The fonts of the text styles and
# the view of a viewport it asks of ``cad2d_ir.fonts.style_font`` and
# ``cad2d_ir.layouts.viewport_to_dxf_view``.


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
    consume: bool = False,
) -> str:
    """Serialize an IR document deterministically.

    entity_map and diagnostics are optional output collectors. The string
    return type is retained for compatibility with the original API.

    With ``consume`` the document is used up: its model space entities are taken
    out of ``ir_document["entities"]`` as they are read (the list is empty
    afterwards). A caller that does not need the IR again saves the memory of
    holding all of it beside the DXF being built.

    The writer itself is native code (the ``cad2d-ir-native`` package). It asks
    this module for what is decided with the codecs and the tables of Python:
    the code page of the text, the inline codes of MTEXT and the fonts of the
    text styles.
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

    dxf_text, written_diagnostics, written_map = native_core().write_dxf(
        ir_document,
        target_version=target_version,
        curve_segments=curve_segments,
        generic_dimensions=generic_dimensions,
        entity_map=entity_map is not None,
        consume=consume,
    )
    export_diagnostics = [
        ExportDiagnostic(
            code=code,
            severity=severity,
            message=message,
            entity_id=entity_id,
            action=action,
        )
        for code, severity, message, entity_id, action in written_diagnostics
    ]
    if warnings is not None:
        warnings.extend(
            diagnostic.message
            for diagnostic in export_diagnostics
            if diagnostic.severity in {"warning", "error"}
        )
    if diagnostics is not None:
        diagnostics.extend(export_diagnostics)
    if entity_map is not None:
        entity_map.extend(written_map)
    return dxf_text


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
    encoding: str = "auto",
) -> None:
    if target_version not in SUPPORTED_DXF_TARGET_VERSIONS:
        choices = ", ".join(SUPPORTED_DXF_TARGET_VERSIONS)
        raise ValueError(f"target_version must be one of: {choices}")
    dxf_text = ir_to_dxf(
        ir_document,
        validate=validate,
        warnings=warnings,
        diagnostics=diagnostics,
        entity_map=entity_map,
        target_version=target_version,
        curve_segments=curve_segments,
        generic_dimensions=generic_dimensions,
    )
    selected_encoding = resolve_dxf_output_encoding(
        target_version=target_version,
        encoding=encoding,
        dxf_text=dxf_text,
    )
    Path(path).write_bytes(dxf_text.encode(selected_encoding))


def dxf_text_encoding(dxf_text: str) -> str:
    """The byte encoding a DXF text is to be stored with.

    A DXF before AC1021 (AutoCAD 2007), such as R12, is stored in the code page
    its header names with ``$DWGCODEPAGE`` (``cp932`` when it names none);
    later versions are UTF-8.
    """
    version: str | None = None
    codepage: str | None = None
    lines = dxf_text.split("\n", 200)[:200]
    for index in range(0, len(lines) - 3, 2):
        code, name = lines[index].strip(), lines[index + 1].strip()
        if code == "0" and name == "ENDSEC":
            break
        if code != "9":
            continue
        if name == "$ACADVER":
            version = lines[index + 3].strip()
        elif name == "$DWGCODEPAGE":
            codepage = lines[index + 3].strip()
    if version is None or version >= "AC1021":
        return "utf-8"
    return _CODEPAGE_ENCODINGS.get((codepage or "").upper(), "cp932")


def resolve_dxf_output_encoding(
    *,
    target_version: TargetVersion,
    encoding: str = "auto",
    dxf_text: str | None = None,
) -> str:
    """Resolve and validate the byte encoding for a DXF output.

    DXF R12 text is stored in the code page the writer chose from the text of
    the drawing and named in ``$DWGCODEPAGE``; pass the text as ``dxf_text`` to
    get that one (``IrToDxfResult.encoding`` holds it as well). Without
    ``dxf_text`` the answer for AC1009 is ``cp932``, the code page of a drawing
    with Japanese or plain ASCII text. An explicit ``encoding`` has to be the
    one the output declares.
    """
    if target_version not in SUPPORTED_DXF_TARGET_VERSIONS:
        choices = ", ".join(SUPPORTED_DXF_TARGET_VERSIONS)
        raise ValueError(f"target_version must be one of: {choices}")

    declared = "utf-8"
    if target_version == "AC1009":
        declared = dxf_text_encoding(dxf_text) if dxf_text is not None else "cp932"
    requested = encoding.strip().lower()
    if requested == "auto":
        return declared

    try:
        selected = codecs.lookup(requested).name
    except LookupError as exc:
        raise ValueError(f"Unknown DXF output encoding: {encoding!r}") from exc

    if target_version == "AC1009" and selected != codecs.lookup(declared).name:
        codepage = next(
            name
            for name, codec in _CODEPAGE_ENCODINGS.items()
            if codec == declared and name.startswith("ANSI_")
        )
        raise ValueError(
            f"AC1009 output declares $DWGCODEPAGE={codepage} and must be "
            f"written with encoding={declared!r} (or encoding='auto')."
        )
    return selected


# The code pages DXF R12 text is written in. ANSI_932 comes first: R12 output
# was always written in it, and it is what Jw_cad reads.
_R12_CJK_CODEPAGES = ("ANSI_932", "ANSI_936", "ANSI_949", "ANSI_950")

_R12_SINGLE_BYTE_CODEPAGES = (
    "ANSI_1251",
    "ANSI_1250",
    "ANSI_1253",
    "ANSI_1254",
    "ANSI_1255",
    "ANSI_1256",
    "ANSI_1257",
    "ANSI_874",
)

# No legacy code page has the diameter signs; every CAD draws ``%%c`` as one.
# The other symbols are written as their code where the code page lacks them.
_R12_DIAMETER_SIGNS = "\u2300\u2205"

_R12_CODED_SYMBOLS = {
    "\u00d8": "%%c",
    "\u00f8": "%%c",
    "\u00b0": "%%d",
    "\u00b1": "%%p",
}


def _select_r12_codepage(characters: dict[str, int]) -> str:
    """The code page for the text of a DXF R12 file.

    ``characters`` counts the non-ASCII characters of the file. Kana anywhere
    make it ANSI_932 and Hangul ANSI_949, so that a Japanese or Korean drawing
    keeps its code page even when a few characters have to become ``?``.
    Otherwise the first code page that stores every character wins, ANSI_932
    before the others, and when none does, the one that loses the fewest.
    Cyrillic, Thai, Hebrew and Arabic text without CJK characters takes its own
    code page before the CJK ones (ANSI_932 stores Cyrillic, as full-width
    letters).
    """

    def has(low: int, high: int) -> bool:
        return any(low <= ord(character) <= high for character in characters)

    kana = has(0x3040, 0x30FF) or has(0xFF66, 0xFF9F)
    hangul = has(0xAC00, 0xD7AF) or has(0x3130, 0x318F)
    han = has(0x4E00, 0x9FFF) or has(0x3400, 0x4DBF)
    if kana:
        return "ANSI_932"
    if hangul:
        return "ANSI_949"
    other_script = has(0x0400, 0x04FF) or has(0x0E00, 0x0E7F) or has(0x0590, 0x06FF)
    if other_script and not han:
        order = ("ANSI_1252", *_R12_SINGLE_BYTE_CODEPAGES, *_R12_CJK_CODEPAGES)
    else:
        order = (
            "ANSI_932",
            "ANSI_1252",
            *_R12_CJK_CODEPAGES[1:],
            *_R12_SINGLE_BYTE_CODEPAGES,
        )
    best: tuple[int, str] | None = None
    for codepage in order:
        codec = _CODEPAGE_ENCODINGS[codepage]
        lost = sum(
            count
            for character, count in characters.items()
            if not _encodable(character, codec)
        )
        if lost == 0:
            return codepage
        if best is None or lost < best[0]:
            best = (lost, codepage)
    return best[1] if best is not None else "ANSI_932"


def _r12_text_plan(
    text_characters: dict[str, int], other_characters: dict[str, int]
) -> tuple[str, dict[str, str], dict[str, str], int, int]:
    """How the text of a DXF R12 file is stored in one Windows code page.

    R12 has no Unicode. ``text_characters`` counts the non-ASCII characters of
    the text of TEXT, ATTRIB, ATTDEF and DIMENSION records, ``other_characters``
    those of every other value. The code page is chosen from them (the signs a
    control code stands for do not ask for one). Returned are the code page;
    what the signs of a text become (the diameter sign ``%%c``, and the degree
    and plus-minus signs ``%%d`` and ``%%p`` where the code page lacks them);
    what a character the code page still cannot store becomes (its
    compatibility form where the code page has that, ``2`` for ``²``, otherwise
    ``?``); and how many characters that are and how many of them got a similar
    character.
    """
    symbols = _R12_DIAMETER_SIGNS + "".join(_R12_CODED_SYMBOLS)
    characters = dict(other_characters)
    for character, count in text_characters.items():
        if character not in symbols:
            characters[character] = characters.get(character, 0) + count
    codepage = _select_r12_codepage(characters)
    codec = _CODEPAGE_ENCODINGS[codepage]
    coded = {sign: "%%c" for sign in _R12_DIAMETER_SIGNS}
    coded.update(
        (symbol, code)
        for symbol, code in _R12_CODED_SYMBOLS.items()
        if not _encodable(symbol, codec)
    )
    lost = {
        character: _r12_similar(character, codec)
        for character in characters
        if not _encodable(character, codec)
    }
    replaced = sum(
        count for character, count in characters.items() if character in lost
    )
    similar = sum(
        count
        for character, count in characters.items()
        if lost.get(character, "?") != "?"
    )
    return codepage, coded, lost, replaced, similar


def _encodable(text: str, codec: str) -> bool:
    try:
        text.encode(codec)
    except UnicodeEncodeError:
        return False
    return True


def _r12_similar(character: str, codec: str) -> str:
    """What DXF R12 stores for a character its code page lacks.

    The compatibility form of the character when the code page has it (``2``
    for ``²``, ``μ`` for the micro sign, ``mm`` for ``㎜``), otherwise ``?``.
    """
    similar = unicodedata.normalize("NFKC", character)
    if similar != character and similar.strip() and _encodable(similar, codec):
        return similar
    return "?"


def _mtext_width_factor(width_factor: Any) -> float | None:
    """The width factor an ``MTEXT`` can have, or ``None``.

    AutoCAD accepts 0.1 to 10; other values are no factor.
    """
    if (
        isinstance(width_factor, bool)
        or not isinstance(width_factor, (int, float))
        or not math.isfinite(width_factor)
        or not WIDTH_FACTOR_RANGE[0] <= width_factor <= WIDTH_FACTOR_RANGE[1]
    ):
        return None
    return float(width_factor)


def _mtext_inline_width(width_factor: Any) -> str:
    """The inline code for the width factor an ``MTEXT`` carries itself.

    A DXF ``MTEXT`` has no width factor of its own: it is drawn with the one of
    its text style unless the text sets one with ``\\W…;``. Factors outside the
    0.1 to 10 that AutoCAD accepts, and 1.0, give no code.
    """
    factor = _mtext_width_factor(width_factor)
    if factor is None or abs(factor - 1.0) < 1e-6:
        return ""
    return f"\\W{f'{factor:.6f}'.rstrip('0').rstrip('.')};"


def _mtext_dxf_text(entity: dict[str, Any]) -> str:
    """The text of an ``MTEXT`` entity as DXF holds it.

    The text with its inline codes (a DWG MTEXT keeps them beside the text),
    the codes only CAXA writes as DXF codes, a line break as the paragraph code
    and, in front, the width factor the entity carries itself.
    """
    text = _DXF_LINE_BREAK.sub(r"\\P", mtext_dxf_codes(mtext_source_text(entity)))
    return _mtext_inline_width(entity.get("width_factor")) + text


def _entity_common_values(entity: dict[str, Any]) -> dict[str, Any]:
    keys = (
        "id",
        "layer",
        "linetype",
        "linetype_scale",
        "color",
        "lineweight_mm",
        "visible",
    )
    return {key: entity[key] for key in keys if key in entity}


def _mtext_base_width(
    entity: dict[str, Any], text_styles: dict[str, Any] | None
) -> float:
    """The width factor an ``MTEXT`` starts with: its own, else its text style's."""
    factor = _mtext_width_factor(entity.get("width_factor"))
    if factor is None and "width_factor" not in entity and text_styles:
        style = text_styles.get(str(entity.get("style", "")))
        if isinstance(style, dict):
            factor = _mtext_width_factor(style.get("width_factor"))
    return 1.0 if factor is None else factor


def _explode_mtext(
    entity: dict[str, Any], text_styles: dict[str, Any] | None = None
) -> tuple[list[dict[str, Any]], bool]:
    """The ``TEXT`` entities an ``MTEXT`` becomes in DXF R12, one per line.

    The lines hang below the attachment point as the lines of the ``MTEXT`` do.
    Each one gets the width factor and the height that the inline codes, the
    entity and its text style give it; the other codes are removed.
    """
    source = mtext_source_text(entity)
    height = float(entity["height"])
    lines = mtext_lines(
        source, width_factor=_mtext_base_width(entity, text_styles), height=height
    )
    vertical, _, horizontal = str(entity.get("attach") or "top_left").partition("_")
    insert = entity["insert"]
    rotation = float(entity.get("rotation", 0.0))
    angle = math.radians(rotation)
    result: list[dict[str, Any]] = []
    # An empty line keeps its room, but is no TEXT.
    for line, drop in mtext_layout(lines, vertical):
        text_entity: dict[str, Any] = {
            **_entity_common_values(entity),
            "kind": "TEXT",
            "insert": [
                float(insert[0]) + math.sin(angle) * drop * height,
                float(insert[1]) - math.cos(angle) * drop * height,
            ],
            "height": height * line.height_factor,
            "text": line.text,
        }
        if horizontal in ("center", "right"):
            text_entity["halign"] = horizontal
        for key in ("rotation", "style"):
            if key in entity:
                text_entity[key] = entity[key]
        if line.width_factor > 0.0 and abs(line.width_factor - 1.0) > 1e-9:
            text_entity["width_factor"] = line.width_factor
        result.append(text_entity)
    if not result:
        result.append(
            {
                **_entity_common_values(entity),
                "kind": "TEXT",
                "insert": [float(insert[0]), float(insert[1])],
                "height": height,
                "text": "",
            }
        )
    formatting_changed = mtext_plain(source) != source or len(lines) > 1
    return result, formatting_changed
