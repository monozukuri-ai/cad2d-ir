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


@pytest.mark.parametrize("source_format", ["dgn", "dwf", "mi"])
def test_validate_ir_accepts_new_source_formats(source_format: str) -> None:
    document = {
        "format": "cad2d-ir",
        "version": "0.2.0",
        "source": {"format": source_format, "name": f"sample.{source_format}"},
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
                "source": {"format": source_format, "id": "1", "kind": "LINE"},
            }
        ],
    }

    validate_ir(document)
    validate_ir(document, strict_jsonschema=True)


def test_validate_ir_0_2_entities_and_provenance_with_strict_schema() -> None:
    document = {
        "format": "cad2d-ir",
        "version": "0.2.0",
        "source": {"format": "jww", "version": "600", "name": "sample.jww"},
        "header": {
            "units": "unknown",
            "angle_unit": "deg",
            "coord_space": "world",
        },
        "entities": [
            {
                "id": "P1",
                "kind": "POINT",
                "position": [1.0, 2.0],
                "source": {"format": "jww", "id": "entities[0]", "kind": "POINT"},
            },
            {
                "id": "EL1",
                "kind": "ELLIPSE",
                "center": [0.0, 0.0],
                "major_axis": [2.0, 0.0],
                "ratio": 0.5,
                "start_param": 0.0,
                "end_param": 6.283185307179586,
            },
            {
                "id": "I1",
                "kind": "INSERT",
                "block": "MIRRORED",
                "insert": [0.0, 0.0],
                "scale": [-1.0, 1.0],
            },
            {
                "id": "D1",
                "kind": "DIMENSION",
                "dim_kind": "GENERIC",
                "definition": {"source_geometry": {}},
            },
        ],
    }

    validate_ir(document, strict_jsonschema=True)


def _image_document(**overrides) -> dict:
    entity = {
        "id": "I1",
        "kind": "IMAGE",
        "insert": [10.0, 20.0],
        "width": 100.0,
        "height": 64.5161,
        "rotation": 0.0,
        "name": "logo.bmp",
        "mime_type": "image/bmp",
        "data": "Qk0=",
    }
    entity.update(overrides)
    return {
        "format": "cad2d-ir",
        "version": "0.3.0",
        "header": {"units": "mm", "angle_unit": "deg", "coord_space": "world"},
        "entities": [entity],
    }


def test_validate_ir_accepts_image_entities_in_both_validators() -> None:
    validate_ir(_image_document())
    validate_ir(_image_document(), strict_jsonschema=True)
    linked = _image_document(href="C:\\pictures\\logo.bmp")
    del linked["entities"][0]["data"]
    del linked["entities"][0]["mime_type"]
    validate_ir(linked, strict_jsonschema=True)


@pytest.mark.parametrize(
    "overrides",
    [
        {"width": 0.0},
        {"height": -1.0},
        {"insert": [1.0]},
        {"data": 123},
        {"rotation": "ninety"},
    ],
)
def test_validate_ir_rejects_malformed_image_entities(overrides: dict) -> None:
    with pytest.raises(IRValidationError):
        validate_ir(_image_document(**overrides))
