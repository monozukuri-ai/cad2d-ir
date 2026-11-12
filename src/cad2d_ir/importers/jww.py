"""Native JWW (drawing) and JWS (registered symbol) to CAD 2D IR importer.

A JWS file is a symbol saved from a Jw_cad drawing with 図形登録. Its entities
are the same records as a JWW drawing's, in the paper millimetres of the
drawing it was registered from, so the entity conversion is shared. Only the
header differs: a symbol has an insertion origin, the scale denominator of each
layer group and recorded bounds, but no layer names, pen palette, line type
patterns, paper size or memo.

The entities are converted by native code (the ``cad2d-ir-native`` package):
of a file it reads itself, or of the ``dict`` of ``ezjww.read_document``. This
module makes what comes from the header - layers, line types, the pen table,
the metadata of the document - and the diagnostics about the document.
"""

from __future__ import annotations

import hashlib
import math
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from cad2d_ir._native import native_core
from cad2d_ir.importers.base import (
    ImportDiagnostic,
    ImporterError,
    ImportOptions,
    ImportResult,
    MissingOptionalDependencyError,
)
from cad2d_ir.importers.linetypes import (
    SXF_LINETYPE_NAMES,
    SXF_LINETYPE_PATTERNS_MM,
)
from cad2d_ir.schema import validate_ir

# One pattern bit prints as (printer pitch / 32) mm. Jw_cad stores, for its
# SXF-compatible line types, both the bit pattern with its printer pitch and the
# SXF segment lengths in millimetres; the two agree at exactly this ratio in every
# header inspected. The Jw_cad defaults below (printer pitch 10 for the standard
# line types) apply when the file's own settings are not available: ``ezjww``
# older than 0.3.3 does not report them, and files older than JWW 3.00 do not
# store them where it reads.
_PATTERN_UNIT_MM = 1.0 / 32.0


def _bit_pattern(runs: tuple[int, ...], printer_pitch: int) -> list[float]:
    """Alternating dash/gap run lengths in pattern bits -> signed ``pattern_mm``."""
    unit = printer_pitch * _PATTERN_UNIT_MM
    return [
        runs[index] * unit * (1.0 if index % 2 == 0 else -1.0)
        for index in range(len(runs))
    ]


# Jw_cad line type number (entity ``pen_style``) -> (name, description, runs, pitch).
# The runs are the default line type patterns saved in the JWW header
# (``m_alLType``): 2-4 are dashed lines, 5-6 chain lines, 7-8 double-dot chain
# lines, 9 the construction line type that is shown on screen but never printed,
# and 16-19 the double-length variants.
_STANDARD_LINE_TYPES: dict[int, tuple[str, str, tuple[int, ...], int]] = {
    2: ("JWW_DASHED1", "Jw_cad dashed line 1", (2, 2), 10),
    3: ("JWW_DASHED2", "Jw_cad dashed line 2", (4, 4), 10),
    4: ("JWW_DASHED3", "Jw_cad dashed line 3", (6, 2), 10),
    5: ("JWW_DASHDOT1", "Jw_cad chain line 1", (10, 2, 2, 2), 10),
    6: ("JWW_DASHDOT2", "Jw_cad chain line 2", (26, 2, 2, 2), 10),
    7: ("JWW_DIVIDE1", "Jw_cad double-dot chain line 1", (8, 2, 1, 2, 1, 2), 10),
    8: ("JWW_DIVIDE2", "Jw_cad double-dot chain line 2", (24, 2, 1, 2, 1, 2), 10),
    9: ("JWW_CONSTRUCTION", "Jw_cad construction line (not printed)", (1, 3), 10),
    16: ("JWW_DASHDOT_X2", "Jw_cad double-length chain line", (26, 2, 2, 2), 20),
    17: (
        "JWW_DIVIDE_X2",
        "Jw_cad double-length double-dot chain line",
        (24, 2, 1, 2, 1, 2),
        20,
    ),
    18: ("JWW_DASHED_X2", "Jw_cad double-length dashed line", (30, 2), 20),
    19: ("JWW_DASHED_X4", "Jw_cad quadruple-length dashed line", (30, 2), 40),
}
_NON_PLOTTING_PEN_STYLES = {9}
# Jw_cad numbers its SXF-compatible line types as 30 + SXF line type code.
_SXF_PEN_STYLE_OFFSET = 30

_LINE_TYPES: dict[int, str] = {0: "CONTINUOUS", 1: "CONTINUOUS"}
_LINETYPE_TABLE: dict[str, dict[str, Any]] = {
    "CONTINUOUS": {"description": "Continuous line", "pattern_mm": []},
    "BYLAYER": {"description": "Use the layer linetype", "pattern_mm": []},
}
for _pen_style, (_name, _description, _runs, _pitch) in _STANDARD_LINE_TYPES.items():
    _LINE_TYPES[_pen_style] = _name
    _LINETYPE_TABLE[_name] = {
        "description": _description,
        "pattern_mm": _bit_pattern(_runs, _pitch),
    }
    if _pen_style in _NON_PLOTTING_PEN_STYLES:
        _LINETYPE_TABLE[_name]["plot"] = False
for _code, _sxf_name in SXF_LINETYPE_NAMES.items():
    if _code == 1:
        _LINE_TYPES[_SXF_PEN_STYLE_OFFSET + _code] = "CONTINUOUS"
        continue
    _name = "SXF_" + _sxf_name.upper().replace(" ", "_").replace("-", "_")
    _LINE_TYPES[_SXF_PEN_STYLE_OFFSET + _code] = _name
    _LINETYPE_TABLE[_name] = {
        "description": f"SXF line type: {_sxf_name}",
        "pattern_mm": list(SXF_LINETYPE_PATTERNS_MM[_sxf_name]),
    }


# SXF line type codes 17-32 (pen styles 47-62) are user-defined.
_SXF_USER_DEFINED_CODES = range(17, 33)


def _signed_pattern(segments: Sequence[Any]) -> list[float]:
    """Alternating dash/gap lengths -> signed ``pattern_mm``."""
    return [
        float(length) if index % 2 == 0 else -float(length)
        for index, length in enumerate(segments)
    ]


def _usable_segments(value: Any) -> list[float] | None:
    """Dash/gap lengths reported by ``ezjww``, or ``None`` when they are unusable."""
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        return None
    segments: list[float] = []
    for item in value:
        if isinstance(item, bool) or not isinstance(item, (int, float)):
            return None
        length = float(item)
        if not math.isfinite(length) or length < 0.0:
            return None
        segments.append(length)
    return segments


def _document_line_types(
    header: Mapping[str, Any],
) -> tuple[dict[int, str], dict[str, dict[str, Any]], bool]:
    """Linetype names by pen style and the linetype table of one document.

    Starts from the Jw_cad defaults and applies the settings the file records
    (``header["line_types"]``, ``ezjww`` >= 0.3.3):

    - standard (2-9) and double-length (16-19) line types take the pattern and
      printer pitch of the file, including a pattern the user turned solid;
    - SXF-compatible line types take the segment lengths stored with them, and
      the SXF reference pattern when the file names them without lengths;
    - user-defined SXF line types (47-62) become ``SXF_USER_<code>``.

    The third value tells whether the file's settings were available.
    """
    names = dict(_LINE_TYPES)
    table = {
        name: {**definition, "pattern_mm": list(definition["pattern_mm"])}
        for name, definition in _LINETYPE_TABLE.items()
    }
    settings = header.get("line_types")
    if not isinstance(settings, Mapping):
        return names, table, False

    for key in ("standard", "double_length"):
        for item in settings.get(key) or ():
            if not isinstance(item, Mapping):
                continue
            name = _LINE_TYPES.get(_optional_int(item.get("number")))
            segments = _usable_segments(item.get("segments_mm"))
            if name is None or name == "CONTINUOUS" or segments is None:
                continue
            # One run or none is a line without gaps.
            table[name]["pattern_mm"] = (
                _signed_pattern(segments) if len(segments) >= 2 else []
            )

    for item in settings.get("sxf") or ():
        if not isinstance(item, Mapping):
            continue
        number = _optional_int(item.get("number"))
        segments = _usable_segments(item.get("segments_mm"))
        if number is None or segments is None:
            continue
        code = number - _SXF_PEN_STYLE_OFFSET
        if code in _SXF_USER_DEFINED_CODES:
            if not segments:
                continue  # an empty slot: entities that refer to it stay BYLAYER
            name = f"SXF_USER_{code}"
            label = str(item.get("name") or "").strip()
            names[number] = name
            table[name] = {
                "description": "SXF user-defined line type"
                + (f": {label}" if label else ""),
                "pattern_mm": _signed_pattern(segments) if len(segments) >= 2 else [],
            }
            continue
        name = _LINE_TYPES.get(number)
        if name is not None and name != "CONTINUOUS" and len(segments) >= 2:
            table[name]["pattern_mm"] = _signed_pattern(segments)
    return names, table, True


def _optional_int(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _colorref_hex(color: int) -> str:
    """A Win32 COLORREF (``0x00BBGGRR``, as ``ezjww`` normalizes it) as ``#RRGGBB``."""
    return f"#{color & 0xFF:02X}{(color >> 8) & 0xFF:02X}{(color >> 16) & 0xFF:02X}"


def _pen_table(header: Mapping[str, Any]) -> dict[str, Any] | None:
    """The printer pens of the file (``header["pens"]``, ``ezjww`` >= 0.7.1) for
    ``header.metadata.jww.pens``.

    Jw_cad prints an entity whose own pen width is 0 (the usual case) with the
    width set for its pen color, so this table carries the line widths of most
    drawings. ``width_mm`` is the printed width in millimetres: the stored width
    directly when the file keeps widths in 1/100 mm, dots over the recorded dpi
    otherwise; ``None`` when the width is 0 or the dpi is not recorded.

    Every stored pen is kept (0 is the background and 9 the construction line
    color, 100 a spare slot), with the raw ``width`` and the screen widths, so a
    JWW writer can restore the table unchanged.
    """
    pens = header.get("pens")
    if not isinstance(pens, Mapping):
        return None
    unit = pens.get("width_unit")
    if unit not in ("hundredth_mm", "dot"):
        return None
    dpi = _optional_int(pens.get("printer_dpi"))

    def width_mm(width: int | None) -> float | None:
        if width is None or width <= 0:
            return None
        if unit == "hundredth_mm":
            return round(width / 100.0, 6)
        if dpi:
            return round(width * 25.4 / dpi, 6)
        return None

    def entry(pen: Any) -> dict[str, Any] | None:
        if not isinstance(pen, Mapping):
            return None
        width = _optional_int(pen.get("width"))
        item: dict[str, Any] = {"width": width, "width_mm": width_mm(width)}
        color = _optional_int(pen.get("color"))
        if color is not None:
            item["color"] = _colorref_hex(color)
        radius = pen.get("point_radius")
        if (
            isinstance(radius, (int, float))
            and not isinstance(radius, bool)
            and math.isfinite(radius)
        ):
            item["point_radius_mm"] = float(radius)
        return item

    printer: dict[str, dict[str, Any]] = {}
    for number, pen in enumerate(pens.get("printer") or ()):
        if number <= 9 and (item := entry(pen)) is not None:
            printer[str(number)] = item
    extended: dict[str, dict[str, Any]] = {}
    for index, pen in enumerate(pens.get("extended_printer") or ()):
        # 100 is a spare slot, 101-116 the SXF colors, 117-356 user colors
        if index < 257 and (item := entry(pen)) is not None:
            extended[str(100 + index)] = item
    table: dict[str, Any] = {
        "unit": unit,
        "max_draw_width": _optional_int(pens.get("max_draw_width")),
        "printer_dpi": dpi,
        "printer": printer,
    }
    screen_widths = pens.get("screen_widths")
    if isinstance(screen_widths, Sequence) and not isinstance(
        screen_widths, (str, bytes)
    ):
        table["screen_widths"] = [_optional_int(width) for width in screen_widths]
    if extended:
        table["extended"] = extended
    return table


def _pen_widths_mm(table: Mapping[str, Any] | None) -> dict[int, float]:
    """Printed width in millimetres per pen color number of a ``_pen_table``
    (1..9 and the extended 100..356); pens without a width in mm are left out."""
    widths: dict[int, float] = {}
    if not table:
        return widths
    for section in ("printer", "extended"):
        pens = table.get(section)
        if not isinstance(pens, Mapping):
            continue
        for key, pen in pens.items():
            mm = pen.get("width_mm") if isinstance(pen, Mapping) else None
            if isinstance(mm, (int, float)) and not isinstance(mm, bool) and mm > 0:
                widths[int(key)] = float(mm)
    return widths


# Colors. Jw_cad writes a pen color into a DXF as the color number (ACI) closest to
# the pen's printer color (基本設定 > 色・画面 > プリンタ出力要素), except pen color 2,
# which is always 7 (Jw_cad Third-Party-Network FAQ 008020 lists the numbers for the
# default colors). Entities get the same numbers here, so a drawing carries the
# colors Jw_cad's own DXF export gives it, and the color-dependent plot styles of the
# CAD that receives it apply as they would to that DXF.
#
# The ACI palette below is the one that reproduces those numbers: the 24 hues of
# 10-249 at the levels 255, 204, 153, 127 and 76, the odd numbers half-way to white,
# and the grey ramp 250-255.
_ACI_LEVELS = (255, 204, 153, 127, 76)
_ACI_GREYS = (51, 80, 105, 130, 190, 255)
# The numbers Jw_cad writes for its default printer colors, used for a pen the file
# records no color for (a symbol has no pen table, nor has a header ezjww could not
# read the pens of): pen colors 1-9, and the SXF colors 1-16 (pen colors 101-116).
_DEFAULT_PEN_ACI = {1: 4, 2: 7, 3: 3, 4: 2, 5: 6, 6: 5, 7: 136, 8: 1, 9: 9}
# SXF black is 7 rather than Jw_cad's 250: see _nearest_aci.
_SXF_DEFAULT_PEN_ACI = {
    101: 7,
    102: 1,
    103: 3,
    104: 5,
    105: 2,
    106: 6,
    107: 4,
    108: 7,
    109: 222,
    110: 23,
    111: 30,
    112: 83,
    113: 150,
    114: 171,
    115: 9,
    116: 8,
}


def _aci_palette() -> dict[int, tuple[int, int, int]]:
    """RGB of the ACI numbers 1-255 (see the note above ``_ACI_LEVELS``)."""
    table: dict[int, tuple[int, int, int]] = {
        1: (255, 0, 0),
        2: (255, 255, 0),
        3: (0, 255, 0),
        4: (0, 255, 255),
        5: (0, 0, 255),
        6: (255, 0, 255),
        7: (255, 255, 255),
        8: (128, 128, 128),
        9: (192, 192, 192),
    }
    for number in range(10, 250):
        sector, fraction = divmod((number - 10) // 10 * 15.0 / 60.0, 1.0)
        rise, fall = 255.0 * fraction, 255.0 * (1.0 - fraction)
        full = (
            (255.0, rise, 0.0),
            (fall, 255.0, 0.0),
            (0.0, 255.0, rise),
            (0.0, fall, 255.0),
            (rise, 0.0, 255.0),
            (255.0, 0.0, fall),
        )[int(sector)]
        if number % 2:
            full = tuple((channel + 255.0) / 2.0 for channel in full)
        level = _ACI_LEVELS[(number % 10) // 2]
        red, green, blue = (int(channel * level / 255.0) for channel in full)
        table[number] = (red, green, blue)
    for number, grey in zip(range(250, 256), _ACI_GREYS):
        table[number] = (grey, grey, grey)
    return table


_ACI_RGB = _aci_palette()
# Searched in this order, so that the named colors win over the numbers of 10-249
# that repeat them (10 is the red of 1). 7 and 255 (white) are left to the neutral
# rule of _nearest_aci.
_ACI_CANDIDATES = (*range(1, 7), 8, 9, *range(10, 255))


def _hex_rgb(value: Any) -> tuple[int, int, int] | None:
    if not isinstance(value, str) or len(value) != 7 or not value.startswith("#"):
        return None
    try:
        return (int(value[1:3], 16), int(value[3:5], 16), int(value[5:7], 16))
    except ValueError:
        return None


def _nearest_aci(rgb: tuple[int, int, int]) -> int:
    """The ACI closest to an RGB color, with black and white as 7.

    7 is the drawing color: black on a white background and white on a black one.
    Jw_cad writes black as 250, a dark grey that all but disappears on the black
    model space of AutoCAD, so black stays 7 here, as for the other formats.
    """
    high, low = max(rgb), min(rgb)
    if high - low < 24 and (high < 40 or low > 215):
        return 7
    return min(
        _ACI_CANDIDATES,
        key=lambda number: sum(
            (channel - target) ** 2 for channel, target in zip(rgb, _ACI_RGB[number])
        ),
    )


def _pen_colors(table: Mapping[str, Any] | None) -> dict[int, int]:
    """The ACI of every pen color number an entity can use (1-9, 101-356).

    From the printer colors of a ``_pen_table``, else Jw_cad's numbers for the
    default colors. Pen color 2 is 7 whatever its printer color, as Jw_cad writes
    it; a number the table leaves out (0 the background, 10 an arbitrary solid
    color, 100 the spare slot) is left to the caller.
    """
    colors = {**_DEFAULT_PEN_ACI, **_SXF_DEFAULT_PEN_ACI}
    sections = (table.get("printer"), table.get("extended")) if table else ()
    for pens in sections:
        if not isinstance(pens, Mapping):
            continue
        for key, pen in pens.items():
            number = int(key)
            if not (1 <= number <= 9 or 101 <= number <= 356):
                continue
            rgb = _hex_rgb(pen.get("color")) if isinstance(pen, Mapping) else None
            if rgb is not None:
                colors[number] = _nearest_aci(rgb)
    colors[2] = 7
    return colors


def _used_linetype_table(
    table: Mapping[str, dict[str, Any]], used: set[str]
) -> dict[str, dict[str, Any]]:
    """The Jw_cad line types, plus the SXF-compatible ones the drawing uses."""
    return {
        name: definition
        for name, definition in table.items()
        if not name.startswith("SXF_") or name in used
    }


@dataclass(slots=True)
class _ConversionContext:
    """The lookup tables a conversion works with (made from the header), and
    what it reports about the entities."""

    options: ImportOptions
    layers: dict[str, dict[str, Any]]
    layer_names: dict[tuple[int, int], str]
    block_names: dict[int, str]
    linetype_names: dict[int, str] = field(default_factory=lambda: dict(_LINE_TYPES))
    used_linetypes: set[str] = field(default_factory=set)
    diagnostics: list[ImportDiagnostic] = field(default_factory=list)
    converted_counts: Counter[str] = field(default_factory=Counter)
    skipped_counts: Counter[str] = field(default_factory=Counter)
    approximation_counts: Counter[str] = field(default_factory=Counter)
    preserved_dimensions: int = 0
    # "jww" (a drawing) or "jws" (a registered symbol); the entity records are the same
    source_format: str = "jww"
    # JWS: the scale denominator of each layer group, kept on the layers created on
    # demand (a symbol has no layer table)
    group_scales: list[float] | None = None
    # Printed width in mm per pen color number, from the file's pen table. Jw_cad
    # prints an entity whose own pen width is 0 (the usual case) at its pen color's
    # width, so such entities get that width as lineweight_mm.
    pen_widths: dict[int, float] = field(default_factory=dict)
    pen_width_entities: int = 0
    # ACI per pen color number (_pen_colors): the printer color's nearest ACI, as
    # Jw_cad writes the color into a DXF.
    pen_colors: dict[int, int] = field(default_factory=lambda: _pen_colors(None))

    @property
    def label(self) -> str:
        return self.source_format.upper()


@dataclass(slots=True)
class NativeImportResult:
    """``ImportResult`` with the document held in native code.

    ``document`` is a ``NativeDocument`` of the ``cad2d-ir-native`` package:
    ``document.to_dict()`` is the IR ``dict``, and its entities do not become
    Python objects unless that is asked for.
    """

    document: Any
    diagnostics: list[ImportDiagnostic]
    statistics: dict[str, Any]


def _ezjww(kind: str) -> Any:
    try:
        import ezjww
    except ImportError as exc:
        raise MissingOptionalDependencyError(
            f"{kind} support requires the optional dependency ezjww; "
            'install it with `pip install "cad2d-ir[jww]"`.'
        ) from exc
    return ezjww


def _as_import_result(
    result: NativeImportResult, options: ImportOptions | None
) -> ImportResult:
    document = result.document.to_dict()
    if (options or ImportOptions()).validate:
        validate_ir(document)
    return ImportResult(
        document=document,
        diagnostics=result.diagnostics,
        statistics=result.statistics,
    )


def convert_jww_file_to_ir(
    path: str | Path,
    *,
    options: ImportOptions | None = None,
) -> ImportResult:
    """Read a JWW file with ``ezjww`` and convert it directly to IR.

    Install the ``jww`` extra to enable this importer::

        pip install "cad2d-ir[jww]"
    """
    return _as_import_result(import_jww_file(path, options=options), options)


def import_jww_file(
    path: str | Path,
    *,
    options: ImportOptions | None = None,
    core: Any = None,
) -> NativeImportResult:
    """Read a JWW file and convert it to the typed IR of the native code.

    ``convert_jww_file_to_ir`` is this function followed by
    ``result.document.to_dict()``. A caller that hands the document on to
    native code (the DXF writer, a renderer) keeps it as it is: the entities of
    a large drawing take a fraction of the memory of their ``dict``s. The
    document is not validated here (``ImportOptions.validate`` applies where
    the ``dict`` is made).

    ``core`` is the module whose bindings do the work: ``cad2d_ir_native``, or
    the extension module of an application that registers the bindings of
    ``cad2d-ir-py`` next to native code of its own. The document of the result
    is of that module.
    """
    ezjww = _ezjww("JWW")
    bindings = native_core() if core is None else core
    import_options = options or ImportOptions()
    source_path = Path(path)
    # The header is read on its own: the layers, line types and pens the
    # entities are converted with come from it.
    header = _mapping(ezjww.read_header(str(source_path)), "header")
    parsed = bindings.parse_jww(str(source_path))
    parser_diagnostics = list(parsed.diagnostics())
    if any(row.get("code") == "TEXT_ENCODING_DETECTED" for row in parser_diagnostics):
        # The strings of this file are UTF-8 (those of a Jw_cad file are CP932,
        # which is what the header above was read as). The document read as a
        # whole has every string in the encoding the reader found.
        return _import_document(
            ezjww.read_document(str(source_path)),
            source_format="jww",
            source_name=source_path.name,
            source_sha256=_sha256_file(source_path),
            options=import_options,
            consume_source=True,
            bindings=bindings,
        )
    return _import(
        source_format="jww",
        header=header,
        block_defs=parsed.block_defs(),
        source_counts=parsed.entity_counts(),
        parser_diagnostics={"diagnostics": parser_diagnostics},
        validation={
            "validation": {"unresolved_def_numbers": parsed.unresolved_def_numbers()}
        },
        convert=parsed.convert,
        source_name=source_path.name,
        source_sha256=_sha256_file(source_path),
        options=import_options,
    )


def convert_jws_file_to_ir(
    path: str | Path,
    *,
    options: ImportOptions | None = None,
) -> ImportResult:
    """Read a JWS registered symbol with ``ezjww`` (0.7 or later) and convert it to IR.

    Install the ``jww`` extra to enable this importer::

        pip install "cad2d-ir[jww]"
    """
    ezjww = _ezjww("JWS")
    read_jws_document = getattr(ezjww, "read_jws_document", None)
    if read_jws_document is None:
        raise MissingOptionalDependencyError(
            "JWS support requires ezjww 0.7 or later; "
            'upgrade it with `pip install --upgrade "cad2d-ir[jww]"`.'
        )

    source_path = Path(path)
    jws_document = read_jws_document(str(source_path))
    return jws_document_to_ir(
        jws_document,
        source_name=source_path.name,
        source_sha256=_sha256_file(source_path),
        options=options,
        consume_source=True,
    )


def jww_document_to_ir(
    jww_document: Mapping[str, Any],
    *,
    source_name: str | None = None,
    source_sha256: str | None = None,
    options: ImportOptions | None = None,
    consume_source: bool = False,
) -> ImportResult:
    """Convert an ``ezjww.read_document`` result to CAD 2D IR.

    ``consume_source=True`` releases each raw entity as soon as it has been
    read (the caller must not reuse ``jww_document["entities"]`` afterwards).
    pyo3 builds about 1.4 KB of dictionaries per JWW entity, so holding the raw
    document and the IR at the same time doubles the peak for large drawings.
    (``convert_jww_file_to_ir`` reads a file without making those dictionaries.)
    """
    return _as_import_result(
        _import_document(
            jww_document,
            source_format="jww",
            source_name=source_name,
            source_sha256=source_sha256,
            options=options or ImportOptions(),
            consume_source=consume_source,
            bindings=native_core(),
        ),
        options,
    )


def jws_document_to_ir(
    jws_document: Mapping[str, Any],
    *,
    source_name: str | None = None,
    source_sha256: str | None = None,
    options: ImportOptions | None = None,
    consume_source: bool = False,
) -> ImportResult:
    """Convert an ``ezjww.read_jws_document`` result to CAD 2D IR.

    The entities are converted exactly like a JWW drawing's: the coordinates
    stay in the paper millimetres of the drawing the symbol was registered
    from. The insertion origin, the scale denominator of each layer group and
    the recorded bounds go to ``header.metadata.jws`` and are not applied
    (``JWS_SYMBOL_COORDINATES``); placing the symbol into a drawing is a
    separate step. Layers are created for the group/layer pairs in use, named
    ``<group>-<layer>`` in hexadecimal like the JWW fallback, each carrying its
    group scale in ``metadata.jww.group_scale``. The pen palette, line type
    patterns, paper size and memo a drawing would have fall back to the Jw_cad
    defaults (``JWS_DRAWING_SETTINGS_DEFAULTED``).
    """
    return _as_import_result(
        _import_document(
            jws_document,
            source_format="jws",
            source_name=source_name,
            source_sha256=source_sha256,
            options=options or ImportOptions(),
            consume_source=consume_source,
            bindings=native_core(),
        ),
        options,
    )


def _import_document(
    jw_document: Mapping[str, Any],
    *,
    source_format: str,
    source_name: str | None,
    source_sha256: str | None,
    options: ImportOptions,
    consume_source: bool,
    bindings: Any,
) -> NativeImportResult:
    """A document held as the ``dict`` of ``ezjww``: its entities are read from
    the ``dict`` by the native code."""

    def convert(**tables: Any) -> Any:
        return bindings.convert_jw_document(
            jw_document, consume=consume_source, **tables
        )

    return _import(
        source_format=source_format,
        header=_mapping(jw_document.get("header"), "header"),
        block_defs=_mapping_sequence(jw_document.get("block_defs", []), "block_defs"),
        source_counts=_mapping(jw_document.get("entity_counts", {}), "entity_counts"),
        parser_diagnostics=jw_document,
        validation=jw_document,
        convert=convert,
        source_name=source_name,
        source_sha256=source_sha256,
        options=options,
    )


def _import(
    *,
    source_format: str,
    header: Mapping[str, Any],
    block_defs: Sequence[Mapping[str, Any]],
    source_counts: Mapping[str, Any],
    parser_diagnostics: Mapping[str, Any],
    validation: Mapping[str, Any],
    convert: Any,
    source_name: str | None,
    source_sha256: str | None,
    options: ImportOptions,
) -> NativeImportResult:
    """The document of a drawing or a symbol.

    What comes from the header (layers, line types, pens, metadata) is made
    here; the entities are converted by the native code (``convert``, which
    takes the lookup tables), and stay there.
    """
    is_symbol = source_format == "jws"
    # A symbol has no layer table: layers are created on demand with the group scale
    layers, layer_names = _build_layers(header)
    block_names = _build_block_names(block_defs)
    linetype_names, linetype_table, linetypes_from_file = _document_line_types(header)
    pen_table = None if is_symbol else _pen_table(header)
    context = _ConversionContext(
        options=options,
        layers=layers,
        layer_names=layer_names,
        block_names=block_names,
        linetype_names=linetype_names,
        source_format=source_format,
        group_scales=_symbol_group_scales(header) if is_symbol else None,
        pen_widths=_pen_widths_mm(pen_table),
        pen_colors=_pen_colors(pen_table),
    )

    try:
        converted = convert(
            label=context.label,
            source_format=source_format,
            entity_provenance=options.entity_provenance,
            strict=options.strict,
            curve_segments=options.curve_segments,
            layer_names=layer_names,
            layer_table=list(layers),
            block_names=block_names,
            linetype_names=linetype_names,
            pen_widths=context.pen_widths,
            pen_colors=context.pen_colors,
        )
    except ValueError as exc:
        raise ImporterError(str(exc)) from exc

    # The layers of entities on a layer the header does not have (every layer
    # of a symbol), in the order the entities came.
    for (layer_group, layer), name in converted.added_layers():
        layer_names[(layer_group, layer)] = name
        layer_metadata: dict[str, Any] = {"layer_group": layer_group, "layer": layer}
        if context.group_scales is not None and 0 <= layer_group < len(
            context.group_scales
        ):
            layer_metadata["group_scale"] = context.group_scales[layer_group]
        layers[name] = {
            "color": (layer_group * 16 + layer) % 255 + 1,
            "linetype": "CONTINUOUS",
            "metadata": {"jww": layer_metadata},
        }

    context.diagnostics.extend(
        ImportDiagnostic(
            code=code,
            severity=severity,
            message=message,
            source_id=source_id,
            source_kind=source_kind,
            action=action,
        )
        for code, severity, message, source_id, source_kind, action in (
            converted.diagnostics()
        )
    )
    context.converted_counts = Counter(converted.converted_counts())
    context.skipped_counts = Counter(converted.skipped_counts())
    context.approximation_counts = Counter(converted.approximation_counts())
    context.preserved_dimensions = converted.preserved_dimensions()
    context.pen_width_entities = converted.pen_width_entities()
    context.used_linetypes = set(converted.used_linetypes())
    metadata_settings = [
        {"entity_index": index, "key": key, "value": value, "raw": raw}
        for index, key, value, raw in converted.metadata_settings()
    ]

    tables: dict[str, Any] = {
        "layers": context.layers,
        "linetypes": _used_linetype_table(linetype_table, context.used_linetypes),
        "text_styles": {font: {"font": font} for font in converted.fonts()},
    }
    block_shells: list[tuple[str, dict[str, Any]]] = []
    block_sizes: dict[str, int] = {}
    for block_def, entity_count in zip(
        block_defs, converted.block_entity_counts(), strict=True
    ):
        number = int(block_def.get("number", 0))
        name = context.block_names[number]
        block_sizes[name] = entity_count
        block_shells.append(
            (
                name,
                {
                    "base_point": [0.0, 0.0],
                    "metadata": {
                        "jww": {
                            "number": number,
                            "is_referenced": bool(
                                block_def.get("is_referenced", False)
                            ),
                            "base": dict(
                                _mapping(block_def.get("base", {}), "block base")
                            ),
                        }
                    },
                },
            )
        )

    raw_version = int(header.get("version", 0))
    source: dict[str, Any] = {"format": source_format, "version": str(raw_version)}
    if source_name is not None:
        source["name"] = source_name
    if source_sha256 is not None:
        source["sha256"] = source_sha256

    format_metadata: dict[str, Any]
    if is_symbol:
        format_metadata = _symbol_metadata(header, raw_version, context)
    else:
        format_metadata = {
            "version": raw_version,
            "memo": str(header.get("memo", "")),
            "paper_size": int(header.get("paper_size", 0)),
            "write_layer_group": int(header.get("write_layer_group", 0)),
            # "file": the linetype patterns are the ones this file records.
            # "default": they are the Jw_cad defaults.
            "line_type_settings": "file" if linetypes_from_file else "default",
        }
        if pen_table is not None:
            format_metadata["pens"] = pen_table
    if metadata_settings:
        format_metadata["settings"] = metadata_settings

    shell: dict[str, Any] = {
        "format": "cad2d-ir",
        "version": options.ir_version,
        "header": {
            "units": "mm",
            "angle_unit": "deg",
            "coord_space": "world",
            "metadata": {source_format: format_metadata},
        },
        "source": source,
        "tables": tables,
    }

    if is_symbol:
        _append_symbol_diagnostics(format_metadata, context)
    _append_parser_diagnostics(parser_diagnostics, context)
    if metadata_settings:
        context.diagnostics.append(
            ImportDiagnostic(
                code="JWW_METADATA_SETTING_EXTRACTED",
                severity="info",
                message=(
                    f"Preserved {len(metadata_settings)} Jw_cad internal setting "
                    "record(s) as document metadata instead of drawing TEXT."
                ),
                source_kind="TEXT",
                action="preserved_metadata",
                details={
                    "count": len(metadata_settings),
                    "keys": sorted({str(item["key"]) for item in metadata_settings}),
                },
            )
        )
    if isinstance(pen_table, dict):
        resolved = sum(
            1
            for table in (pen_table["printer"], pen_table.get("extended", {}))
            for pen in table.values()
            if pen.get("width_mm") is not None
        )
        context.diagnostics.append(
            ImportDiagnostic(
                code="JWW_PEN_TABLE_PRESERVED",
                severity="info",
                message=(
                    "Preserved the printer pen table (line width per pen color) as "
                    "header.metadata.jww.pens; "
                    f"{context.pen_width_entities} entities with pen width 0 took "
                    "their pen color's width as lineweight_mm."
                ),
                action="preserved_metadata",
                details={
                    "unit": pen_table["unit"],
                    "printer_dpi": pen_table["printer_dpi"],
                    "widths_in_mm": resolved,
                    "entities_with_pen_color_width": context.pen_width_entities,
                },
            )
        )
    _append_summary_diagnostics(validation, context)

    statistics: dict[str, Any] = {
        "source_format": source_format,
        "source_entities": converted.source_entities(),
        "source_entity_counts": dict(
            sorted((str(key), int(value)) for key, value in source_counts.items())
        ),
        "metadata_settings": len(metadata_settings),
        "source_block_definitions": len(block_defs),
        "converted_entities": converted.entity_count(),
        "converted_block_entities": sum(block_sizes.values()),
        "converted_entity_counts": dict(sorted(context.converted_counts.items())),
        "skipped_entities": sum(context.skipped_counts.values()),
        "skipped_entity_counts": dict(sorted(context.skipped_counts.items())),
        "approximated_entities": sum(context.approximation_counts.values()),
        "preserved_dimensions": context.preserved_dimensions,
        "embedded_images": converted.embedded_images(),
        "linked_images": converted.linked_images(),
    }
    document = converted.take_document(shell, block_shells)
    return NativeImportResult(
        document=document, diagnostics=context.diagnostics, statistics=statistics
    )


def _coordinate(value: Any, path: str) -> list[float]:
    """``{"x": .., "y": ..}`` (ezjww) or a two-item sequence -> ``[x, y]``."""
    if isinstance(value, Mapping):
        return [float(value["x"]), float(value["y"])]
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        if len(value) != 2:
            raise ImporterError(f"{path} must have two coordinates")
        return [float(value[0]), float(value[1])]
    raise ImporterError(f"{path} must be a coordinate")


def _symbol_group_scales(header: Mapping[str, Any]) -> list[float]:
    """The 16 scale denominators of a JWS header, indexed by layer group."""
    raw = header.get("group_scales")
    if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)) or not raw:
        raise ImporterError("header.group_scales must be a list of scale denominators")
    scales: list[float] = []
    for index, item in enumerate(raw):
        if isinstance(item, bool) or not isinstance(item, (int, float)):
            raise ImporterError(f"header.group_scales[{index}] must be a number")
        scale = float(item)
        if not math.isfinite(scale) or scale <= 0.0:
            raise ImporterError(f"header.group_scales[{index}] must be positive")
        scales.append(scale)
    return scales


def _symbol_metadata(
    header: Mapping[str, Any], version: int, context: _ConversionContext
) -> dict[str, Any]:
    """``header.metadata.jws``: what the symbol header records besides the entities."""
    assert context.group_scales is not None
    return {
        "version": version,
        # Where the symbol is picked up when it is placed, in its own coordinates.
        "origin": _coordinate(header.get("origin"), "header.origin"),
        "group_scales": list(context.group_scales),
        # Bounds as Jw_cad recorded them, not a computed extent of the geometry.
        "bounds_min": _coordinate(header.get("bounds_min"), "header.bounds_min"),
        "bounds_max": _coordinate(header.get("bounds_max"), "header.bounds_max"),
        # The coordinates were not moved to the origin or scaled to real size.
        "coordinate_space": "paper_millimeters",
    }


def _append_symbol_diagnostics(
    metadata: Mapping[str, Any], context: _ConversionContext
) -> None:
    """Document-level facts about a JWS that a reader of the IR needs to know."""
    scales = list(metadata["group_scales"])
    used_groups = sorted({group for group, _layer in context.layer_names})
    used_scales = {
        str(group): scales[group] for group in used_groups if group < len(scales)
    }
    origin = list(metadata["origin"])
    scale_text = ", ".join(
        f"1/{scale:g} (group {group})" for group, scale in used_scales.items()
    )
    context.diagnostics.insert(
        0,
        ImportDiagnostic(
            code="JWS_SYMBOL_COORDINATES",
            severity="info",
            message=(
                "Coordinates are the paper millimetres of the drawing the symbol was "
                f"registered from; the insertion origin ({origin[0]:g}, {origin[1]:g})"
                + (f" and the layer group scales {scale_text}" if scale_text else "")
                + " were not applied and are kept in header.metadata.jws."
            ),
            details={"origin": origin, "group_scales": used_scales},
        ),
    )
    context.diagnostics.insert(
        1,
        ImportDiagnostic(
            code="JWS_DRAWING_SETTINGS_DEFAULTED",
            severity="info",
            message=(
                "A JWS symbol stores no layer names, pen palette, line type patterns, "
                "paper size or memo; layers are named <group>-<layer> and line types "
                "use the Jw_cad defaults."
            ),
            action="normalized",
        ),
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
            # State 0 hides the layer, and a group in state 0 hides all of its layers
            hidden = int(layer.get("state", 2)) == 0 or int(group.get("state", 2)) == 0
            layers[unique_name] = {
                "color": (group_index * 16 + layer_index) % 255 + 1,
                "linetype": "CONTINUOUS",
                "plot": not hidden,
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
            if hidden:
                layers[unique_name]["visible"] = False
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


def _append_parser_diagnostics(
    jww_document: Mapping[str, Any],
    context: _ConversionContext,
) -> None:
    """Map ``ezjww`` parser diagnostics onto the stable IR diagnostic codes."""
    for raw in jww_document.get("diagnostics", []) or []:
        if not isinstance(raw, Mapping):
            continue
        code = str(raw.get("code", ""))
        details = raw.get("details")
        details_dict = dict(details) if isinstance(details, Mapping) else None
        if code == "ENTITY_LIST_TRUNCATED":
            parsed = int((details_dict or {}).get("parsed_entities", 0) or 0)
            expected = int((details_dict or {}).get("expected_entities", 0) or 0)
            error = str((details_dict or {}).get("error", "") or "")
            context.diagnostics.append(
                ImportDiagnostic(
                    code="JWW_ENTITY_LIST_TRUNCATED",
                    severity="error",
                    message=(
                        f"{context.label} entity list could not be read to its end: kept "
                        f"{parsed} of {expected} announced entities"
                        + (f" ({error})" if error else "")
                        + "; the remaining entities and block definitions were skipped."
                    ),
                    action="skipped",
                    details=details_dict,
                )
            )
        elif code == "CP932_DECODE_REPLACED":
            context.diagnostics.append(
                ImportDiagnostic(
                    code="JWW_DECODE_REPLACED",
                    severity="warning",
                    message=str(
                        raw.get("message") or "CP932 decoding replaced characters."
                    ),
                    action="normalized",
                    details=details_dict,
                )
            )


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
                    message=f"Skipped {count} unsupported {context.label} {source_kind} entities.",
                    source_kind=source_kind,
                    action="skipped",
                )
            )
    for source_kind, count in sorted(context.approximation_counts.items()):
        context.diagnostics.append(
            ImportDiagnostic(
                code="JWW_CURVE_APPROXIMATED",
                severity="warning",
                message=(
                    f"Approximated {count} {context.label} {source_kind} entities "
                    "with polyline hatch boundaries."
                ),
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
                message=(
                    f"{context.label} contains unresolved block definition numbers: "
                    f"{unresolved}."
                ),
                source_kind="BLOCK",
                action="preserved_reference",
            )
        )


def _unique_name(candidate: str, fallback: str, used: Mapping[str, Any]) -> str:
    if candidate not in used:
        return candidate
    suffixed = f"{candidate} [{fallback}]"
    serial = 2
    while suffixed in used:
        suffixed = f"{candidate} [{fallback}-{serial}]"
        serial += 1
    return suffixed


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


__all__ = [
    "NativeImportResult",
    "convert_jws_file_to_ir",
    "convert_jww_file_to_ir",
    "import_jww_file",
    "jws_document_to_ir",
    "jww_document_to_ir",
]
