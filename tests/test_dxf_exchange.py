"""What a DXF needs so that the program at the other end opens it well.

Found by opening converted drawings in Jw_cad 10.02.1:

- the extents of model space (``$EXTMIN``, ``$EXTMAX``), from which Jw_cad
  chooses the scale of the drawing;
- no linetype on an entity that has the linetype of its layer (Jw_cad draws an
  entity that states ``BYLAYER`` with a solid line);
- CR and LF at the end of the lines of a file (Jw_cad reads no entity of a
  UTF-8 DXF with Japanese names whose lines end with a line feed alone);
- a solid fill that says it is one, for the consumers that fill nothing else.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from cad2d_ir import (
    convert_dxf_file_to_ir,
    dxf_text_encoding,
    dxf_text_to_bytes,
    dxf_to_ir,
    ir_to_dxf,
    write_dxf_file,
)


def _document(*entities: dict[str, Any], **tables: Any) -> dict[str, Any]:
    return {
        "format": "cad2d-ir",
        "version": "0.2.0",
        "header": {"units": "mm", "angle_unit": "deg", "coord_space": "world"},
        "tables": {
            "layers": {
                "0": {},
                "壁": {},
                "通り芯": {"linetype": "CENTER"},
            },
            **tables,
        },
        "entities": [
            {"id": f"E{index}", "layer": "0", **entity}
            for index, entity in enumerate(entities)
        ],
    }


def _pairs(dxf_text: str) -> list[tuple[str, str]]:
    lines = dxf_text.splitlines()
    return list(zip(lines[0::2], lines[1::2], strict=True))


def _header_point(dxf_text: str, name: str) -> tuple[float, float, float]:
    pairs = _pairs(dxf_text)
    index = pairs.index(("9", name))
    codes = [code for code, _ in pairs[index + 1 : index + 4]]
    assert codes == ["10", "20", "30"]
    x, y, z = (float(value) for _, value in pairs[index + 1 : index + 4])
    return x, y, z


def _line(p1: list[float], p2: list[float], **more: Any) -> dict[str, Any]:
    return {"kind": "LINE", "p1": p1, "p2": p2, **more}


# --- the extents of model space ------------------------------------------


@pytest.mark.parametrize("target_version", ["AC1024", "AC1009"])
def test_the_header_states_the_extents_of_model_space(target_version: str) -> None:
    document = _document(
        _line([1000.0, 2000.0], [38000.0, 2000.0]),
        {"kind": "CIRCLE", "center": [5000.0, 20000.0], "radius": 6500.0},
        # a quarter of a circle: as far as that quarter reaches, not its circle
        {
            "kind": "ARC",
            "center": [38000.0, 2000.0],
            "radius": 900.0,
            "start_angle": 0.0,
            "end_angle": 90.0,
        },
    )
    text = ir_to_dxf(document, target_version=target_version)

    assert _header_point(text, "$EXTMIN") == (-1500.0, 2000.0, 0.0)
    assert _header_point(text, "$EXTMAX") == (38900.0, 26500.0, 0.0)
    # right behind the version, before anything a reader may stop at
    assert _pairs(text)[2:4] == [("9", "$ACADVER"), ("1", target_version)]
    assert _pairs(text)[4] == ("9", "$EXTMIN")


def test_the_extents_take_in_what_a_block_reference_shows() -> None:
    document = _document(
        {"kind": "INSERT", "block": "DOOR", "insert": [100.0, 50.0], "rotation": 90.0},
        blocks={
            "DOOR": {
                "base_point": [0.0, 0.0],
                "entities": [
                    {
                        "id": "B1",
                        "kind": "LINE",
                        "layer": "0",
                        "p1": [0.0, 0.0],
                        "p2": [8.0, 1.0],
                    }
                ],
            }
        },
    )
    text = ir_to_dxf(document)

    # (0..8, 0..1) turned a quarter around the insertion point
    assert _header_point(text, "$EXTMIN") == pytest.approx((99.0, 50.0, 0.0))
    assert _header_point(text, "$EXTMAX") == pytest.approx((100.0, 58.0, 0.0))


def test_a_drawing_without_geometry_states_no_extents() -> None:
    assert "$EXTMIN" not in ir_to_dxf(_document())


def test_the_extents_do_not_change_what_is_read_back() -> None:
    document = _document(_line([0.0, 0.0], [10.0, 5.0]))
    again = dxf_to_ir(ir_to_dxf(document))
    assert [entity["kind"] for entity in again["entities"]] == ["LINE"]


# --- the linetype of the layer -------------------------------------------


@pytest.mark.parametrize("name", ["BYLAYER", "ByLayer", "bylayer"])
@pytest.mark.parametrize("target_version", ["AC1024", "AC1009"])
def test_the_linetype_of_the_layer_is_not_written(
    name: str, target_version: str
) -> None:
    document = _document(
        _line([0.0, 0.0], [10.0, 0.0], layer="通り芯", linetype=name),
    )
    diagnostics: list[Any] = []
    text = ir_to_dxf(document, target_version=target_version, diagnostics=diagnostics)
    pairs = _pairs(text)

    start = pairs.index(("0", "LINE"))
    entity = pairs[start : pairs.index(("0", "ENDSEC"), start)]
    assert ("8", "通り芯") in entity
    assert "6" not in [code for code, _ in entity]
    # and no linetype of that name is made up for the table
    names = [
        value for code, value in pairs if code == "2" and value.upper() == name.upper()
    ]
    assert names in ([], ["ByLayer"])
    assert not any(name in diagnostic.message for diagnostic in diagnostics)


def test_other_linetypes_are_written() -> None:
    document = _document(
        _line([0.0, 0.0], [10.0, 0.0], linetype="CENTER"),
        _line([0.0, 5.0], [10.0, 5.0], linetype="BYBLOCK"),
    )
    pairs = _pairs(ir_to_dxf(document))
    start = pairs.index(("2", "ENTITIES"))
    assert [value for code, value in pairs[start:] if code == "6"] == [
        "CENTER",
        "BYBLOCK",
    ]


# --- the six pure colours -------------------------------------------------


@pytest.mark.parametrize("target_version", ["AC1024", "AC1009"])
def test_a_pure_colour_is_written_as_its_number(target_version: str) -> None:
    # Jw_cad shows a pure red and a pure blue of group 420 as each other.
    document = _document(
        _line([0.0, 0.0], [10.0, 0.0], color="#FF0000"),
        _line([0.0, 5.0], [10.0, 5.0], color="#0000ff"),
        _line([0.0, 9.0], [10.0, 9.0], color="#FF8000"),
    )
    document["tables"]["layers"]["壁"] = {"color": "#00FFFF"}
    diagnostics: list[Any] = []
    text = ir_to_dxf(document, target_version=target_version, diagnostics=diagnostics)
    pairs = _pairs(text)

    start = pairs.index(("2", "ENTITIES"))
    colours = [(code, value) for code, value in pairs[start:] if code in {"62", "420"}]
    if target_version == "AC1024":
        assert colours == [("62", "1"), ("62", "5"), ("420", str(0xFF8000))]
    else:
        # DXF R12 has numbers only: orange becomes the nearest of them, yellow
        assert colours == [("62", "1"), ("62", "5"), ("62", "2")]
    layer = pairs.index(("2", "壁"))
    assert ("62", "4") in pairs[layer : layer + 4]
    assert ("420", str(0x00FFFF)) not in pairs
    # a pure colour is exact as a number: nothing was approximated for it
    approximated = [
        d for d in diagnostics if d.code == "DXF_R12_TRUE_COLOR_APPROXIMATED"
    ]
    assert len(approximated) == (1 if target_version == "AC1009" else 0)

    again = dxf_to_ir(text)
    assert [entity.get("color") for entity in again["entities"]][:2] == [1, 5]
    assert again["tables"]["layers"]["壁"]["color"] == 4


# --- the bytes of a file --------------------------------------------------


def test_the_bytes_of_a_file_end_their_lines_with_cr_and_lf(tmp_path: Path) -> None:
    document = _document(
        _line([0.0, 0.0], [10.0, 0.0], layer="壁"),
        {"kind": "TEXT", "insert": [0.0, 0.0], "height": 3.0, "text": "寸法"},
    )
    text = ir_to_dxf(document)
    # the text has the line feed of a Python text, for a caller that writes it
    # in text mode
    assert "\r" not in text

    data = dxf_text_to_bytes(text)
    assert data == text.replace("\n", "\r\n").encode("utf-8")
    assert dxf_text_to_bytes(data.decode("utf-8")) == data  # once is enough

    output = tmp_path / "drawing.dxf"
    write_dxf_file(document, output)
    assert output.read_bytes() == data
    again = convert_dxf_file_to_ir(output).document
    assert [entity["layer"] for entity in again["entities"]] == ["壁", "0"]


def test_the_bytes_of_an_r12_file_are_in_its_code_page(tmp_path: Path) -> None:
    document = _document(
        {"kind": "TEXT", "insert": [0.0, 0.0], "height": 3.0, "text": "寸法"},
    )
    text = ir_to_dxf(document, target_version="AC1009")
    assert dxf_text_encoding(text) == "cp932"

    data = dxf_text_to_bytes(text)
    assert "寸法".encode("cp932") in data and "寸法".encode() not in data
    assert data.count(b"\n") == data.count(b"\r\n") > 0
    assert dxf_text_to_bytes(text, "cp932") == data


# --- solid fills ----------------------------------------------------------


def test_a_solid_fill_read_from_dxf_says_so() -> None:
    hatch = [
        "0", "HATCH", "5", "H1", "8", "0", "2", "SOLID", "70", "1", "71", "0",
        "91", "1", "92", "3", "72", "0", "73", "1", "93", "3",
        "10", "0", "20", "0", "10", "10", "20", "0", "10", "10", "20", "5",
        "97", "0", "75", "1", "76", "1", "98", "0",
    ]  # fmt: skip
    solid = [
        "0", "SOLID", "5", "S1", "8", "0",
        "10", "0", "20", "0", "11", "4", "21", "0", "12", "2", "22", "3", "13", "2", "23", "3",
    ]  # fmt: skip
    text = "\n".join(
        ["0", "SECTION", "2", "ENTITIES", *hatch, *solid, "0", "ENDSEC", "0", "EOF", ""]
    )

    filled, arrowhead = dxf_to_ir(text)["entities"]
    assert filled["kind"] == arrowhead["kind"] == "HATCH"
    assert filled["solid"] is True and arrowhead["solid"] is True
