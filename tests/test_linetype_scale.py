from __future__ import annotations

import pytest

from cad2d_ir.codecs.dxf import dxf_to_ir, ir_to_dxf
from cad2d_ir.diagnostics import ExportDiagnostic
from cad2d_ir.schema import IRValidationError, validate_ir


def _dxf(*, header: list[str], entities: list[str]) -> str:
    return "\n".join(
        [
            "0",
            "SECTION",
            "2",
            "HEADER",
            *header,
            "0",
            "ENDSEC",
            "0",
            "SECTION",
            "2",
            "ENTITIES",
            *entities,
            "0",
            "ENDSEC",
            "0",
            "EOF",
        ]
    )


def _line(*common: str) -> list[str]:
    return ["0", "LINE", "8", "0", *common, "10", "0", "20", "0", "11", "10", "21", "0"]


def _document() -> dict:
    return {
        "format": "cad2d-ir",
        "version": "0.2.0",
        "header": {
            "units": "mm",
            "angle_unit": "deg",
            "coord_space": "world",
            "linetype_scale": 50.0,
        },
        "tables": {
            "linetypes": {
                "DASHED": {"description": "Dashed", "pattern_mm": [12.7, -6.35]}
            }
        },
        "entities": [
            {
                "id": "E1",
                "kind": "LINE",
                "linetype": "DASHED",
                "linetype_scale": 0.5,
                "p1": [0, 0],
                "p2": [10, 0],
            },
            {"id": "E2", "kind": "LINE", "p1": [0, 1], "p2": [10, 1]},
        ],
    }


def test_dxf_import_reads_global_and_entity_linetype_scale() -> None:
    document = dxf_to_ir(
        _dxf(
            header=["9", "$LTSCALE", "40", "50.0"],
            entities=[*_line("6", "DASHED", "48", "0.5"), *_line("6", "DASHED")],
        )
    )

    assert document["header"]["linetype_scale"] == pytest.approx(50.0)
    scaled, plain = document["entities"]
    assert scaled["linetype_scale"] == pytest.approx(0.5)
    assert "linetype_scale" not in plain


def test_dxf_import_omits_default_and_unusable_linetype_scales() -> None:
    default = dxf_to_ir(
        _dxf(header=["9", "$LTSCALE", "40", "1.0"], entities=_line("48", "1.0"))
    )
    assert "linetype_scale" not in default["header"]
    assert "linetype_scale" not in default["entities"][0]

    unusable = dxf_to_ir(
        _dxf(header=["9", "$LTSCALE", "40", "0.0"], entities=_line("48", "-2.0"))
    )
    assert "linetype_scale" not in unusable["header"]
    assert "linetype_scale" not in unusable["entities"][0]


def test_dxf_import_does_not_read_mtext_column_width_as_linetype_scale() -> None:
    # After the entity-specific subclass marker, group 48 is the MTEXT column width.
    mtext = [
        "0",
        "MTEXT",
        "100",
        "AcDbEntity",
        "8",
        "0",
        "100",
        "AcDbMText",
        "10",
        "0",
        "20",
        "0",
        "40",
        "2.5",
        "1",
        "note",
        "48",
        "120.0",
    ]
    document = dxf_to_ir(_dxf(header=[], entities=mtext))

    assert document["entities"][0]["kind"] == "MTEXT"
    assert "linetype_scale" not in document["entities"][0]


def test_linetype_scale_roundtrips_through_dxf() -> None:
    text = ir_to_dxf(_document())

    lines = text.splitlines()
    assert lines[lines.index("$LTSCALE") + 2] == "50"
    restored = dxf_to_ir(text)
    assert restored["header"]["linetype_scale"] == pytest.approx(50.0)
    by_id = {entity["source"]["id"]: entity for entity in restored["entities"]}
    scales = [entity.get("linetype_scale") for entity in by_id.values()]
    assert scales.count(None) == 1
    assert pytest.approx(0.5) in [scale for scale in scales if scale is not None]


def test_r12_export_keeps_ltscale_and_reports_entity_scale() -> None:
    diagnostics: list[ExportDiagnostic] = []
    text = ir_to_dxf(_document(), target_version="AC1009", diagnostics=diagnostics)

    assert "$LTSCALE" in text.splitlines()
    restored = dxf_to_ir(text)
    assert restored["header"]["linetype_scale"] == pytest.approx(50.0)
    assert all("linetype_scale" not in entity for entity in restored["entities"])
    codes = [diagnostic.code for diagnostic in diagnostics]
    assert codes.count("DXF_R12_LINETYPE_SCALE_OMITTED") == 1


def test_schema_accepts_linetype_scale_and_linetype_plot_flag() -> None:
    document = _document()
    document["tables"]["linetypes"]["CONSTRUCTION"] = {
        "description": "Not printed",
        "pattern_mm": [0.625, -1.875],
        "plot": False,
    }
    validate_ir(document)
    validate_ir(document, strict_jsonschema=True)


@pytest.mark.parametrize("strict", [False, True])
@pytest.mark.parametrize("value", [0, -1.0, "2"])
def test_schema_rejects_invalid_linetype_scale(value: object, strict: bool) -> None:
    header = _document()
    header["header"]["linetype_scale"] = value
    with pytest.raises(IRValidationError):
        validate_ir(header, strict_jsonschema=strict)

    entity = _document()
    entity["entities"][0]["linetype_scale"] = value
    with pytest.raises(IRValidationError):
        validate_ir(entity, strict_jsonschema=strict)


@pytest.mark.parametrize("strict", [False, True])
def test_schema_rejects_non_boolean_linetype_plot(strict: bool) -> None:
    document = _document()
    document["tables"]["linetypes"]["DASHED"]["plot"] = "no"
    with pytest.raises(IRValidationError):
        validate_ir(document, strict_jsonschema=strict)
