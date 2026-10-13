"""JWS (Jw_cad registered symbol) import.

The entity records are JWW's, so the JWW conversion is exercised by
``test_jww_importer.py``; these tests cover what a symbol does differently:
the header, the layers created on demand with their group scales, the image
reference without an archive and the format labels. The fixtures in
``tests/data/jws`` are original symbols registered with Jw_cad (see the README
there); they need ``ezjww`` 0.7 or later.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from cad2d_ir import (
    convert_file_to_ir,
    convert_ir_to_dxf_text,
    convert_jws_file_to_ir,
    detect_source_format,
)
from cad2d_ir.cli import main
from cad2d_ir.importers import ImporterError, ImportOptions
from cad2d_ir.importers.jww import jws_document_to_ir
from cad2d_ir.schema import validate_ir

FIXTURES = Path(__file__).parent / "data" / "jws"
FIXTURE_SHA256 = {
    "basic_v600.jws": "e0fe69555566167dcd525ceb4513f9156305f5f78ceed174bc8bc8a3de511c95",
    "basic_v700.jws": "2519743d0b6755f0483863860a38cc4007726f0a524078cd4b5040ac60ca8df1",
    "scales_v700.jws": "82d569925b95f20476aefa6b6a63bbc7cb4e2b5413bb2f4cba02bc7fd5a70855",
    "geometry_v700.jws": "a1a19098c2c797d27dbf48f2ec00603d721a4224d04d4fe2ffa070616f11a43d",
    "unicode_v700.jws": "20d238a531ebdabf0d8ab58420c7d3ed10cafa7004755f84cf60ca4f9f802f05",
    "blocks_v700.jws": "abdf1fb42228dd10cab11de54e8bdfc9e594cb33d339ccee9f6899f5d49d7238",
    "image_v700.jws": "23f2d4719bf1fb15cb62317528a65a632af2868adde2d7e8aca5704de47d5056",
}


def _ezjww_reads_jws() -> bool:
    try:
        import ezjww
    except ImportError:
        return False
    return hasattr(ezjww, "read_jws_document")


requires_jws_reader = pytest.mark.skipif(
    not _ezjww_reads_jws(), reason="ezjww 0.7 or later is required to read JWS"
)


def _base(*, layer_group: int = 0, layer: int = 0) -> dict:
    return {
        "group": 0,
        "pen_style": 1,
        "pen_color": 2,
        "pen_width": 0,
        "layer": layer,
        "layer_group": layer_group,
        "flag": 0,
    }


def _line(x1: float, y1: float, x2: float, y2: float, **base: int) -> dict:
    return {
        "type": "LINE",
        "base": _base(**base),
        "start_x": x1,
        "start_y": y1,
        "end_x": x2,
        "end_y": y2,
    }


def _text(content: str) -> dict:
    return {
        "type": "TEXT",
        "base": _base(),
        "start_x": -30.0,
        "start_y": -20.0,
        "end_x": -10.0,
        "end_y": -20.0,
        "text_type": 0,
        "size_x": 3.0,
        "size_y": 3.0,
        "spacing": 0.0,
        "angle": 0.0,
        "font_name": "ＭＳ ゴシック",
        "content": content,
    }


def _symbol(entities: list[dict]) -> dict:
    """What ``ezjww.read_jws_document`` returns for a symbol with two scales."""
    return {
        "header": {
            "version": 700,
            "origin": {"x": 10.0, "y": 20.0},
            "group_scales": [50.0, 100.0] + [1.0] * 14,
            "bounds_min": {"x": 10.0, "y": 20.0},
            "bounds_max": {"x": 40.0, "y": 40.0},
            "reserved_prefix": [46] * 192,
            "reserved_integers": [0] * 6,
            "reserved_reals": [0.0] * 6,
        },
        "entities": entities,
        "metadata_settings": [],
        "block_defs": [],
        "images": [],
        "block_def_names": {},
        "entity_counts": {},
        "validation": {
            "total_references": 0,
            "resolved_references": 0,
            "unresolved_def_numbers": [],
            "has_unresolved": False,
        },
        "diagnostics": [],
    }


def test_jws_document_keeps_symbol_header_and_group_scales() -> None:
    result = jws_document_to_ir(
        _symbol(
            [
                _line(10.0, 20.0, 40.0, 20.0),
                _line(10.0, 20.0, 10.0, 40.0),
                _line(-40.0, -20.0, -20.0, -20.0, layer_group=1),
            ]
        ),
        source_name="scales.jws",
        source_sha256="a" * 64,
    )
    document = result.document

    assert document["source"] == {
        "format": "jws",
        "version": "700",
        "name": "scales.jws",
        "sha256": "a" * 64,
    }
    assert document["header"]["metadata"] == {
        "jws": {
            "version": 700,
            "origin": [10.0, 20.0],
            "group_scales": [50.0, 100.0] + [1.0] * 14,
            "bounds_min": [10.0, 20.0],
            "bounds_max": [40.0, 40.0],
            "coordinate_space": "paper_millimeters",
        }
    }
    # The coordinates are the registering drawing's paper millimetres, untouched.
    assert document["entities"][2]["p1"] == [-40.0, -20.0]
    assert [entity["id"] for entity in document["entities"]] == [
        "JWS_E00000001",
        "JWS_E00000002",
        "JWS_E00000003",
    ]
    assert document["entities"][0]["source"] == {
        "format": "jws",
        "id": "entities[0]",
        "kind": "LINE",
    }
    # Layers exist for the group/layer pairs in use, each with its group scale.
    layers = document["tables"]["layers"]
    assert list(layers) == ["0-0", "1-0"]
    assert layers["0-0"]["metadata"]["jww"] == {
        "layer_group": 0,
        "layer": 0,
        "group_scale": 50.0,
    }
    assert layers["1-0"]["metadata"]["jww"]["group_scale"] == 100.0
    assert document["tables"]["linetypes"]["CONTINUOUS"]

    codes = [diagnostic.code for diagnostic in result.diagnostics]
    assert codes == ["JWS_SYMBOL_COORDINATES", "JWS_DRAWING_SETTINGS_DEFAULTED"]
    coordinates = result.diagnostics[0]
    assert coordinates.severity == "info"
    assert coordinates.details == {
        "origin": [10.0, 20.0],
        "group_scales": {"0": 50.0, "1": 100.0},
    }
    assert "(10, 20)" in coordinates.message
    assert "1/50 (group 0)" in coordinates.message
    assert "1/100 (group 1)" in coordinates.message
    assert result.statistics["source_format"] == "jws"
    assert result.statistics["converted_entities"] == 3
    validate_ir(document, strict_jsonschema=True)


def test_jws_image_placement_keeps_the_path_without_an_archive() -> None:
    result = jws_document_to_ir(
        _symbol([_line(0.0, 0.0, 1.0, 0.0), _text("^@BM%temp%symbol.bmp,20,10,0,0")])
    )
    image = result.document["entities"][1]
    assert image["kind"] == "IMAGE"
    assert image["href"] == "%temp%symbol.bmp"
    assert image["name"] == "symbol.bmp"
    assert "data" not in image
    assert [d.code for d in result.diagnostics if d.code.startswith("JWS_IMAGE")] == [
        "JWS_IMAGE_REFERENCE_ONLY"
    ]
    assert result.statistics["linked_images"] == 1
    assert result.statistics["embedded_images"] == 0
    validate_ir(result.document, strict_jsonschema=True)


def test_jws_lenient_conversion_failure_names_the_symbol_format() -> None:
    broken = _line(0.0, 0.0, 1.0, 0.0)
    broken["end_x"] = "not a number"
    result = jws_document_to_ir(_symbol([broken]), options=ImportOptions(strict=False))
    failed = [d for d in result.diagnostics if d.code == "JWW_ENTITY_CONVERSION_FAILED"]
    assert len(failed) == 1
    assert failed[0].message.startswith("Failed to convert JWS LINE")
    assert result.document["entities"] == []

    with pytest.raises(ImporterError, match="Failed to convert JWS LINE"):
        jws_document_to_ir(_symbol([broken]))


def test_jws_header_without_group_scales_is_rejected() -> None:
    symbol = _symbol([_line(0.0, 0.0, 1.0, 0.0)])
    del symbol["header"]["group_scales"]
    with pytest.raises(ImporterError, match="group_scales"):
        jws_document_to_ir(symbol)

    symbol = _symbol([_line(0.0, 0.0, 1.0, 0.0)])
    symbol["header"]["group_scales"][3] = 0.0
    with pytest.raises(ImporterError, match=r"group_scales\[3\]"):
        jws_document_to_ir(symbol)


def test_jws_entity_provenance_can_be_omitted() -> None:
    result = jws_document_to_ir(
        _symbol([_line(0.0, 0.0, 1.0, 0.0)]),
        options=ImportOptions(entity_provenance=False),
    )
    entity = result.document["entities"][0]
    assert "source" not in entity and "metadata" not in entity
    # The layer keeps its group scale regardless; a writer needs it.
    assert (
        result.document["tables"]["layers"]["0-0"]["metadata"]["jww"]["group_scale"]
        == 50.0
    )


def test_detect_source_format_knows_jws() -> None:
    assert detect_source_format("symbol.JWS") == "jws"


@pytest.mark.parametrize("name", sorted(FIXTURE_SHA256))
def test_jws_fixtures_are_byte_stable(name: str) -> None:
    digest = hashlib.sha256((FIXTURES / name).read_bytes()).hexdigest()
    assert digest == FIXTURE_SHA256[name]


@requires_jws_reader
@pytest.mark.parametrize(
    ("name", "version", "kinds"),
    [
        ("basic_v600.jws", "600", {"LINE": 2, "CIRCLE": 1}),
        ("basic_v700.jws", "700", {"LINE": 2, "CIRCLE": 1}),
        ("scales_v700.jws", "700", {"LINE": 3}),
        (
            "geometry_v700.jws",
            "700",
            {
                "LINE": 2,
                "CIRCLE": 1,
                "ARC": 1,
                "ELLIPSE": 1,
                "POINT": 1,
                "HATCH": 2,
                "DIMENSION": 1,
            },
        ),
        ("unicode_v700.jws", "700", {"LINE": 2, "TEXT": 2}),
        ("blocks_v700.jws", "700", {"LINE": 2, "INSERT": 2}),
        ("image_v700.jws", "700", {"LINE": 2, "IMAGE": 1}),
    ],
)
def test_native_jws_fixtures_import_and_export(
    name: str, version: str, kinds: dict[str, int]
) -> None:
    result = convert_file_to_ir(FIXTURES / name)
    document = result.document

    assert document["source"]["format"] == "jws"
    assert document["source"]["version"] == version
    assert document["source"]["name"] == name
    main_kinds: dict[str, int] = {}
    for entity in document["entities"]:
        main_kinds[entity["kind"]] = main_kinds.get(entity["kind"], 0) + 1
    assert main_kinds == kinds
    assert result.statistics["skipped_entities"] == 0
    metadata = document["header"]["metadata"]["jws"]
    assert metadata["origin"] == [10.0, 20.0]
    assert len(metadata["group_scales"]) == 16
    assert metadata["bounds_min"][0] <= metadata["bounds_max"][0]
    assert [d.code for d in result.diagnostics[:2]] == [
        "JWS_SYMBOL_COORDINATES",
        "JWS_DRAWING_SETTINGS_DEFAULTED",
    ]
    validate_ir(document, strict_jsonschema=True)

    # The same symbol through the explicit helper and the JWW-style file API.
    explicit = convert_jws_file_to_ir(FIXTURES / name)
    assert explicit.document["entities"] == document["entities"]

    dxf = convert_ir_to_dxf_text(document, target_version="AC1024")
    assert "ENTITIES" in dxf.dxf_text
    assert not any(d.severity == "error" for d in dxf.diagnostics)


@requires_jws_reader
def test_native_scales_symbol_keeps_each_group_scale_on_its_layer() -> None:
    document = convert_file_to_ir(FIXTURES / "scales_v700.jws").document
    layers = document["tables"]["layers"]
    assert layers["0-0"]["metadata"]["jww"]["group_scale"] == 50.0
    assert layers["1-0"]["metadata"]["jww"]["group_scale"] == 100.0
    # Paper millimetres of the registering drawing, not scaled, not re-based.
    lines = {tuple(e["p1"]) + tuple(e["p2"]) for e in document["entities"]}
    assert lines == {
        (10.0, 20.0, 40.0, 20.0),
        (10.0, 20.0, 10.0, 40.0),
        (-40.0, -20.0, -20.0, -20.0),
    }
    assert document["header"]["metadata"]["jws"]["group_scales"][:2] == [50.0, 100.0]


@requires_jws_reader
def test_native_unicode_and_blocks_symbols() -> None:
    unicode_doc = convert_file_to_ir(FIXTURES / "unicode_v700.jws").document
    texts = {e["text"] for e in unicode_doc["entities"] if e["kind"] == "TEXT"}
    assert "日本語𠮷ｶﾅ ABC" in texts
    assert "縦書き" in texts
    assert unicode_doc["tables"]["text_styles"]["ＭＳ ゴシック"] == {
        "font": "ＭＳ ゴシック"
    }

    blocks_doc = convert_file_to_ir(FIXTURES / "blocks_v700.jws").document
    assert len(blocks_doc["tables"]["blocks"]) == 2
    inserts = [e for e in blocks_doc["entities"] if e["kind"] == "INSERT"]
    assert {insert["block"] for insert in inserts} <= set(
        blocks_doc["tables"]["blocks"]
    )


@requires_jws_reader
def test_native_image_symbol_keeps_only_the_reference() -> None:
    result = convert_file_to_ir(FIXTURES / "image_v700.jws")
    image = next(e for e in result.document["entities"] if e["kind"] == "IMAGE")
    assert image["href"].lower().startswith("%temp%")
    # The input placed a 20 x 10 image; Jw_cad rewrote the placement as 20 x 20
    # when it registered the symbol, and the native file is what we read.
    assert image["width"] == 20.0 and image["height"] == 20.0
    assert "data" not in image
    assert "JWS_IMAGE_REFERENCE_ONLY" in {d.code for d in result.diagnostics}


@requires_jws_reader
def test_cli_imports_jws(tmp_path: Path) -> None:
    output = tmp_path / "symbol.json"
    assert main(["import", str(FIXTURES / "basic_v700.jws"), "-o", str(output)]) == 0
    document = json.loads(output.read_text(encoding="utf-8"))
    assert document["source"]["format"] == "jws"
    assert len(document["entities"]) == 3

    explicit = tmp_path / "explicit.json"
    assert (
        main(
            [
                "import",
                str(FIXTURES / "basic_v700.jws"),
                "--format",
                "jws",
                "-o",
                str(explicit),
            ]
        )
        == 0
    )
    assert json.loads(explicit.read_text(encoding="utf-8"))["source"]["format"] == "jws"
