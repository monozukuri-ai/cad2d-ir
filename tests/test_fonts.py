from __future__ import annotations

import pytest

from cad2d_ir import convert_ir_to_dxf_text
from cad2d_ir.codecs.dxf import dxf_to_ir
from cad2d_ir.fonts import StyleFont, face_name, style_font
from cad2d_ir.schema import validate_ir


@pytest.mark.parametrize(
    ("font", "expected"),
    [
        # Jw_cad and SXF name Windows faces; AutoCAD needs the file.
        ("ＭＳ ゴシック", StyleFont("msgothic.ttc", "MS Gothic", 0x8031)),
        ("ＭＳ Ｐゴシック", StyleFont("msgothic.ttc", "MS PGothic", 0x8032)),
        ("ＭＳ 明朝", StyleFont("msmincho.ttc", "MS Mincho", 0x8011)),
        ("@ＭＳ ゴシック", StyleFont("msgothic.ttc", "@MS Gothic", 0x8031)),
        ("HGｺﾞｼｯｸM", StyleFont("HGRGM.TTC", "HGGothicM", 0x8031)),
        ("BIZ UDPゴシック", StyleFont("BIZ-UDGothicR.ttc", "BIZ UDPGothic", 0x8032)),
        ("Arial", StyleFont("arial.ttf", "Arial", 0x22)),
        ("Arial Bold", StyleFont("arial.ttf", "Arial", 0x22)),
        ("ABCDEF+MS-Gothic", StyleFont("msgothic.ttc", "MS Gothic", 0x8031)),
        # Some CAD programs write the family with a .ttf suffix.
        ("MS PGothic.ttf", StyleFont("msgothic.ttc", "MS PGothic", 0x8032)),
        ("msmincho.ttc", StyleFont("msmincho.ttc", "MS Mincho", 0x8011)),
        # SHX fonts and unknown font files pass through.
        ("txt", StyleFont("txt")),
        ("romans.shx", StyleFont("romans.shx")),
        ("swisscl.ttf", StyleFont("swisscl.ttf", None, 0)),
        ("STANDARD", StyleFont("txt")),
        (None, StyleFont("txt")),
    ],
)
def test_style_font_names_the_file_autocad_looks_for(
    font: str | None, expected: StyleFont
) -> None:
    assert style_font(font) == expected


def test_style_font_keeps_an_unknown_face_as_the_family() -> None:
    # The file still draws the script when the face is not installed.
    assert style_font("FCフォント") == StyleFont("msgothic.ttc", "FCフォント", 0x8000)
    assert style_font("仿宋_GB2312") == StyleFont("simfang.ttf", "FangSong", 0x8631)
    assert style_font("Swis721 BT") == StyleFont("Swis721 BT.ttf", "Swis721 BT", 0)


def test_style_font_prefers_the_family_of_the_xdata() -> None:
    # An AutoCAD 2000 style may name only the family; a .ttc names its first face.
    assert style_font("", "MS PGothic") == StyleFont(
        "msgothic.ttc", "MS PGothic", 0x8032
    )
    assert style_font("msgothic.ttc", "MS UI Gothic") == StyleFont(
        "msgothic.ttc", "MS UI Gothic", 0x8032
    )


@pytest.mark.parametrize(
    ("font", "family", "expected"),
    [
        ("msgothic.ttc", None, "ＭＳ ゴシック"),
        ("msgothic.ttc", "MS PGothic", "ＭＳ Ｐゴシック"),
        ("MS Gothic.ttf", "MS Gothic", "ＭＳ ゴシック"),
        ("@MS PGothic.ttf", "@MS PGothic", "@ＭＳ Ｐゴシック"),
        ("ＭＳ 明朝", None, "ＭＳ 明朝"),
        ("FCフォント", None, "FCフォント"),
        ("swisscl.ttf", None, "swisscl"),
        ("txt", None, None),
        ("romans.shx", None, None),
        ("STANDARD", None, None),
    ],
)
def test_face_name_is_the_inverse_for_face_based_formats(
    font: str, family: str | None, expected: str | None
) -> None:
    assert face_name(font, family) == expected


def _document(text_styles: dict) -> dict:
    return {
        "format": "cad2d-ir",
        "version": "0.3.0",
        "header": {"units": "mm", "angle_unit": "deg", "coord_space": "world"},
        "tables": {"text_styles": text_styles},
        "entities": [
            {
                "id": f"T{index}",
                "kind": "TEXT",
                "insert": [0, index * 10],
                "height": 3.5,
                "text": "日本語",
                "style": name,
            }
            for index, name in enumerate(text_styles)
        ],
    }


def _style_records(dxf_text: str) -> dict[str, list[tuple[str, str]]]:
    lines = [line.strip() for line in dxf_text.splitlines()]
    pairs = list(zip(lines[::2], lines[1::2], strict=False))
    records: dict[str, list[tuple[str, str]]] = {}
    for index, (code, value) in enumerate(pairs):
        if code == "0" and value == "STYLE":
            end = index + 1
            while pairs[end][0] != "0":
                end += 1
            body = pairs[index + 1 : end]
            records[dict(body)["2"]] = body
    return records


def test_dxf_writes_the_font_file_and_the_family_xdata() -> None:
    document = _document(
        {
            "ＭＳ Ｐゴシック": {"font": "ＭＳ Ｐゴシック"},
            "JIS": {"font": "txt", "big_font": "extfont2.shx"},
        }
    )
    dxf_text = convert_ir_to_dxf_text(document).dxf_text
    records = _style_records(dxf_text)

    gothic = records["ＭＳ Ｐゴシック"]
    assert ("3", "msgothic.ttc") in gothic
    assert gothic[-3:] == [("1001", "ACAD"), ("1000", "MS PGothic"), ("1071", "32818")]
    assert "AcDbRegAppTableRecord\n2\nACAD" in dxf_text
    shx = records["JIS"]
    assert ("3", "txt") in shx
    assert ("4", "extfont2.shx") in shx
    assert ("1001", "ACAD") not in shx

    reparsed = dxf_to_ir(dxf_text, validate=True)
    validate_ir(reparsed, strict_jsonschema=True)
    styles = reparsed["tables"]["text_styles"]
    assert styles["ＭＳ Ｐゴシック"]["font"] == "msgothic.ttc"
    assert styles["ＭＳ Ｐゴシック"]["family"] == "MS PGothic"
    assert styles["JIS"]["big_font"] == "extfont2.shx"
    assert "family" not in styles["JIS"]


def test_dxf_r12_names_the_file_without_xdata() -> None:
    dxf_text = convert_ir_to_dxf_text(
        _document({"ＭＳ 明朝": {"font": "ＭＳ 明朝"}}), target_version="AC1009"
    ).dxf_text
    assert ("3", "msmincho.ttc") in _style_records(dxf_text)["ＭＳ 明朝"]
    assert "1001" not in dxf_text.split()
    assert "APPID" not in dxf_text


def test_dxf_import_reads_the_family_of_a_style_without_a_file() -> None:
    # AutoCAD 2000 leaves group 3 empty and keeps only the family.
    pairs = [
        (0, "SECTION"), (2, "TABLES"),
        (0, "TABLE"), (2, "STYLE"), (70, "1"),
        (0, "STYLE"), (2, "NOTE"), (70, "0"), (40, "0"), (41, "1"), (3, ""), (4, ""),
        (1001, "AcadAnnotative"), (1000, "AnnotativeData"),
        (1001, "ACAD"), (1000, "MS PGothic"), (1071, "32818"),
        (0, "ENDTAB"), (0, "ENDSEC"),
        (0, "SECTION"), (2, "ENTITIES"), (0, "ENDSEC"), (0, "EOF"),
    ]  # fmt: skip
    dxf_text = "".join(f"{code}\n{value}\n" for code, value in pairs)
    style = dxf_to_ir(dxf_text)["tables"]["text_styles"]["NOTE"]
    assert "font" not in style
    assert style["family"] == "MS PGothic"
    assert style_font(style.get("font"), style["family"]).file == "msgothic.ttc"
