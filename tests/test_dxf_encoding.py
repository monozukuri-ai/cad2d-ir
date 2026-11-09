from __future__ import annotations

import codecs
from pathlib import Path

import pytest

from cad2d_ir import (
    convert_dxf_file_to_ir,
    convert_file_to_ir,
    ir_to_dxf,
    write_dxf_file,
)


def _text_dxf(*, codepage: bool) -> str:
    header = [
        "0",
        "SECTION",
        "2",
        "HEADER",
    ]
    if codepage:
        header.extend(["9", "$DWGCODEPAGE", "3", "ANSI_932"])
    header.extend(["0", "ENDSEC"])
    return "\n".join(
        [
            *header,
            "0",
            "SECTION",
            "2",
            "ENTITIES",
            "0",
            "TEXT",
            "8",
            "\u65e5\u672c\u8a9e\u30ec\u30a4\u30e4",
            "10",
            "0",
            "20",
            "0",
            "40",
            "1",
            "1",
            "\u5bf8\u6cd5",
            "0",
            "ENDSEC",
            "0",
            "EOF",
            "",
        ]
    )


def test_cp932_dxf_uses_dwgcodepage_and_reports_encoding(tmp_path: Path) -> None:
    source = tmp_path / "with-codepage.dxf"
    source.write_bytes(_text_dxf(codepage=True).encode("cp932"))

    result = convert_dxf_file_to_ir(source)

    assert result.encoding == "cp932"
    assert (
        result.document["entities"][0]["layer"]
        == "\u65e5\u672c\u8a9e\u30ec\u30a4\u30e4"
    )
    assert result.document["entities"][0]["text"] == "\u5bf8\u6cd5"
    diagnostic = next(
        item for item in result.diagnostics if item.code == "DXF_ENCODING_DETECTED"
    )
    assert diagnostic.details == {
        "encoding": "cp932",
        "source": "$DWGCODEPAGE=ANSI_932",
    }


def test_cp932_dxf_without_codepage_uses_fallback(tmp_path: Path) -> None:
    source = tmp_path / "fallback.dxf"
    source.write_bytes(_text_dxf(codepage=False).encode("cp932"))

    result = convert_file_to_ir(source)

    assert result.statistics["encoding"] == "cp932"
    assert result.statistics["encoding_source"] == "cp932-fallback"
    assert result.document["entities"][0]["text"] == "\u5bf8\u6cd5"


def test_utf8_dxf_remains_backward_compatible(tmp_path: Path) -> None:
    source = tmp_path / "utf8.dxf"
    source.write_text(_text_dxf(codepage=False), encoding="utf-8")

    result = convert_dxf_file_to_ir(source)

    assert result.encoding == "utf-8"
    assert result.document["entities"][0]["text"] == "\u5bf8\u6cd5"
    assert result.warnings == []


def test_decode_replacement_is_structured_and_machine_readable(
    tmp_path: Path,
) -> None:
    source = tmp_path / "damaged.dxf"
    raw = _text_dxf(codepage=True).replace("\u5bf8\u6cd5", "BROKEN").encode("cp932")
    source.write_bytes(raw.replace(b"BROKEN", b"bad\x81"))

    result = convert_dxf_file_to_ir(source, encoding="cp932")

    diagnostic = next(
        item for item in result.diagnostics if item.code == "DXF_DECODE_REPLACED"
    )
    assert diagnostic.details == {
        "encoding": "cp932",
        "replacement_characters": 1,
        "affected_lines": 1,
    }
    assert "\ufffd" in result.document["entities"][0]["text"]


def test_bom_takes_precedence_during_auto_detection(tmp_path: Path) -> None:
    source = tmp_path / "bom.dxf"
    source.write_bytes(codecs.BOM_UTF8 + _text_dxf(codepage=False).encode("utf-8"))

    result = convert_dxf_file_to_ir(source)

    assert result.encoding == "utf-8-sig"
    detected = next(
        item for item in result.diagnostics if item.code == "DXF_ENCODING_DETECTED"
    )
    assert detected.details == {"encoding": "utf-8-sig", "source": "bom"}


def _japanese_ir_document() -> dict:
    return {
        "format": "cad2d-ir",
        "version": "0.2.0",
        "header": {
            "units": "mm",
            "angle_unit": "deg",
            "coord_space": "world",
        },
        "entities": [
            {
                "id": "T1",
                "kind": "TEXT",
                "layer": "日本語レイヤ",
                "insert": [0, 0],
                "height": 2.5,
                "text": "寸法",
            }
        ],
    }


def test_r12_text_declares_ansi_932_codepage() -> None:
    dxf_text = ir_to_dxf(_japanese_ir_document(), target_version="AC1009")

    assert "\n$DWGCODEPAGE\n3\nANSI_932\n" in dxf_text
    assert "日本語レイヤ" in dxf_text
    assert "寸法" in dxf_text


@pytest.mark.parametrize("encoding", ["auto", "cp932"])
def test_r12_file_writer_emits_cp932_bytes(
    tmp_path: Path,
    encoding: str,
) -> None:
    output = tmp_path / f"r12-{encoding}.dxf"

    write_dxf_file(
        _japanese_ir_document(),
        output,
        target_version="AC1009",
        encoding=encoding,
    )

    raw = output.read_bytes()
    assert "日本語レイヤ".encode("cp932") in raw
    assert "寸法".encode("cp932") in raw
    assert "日本語レイヤ".encode("utf-8") not in raw

    imported = convert_dxf_file_to_ir(output)
    assert imported.encoding == "cp932"
    assert imported.document["entities"][0]["layer"] == "日本語レイヤ"
    assert imported.document["entities"][0]["text"] == "寸法"


def test_r12_file_writer_rejects_encoding_that_conflicts_with_codepage(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError, match="ANSI_932"):
        write_dxf_file(
            _japanese_ir_document(),
            tmp_path / "invalid.dxf",
            target_version="AC1009",
            encoding="utf-8",
        )


def test_text_escapes_outside_codepage_are_decoded() -> None:
    from cad2d_ir import convert_dxf_text_to_ir

    mif = "".join(f"\\M+1{ch.encode('cp932').hex().upper()}" for ch in "寸法")
    dxf_text = "\n".join(
        [
            "0",
            "SECTION",
            "2",
            "HEADER",
            "9",
            "$DWGCODEPAGE",
            "3",
            "ANSI_1252",
            "0",
            "ENDSEC",
            "0",
            "SECTION",
            "2",
            "ENTITIES",
            "0",
            "TEXT",
            "8",
            "\\U+56F3\\U+9762",
            "10",
            "0",
            "20",
            "0",
            "40",
            "1",
            "1",
            "\\U+65E5\\U+672C\\U+8A9E ABC",
            "0",
            "TEXT",
            "8",
            "0",
            "10",
            "0",
            "20",
            "0",
            "40",
            "1",
            "1",
            mif + " \\U+D83D",
            "0",
            "ENDSEC",
            "0",
            "EOF",
        ]
    )

    imported = convert_dxf_text_to_ir(dxf_text)
    entities = imported.document["entities"]
    assert entities[0]["layer"] == "図面"
    assert entities[0]["text"] == "日本語 ABC"
    # MIF (\M+1 = cp932) decodes; a lone surrogate escape stays literal
    assert entities[1]["text"] == "寸法 \\U+D83D"


def _versioned_dxf(version: str, *, codepage: str = "ANSI_932") -> str:
    return _text_dxf(codepage=False).replace(
        "HEADER\n",
        f"HEADER\n9\n$ACADVER\n1\n{version}\n9\n$DWGCODEPAGE\n3\n{codepage}\n",
        1,
    )


def test_ac1021_and_later_dxf_is_utf8_despite_the_declared_codepage(
    tmp_path: Path,
) -> None:
    # AutoCAD 2007+ writes UTF-8 and still declares the system codepage.
    source = tmp_path / "ac1032.dxf"
    source.write_text(_versioned_dxf("AC1032"), encoding="utf-8")

    result = convert_dxf_file_to_ir(source)

    assert result.encoding == "utf-8"
    assert result.document["entities"][0]["text"] == "\u5bf8\u6cd5"
    assert (
        result.document["entities"][0]["layer"]
        == "\u65e5\u672c\u8a9e\u30ec\u30a4\u30e4"
    )
    codes = [diagnostic.code for diagnostic in result.diagnostics]
    assert "DXF_DECODE_REPLACED" not in codes
    diagnostic = next(
        item for item in result.diagnostics if item.code == "DXF_ENCODING_DETECTED"
    )
    assert diagnostic.details == {
        "encoding": "utf-8",
        "source": "utf-8-probe ($DWGCODEPAGE=ANSI_932 ignored)",
    }


def test_declared_codepage_still_wins_when_the_bytes_are_not_utf8(
    tmp_path: Path,
) -> None:
    # Some writers emit AC1021+ headers with legacy-encoded text.
    modern = tmp_path / "ac1032-cp932.dxf"
    modern.write_bytes(_versioned_dxf("AC1032").encode("cp932"))
    legacy = tmp_path / "ac1015-cp932.dxf"
    legacy.write_bytes(_versioned_dxf("AC1015").encode("cp932"))

    for source in (modern, legacy):
        result = convert_file_to_ir(source)
        assert result.statistics["encoding"] == "cp932"
        assert result.statistics["encoding_source"] == "$DWGCODEPAGE=ANSI_932"
        assert result.document["entities"][0]["text"] == "\u5bf8\u6cd5"


def test_utf8_bytes_the_codepage_cannot_decode_are_read_as_utf8(
    tmp_path: Path,
) -> None:
    # Pre-AC1021 header, but the text is UTF-8 and not valid CP932.
    source = tmp_path / "ac1015-utf8.dxf"
    raw = _versioned_dxf("AC1015").encode("utf-8")
    with pytest.raises(UnicodeDecodeError):
        raw.decode("cp932")
    source.write_bytes(raw)

    result = convert_dxf_file_to_ir(source)

    assert result.encoding == "utf-8"
    assert result.document["entities"][0]["text"] == "\u5bf8\u6cd5"


def test_ascii_only_dxf_keeps_the_declared_codepage(tmp_path: Path) -> None:
    source = tmp_path / "ascii.dxf"
    text = _versioned_dxf("AC1032")
    text = text.replace("\u65e5\u672c\u8a9e\u30ec\u30a4\u30e4", "LAYER0").replace(
        "\u5bf8\u6cd5", "DIM"
    )
    source.write_text(text, encoding="ascii")

    result = convert_file_to_ir(source)

    assert result.statistics["encoding"] == "cp932"
    assert result.statistics["encoding_source"] == "$DWGCODEPAGE=ANSI_932"


def _texts_document(*texts: str, **layers: dict) -> dict:
    return {
        "format": "cad2d-ir",
        "version": "0.2.0",
        "header": {"units": "mm", "angle_unit": "deg", "coord_space": "world"},
        "tables": {"layers": {"0": {}, **layers}},
        "entities": [
            {
                "id": f"T{index}",
                "kind": "TEXT",
                "layer": "0",
                "insert": [0.0, 10.0 * index],
                "height": 5.0,
                "text": text,
            }
            for index, text in enumerate(texts)
        ],
    }


def _text_values(dxf_text: str) -> list[str]:
    lines = dxf_text.splitlines()
    return [
        lines[index + 1]
        for index in range(0, len(lines) - 1, 2)
        if lines[index].strip() == "1" and lines[index + 1] != "AC1009"
    ]


@pytest.mark.parametrize(
    ("texts", "codepage", "encoding", "written"),
    [
        # Japanese and plain ASCII drawings stay in ANSI_932, as R12 always was.
        (["図面 カナ"], "ANSI_932", "cp932", ["図面 カナ"]),
        (["PLAIN"], "ANSI_932", "cp932", ["PLAIN"]),
        # Simplified Chinese asks for GBK, Hangul for ANSI_949.
        (["技术要求 图样"], "ANSI_936", "gbk", ["技术要求 图样"]),
        (["도면"], "ANSI_949", "cp949", ["도면"]),
        # Latin and Cyrillic text takes its own Windows code page.
        (["café Ørsted"], "ANSI_1252", "cp1252", ["café Ørsted"]),
        (["Чертёж"], "ANSI_1251", "cp1251", ["Чертёж"]),
    ],
)
def test_r12_code_page_follows_the_characters_of_the_drawing(
    texts: list[str], codepage: str, encoding: str, written: list[str], tmp_path: Path
) -> None:
    from cad2d_ir import convert_ir_to_dxf_text, dxf_text_encoding

    result = convert_ir_to_dxf_text(_texts_document(*texts), target_version="AC1009")

    assert f"\n$DWGCODEPAGE\n3\n{codepage}\n" in result.dxf_text
    assert result.encoding == dxf_text_encoding(result.dxf_text) == encoding
    assert _text_values(result.dxf_text) == written
    codes = [diagnostic.code for diagnostic in result.diagnostics]
    assert ("DXF_R12_CODEPAGE_SELECTED" in codes) == (codepage != "ANSI_932")
    assert "DXF_R12_CHARACTERS_REPLACED" not in codes

    # The file writer stores the text in that code page, and it reads back.
    output = tmp_path / "r12.dxf"
    write_dxf_file(_texts_document(*texts), output, target_version="AC1009")
    assert output.read_bytes() == result.dxf_text.encode(encoding)
    imported = convert_dxf_file_to_ir(output)
    assert [entity["text"] for entity in imported.document["entities"]] == written
    # An explicit encoding has to be the one the file declares.
    if encoding != "cp932":
        with pytest.raises(ValueError, match=codepage):
            write_dxf_file(
                _texts_document(*texts),
                tmp_path / "invalid.dxf",
                target_version="AC1009",
                encoding="cp932",
            )


def test_r12_symbols_become_codes_and_lost_characters_are_reported() -> None:
    from cad2d_ir import convert_ir_to_dxf_text

    document = _texts_document(
        "⌀25 ∅3 Ø8 ±0.1 45°", "カナ é ✓", "A", **{"層 é": {}, "層 è": {}, "層 _": {}}
    )
    document["entities"][1]["layer"] = "層 é"
    document["entities"][2]["layer"] = "層 è"
    result = convert_ir_to_dxf_text(document, target_version="AC1009")

    # No legacy code page has the diameter sign: every CAD draws %%c as one.
    # ANSI_932 has ± and ° but not the letter Ø, which is a diameter there.
    assert _text_values(result.dxf_text) == [
        "%%c25 %%c3 %%c8 ±0.1 45°",
        "カナ ? ?",
        "A",
    ]
    # A name keeps the characters the code page has, takes "_" for the others
    # ("?" is no character of a name) and stays a name of its own: here three
    # layers would all be called "層 _".
    lines = result.dxf_text.splitlines()
    layers = [
        lines[index + 1]
        for index in range(0, len(lines) - 1, 2)
        if lines[index].strip() == "8"
    ]
    # The layer table comes first and lists "層 è" before "層 é".
    assert layers == ["0", "層 __3", "層 __2"]
    tables = result.dxf_text[: result.dxf_text.index("ENTITIES")]
    assert all(f"\n{name}\n" in tables for name in ("層 _", "層 __2", "層 __3"))
    replaced = [
        diagnostic
        for diagnostic in result.diagnostics
        if diagnostic.code == "DXF_R12_CHARACTERS_REPLACED"
    ]
    assert len(replaced) == 1 and replaced[0].message.startswith("6 character(s)")
    # The text is storable as it is.
    assert result.dxf_text.encode(result.encoding)

    # In a code page without them, ± and ° become their codes too.
    korean = convert_ir_to_dxf_text(
        _texts_document("도면 ⌀5", "Ø ± °"), target_version="AC1009"
    )
    values = _text_values(korean.dxf_text)
    assert values[0] == "도면 %%c5"
    assert korean.dxf_text.encode(korean.encoding)

    # DXF R2010 is UTF-8 and keeps every character.
    modern = convert_ir_to_dxf_text(document, target_version="AC1024")
    assert modern.encoding == "utf-8"
    assert "⌀25 ∅3 Ø8 ±0.1 45°" in modern.dxf_text and "カナ é ✓" in modern.dxf_text


def test_r12_keeps_a_similar_character_where_it_can() -> None:
    from cad2d_ir import convert_ir_to_dxf_text

    # GBK has neither the superscript two nor the micro sign, but it has 2 and
    # the Greek mu: "mm²" is written "mm2", not "mm?". A check mark has no
    # such form and becomes "?".
    document = _texts_document("技术要求 mm² µm ✓", **{"面积m²": {}})
    document["entities"][0]["layer"] = "面积m²"
    result = convert_ir_to_dxf_text(document, target_version="AC1009")

    assert result.encoding == "gbk"
    assert _text_values(result.dxf_text) == ["技术要求 mm2 μm ?"]
    assert result.dxf_text.count("\n面积m2\n") == 2
    (replaced,) = [
        diagnostic
        for diagnostic in result.diagnostics
        if diagnostic.code == "DXF_R12_CHARACTERS_REPLACED"
    ]
    # Three superscript twos (the text, the layer and its reference), the micro
    # sign and the check mark; four of them have a similar character.
    assert replaced.message.startswith("5 character(s)")
    assert "similar character (4," in replaced.message


def test_r12_font_files_lose_characters_without_a_number() -> None:
    from cad2d_ir import convert_ir_to_dxf_text

    # A Japanese drawing keeps ANSI_932; a font file named in simplified
    # Chinese loses those characters but keeps its extension, and two such
    # files may well end up with the same name.
    document = _texts_document("カナ")
    document["tables"]["text_styles"] = {
        "A": {"font": "这们.ttf"},
        "B": {"font": "们这.ttf"},
    }
    result = convert_ir_to_dxf_text(document, target_version="AC1009")
    assert result.encoding == "cp932"
    assert result.dxf_text.count("\n__.ttf\n") == 2


@pytest.mark.parametrize("target_version", ["AC1009", "AC1024"])
def test_percent_signs_that_are_no_control_code_stay_percent_signs(
    target_version: str,
) -> None:
    from cad2d_ir import convert_ir_to_dxf_text

    # "%%" starts a control code; one that is none is two percent signs, which
    # AutoCAD and ODA would drop. They are written as "%%%" each. Control codes
    # read from DXF stay as they are.
    document = _texts_document("120±5%%", "50%% off", "%%c10 %%%", "%%u%%123")
    result = convert_ir_to_dxf_text(document, target_version=target_version)
    lines = result.dxf_text.splitlines()
    texts, record = [], ""
    for index in range(0, len(lines) - 1, 2):
        code, value = lines[index].strip(), lines[index + 1]
        if code == "0":
            record = value
        elif code == "1" and record == "TEXT":
            texts.append(value)
    assert texts == ["120±5%%%%%%", "50%%%%%% off", "%%c10 %%%", "%%u%%123"]


@pytest.mark.parametrize("target_version", ["AC1009", "AC1024"])
def test_names_lose_the_characters_autocad_does_not_allow(target_version: str) -> None:
    from cad2d_ir import convert_ir_to_dxf_text

    # Applications keep layers named "*…" in DWG; AutoCAD and ODA reject such a
    # name in DXF, and the entities on the layer were not shown.
    document = _texts_document("A", "B", **{"*SYSTEM": {}, "_SYSTEM": {}})
    document["entities"][0]["layer"] = "*SYSTEM"
    document["entities"][1]["layer"] = "_SYSTEM"
    result = convert_ir_to_dxf_text(document, target_version=target_version)
    lines = result.dxf_text.splitlines()
    layers = [
        lines[index + 1]
        for index in range(0, len(lines) - 1, 2)
        if lines[index].strip() == "8"
    ]
    # The renamed layer keeps apart from the one that already had the name.
    assert "*SYSTEM" not in result.dxf_text
    assert "_SYSTEM_2" in layers and "_SYSTEM" in layers
    assert [
        d.code for d in result.diagnostics if d.code == "DXF_SYMBOL_NAME_NORMALIZED"
    ] == ["DXF_SYMBOL_NAME_NORMALIZED"]


def test_carets_and_line_breaks_use_the_caret_notation() -> None:
    from cad2d_ir import convert_ir_to_dxf_text, dxf_to_ir

    # AutoCAD and ODA read "^" and the character after it as one control
    # character, so a caret is written "^ " and a line break "^J".
    document = _texts_document("A^B", "2^J")
    document["entities"].append(
        {
            "id": "M",
            "kind": "MTEXT",
            "layer": "0",
            "insert": [0.0, 50.0],
            "height": 5.0,
            "text": "{\\H0.7x;\\S+0.1^-0.2;}",
        }
    )
    modern = convert_ir_to_dxf_text(document, target_version="AC1024").dxf_text
    assert "\nA^ B\n" in modern and "\n2^ J\n" in modern
    assert "\\S+0.1^ -0.2;" in modern
    texts = {entity["id"]: entity["text"] for entity in dxf_to_ir(modern)["entities"]}
    assert list(texts.values()) == ["A^B", "2^J", "{\\H0.7x;\\S+0.1^-0.2;}"]

    # A reader undoes "^ ", "^J", "^M" and "^I" only: writers that do not
    # escape the caret put stacks such as \S1^2; into the text.
    lines = [
        *("0", "SECTION", "2", "ENTITIES"),
        *("0", "MTEXT", "8", "0", "10", "0", "20", "0", "40", "1", "1", "\\S1^ 2;"),
        *("0", "MTEXT", "8", "0", "10", "0", "20", "9", "40", "1", "1", "\\S1^2;"),
        *("0", "TEXT", "8", "0", "10", "0", "20", "18", "40", "1", "1", "A^IB^JC"),
        *("0", "ENDSEC", "0", "EOF"),
    ]
    read = dxf_to_ir("\n".join(lines) + "\n")["entities"]
    assert [entity["text"] for entity in read] == ["\\S1^2;", "\\S1^2;", "A\tB\nC"]


@pytest.mark.parametrize(
    ("codepage", "encoding", "text"),
    [
        ("ANSI_936", "gbk", "技术要求"),
        ("ANSI_949", "cp949", "도면"),
        ("ANSI_950", "cp950", "圖樣"),
        ("ANSI_1251", "cp1251", "Чертёж"),
    ],
)
def test_legacy_dxf_is_read_in_the_code_page_it_names(
    codepage: str, encoding: str, text: str, tmp_path: Path
) -> None:
    # A DXF before AutoCAD 2007 is stored in the Windows code page of its author.
    source = tmp_path / "legacy.dxf"
    lines = [
        *("0", "SECTION", "2", "HEADER"),
        *("9", "$ACADVER", "1", "AC1015"),
        *("9", "$DWGCODEPAGE", "3", codepage),
        *("0", "ENDSEC", "0", "SECTION", "2", "ENTITIES"),
        *("0", "TEXT", "8", "0", "10", "0", "20", "0", "40", "2.5", "1", text),
        *("0", "ENDSEC", "0", "EOF"),
    ]
    source.write_bytes(("\n".join(lines) + "\n").encode(encoding))

    imported = convert_dxf_file_to_ir(source)

    assert codecs.lookup(imported.encoding).name == codecs.lookup(encoding).name
    assert imported.document["entities"][0]["text"] == text


def test_justified_text_starts_where_its_cells_start() -> None:
    # A reader that ignores the justification (Jw_cad) draws a text from its
    # first point. It is the left end of the baseline of a text of that many
    # character cells; the second point stays the point of the justification.
    def text(index: int, value: str, **extra: object) -> dict:
        return {
            "id": f"T{index}",
            "kind": "TEXT",
            "insert": [100.0, 20.0 * index],
            "height": 5.0,
            "text": value,
            **extra,
        }

    document = {
        "format": "cad2d-ir",
        "version": "0.2.0",
        "header": {"units": "mm", "angle_unit": "deg", "coord_space": "world"},
        "tables": {"layers": {"0": {}}},
        "entities": [
            text(0, "LEFT"),
            # six half-width characters are three cells of the text height
            text(1, "CENTER", halign="center"),
            # full-width characters are one cell each, and a symbol code too
            text(2, "図面%%c", halign="right"),
            text(3, "WIDE", halign="center", width_factor=2.0),
            # top: the baseline lies one height below the point; rotated by 90
            # degrees that is to the right of it
            text(4, "TOP", halign="right", valign="top", rotation=90.0),
            text(5, "MID", valign="middle"),
        ],
    }
    for version in ("AC1009", "AC1024"):
        lines = ir_to_dxf(document, target_version=version).splitlines()
        pairs = list(zip(lines[0::2], lines[1::2]))
        records: list[dict[str, str]] = []
        for code, value in pairs:
            if code.strip() == "0":
                records.append({"0": value})
            elif records:
                records[-1].setdefault(code.strip(), value)
        points = {
            record["1"]: (
                float(record["10"]),
                float(record["20"]),
                float(record.get("11", "nan")),
                float(record.get("21", "nan")),
            )
            for record in records
            if record["0"] == "TEXT"
        }
        assert points["LEFT"][:2] == (100.0, 0.0)
        assert points["CENTER"] == (92.5, 20.0, 100.0, 20.0)
        assert points["図面%%c"] == (85.0, 40.0, 100.0, 40.0)
        # four half-width characters at twice their width: 20 wide
        assert points["WIDE"] == (90.0, 60.0, 100.0, 60.0)
        assert points["TOP"] == pytest.approx((105.0, 72.5, 100.0, 80.0))
        assert points["MID"] == (100.0, 97.5, 100.0, 100.0)

    # Reading it back puts the point of the justification into insert.
    from cad2d_ir.codecs.dxf import dxf_to_ir

    restored = dxf_to_ir(ir_to_dxf(document))
    assert [entity["insert"] for entity in restored["entities"]] == [
        [100.0, 20.0 * index] for index in range(6)
    ]
