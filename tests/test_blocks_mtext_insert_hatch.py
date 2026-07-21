from __future__ import annotations

import pytest

from cad2d_ir.codecs.dxf import dxf_to_ir, ir_to_dxf


def _entity_by_kind(document: dict, kind: str) -> dict:
    return next(entity for entity in document["entities"] if entity["kind"] == kind)


def test_dxf_to_ir_parses_blocks_mtext_insert_hatch() -> None:
    dxf_text = "\n".join(
        [
            "0",
            "SECTION",
            "2",
            "HEADER",
            "9",
            "$INSUNITS",
            "70",
            "4",
            "0",
            "ENDSEC",
            "0",
            "SECTION",
            "2",
            "BLOCKS",
            "0",
            "BLOCK",
            "2",
            "B-BASE",
            "3",
            "B-BASE",
            "8",
            "0",
            "10",
            "1",
            "20",
            "2",
            "70",
            "0",
            "0",
            "LINE",
            "10",
            "0",
            "20",
            "0",
            "11",
            "1",
            "21",
            "1",
            "0",
            "ENDBLK",
            "8",
            "0",
            "0",
            "ENDSEC",
            "0",
            "SECTION",
            "2",
            "ENTITIES",
            "0",
            "MTEXT",
            "10",
            "5",
            "20",
            "6",
            "40",
            "2",
            "71",
            "5",
            "3",
            "MAIN-",
            "1",
            "TXT",
            "0",
            "INSERT",
            "2",
            "B-BASE",
            "10",
            "10",
            "20",
            "20",
            "41",
            "2",
            "42",
            "1",
            "66",
            "1",
            "0",
            "ATTRIB",
            "2",
            "TAG1",
            "1",
            "VALUE1",
            "10",
            "10",
            "20",
            "20",
            "40",
            "1",
            "0",
            "SEQEND",
            "0",
            "HATCH",
            "70",
            "1",
            "2",
            "SOLID",
            "91",
            "1",
            "92",
            "2",
            "72",
            "1",
            "73",
            "1",
            "93",
            "4",
            "10",
            "0",
            "20",
            "0",
            "42",
            "0",
            "10",
            "3",
            "20",
            "0",
            "42",
            "0",
            "10",
            "3",
            "20",
            "2",
            "42",
            "0",
            "10",
            "0",
            "20",
            "2",
            "42",
            "0",
            "75",
            "0",
            "76",
            "1",
            "98",
            "0",
            "0",
            "ENDSEC",
            "0",
            "EOF",
            "",
        ]
    )

    ir = dxf_to_ir(dxf_text, validate=True)

    assert "tables" in ir
    assert "blocks" in ir["tables"]
    block = ir["tables"]["blocks"]["B-BASE"]
    assert block["base_point"] == pytest.approx([1.0, 2.0])
    assert block["entities"][0]["kind"] == "LINE"

    kinds = [entity["kind"] for entity in ir["entities"]]
    assert kinds == ["MTEXT", "INSERT", "HATCH"]

    mtext = _entity_by_kind(ir, "MTEXT")
    assert mtext["text"] == "MAIN-TXT"
    assert mtext["attach"] == "middle_center"

    insert = _entity_by_kind(ir, "INSERT")
    assert insert["block"] == "B-BASE"
    assert insert["scale"] == pytest.approx([2.0, 1.0])
    assert insert["attributes"] == {"TAG1": "VALUE1"}

    hatch = _entity_by_kind(ir, "HATCH")
    assert len(hatch["loops"]) == 1
    assert len(hatch["loops"][0]["vertices"]) == 4


def test_ir_to_dxf_roundtrip_with_blocks_mtext_insert_hatch() -> None:
    ir_document = {
        "format": "cad2d-ir",
        "version": "0.1.0",
        "header": {"units": "mm", "angle_unit": "deg", "coord_space": "world"},
        "tables": {
            "blocks": {
                "SYM1": {
                    "base_point": [0, 0],
                    "entities": [
                        {"id": "B1", "kind": "LINE", "p1": [0, 0], "p2": [1, 0]},
                    ],
                }
            }
        },
        "entities": [
            {
                "id": "E1",
                "kind": "MTEXT",
                "insert": [1, 2],
                "height": 0.5,
                "text": "NOTE",
                "attach": "bottom_right",
                "width": 8.0,
            },
            {
                "id": "E2",
                "kind": "INSERT",
                "block": "SYM1",
                "insert": [5, 6],
                "scale": [2.0, 1.5],
                "attributes": {"TAG": "VALUE"},
            },
            {
                "id": "E3",
                "kind": "HATCH",
                "solid": True,
                "loops": [
                    {
                        "vertices": [[0, 0], [4, 0, 0.2], [4, 3], [0, 3]],
                        "is_outer": True,
                    },
                ],
            },
        ],
    }

    dxf_text = ir_to_dxf(ir_document, validate=True)
    reparsed = dxf_to_ir(dxf_text, validate=True)

    assert "SECTION\n2\nBLOCKS" in dxf_text
    assert "MTEXT" in dxf_text
    assert "INSERT" in dxf_text
    assert "HATCH" in dxf_text

    assert "tables" in reparsed
    assert "SYM1" in reparsed["tables"]["blocks"]

    mtext = _entity_by_kind(reparsed, "MTEXT")
    assert mtext["text"] == "NOTE"
    assert mtext["attach"] == "bottom_right"

    insert = _entity_by_kind(reparsed, "INSERT")
    assert insert["block"] == "SYM1"
    assert insert["attributes"] == {"TAG": "VALUE"}

    hatch = _entity_by_kind(reparsed, "HATCH")
    assert hatch["loops"][0]["vertices"][1][2] == pytest.approx(0.2)
