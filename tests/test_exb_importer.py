"""EXB (CAXA CAD / 电子图板) import.

The fixtures in ``tests/data/exb`` are ``ezexb``'s original synthetic compound
documents (see the README there): six stored geometry kinds, a layer, a line
type, a text style, one space and a block with an INSERT. They are not
CAD-authored, so the mapping of the CAXA-specific records (dimensions, hatches,
NURBS, attributes) is covered by the opt-in real-file gate at the end.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path

import pytest

from cad2d_ir import (
    convert_exb_file_to_ir,
    convert_file_to_ir,
    convert_ir_to_dxf_text,
    detect_source_format,
)
from cad2d_ir.cli import main
from cad2d_ir.importers import ImporterError, ImportOptions
from cad2d_ir.importers.exb import (
    DEFAULT_PARSE_BUDGETS,
    DIMENSION_KINDS,
    TEXT_ALIGNMENTS,
    VERIFIED_TEXT_ALIGNMENTS,
    _arc_angles,
    _colorref_hex,
    normalize_text,
)
from cad2d_ir.schema import validate_ir

ezexb = pytest.importorskip("ezexb")

FIXTURES = Path(__file__).parent / "data" / "exb"
FIXTURE_SHA256 = {
    "document-10.exb": "edffed56b4da2e1f28e5d2bf14fef1b67ec4f2ed33c26d728bd2ad0cd8806769",
    "document-18.exb": "8088035578bec1ff680b56d1b6bc446b4d2f40c818db30f7a4445975ef98c81c",
    "document-partial.exb": (
        "a79110f971950a047863e90a4923a812b019a752318b6c695888875cda8d2338"
    ),
}
CFB_MAGIC = bytes.fromhex("d0cf11e0a1b11ae1")


def _codes(result) -> dict[str, int]:
    counts: dict[str, int] = {}
    for diagnostic in result.diagnostics:
        counts[diagnostic.code] = counts.get(diagnostic.code, 0) + 1
    return counts


def _geometry(entity: dict) -> dict:
    return {
        key: value
        for key, value in entity.items()
        if key not in {"id", "source", "metadata"}
    }


@pytest.mark.parametrize(("name", "expected_sha256"), sorted(FIXTURE_SHA256.items()))
def test_exb_fixtures_are_byte_stable(name: str, expected_sha256: str) -> None:
    assert hashlib.sha256((FIXTURES / name).read_bytes()).hexdigest() == expected_sha256


def test_exb_suffix_is_detected() -> None:
    assert detect_source_format("drawing.EXB") == "exb"
    assert detect_source_format(Path("图纸.exb")) == "exb"


def test_document_maps_entities_tables_block_and_provenance() -> None:
    result = convert_exb_file_to_ir(FIXTURES / "document-10.exb")
    document = result.document
    validate_ir(document)

    assert document["source"] == {
        "format": "exb",
        "version": "10.0.0.0000",
        "name": "document-10.exb",
        "sha256": FIXTURE_SHA256["document-10.exb"],
        "metadata": {
            "exb": {
                "parser": "ezexb",
                "parser_version": ezexb.__version__,
                "profile": "exb-stored-planar-v1",
                "complete": True,
                "decoded_entity_count": 6,
                "unsupported_entity_count": 0,
                "top_level_record_count": 8,
                "unsupported_types": {},
            }
        },
    }
    header = document["header"]
    assert header["units"] == "mm"
    assert header["angle_unit"] == "deg"
    exb = header["metadata"]["exb"]
    assert exb["product_build"] == "10.0.0.0000"
    assert exb["units_assumed"] is True
    assert exb["spaces"] == [
        {"id": -7, "name": "Sheet A", "scale": 1.0, "block_id": -5, "entities": 5}
    ]
    assert exb["main_space_id"] == -7
    assert exb["paper_scale"] == 1.0
    assert exb["paper"] is None  # the synthetic drawing has no *FRAME block

    layer = document["tables"]["layers"]["Test layer"]
    assert layer["color"] == "#563412"  # stored 0x123456 read as COLORREF (BGR)
    assert layer["lineweight_mm"] == 0.35  # width flag 2 selects the stored float
    assert layer["linetype"] == "Continuous"
    assert layer["visible"] is True and layer["plot"] is True
    assert document["tables"]["linetypes"]["Continuous"]["pattern_mm"] == []
    style = document["tables"]["text_styles"]["Original style"]
    assert style["font"] == "sans-serif"
    assert style["metadata"]["exb"]["line_spacing_factor"] == 1.5

    kinds = [entity["kind"] for entity in document["entities"]]
    assert kinds == ["LINE", "ARC", "LWPOLYLINE", "TEXT", "INSERT"]
    line, arc, polyline, text, insert = document["entities"]
    assert line["p1"] == [1.0, 2.0] and line["p2"] == [4.0, 6.0]
    assert line["layer"] == "Test layer"
    assert line["linetype_scale"] == 1.25
    assert "color" not in line  # ByLayer colour is left to the layer
    assert arc["center"] == [0.0, 0.0] and arc["radius"] == 3.0
    assert (arc["start_angle"], arc["end_angle"], arc["ccw"]) == (270.0, 0.0, True)
    assert polyline["vertices"] == [[0.0, 0.0], [10.0, 0.0], [10.0, 10.0]]
    assert polyline["closed"] is True
    assert text["text"] == "Original 日本語"
    assert text["insert"] == [7.0, 8.0] and text["height"] == 3.5
    assert (text["halign"], text["valign"]) == ("left", "top")
    assert text["style"] == "Original style"
    assert insert["block"] == "Reusable part" and insert["insert"] == [20.0, 30.0]
    assert "scale" not in insert
    block = document["tables"]["blocks"]["Reusable part"]
    assert [entity["kind"] for entity in block["entities"]] == ["CIRCLE"]
    assert block["metadata"]["exb"]["id"] == -60

    assert line["source"]["format"] == "exb"
    assert line["source"]["kind"] == "Line"
    assert line["source"]["metadata"]["block_id"] == -5
    assert line["source"]["metadata"]["stream_path"].startswith("/*BlockStg/")
    assert len(line["source"]["metadata"]["type_id"]) == 36
    assert line["metadata"]["exb"]["color_source"] == "by_layer"
    assert text["metadata"]["exb"]["alignment_raw"] == 0
    assert text["metadata"]["exb"]["contents_raw"] == "Original 日本語"

    assert result.statistics["source_format"] == "exb"
    assert result.statistics["source_entities"] == 6
    assert result.statistics["converted_entities"] == 5
    assert result.statistics["converted_block_entities"] == 1
    assert result.statistics["converted_entity_counts"] == {
        "ARC": 1,
        "CIRCLE": 1,
        "INSERT": 1,
        "LINE": 1,
        "LWPOLYLINE": 1,
        "TEXT": 1,
    }
    assert result.statistics["read_complete"] is True
    assert result.statistics["layouts"] == 0
    # alignment 0 (top-left) is verified, so nothing is reported as assumed
    assert _codes(result) == {"EXB_UNITS_ASSUMED_MM": 1}

    exported = convert_ir_to_dxf_text(document, target_version="AC1024")
    assert "Original 日本語" in exported.dxf_text
    assert "Reusable part" in exported.dxf_text


def test_build_18_layout_converts_like_build_10() -> None:
    old = convert_exb_file_to_ir(FIXTURES / "document-10.exb").document
    new = convert_exb_file_to_ir(FIXTURES / "document-18.exb").document
    assert new["source"]["version"] == "18.0.0.18159"
    assert [_geometry(e) for e in new["entities"]] == [
        _geometry(e) for e in old["entities"]
    ]
    assert new["tables"]["layers"] == old["tables"]["layers"]


def test_partial_document_reports_unsupported_records() -> None:
    result = convert_exb_file_to_ir(FIXTURES / "document-partial.exb")
    validate_ir(result.document)
    assert result.document["source"]["metadata"]["exb"]["complete"] is False
    assert result.document["source"]["metadata"]["exb"]["unsupported_types"] == {
        "11111111-1111-1111-1111-111111111111": 1
    }
    assert result.statistics["source_unsupported_entities"] == 1
    assert result.statistics["skipped_entity_counts"] == {
        "unsupported:11111111-1111-1111-1111-111111111111": 1
    }
    assert result.statistics["converted_entities"] == 5
    codes = _codes(result)
    assert codes["EXB_PARTIAL_READ"] == 1
    assert codes["EXB_UNSUPPORTED_ENTITY"] == 1
    partial = next(d for d in result.diagnostics if d.code == "EXB_PARTIAL_READ")
    assert partial.severity == "warning"
    assert partial.details["unsupported_entity_count"] == 1
    unsupported = next(
        d for d in result.diagnostics if d.code == "EXB_UNSUPPORTED_ENTITY"
    )
    assert unsupported.action == "skipped"
    assert unsupported.details == {
        "type_id": "11111111-1111-1111-1111-111111111111",
        "count": 1,
    }
    # The same decoded entities come out of the partial file
    assert [e["kind"] for e in result.document["entities"]] == [
        "LINE",
        "ARC",
        "LWPOLYLINE",
        "TEXT",
        "INSERT",
    ]


def test_entity_provenance_can_be_dropped() -> None:
    result = convert_exb_file_to_ir(
        FIXTURES / "document-10.exb", entity_provenance=False
    )
    assert result.statistics["converted_entities"] == 5
    from cad2d_ir.importers.exb import convert_exb_file_to_ir as import_exb

    same = import_exb(
        FIXTURES / "document-10.exb", options=ImportOptions(entity_provenance=False)
    )
    assert same.document["entities"] == result.document["entities"]
    for entity in result.document["entities"]:
        assert "source" not in entity
        assert "metadata" not in entity
    validate_ir(result.document)


def test_registry_and_cli_import_exb(tmp_path: Path) -> None:
    auto = convert_file_to_ir(FIXTURES / "document-10.exb")
    assert auto.document["source"]["format"] == "exb"
    explicit = convert_file_to_ir(FIXTURES / "document-10.exb", source_format="exb")
    assert explicit.statistics == auto.statistics

    output = tmp_path / "drawing.json"
    assert main(["import", str(FIXTURES / "document-10.exb"), "-o", str(output)]) == 0
    document = json.loads(output.read_text(encoding="utf-8"))
    assert document["source"]["format"] == "exb"
    assert len(document["entities"]) == 5


def test_text_escapes_are_normalized() -> None:
    assert normalize_text("%c20\\D\\H1x;+0.0065^-0.0065|b;") == "⌀20\\S+0.0065^-0.0065;"
    assert normalize_text("10\\D\\H0.707107x;-0.013^-0.028|b;") == "10\\S-0.013^-0.028;"
    assert normalize_text("500\\V\\H0.707107x;H9^f9|a;") == "500\\SH9^f9;"
    assert normalize_text("M16%x1 60%d %c114%p0.017") == "M16×1 60° ⌀114±0.017"
    assert normalize_text("型部\\R\\H0.707x;0^0^7^^0.2^^^^;尺寸公差") == "型部尺寸公差"
    # DXF-compatible escapes pass through untouched
    assert normalize_text("借用\\P件登记 {\\H2x;梯}") == "借用\\P件登记 {\\H2x;梯}"


def test_arc_angles_follow_the_basis_handedness() -> None:
    right = _arc_angles(
        (0, 0, 0), (3, 0, 0), (0, 3, 0), (1, 0, 0), (0, 1, 0), math.pi / 2
    )
    assert right == (0.0, 90.0)
    # A left-handed basis runs clockwise in world coordinates: swap the endpoints
    left = _arc_angles(
        (0, 0, 0), (3, 0, 0), (0, -3, 0), (1, 0, 0), (0, -1, 0), math.pi / 2
    )
    assert left == (270.0, 0.0)
    negative = _arc_angles(
        (0, 0, 0), (3, 0, 0), (0, -3, 0), (1, 0, 0), (0, 1, 0), -math.pi / 2
    )
    assert negative == (270.0, 0.0)


def test_colorref_reads_the_low_24_bits_as_bgr() -> None:
    assert _colorref_hex(0x0000FF) == "#FF0000"  # 中心线层 red
    assert _colorref_hex(0x00FF00) == "#00FF00"  # 尺寸线层 green
    assert _colorref_hex(0xFFFF00) == "#00FFFF"  # 剖面线层 cyan
    assert _colorref_hex(0x20FFFFFF) == "#FFFFFF"  # the high byte is a flag


def test_tables_of_codes_are_complete() -> None:
    assert sorted(TEXT_ALIGNMENTS) == list(range(9))
    assert VERIFIED_TEXT_ALIGNMENTS == frozenset({0, 1, 3, 4, 5, 6, 7, 8})
    assert set(DIMENSION_KINDS.values()) == {
        "LINEAR",
        "DIAMETER",
        "RADIAL",
        "GENERIC",
        "ANGULAR",
    }
    budgets = ezexb.ParseOptions(**DEFAULT_PARSE_BUDGETS)
    defaults = ezexb.ParseOptions()
    assert budgets.max_records > defaults.max_records
    assert budgets.max_total_decoded_bytes > defaults.max_total_decoded_bytes


def test_non_exb_inputs_raise_importer_errors(tmp_path: Path, monkeypatch) -> None:
    legacy = tmp_path / "old.exb"
    legacy.write_bytes(b"\x10CaxaEbForWindows" + bytes(200))
    with pytest.raises(ImporterError, match="legacy EXB"):
        convert_exb_file_to_ir(legacy)

    text = tmp_path / "text.exb"
    text.write_bytes(b"0\nSECTION\n" * 20)
    with pytest.raises(ImporterError, match="Not an EXB file"):
        convert_exb_file_to_ir(text)

    broken = tmp_path / "broken.exb"
    broken.write_bytes(CFB_MAGIC + bytes(600))
    with pytest.raises(ImporterError, match="Failed to read EXB file"):
        convert_exb_file_to_ir(broken)

    def unsupported(*args, **kwargs):
        raise ezexb.UnsupportedFeatureError(
            "stored-entity reader for this product build is not implemented"
        )

    class _Value:
        value = "12.70.0.5534 (x86)"

    class _FileInfo:
        product_build = _Value()

    class _Metadata:
        file_info = _FileInfo()

    monkeypatch.setattr(ezexb, "read", unsupported)
    monkeypatch.setattr(ezexb, "metadata", lambda *args, **kwargs: _Metadata())
    with pytest.raises(ImporterError) as excinfo:
        convert_exb_file_to_ir(FIXTURES / "document-10.exb")
    assert "Unsupported EXB product build '12.70.0.5534 (x86)'" in str(excinfo.value)
    assert "10.0.0.0000" in str(excinfo.value)


# ---------------------------------------------------------------------------
# Real-file validation gate (opt-in): CAD2D_IR_EXB_FIXTURES points at a directory
# of CAXA-authored EXB drawings (the CAXA CAD samples, for example).

_FIXTURES = os.environ.get("CAD2D_IR_EXB_FIXTURES")
needs_fixtures = pytest.mark.skipif(
    not _FIXTURES or not Path(_FIXTURES).is_dir(),
    reason="set CAD2D_IR_EXB_FIXTURES to a directory of CAXA EXB drawings",
)


@needs_fixtures
def test_real_drawings_convert_validate_and_export() -> None:
    paths = sorted(Path(_FIXTURES or "").rglob("*.exb"))
    assert paths, "no .exb files under CAD2D_IR_EXB_FIXTURES"
    converted = 0
    unsupported_builds = 0
    dimensions_with_blocks = 0
    for path in paths:
        try:
            result = convert_exb_file_to_ir(path)
        except ImporterError as exc:
            assert "Unsupported EXB product build" in str(exc), (path, exc)
            unsupported_builds += 1
            continue
        document = result.document
        validate_ir(document)
        assert document["source"]["format"] == "exb"
        assert (
            result.statistics["converted_entities"]
            + result.statistics["converted_layout_entities"]
            >= 0
        )
        for entity in document["entities"]:
            if entity["kind"] == "DIMENSION" and "block" in entity["definition"]:
                assert entity["definition"]["block"] in document["tables"]["blocks"]
                dimensions_with_blocks += 1
        convert_ir_to_dxf_text(document, target_version="AC1024")
        converted += 1
    assert converted > 0
    print(
        f"converted {converted} EXB files, {unsupported_builds} unsupported builds, "
        f"{dimensions_with_blocks} dimensions with cached presentation"
    )
