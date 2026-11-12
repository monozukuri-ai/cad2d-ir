"""The table of wide characters of the native code against the codec of Python.

The DXF writer lays a justified text out in character cells: a character that
takes two bytes in Shift_JIS (the Windows code page 932) is one cell, any other
half a cell. Rust has no ``cp932`` codec, so the native code carries a table
made from the one of Python (``cad2d-ir-core/src/dxf/text.rs``). Every
character is checked against the codec here.
"""

from __future__ import annotations

import sys

import cad2d_ir_native as native

# The signs a reader draws for %%c, %%d and %%p: one cell each, whatever the
# code page makes of them.
CODED_SYMBOLS = set("⌀∅Øø°±")


def _cells(character: str) -> float:
    if character in CODED_SYMBOLS:
        return 1.0
    try:
        wide = len(character.encode("cp932")) == 2
    except UnicodeEncodeError:
        # what the code page does not have: wide from the CJK radicals on
        wide = ord(character) > 0x2E7F
    return 1.0 if wide else 0.5


def test_every_character_of_the_basic_plane_has_the_width_of_the_codec() -> None:
    wrong = [
        f"U+{code:04X}"
        for code in range(0x10000)
        if not 0xD800 <= code <= 0xDFFF  # half of a pair is no character
        and native.text_cells(chr(code)) != _cells(chr(code))
    ]
    assert wrong == []


def test_the_characters_of_the_other_planes_are_one_cell_each() -> None:
    step = 4096
    for start in range(0x10000, sys.maxunicode + 1, step):
        block = "".join(map(chr, range(start, start + step)))
        # the code page has none of them
        assert block.encode("cp932", "ignore") == b"", hex(start)
        assert native.text_cells(block) == float(step), hex(start)


def test_codes_count_as_what_a_reader_draws_for_them() -> None:
    assert native.text_cells("") == 0.0
    assert native.text_cells("ABC") == 1.5
    assert native.text_cells("図面") == 2.0
    assert native.text_cells("ｱｲｳ") == 1.5
    # a diameter sign and two digits
    assert native.text_cells("%%c10") == 2.0
    assert native.text_cells("%%C10") == 2.0
    # underline on and off: nothing is drawn for the codes
    assert native.text_cells("%%uAB%%u") == 1.0
    # a percent sign written as a code, and one that is not a code
    assert native.text_cells("%%%") == 1.0
    assert native.text_cells("50%") == 1.5
