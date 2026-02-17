from __future__ import annotations

import pytest

from cad2d_ir.schema import IRValidationError, validate_ir


def test_validate_ir_accepts_minimum_document() -> None:
    document = {
        "format": "cad2d-ir",
        "version": "0.1.0",
        "header": {
            "units": "mm",
            "angle_unit": "deg",
            "coord_space": "world",
        },
        "entities": [
            {
                "id": "E1",
                "kind": "LINE",
                "p1": [0.0, 0.0],
                "p2": [10.0, 0.0],
            }
        ],
    }

    validate_ir(document)


def test_validate_ir_rejects_missing_required_fields() -> None:
    document = {
        "format": "cad2d-ir",
        "version": "0.1.0",
        "entities": [],
    }

    with pytest.raises(IRValidationError):
        validate_ir(document)
