"""Inline formatting codes of MTEXT text.

The text of an ``MTEXT`` carries the inline codes of DXF: ``\\P`` starts a
paragraph, ``\\W0.8;`` sets the width factor, ``\\H0.7x;`` the character
height, ``\\S+0.1^-0.2;`` stacks a tolerance, braces limit a change to the
text they enclose, and so on. A consumer that draws one string per line, or
writes a format without such codes (DXF R12, JWW, SXF), needs the lines
without the codes together with what the codes say about each line.

``mtext_lines`` gives that. A line has one width factor and one height: when
they change inside a line, the height is the one most of the line has, and the
width factor is the one that keeps the length of the line.

The codes are the ones AutoCAD writes. Two forms CAXA writes in EXB drawings
are read as well: a width factor for Latin characters and one for CJK
characters (``\\W0.85^0.85;``, or ``\\W^0.8;`` for CJK alone), and a stack that
names the height of its characters (``\\S\\H0.7x;2^;``). ``mtext_dxf_codes``
rewrites the CAXA forms as DXF codes.
"""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Mapping, Sequence
from typing import Any, NamedTuple

__all__ = [
    "LINE_PITCH",
    "WIDTH_FACTOR_RANGE",
    "MTextLine",
    "mtext_baselines",
    "mtext_dxf_codes",
    "mtext_layout",
    "mtext_lines",
    "mtext_plain",
    "mtext_source_text",
]

#: The width factors an MTEXT accepts (AutoCAD rejects others); values outside
#: of the range are ignored.
WIDTH_FACTOR_RANGE = (0.1, 10.0)
#: Distance from one baseline to the next, as a multiple of the height of the
#: lower line.
LINE_PITCH = 1.6

# A code with an argument ends with ";". The toggles (underline \L \l, overline
# \O \o, strike-through \K \k) and the breaks (\P paragraph, \N column, \X the
# text above and below a dimension line) are two characters and take none. An
# unknown code, and one that lacks its ";", loses the backslash and the letter
# only, so that the text behind it stays.
_TOKEN_RE = re.compile(
    r"\\(?:"
    r"(?P<literal>[\\{}])"
    r"|(?P<space>~)"
    r"|(?P<break>[PNX])"
    r"|[LlOoKk]"
    r"|S(?:\\H[^;\\{}\n]*;)?(?P<stack>(?:[^;\\\n]|\\[^A-Za-z\n])*);"
    r"|W(?P<width>[^;\\{}\n]*);"
    r"|H(?P<height>[^;\\{}\n]*);"
    r"|[ACcFfQTp][^;\\{}\n]*;"
    r"|U\+(?P<unicode>[0-9A-Fa-f]{4})"
    r"|M\+(?P<page>[1-5])(?P<mif>[0-9A-Fa-f]{4})"
    r"|[A-Za-z]"
    r")|(?P<brace>[{}])|(?P<newline>\n)"
)
_STACK_PARTS_RE = re.compile(r"((?:[^\\^/#]|\\.)*)(?:[\^/#](.*))?", re.DOTALL)
_ESCAPED_RE = re.compile(r"\\(.)", re.DOTALL)
# The code page ``\M+nXXXX`` selects with ``n``.
_MIF_CODECS = {"1": "cp932", "2": "cp950", "3": "cp949", "4": "cp1361", "5": "cp936"}
# Characters above this one count as full-width: one cell, as wide as the text
# is high. The others count as half a cell.
_WIDE_FROM = 0x2E7F

_VERTICAL_ALIGNMENTS = ("top", "middle", "bottom")


class MTextLine(NamedTuple):
    """One line of an ``MTEXT`` without its inline codes."""

    text: str
    #: Width factor of the line; the factor of the entity or of its text style
    #: unless the text sets one with ``\\W``.
    width_factor: float
    #: Character height of the line as a multiple of the height of the entity.
    height_factor: float


class _State(NamedTuple):
    narrow: float
    wide: float
    height: float


def mtext_lines(
    text: str, *, width_factor: float = 1.0, height: float | None = None
) -> list[MTextLine]:
    """Split the text of an ``MTEXT`` into lines without the inline codes.

    ``width_factor`` is the factor the text starts with: the one of the entity
    or, for a DXF or DWG ``MTEXT``, of its text style. ``\\W0.8;`` replaces it
    and ``\\W0.8x;`` multiplies it. ``height`` is the height of the entity, which
    an absolute ``\\H2.5;`` is measured against (without it such a code is
    ignored); ``\\H0.7x;`` multiplies the height in force. A change inside
    braces ends with the closing brace. Fonts, colours and the other codes are
    removed.
    """
    return _lines(text, width_factor, height, _stack)


def _lines(
    text: str,
    width_factor: float,
    height: float | None,
    stack: Callable[[str], str],
) -> list[MTextLine]:
    if "\\" not in text and "{" not in text and "}" not in text:
        return [MTextLine(line, width_factor, 1.0) for line in text.split("\n")]
    lines: list[MTextLine] = []
    runs: list[tuple[str, _State]] = []
    state = _State(width_factor, width_factor, 1.0)
    scopes: list[_State] = []
    position = 0
    for match in _TOKEN_RE.finditer(text):
        if match.start() > position:
            runs.append((text[position : match.start()], state))
        position = match.end()
        kind = match.lastgroup
        if kind is None:  # a code that is only removed
            continue
        value = match.group(kind)
        if kind == "width":
            state = _with_width(state, value)
        elif kind == "height":
            state = state._replace(height=_height(value, state.height, height))
        elif kind == "brace":
            if value == "{":
                scopes.append(state)
            elif scopes:
                state = scopes.pop()
        elif kind in ("break", "newline"):
            lines.append(_line(runs, state))
            runs = []
        else:
            if kind == "stack":
                value = stack(value)
            elif kind == "space":
                value = " "
            elif kind == "unicode":
                code = int(value, 16)
                # Half of a surrogate pair is no character; it stays as written.
                value = match.group(0) if 0xD800 <= code <= 0xDFFF else chr(code)
            elif kind == "mif":
                codec = _MIF_CODECS[match.group("page")]
                value = bytes.fromhex(value).decode(codec, "replace")
            runs.append((value, state))
    if position < len(text):
        runs.append((text[position:], state))
    lines.append(_line(runs, state))
    return lines


def mtext_plain(text: str) -> str:
    """The text of an ``MTEXT`` without its inline codes, one line per paragraph."""
    return "\n".join(line.text for line in mtext_lines(text))


def mtext_source_text(entity: Mapping[str, Any]) -> str:
    """The text of an ``MTEXT`` entity with its inline codes.

    The DWG importer puts the text without the codes into ``text`` and keeps the
    stored text in ``metadata.dwg.raw_text``. The stored text is returned as long
    as it reads as ``text``. Once ``text`` was edited it does not, and ``text``
    is returned.
    """
    text = str(entity.get("text", ""))
    metadata = entity.get("metadata")
    section = metadata.get("dwg") if isinstance(metadata, Mapping) else None
    stored = section.get("raw_text") if isinstance(section, Mapping) else None
    if not isinstance(stored, str) or stored == text:
        return text
    # ``text`` is the stored text as the importer read it: a stack is there with
    # a slash between its parts, also when it has one part only.
    for stack in (_stack, _stored_stack):
        lines = _lines(stored, 1.0, None, stack)
        if "\n".join(line.text for line in lines) == text:
            return stored
    return text


def mtext_baselines(height_factors: Sequence[float], vertical: str) -> list[float]:
    """How far below the insertion point each baseline lies.

    The distances are multiples of the height of the entity. ``vertical`` is
    the first part of ``attach`` (``top``, ``middle`` or ``bottom``): the top of
    the first line, the middle of the lines or the last baseline is at the
    insertion point. Any other value puts the first baseline there.
    """
    if not height_factors:
        return []
    offsets = [0.0]
    for factor in height_factors[1:]:
        offsets.append(offsets[-1] + LINE_PITCH * factor)
    first = height_factors[0]
    if vertical == "top":
        shift = first
    elif vertical == "middle":
        shift = (first - offsets[-1]) / 2.0
    elif vertical == "bottom":
        shift = -offsets[-1]
    else:
        shift = 0.0
    return [shift + offset for offset in offsets]


def mtext_layout(
    lines: Sequence[MTextLine], vertical: str
) -> list[tuple[MTextLine, float]]:
    """The lines to draw, each with how far its baseline lies below the insertion point.

    The distances are those of ``mtext_baselines``. An empty line keeps its room
    between the lines around it and is left out of the result; empty lines at
    the end take no room.
    """
    kept = list(lines)
    while kept and not kept[-1].text.strip():
        kept.pop()
    drops = mtext_baselines([line.height_factor for line in kept], vertical)
    return [
        (line, drop) for line, drop in zip(kept, drops) if line.text.strip()
    ]


def _with_width(state: _State, argument: str) -> _State:
    """Apply ``\\W``: one value for all characters, or ``latin^cjk``."""
    first, caret, second = argument.partition("^")
    if not caret:
        return state._replace(
            narrow=_width(first, state.narrow), wide=_width(first, state.wide)
        )
    narrow = _width(first, state.narrow) if first.strip() else state.narrow
    wide = _width(second, state.wide) if second.strip() else state.wide
    return state._replace(narrow=narrow, wide=wide)


def _number(token: str) -> tuple[float | None, bool]:
    """A value and whether it multiplies the value in force (``0.8x``)."""
    token = token.strip()
    relative = token[-1:] in ("x", "X")
    try:
        value = float(token[:-1] if relative else token)
    except ValueError:
        return None, relative
    return (value if math.isfinite(value) else None), relative


def _width(token: str, current: float) -> float:
    value, relative = _number(token)
    if value is None:
        return current
    if relative:
        value *= current
    low, high = WIDTH_FACTOR_RANGE
    return value if low <= value <= high else current


def _height(token: str, current: float, entity_height: float | None) -> float:
    value, relative = _number(token)
    if value is None or value <= 0.0:
        return current
    if relative:
        return current * value
    if entity_height is None or not entity_height > 0.0:
        return current
    return value / entity_height


# Codes only CAXA writes: a width factor or a font for Latin and for CJK
# characters (``\W0.8^0.7;``, ``\W^0.8;``, ``\fArial^SimSun||;``), and ``\A3;``.
_CAXA_CODE_RE = re.compile(r"\\[Wf][^;\\{}\n]*\^|\\A3;")


class _Override(NamedTuple):
    """What a run of characters needs on top of the DXF state around it."""

    #: ``None`` for the width in force, ``("abs", w)`` or ``("rel", k)``.
    width: tuple[str, float] | None
    #: ``None`` for the font in force, or the argument of ``\f`` (``Arial|b0|i0``).
    font: str | None


_INHERITED = _Override(None, None)


def mtext_dxf_codes(text: str) -> str:
    """The text of an ``MTEXT`` with the codes only CAXA writes made DXF codes.

    CAXA gives Latin and CJK characters a width factor and a font each
    (``\\W0.8^0.7;``, ``\\W^0.8;`` for CJK alone, ``\\fArial^SimSun||;``); DXF
    has one of each. A pair with two equal sides becomes the one code, and
    otherwise the characters of a script that differ from the rest are wrapped
    in braces with their own code: ``\\W^0.8;中文AB`` becomes
    ``{\\W0.8;中文}AB``. ``\\A3;``, which DXF does not have, is removed. Every
    other code stays as written, and a text without these forms is returned as
    it is; ``mtext_lines`` reads the result as it read the CAXA form.
    """
    if not _CAXA_CODE_RE.search(text):
        return text
    output: list[str] = []
    # The override of Latin and of CJK characters; braces keep both.
    narrow, wide = _INHERITED, _INHERITED
    scopes: list[tuple[_Override, _Override]] = []
    group: list[str] = []
    group_override = _INHERITED

    def flush() -> None:
        nonlocal group, group_override
        if not group:
            return
        body = "".join(group)
        if group_override == _INHERITED:
            output.append(body)
        else:
            codes = ""
            if group_override.width is not None:
                kind, value = group_override.width
                codes += f"\\W{_format_factor(value)}{'x' if kind == 'rel' else ''};"
            if group_override.font is not None:
                codes += f"\\f{group_override.font};"
            output.append("{" + codes + body + "}")
        group = []
        group_override = _INHERITED

    def add(piece: str, is_wide: bool) -> None:
        nonlocal group_override
        override = wide if is_wide else narrow
        if group and override != group_override:
            flush()
        group_override = override
        group.append(piece)

    def add_text(piece: str) -> None:
        start = 0
        for index in range(1, len(piece) + 1):
            if index == len(piece) or _is_wide(piece[index]) != _is_wide(piece[start]):
                add(piece[start:index], _is_wide(piece[start]))
                start = index

    position = 0
    for match in _TOKEN_RE.finditer(text):
        if match.start() > position:
            add_text(text[position : match.start()])
        position = match.end()
        token = match.group(0)
        kind = match.lastgroup
        if kind == "width":
            first, caret, second = match.group("width").partition("^")
            if not caret:
                flush()
                output.append(token)
                narrow, wide = _scaled(narrow, first), _scaled(wide, first)
                continue
            if first.strip() and _same_number(first, second):
                flush()
                output.append(f"\\W{first.strip()};")
                narrow, wide = _scaled(narrow, first), _scaled(wide, first)
                continue
            if first.strip():
                narrow = narrow._replace(width=_set_width(narrow.width, first))
            if second.strip():
                wide = wide._replace(width=_set_width(wide.width, second))
            continue
        if token[:2] == "\\f" and "^" in token:
            names, _, flags = token[2:-1].partition("|")
            flags = flags.strip("|")
            western, _, eastern = names.partition("^")
            if western and western == eastern:
                flush()
                output.append(f"\\f{_font_argument(western, flags)};")
                narrow, wide = narrow._replace(font=None), wide._replace(font=None)
                continue
            if western:
                narrow = narrow._replace(font=_font_argument(western, flags))
            if eastern:
                wide = wide._replace(font=_font_argument(eastern, flags))
            continue
        if token == "\\A3;":
            continue
        if kind == "brace":
            flush()
            output.append(token)
            if token == "{":
                scopes.append((narrow, wide))
            elif scopes:
                narrow, wide = scopes.pop()
            continue
        if kind in ("stack", "space", "literal"):
            add(token, False)
            continue
        if kind == "unicode":
            add(token, int(match.group("unicode"), 16) > _WIDE_FROM)
            continue
        if kind == "mif":
            add(token, True)
            continue
        flush()
        output.append(token)
        if token[:2] == "\\f":
            # A font for all characters: the CAXA fonts in force are replaced.
            narrow, wide = narrow._replace(font=None), wide._replace(font=None)
    if position < len(text):
        add_text(text[position:])
    flush()
    return "".join(output)


def _font_argument(name: str, flags: str) -> str:
    """The argument of ``\\f`` for a TrueType font (bold and italic flags)."""
    return f"{name}|{flags}" if flags else f"{name}|b0|i0"


def _is_wide(character: str) -> bool:
    return ord(character) > _WIDE_FROM


def _same_number(first: str, second: str) -> bool:
    return first.strip() == second.strip()


def _set_width(
    current: tuple[str, float] | None, token: str
) -> tuple[str, float] | None:
    """The width override after one side of ``\\Wa^b;``."""
    value, relative = _number(token)
    if value is None:
        return current
    if not relative:
        low, high = WIDTH_FACTOR_RANGE
        return ("abs", value) if low <= value <= high else current
    if current is None:
        return ("rel", value)
    kind, base = current
    return (kind, base * value)


def _scaled(override: _Override, token: str) -> _Override:
    """The override after ``\\Wv;`` for all characters.

    An absolute value is the width in force for all characters, so no
    override is left; a relative one scales an absolute override with it.
    """
    value, relative = _number(token)
    if value is None or override.width is None:
        return override
    if not relative:
        return override._replace(width=None)
    kind, base = override.width
    return override._replace(
        width=(kind, base * value) if kind == "abs" else override.width
    )


def _format_factor(value: float) -> str:
    text = f"{value:.6f}".rstrip("0").rstrip(".")
    return text or "0"


def _stack(content: str) -> str:
    """``upper/lower`` for a stack, or the one side it has."""
    match = _STACK_PARTS_RE.fullmatch(content)
    if match is None:  # ends with a backslash
        return content
    upper = _ESCAPED_RE.sub(r"\1", match.group(1)).strip()
    lower = _ESCAPED_RE.sub(r"\1", match.group(2) or "").strip()
    return f"{upper}/{lower}" if upper and lower else upper or lower


def _stored_stack(content: str) -> str:
    """A stack as the DWG importer puts it into ``text``."""
    return content.replace("^", "/").replace("#", "/")


def _line(runs: list[tuple[str, _State]], state: _State) -> MTextLine:
    text = "".join(run for run, _ in runs)
    states = {run_state for run, run_state in runs if run}
    if len(states) == 1:
        only = next(iter(states))
        if only.narrow == only.wide:
            return MTextLine(text, only.narrow, only.height)
    # Cells of the line by character height, and the length of the line in
    # multiples of the height of the entity.
    cells_by_height: dict[float, float] = {}
    length = 0.0
    for run, run_state in runs:
        wide = sum(1 for character in run if ord(character) > _WIDE_FROM)
        narrow = len(run) - wide
        cells = wide + 0.5 * narrow
        if not cells:
            continue
        cells_by_height[run_state.height] = (
            cells_by_height.get(run_state.height, 0.0) + cells
        )
        length += (
            wide * run_state.wide + 0.5 * narrow * run_state.narrow
        ) * run_state.height
    if not cells_by_height:
        return MTextLine(text, state.narrow, state.height)
    height = max(cells_by_height, key=lambda value: (cells_by_height[value], value))
    return MTextLine(text, length / (sum(cells_by_height.values()) * height), height)
