from __future__ import annotations

import pytest

from cad2d_ir.codecs.dxf import dxf_to_ir, ir_to_dxf


def _entity_by_kind(document: dict, kind: str) -> dict:
    return next(entity for entity in document["entities"] if entity["kind"] == kind)


def test_insert_scale_factors_default_to_one_each() -> None:
    # AutoCAD and ODA leave out a scale factor of 1, so a block mirrored in X
    # comes with group 41 alone; group 42 does not follow 41.
    from cad2d_ir import dxf_to_ir

    def insert(groups: list[str]) -> list[str]:
        return ["0", "INSERT", "8", "0", "2", "B", "10", "0", "20", "0", *groups]

    lines = [
        *("0", "SECTION", "2", "BLOCKS"),
        *("0", "BLOCK", "2", "B", "3", "B", "8", "0", "10", "0", "20", "0", "70", "0"),
        *("0", "LINE", "8", "0", "10", "0", "20", "0", "11", "1", "21", "0"),
        *("0", "ENDBLK", "8", "0", "0", "ENDSEC"),
        *("0", "SECTION", "2", "ENTITIES"),
        *insert(["41", "-1"]),
        *insert(["42", "-1"]),
        *insert(["41", "2", "42", "2"]),
        *insert(["41", "2", "42", "1"]),
        *("0", "ENDSEC", "0", "EOF"),
    ]
    document = dxf_to_ir("\n".join(lines) + "\n")
    scales = [entity.get("scale") for entity in document["entities"]]
    assert scales == [[-1.0, 1.0], [1.0, -1.0], 2.0, [2.0, 1.0]]


@pytest.mark.parametrize("target_version", ["AC1009", "AC1024"])
def test_insert_transform_is_written_as_rotation_and_scale(target_version: str) -> None:
    import math

    from cad2d_ir import convert_ir_to_dxf_text, dxf_to_ir

    cos, sin = math.cos(math.radians(30.0)), math.sin(math.radians(30.0))
    transforms = {
        "turned": [2 * cos, 2 * sin, -2 * sin, 2 * cos, 5.0, 6.0],
        "mirrored": [cos, sin, 0.5 * sin, -0.5 * cos, -3.0, 4.0],
        "sheared": [1.0, 0.0, 0.5, 1.0, 0.0, 0.0],
    }
    base = [2.0, 1.0]
    document = {
        "format": "cad2d-ir",
        "version": "0.2.0",
        "header": {"units": "mm", "angle_unit": "deg", "coord_space": "world"},
        "tables": {
            "layers": {"0": {}},
            "blocks": {
                "B": {
                    "base_point": base,
                    "entities": [
                        {"id": "L", "kind": "LINE", "p1": [2, 1], "p2": [12, 4]}
                    ],
                }
            },
        },
        "entities": [
            {
                "id": name,
                "kind": "INSERT",
                "block": "B",
                "insert": [0, 0],
                "transform": transform,
            }
            for name, transform in transforms.items()
        ],
    }
    result = convert_ir_to_dxf_text(document, target_version=target_version)
    inserts = [
        e for e in dxf_to_ir(result.dxf_text)["entities"] if e["kind"] == "INSERT"
    ]

    def placed(insert: dict, point: list[float]) -> tuple[float, float]:
        scale = insert.get("scale", 1.0)
        sx, sy = (scale, scale) if not isinstance(scale, list) else scale
        angle = math.radians(insert.get("rotation", 0.0))
        x, y = (point[0] - base[0]) * sx, (point[1] - base[1]) * sy
        return (
            insert["insert"][0] + x * math.cos(angle) - y * math.sin(angle),
            insert["insert"][1] + x * math.sin(angle) + y * math.cos(angle),
        )

    # A rotation, scaling and mirroring land every point where the transform puts it.
    for insert, (name, (a, b, c, d, e, f)) in zip(inserts, transforms.items()):
        if name == "sheared":
            continue
        for point in ([2, 1], [12, 4]):
            expected = (
                a * point[0] + c * point[1] + e,
                b * point[0] + d * point[1] + f,
            )
            assert placed(insert, point) == pytest.approx(expected), name
    # A shear is dropped, and said so.
    omitted = [
        d for d in result.diagnostics if d.code == "DXF_INSERT_TRANSFORM_OMITTED"
    ]
    assert [d.entity_id for d in omitted] == ["sheared"]
    assert omitted[0].action == "approximated"


def test_mtext_rotation_comes_from_its_direction_vector() -> None:
    # AutoCAD and ODA write the direction of an MTEXT (11, 21, 31), no angle.
    from cad2d_ir import dxf_to_ir

    def mtext(groups: list[str]) -> list[str]:
        return ["0", "MTEXT", "8", "0", "10", "0", "20", "0", "40", "1", *groups]

    lines = [
        *("0", "SECTION", "2", "ENTITIES"),
        *mtext(["1", "UP", "11", "6.123233995736766E-17", "21", "1.0", "31", "0.0"]),
        *mtext(["1", "DOWN", "11", "0", "21", "-2", "31", "0"]),
        *mtext(["1", "ANGLE", "50", "30"]),
        *mtext(["1", "PLAIN", "11", "1.0", "21", "0.0"]),
        *("0", "ENDSEC", "0", "EOF"),
    ]
    entities = dxf_to_ir("\n".join(lines) + "\n")["entities"]
    rotations = {entity["text"]: entity.get("rotation") for entity in entities}
    assert rotations == pytest.approx(
        {"UP": 90.0, "DOWN": -90.0, "ANGLE": 30.0, "PLAIN": None}
    )


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
