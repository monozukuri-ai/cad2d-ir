from __future__ import annotations

import pytest

from cad2d_ir.codecs.dxf import dxf_to_ir
from cad2d_ir.schema import IRValidationError, validate_ir


def _document(
    entities: list[dict], *, block_entities: list[dict] | None = None
) -> dict:
    document = {
        "format": "cad2d-ir",
        "version": "0.2.0",
        "header": {"units": "mm", "angle_unit": "deg", "coord_space": "world"},
        "entities": entities,
    }
    if block_entities is not None:
        document["tables"] = {
            "blocks": {
                "B": {
                    "base_point": [0, 0],
                    "entities": block_entities,
                }
            }
        }
    return document


def _line(entity_id: str) -> dict:
    return {
        "id": entity_id,
        "kind": "LINE",
        "p1": [0, 0],
        "p2": [1, 0],
    }


def test_validate_ir_rejects_duplicate_ids_in_modelspace() -> None:
    with pytest.raises(IRValidationError, match="duplicates"):
        validate_ir(_document([_line("E1"), _line("E1")]))


def test_validate_ir_rejects_duplicate_ids_within_a_block() -> None:
    with pytest.raises(IRValidationError, match="duplicates"):
        validate_ir(
            _document(
                [],
                block_entities=[_line("B1"), _line("B1")],
            )
        )


def test_entity_id_scopes_allow_the_same_id_in_modelspace_and_a_block() -> None:
    validate_ir(_document([_line("E1")], block_entities=[_line("E1")]))


def test_duplicate_dxf_handles_are_renamed_but_source_handles_are_preserved() -> None:
    dxf = "\n".join(
        [
            "0",
            "SECTION",
            "2",
            "ENTITIES",
            "0",
            "LINE",
            "5",
            "A",
            "10",
            "0",
            "20",
            "0",
            "11",
            "1",
            "21",
            "0",
            "0",
            "LINE",
            "5",
            "A",
            "10",
            "0",
            "20",
            "1",
            "11",
            "1",
            "21",
            "1",
            "0",
            "ENDSEC",
            "0",
            "EOF",
            "",
        ]
    )

    document = dxf_to_ir(dxf, validate=True)

    assert [entity["id"] for entity in document["entities"]] == ["EA", "EA_2"]
    assert [entity["source"]["id"] for entity in document["entities"]] == ["A", "A"]
