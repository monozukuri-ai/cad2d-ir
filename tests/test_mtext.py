"""Inline formatting codes of MTEXT text (``cad2d_ir.mtext``)."""

from __future__ import annotations

import pytest

from cad2d_ir.mtext import (
    LINE_PITCH,
    MTextLine,
    mtext_baselines,
    mtext_dxf_codes,
    mtext_layout,
    mtext_lines,
    mtext_plain,
    mtext_source_text,
)


def test_codes_are_removed_without_swallowing_the_text() -> None:
    # The toggles take no argument: the text behind them stays.
    assert mtext_plain(r"\LTitle\l rest") == "Title rest"
    assert mtext_plain(r"\OOver\o and \Kstrike\k") == "Over and strike"
    # A code with an argument ends with ";".
    codes = r"\A1;\fArial|b0|i0|c0|p34;a\H2.5x;b\C1;c\c255;d\T1.5;e\Q15;f\pxqc;g"
    assert mtext_plain(codes) == "abcdefg"
    # Escaped characters, the non-breaking space and the breaks.
    assert mtext_plain(r"a\~b \\ \{c\}") == "a b \\ {c}"
    assert mtext_plain(r"one\Ptwo\Nthree\Xfour") == "one\ntwo\nthree\nfour"
    # Characters by code: Unicode, and two bytes in a named code page.
    assert mtext_plain(r"45\U+00B0 \M+182A0") == "45° あ"
    # Half of a surrogate pair is no character.
    assert mtext_plain(r"\U+D83D") == r"\U+D83D"
    # An unknown code, and one without its ";", lose two characters only.
    assert mtext_plain(r"\Zkeep \H2.5 keep") == "keep 2.5 keep"
    # Braces need no partner.
    assert mtext_plain("a}b{c") == "abc"
    assert mtext_plain("plain\ntext") == "plain\ntext"


def test_stacks_become_upper_slash_lower() -> None:
    assert mtext_plain(r"10\S+0.1^-0.2;") == "10+0.1/-0.2"
    assert mtext_plain(r"1\S1/2; 1\S1#2;") == "11/2 11/2"
    # One side alone stands without the slash.
    assert mtext_plain(r"\S^-0.2; \S+0.1^;") == "-0.2 +0.1"
    # Escaped separators belong to the text.
    assert mtext_plain(r"\S1\/2^3;") == "1/2/3"
    # CAXA names the height of the stacked characters behind \S.
    assert mtext_plain(r"mm\S\H0.7x;2^; D\S\H0.7x;^-0.1;") == "mm2 D-0.1"


def test_width_code_sets_the_factor_of_a_line() -> None:
    assert mtext_lines(r"\W0.8;narrow") == [MTextLine("narrow", 0.8, 1.0)]
    # A relative value multiplies the factor in force.
    assert mtext_lines(r"\W0.5x;half", width_factor=0.8) == [("half", 0.4, 1.0)]
    # The factor replaces the one of the entity and lasts over paragraphs,
    assert mtext_lines(r"\W0.5;one\Ptwo", width_factor=0.8) == [
        ("one", 0.5, 1.0),
        ("two", 0.5, 1.0),
    ]
    # but ends with the closing brace.
    assert mtext_lines(r"{\W0.5;one}\Ptwo", width_factor=0.8) == [
        ("one", 0.5, 1.0),
        ("two", 0.8, 1.0),
    ]
    # Values outside 0.1 to 10, and values that are no number, are ignored.
    ignored = mtext_lines(r"\W0;a\P\W99;b\P\Wwide;c\P\W;d\P\Wnan;e", width_factor=0.8)
    assert ignored == [(character, 0.8, 1.0) for character in "abcde"]
    # A line whose factor changes gets the one that keeps its length.
    ((text, factor, _),) = mtext_lines(r"abcd{\W0.5;efgh}")
    assert text == "abcdefgh" and factor == pytest.approx(0.75)


def test_caxa_width_code_has_a_value_for_latin_and_one_for_cjk() -> None:
    assert mtext_lines(r"\A3;{\W0.85^0.85;图样ab}") == [("图样ab", 0.85, 1.0)]
    # The CJK value alone leaves Latin characters as they are.
    assert mtext_lines(r"\W^0.6;ab") == [("ab", 1.0, 1.0)]
    assert mtext_lines(r"\W^0.6;图样") == [("图样", 0.6, 1.0)]
    assert mtext_lines(r"\W0.8;\W^0.8;\A3;M12图") == [("M12图", 0.8, 1.0)]
    # Two Latin characters (one cell) at 0.8 and two CJK ones (two cells) at 0.5.
    ((_, factor, _),) = mtext_lines(r"\W0.8^0.5;ab图样")
    assert factor == pytest.approx((1 * 0.8 + 2 * 0.5) / 3)


def test_height_code_sets_the_height_of_a_line() -> None:
    # A relative height multiplies the one in force.
    assert mtext_lines(r"\H2x;big") == [("big", 1.0, 2.0)]
    assert mtext_lines(r"\H2x;{\H0.5x;same}\Pbig") == [
        ("same", 1.0, 1.0),
        ("big", 1.0, 2.0),
    ]
    # An absolute height is measured against the height of the entity,
    assert mtext_lines(r"\H7;title", height=3.5) == [("title", 1.0, 2.0)]
    # and ignored when that one is not known.
    assert mtext_lines(r"\H7;title") == [("title", 1.0, 1.0)]
    # Values that are no height are ignored.
    assert mtext_lines(r"\H0x;a\P\H-1;b\P\Hx;c", height=5.0) == [
        (character, 1.0, 1.0) for character in "abc"
    ]


def test_a_line_of_mixed_heights_keeps_its_length() -> None:
    # Five characters at the height of the entity, four at half of it: the line
    # has the height most of it has, and is as long as the nine characters are.
    ((text, width, height),) = mtext_lines(r"10.55{\H0.5x;+0.1}")
    assert (text, height) == ("10.55+0.1", 1.0)
    assert width == pytest.approx((5 * 1.0 + 4 * 0.5) / 9)
    # The taller part decides when both have as many characters.
    ((_, width, height),) = mtext_lines(r"ab{\H2x;cd}")
    assert height == 2.0 and width == pytest.approx((2 * 1.0 + 2 * 2.0) / (4 * 2.0))


def test_source_text_of_a_dwg_mtext() -> None:
    def entity(text: str, stored: str | None) -> dict:
        dwg = {"handle": 1} if stored is None else {"handle": 1, "raw_text": stored}
        return {"kind": "MTEXT", "text": text, "metadata": {"dwg": dwg}}

    # The DWG importer keeps the stored text beside the text without codes.
    assert mtext_source_text(entity("HALF\nPLAIN", r"{\W0.5;HALF}\PPLAIN")) == (
        r"{\W0.5;HALF}\PPLAIN"
    )
    # The importer reads a stack with one part as "part/".
    assert mtext_source_text(entity("mm2/", r"mm\S2^;")) == r"mm\S2^;"
    assert mtext_source_text(entity("10+0.1/-0.2", r"10\S+0.1^-0.2;")) == (
        r"10\S+0.1^-0.2;"
    )
    # An edited text no longer reads as the stored one.
    assert mtext_source_text(entity("EDITED", r"{\W0.5;HALF}")) == "EDITED"
    assert mtext_source_text(entity("HALF.", r"{\W0.5;HALF}")) == "HALF."
    # Without a stored text, and for other sources, the text is all there is.
    assert mtext_source_text(entity("plain", None)) == "plain"
    assert mtext_source_text({"kind": "MTEXT", "text": r"\W0.5;dxf"}) == r"\W0.5;dxf"
    assert mtext_source_text({"kind": "MTEXT"}) == ""


def test_baselines_hang_below_the_attachment_point() -> None:
    # Top: the first baseline is one line height below the insertion point.
    assert mtext_baselines([1.0, 1.0, 1.0], "top") == pytest.approx(
        [1.0, 1.0 + LINE_PITCH, 1.0 + 2 * LINE_PITCH]
    )
    # Bottom: the last baseline is at the insertion point.
    assert mtext_baselines([1.0, 1.0], "bottom") == pytest.approx([-LINE_PITCH, 0.0])
    # Middle: half of the lines is above the insertion point.
    assert mtext_baselines([1.0], "middle") == pytest.approx([0.5])
    assert mtext_baselines([1.0, 1.0], "middle") == pytest.approx(
        [0.5 - LINE_PITCH / 2, 0.5 + LINE_PITCH / 2]
    )
    # A line advances by its own height.
    assert mtext_baselines([2.0, 1.0, 3.0], "top") == pytest.approx(
        [2.0, 2.0 + LINE_PITCH, 2.0 + 4 * LINE_PITCH]
    )
    # Any other value: the first baseline is at the insertion point.
    assert mtext_baselines([1.0, 1.0], "baseline") == pytest.approx([0.0, LINE_PITCH])
    assert mtext_baselines([], "top") == []


def test_layout_keeps_the_room_of_an_empty_line_between_lines() -> None:
    lines = mtext_lines(r"a\P\Pb\P\P")
    assert [line.text for line in lines] == ["a", "", "b", "", ""]
    # The empty line between "a" and "b" keeps its room and is not drawn; the
    # empty lines at the end take none.
    placed = mtext_layout(lines, "top")
    assert [(line.text, drop) for line, drop in placed] == [
        ("a", pytest.approx(1.0)),
        ("b", pytest.approx(1.0 + 2 * LINE_PITCH)),
    ]
    # So a line with a break behind it is centered as the line alone is.
    assert [drop for _, drop in mtext_layout(mtext_lines(r"one\P"), "middle")] == [
        pytest.approx(0.5)
    ]
    assert mtext_layout(mtext_lines(r"\P\P"), "top") == []


@pytest.mark.parametrize(
    ("caxa", "dxf"),
    [
        # Two equal sides are the one code.
        (r"\W0.667^0.667;技术要求", r"\W0.667;技术要求"),
        (r"\fSimSun^SimSun||;AB中文", r"\fSimSun|b0|i0;AB中文"),
        # Otherwise the characters of a script get their own code in braces.
        (r"\W^0.8;中文AB中文", r"{\W0.8;中文}AB{\W0.8;中文}"),
        (r"\W0.8^0.6;AB中文", r"{\W0.8;AB}{\W0.6;中文}"),
        (r"\f^simsun|b0|i0;中文AB", r"{\fsimsun|b0|i0;中文}AB"),
        # \A3; has no meaning in DXF; the other codes and the stacks stay.
        (r"\A3;{\H0.7x;\S+0.1^-0.2;}\P第二行", r"{\H0.7x;\S+0.1^-0.2;}\P第二行"),
        (r"\A2;PLAIN \S1^2;", r"\A2;PLAIN \S1^2;"),
    ],
)
def test_caxa_codes_become_dxf_codes(caxa: str, dxf: str) -> None:
    assert mtext_dxf_codes(caxa) == dxf
    # The lines read the same, whatever the width factor of the entity.
    for width_factor in (1.0, 0.7):
        assert mtext_lines(dxf, width_factor=width_factor, height=2.5) == mtext_lines(
            caxa, width_factor=width_factor, height=2.5
        )
    assert mtext_dxf_codes(dxf) == dxf
