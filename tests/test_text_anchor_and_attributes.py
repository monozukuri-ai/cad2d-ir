"""Justified text, block attributes and layer visibility through the DXF codec."""

from __future__ import annotations

import copy
from typing import Any

import pytest

from cad2d_ir.codecs.dxf import dxf_to_ir, ir_to_dxf, text_alignment_from_dxf
from cad2d_ir.schema import IRValidationError, validate_ir


def _dxf(
    *records: list[tuple[int, Any]], tables: list[tuple[int, Any]] | None = None
) -> str:
    pairs: list[tuple[int, Any]] = []
    if tables:
        pairs += [(0, "SECTION"), (2, "TABLES"), *tables, (0, "ENDSEC")]
    pairs += [(0, "SECTION"), (2, "ENTITIES")]
    for record in records:
        pairs += record
    pairs += [(0, "ENDSEC"), (0, "EOF")]
    return "\n".join(f"{code}\n{value}" for code, value in pairs) + "\n"


def _text(*extra: tuple[int, Any]) -> list[tuple[int, Any]]:
    return [(0, "TEXT"), (8, "0"), (10, 10), (20, 20), (40, 2.5), (1, "note"), *extra]


@pytest.mark.parametrize(
    ("codes", "expected"),
    [
        ((0, 0), ("left", "baseline", False)),
        ((1, 0), ("center", "baseline", True)),
        ((2, 3), ("right", "top", True)),
        ((0, 1), ("left", "bottom", True)),
        # Aligned and fit run from the first point; middle centers on the second.
        ((3, 0), ("left", "baseline", False)),
        ((5, 0), ("left", "baseline", False)),
        ((4, 0), ("center", "middle", True)),
        ((None, None), ("left", "baseline", False)),
    ],
)
def test_text_alignment_from_dxf(
    codes: tuple[int | None, int | None], expected: tuple[str, str, bool]
) -> None:
    assert text_alignment_from_dxf(*codes) == expected


def test_justified_text_is_anchored_at_its_second_point() -> None:
    second = [(11, 15), (21, 21)]
    document = dxf_to_ir(
        _dxf(
            _text(),
            _text((72, 1), (73, 2), *second),
            _text((72, 4), *second),
            _text((72, 3), *second),
            _text((72, 5), *second),
            # A second point without justification means nothing.
            _text(*second),
            # No second point: the first one is all there is.
            _text((72, 2), (73, 3)),
        )
    )

    placed = [
        (
            entity["insert"],
            entity.get("halign", "left"),
            entity.get("valign", "baseline"),
        )
        for entity in document["entities"]
    ]
    assert placed == [
        ([10.0, 20.0], "left", "baseline"),
        ([15.0, 21.0], "center", "middle"),
        ([15.0, 21.0], "center", "middle"),
        ([10.0, 20.0], "left", "baseline"),
        ([10.0, 20.0], "left", "baseline"),
        ([10.0, 20.0], "left", "baseline"),
        ([10.0, 20.0], "right", "top"),
    ]
    validate_ir(document, strict_jsonschema=True)


def test_justified_text_keeps_its_anchor_through_a_dxf_round_trip() -> None:
    document = dxf_to_ir(_dxf(_text((72, 2), (73, 3), (11, 30), (21, 25))))
    exported = ir_to_dxf(document)
    again = dxf_to_ir(exported)

    for result in (document, again):
        (entity,) = result["entities"]
        assert entity["insert"] == [30.0, 25.0]
        assert (entity["halign"], entity["valign"]) == ("right", "top")
    # The justification point is written as the second point.
    assert "\n11\n30\n" in exported.replace(" ", "")


def _insert_with_attributes() -> list[tuple[int, Any]]:
    return [
        (0, "INSERT"),
        (5, "A0"),
        (8, "PARTS"),
        (66, 1),
        (2, "TITLE"),
        (10, 100),
        (20, 50),
        # A plain attribute, the way a DXF without subclass markers has it.
        (0, "ATTRIB"),
        (8, "TEXT"),
        (62, 3),
        (10, 101),
        (20, 51),
        (40, 2.5),
        (1, "A-100"),
        (2, "NUMBER"),
        (70, 0),
        (50, 90),
        (41, 0.8),
        (7, "ROMANS"),
        # A justified, invisible attribute with subclass markers. In the
        # attribute part, group 72 is a lock flag and 73 a field length.
        (0, "ATTRIB"),
        (100, "AcDbEntity"),
        (8, "TEXT"),
        (100, "AcDbText"),
        (10, 110),
        (20, 60),
        (40, 3),
        (1, "secret"),
        (72, 1),
        (11, 115),
        (21, 61.5),
        (100, "AcDbAttribute"),
        (280, 0),
        (2, "OWNER"),
        (70, 1),
        (73, 12),
        (74, 2),
        (280, 1),
        # A multi-line attribute: its text is the embedded MTEXT.
        (0, "ATTRIB"),
        (100, "AcDbEntity"),
        (8, "TEXT"),
        (100, "AcDbText"),
        (10, 120),
        (20, 68),
        (40, 2),
        (1, ""),
        (11, 120),
        (21, 70),
        (100, "AcDbAttribute"),
        (280, 0),
        (2, "NOTE"),
        (70, 0),
        (74, 3),
        (280, 1),
        (71, 2),
        (72, 1),
        (11, 120),
        (21, 70),
        (101, "Embedded Object"),
        (10, 120),
        (20, 70),
        (40, 2),
        (41, 0),
        (71, 1),
        (1, "first\\Psecond"),
        # A record without a usable height keeps its value only.
        (0, "ATTRIB"),
        (8, "TEXT"),
        (10, 0),
        (20, 0),
        (40, 0),
        (1, "42"),
        (2, "COUNT"),
        (0, "SEQEND"),
        (8, "PARTS"),
    ]


def test_insert_attributes_keep_their_text_geometry() -> None:
    document = dxf_to_ir(_dxf(_insert_with_attributes()))

    (insert,) = document["entities"]
    assert insert["attributes"] == {
        "NUMBER": "A-100",
        "OWNER": "secret",
        "NOTE": "first\nsecond",
        "COUNT": "42",
    }
    assert insert["attribute_texts"] == [
        {
            "tag": "NUMBER",
            "insert": [101.0, 51.0],
            "height": 2.5,
            "text": "A-100",
            "rotation": 90.0,
            "style": "ROMANS",
            "width_factor": 0.8,
            "layer": "TEXT",
            "color": 3,
        },
        {
            "tag": "OWNER",
            "insert": [115.0, 61.5],
            "height": 3.0,
            "text": "secret",
            "halign": "center",
            "valign": "middle",
            "layer": "TEXT",
            "visible": False,
        },
        {
            "tag": "NOTE",
            "insert": [120.0, 70.0],
            "height": 2.0,
            "text": "first\nsecond",
            "valign": "top",
            "layer": "TEXT",
        },
    ]
    validate_ir(document, strict_jsonschema=True)


@pytest.mark.parametrize("target_version", ["AC1009", "AC1024"])
def test_insert_attributes_survive_a_dxf_round_trip(target_version: str) -> None:
    document = dxf_to_ir(_dxf(_insert_with_attributes()))
    exported = ir_to_dxf(document, target_version=target_version)
    again = dxf_to_ir(exported)

    (before,) = document["entities"]
    (after,) = again["entities"]
    # A multi-line attribute is written as one ATTRIB, its line break in the
    # caret notation DXF has for control characters; the rest is unchanged.
    expected = copy.deepcopy(before["attribute_texts"])
    expected[2]["text"] = "first^Jsecond"
    placed = [text for text in after["attribute_texts"] if text["tag"] != "COUNT"]
    assert placed == expected
    assert after["attributes"] == {**before["attributes"], "NOTE": "first^Jsecond"}
    # The value without geometry is still written at the insertion point.
    (count,) = [text for text in after["attribute_texts"] if text["tag"] == "COUNT"]
    assert (count["insert"], count["height"], count["text"]) == (
        [100.0, 50.0],
        1.0,
        "42",
    )


def test_attribute_values_without_geometry_are_still_exported() -> None:
    document: dict[str, Any] = {
        "format": "cad2d-ir",
        "version": "0.2.0",
        "header": {"units": "mm", "angle_unit": "deg", "coord_space": "world"},
        "tables": {"blocks": {"B": {"base_point": [0.0, 0.0], "entities": []}}},
        "entities": [
            {
                "id": "E1",
                "kind": "INSERT",
                "block": "B",
                "insert": [1.0, 2.0],
                "attributes": {"A": "1", "B": "2"},
                "attribute_texts": [
                    {"tag": "A", "text": "1", "insert": [5.0, 6.0], "height": 2.0}
                ],
            }
        ],
    }
    validate_ir(document, strict_jsonschema=True)

    (insert,) = dxf_to_ir(ir_to_dxf(document))["entities"]
    assert insert["attributes"] == {"A": "1", "B": "2"}
    texts = {text["tag"]: text for text in insert["attribute_texts"]}
    assert (texts["A"]["insert"], texts["A"]["height"]) == ([5.0, 6.0], 2.0)
    assert (texts["B"]["insert"], texts["B"]["height"]) == ([1.0, 2.0], 1.0)


def _attdef(
    tag: str, value: str, flags: int, *extra: tuple[int, Any]
) -> list[tuple[int, Any]]:
    return [
        (0, "ATTDEF"),
        (100, "AcDbEntity"),
        (8, "TITLE"),
        (100, "AcDbText"),
        (10, 5),
        (20, 6),
        (40, 2.5),
        (1, value),
        *extra,
        (100, "AcDbAttributeDefinition"),
        (280, 0),
        (3, "prompt"),
        (2, tag),
        (70, flags),
        (280, 1),
    ]


def test_attribute_definitions_are_shown_where_the_drawing_shows_them() -> None:
    block = [
        (0, "SECTION"),
        (2, "BLOCKS"),
        (0, "BLOCK"),
        (2, "FRAME"),
        (10, 0),
        (20, 0),
        # A template: block references show their own ATTRIB instead.
        *_attdef("NAME", "default", 0),
        # A constant definition has no ATTRIB: its value is part of the block.
        *_attdef("COMPANY", "ACME", 2, (72, 1), (11, 9), (21, 6), (74, 2)),
        *_attdef("HIDDEN", "x", 3),
        (0, "ENDBLK"),
        (0, "ENDSEC"),
    ]
    entities = [
        (0, "SECTION"),
        (2, "ENTITIES"),
        # Outside of a block the definition itself is displayed, by its tag.
        *_attdef("PART_NO", "", 0),
        *_attdef("SECRET", "y", 1),
        (0, "ENDSEC"),
        (0, "EOF"),
    ]
    text = "\n".join(f"{code}\n{value}" for code, value in [*block, *entities]) + "\n"
    document = dxf_to_ir(text)

    (constant,) = document["tables"]["blocks"]["FRAME"]["entities"]
    assert (constant["kind"], constant["text"], constant["layer"]) == (
        "TEXT",
        "ACME",
        "TITLE",
    )
    # Justified in the attribute part of the record (group 74).
    assert constant["insert"] == [9.0, 6.0]
    assert (constant["halign"], constant["valign"]) == ("center", "middle")
    assert constant["source"]["kind"] == "ATTDEF"

    (shown,) = document["entities"]
    assert (shown["kind"], shown["text"], shown["insert"]) == (
        "TEXT",
        "PART_NO",
        [5.0, 6.0],
    )
    validate_ir(document, strict_jsonschema=True)


def _layer(name: str, color: int, flags: int = 0) -> list[tuple[int, Any]]:
    return [(0, "LAYER"), (2, name), (70, flags), (62, color), (6, "CONTINUOUS")]


def test_layers_that_are_off_or_frozen_are_not_visible() -> None:
    tables = [
        (0, "TABLE"),
        (2, "LAYER"),
        *_layer("0", 7),
        *_layer("OFF", -3),
        *_layer("FROZEN", 5, flags=1),
        *_layer("LOCKED", 2, flags=4),
        (0, "ENDTAB"),
    ]
    document = dxf_to_ir(_dxf(tables=tables))

    layers = document["tables"]["layers"]
    assert "visible" not in layers["0"]
    assert layers["OFF"] == {"color": 3, "linetype": "CONTINUOUS", "visible": False}
    assert layers["FROZEN"]["visible"] is False
    assert "visible" not in layers["LOCKED"]
    validate_ir(document, strict_jsonschema=True)

    exported = ir_to_dxf(document)
    assert "\n62\n-3\n" in exported.replace(" ", "")
    again = dxf_to_ir(exported)["tables"]["layers"]
    assert {name: layer.get("visible", True) for name, layer in again.items()} == {
        "0": True,
        "OFF": False,
        "FROZEN": False,
        "LOCKED": True,
    }


def _document_with(
    insert: dict[str, Any], layers: dict[str, Any] | None = None
) -> dict:
    document: dict[str, Any] = {
        "format": "cad2d-ir",
        "version": "0.2.0",
        "header": {"units": "mm", "angle_unit": "deg", "coord_space": "world"},
        "tables": {"blocks": {"B": {"base_point": [0.0, 0.0], "entities": []}}},
        "entities": [
            {"id": "E1", "kind": "INSERT", "block": "B", "insert": [0.0, 0.0], **insert}
        ],
    }
    if layers is not None:
        document["tables"]["layers"] = layers
    return document


@pytest.mark.parametrize("strict", [False, True])
@pytest.mark.parametrize(
    "attribute_texts",
    [
        {"tag": "A"},
        [{"tag": "A", "text": "1", "insert": [0.0, 0.0]}],
        [{"tag": "A", "text": "1", "insert": [0.0, 0.0], "height": 0.0}],
        [{"tag": "A", "text": 1, "insert": [0.0, 0.0], "height": 1.0}],
        [{"tag": "A", "text": "1", "insert": [0.0], "height": 1.0}],
        [{"tag": "A", "text": "1", "insert": [0.0, 0.0], "height": 1.0, "halign": "x"}],
        [{"tag": "A", "text": "1", "insert": [0.0, 0.0], "height": 1.0, "visible": 0}],
        [
            {
                "tag": "A",
                "text": "1",
                "insert": [0.0, 0.0],
                "height": 1.0,
                "kind": "TEXT",
            }
        ],
    ],
)
def test_malformed_attribute_texts_are_rejected(
    attribute_texts: Any, strict: bool
) -> None:
    document = _document_with({"attribute_texts": attribute_texts})
    with pytest.raises(IRValidationError):
        validate_ir(document, strict_jsonschema=strict)


@pytest.mark.parametrize("strict", [False, True])
def test_layer_visibility_must_be_boolean(strict: bool) -> None:
    validate_ir(_document_with({}, {"0": {"visible": False}}), strict_jsonschema=strict)
    with pytest.raises(IRValidationError):
        validate_ir(
            _document_with({}, {"0": {"visible": "no"}}), strict_jsonschema=strict
        )
