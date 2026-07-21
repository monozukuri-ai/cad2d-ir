from __future__ import annotations

import codecs
from pathlib import Path

from cad2d_ir import convert_dxf_file_to_ir, convert_file_to_ir


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
