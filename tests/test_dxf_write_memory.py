"""Writing a DXF while using the IR up (``consume``)."""

from __future__ import annotations

import copy

import pytest

from cad2d_ir import convert_ir_to_dxf_text
from cad2d_ir.constants import CURRENT_IR_VERSION


def _document() -> dict:
    entities: list[dict] = [
        {
            "id": f"L{index}",
            "kind": "LINE",
            "layer": "A",
            "p1": [0.0, float(index)],
            "p2": [10.0, float(index)],
        }
        for index in range(40)
    ]
    entities.append(
        {
            "id": "T1",
            "kind": "TEXT",
            "layer": "A",
            "insert": [1.0, 2.0],
            "height": 2.5,
            "text": "100%% of 50%",
        }
    )
    entities.append(
        {
            "id": "I1",
            "kind": "INSERT",
            "layer": "A",
            "block": "MARK",
            "insert": [5.0, 5.0],
        }
    )
    return {
        "format": "cad2d-ir",
        "version": CURRENT_IR_VERSION,
        "header": {"units": "mm", "angle_unit": "deg", "coord_space": "world"},
        "source": {"format": "dxf"},
        "tables": {
            "layers": {"A": {"color": 1}},
            "blocks": {
                "MARK": {
                    "base_point": [0.0, 0.0],
                    "entities": [
                        {
                            "id": "B1",
                            "kind": "LINE",
                            "layer": "0",
                            "p1": [0.0, 0.0],
                            "p2": [1.0, 1.0],
                        }
                    ],
                }
            },
        },
        "entities": entities,
    }


@pytest.mark.parametrize("target_version", ["AC1024", "AC1009"])
def test_consume_writes_the_same_dxf_and_empties_the_model_space(
    target_version: str,
) -> None:
    kept = _document()
    untouched = copy.deepcopy(kept)
    expected = convert_ir_to_dxf_text(kept, target_version=target_version)
    assert kept == untouched  # the default leaves the document as it was

    used = _document()
    result = convert_ir_to_dxf_text(used, target_version=target_version, consume=True)
    assert result.dxf_text == expected.dxf_text
    assert result.entity_map == expected.entity_map
    assert used["entities"] == []
    assert used["tables"] == untouched["tables"]
