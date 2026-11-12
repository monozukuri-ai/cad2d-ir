"""The native code (the ``cad2d-ir-native`` package) through its own interface.

``cad2d_ir`` uses the native code for the DXF writer and for the entity
conversion of the JWW importer, and the tests under ``tests/`` cover both
through the functions of ``cad2d_ir``. Here is what this package adds: the
typed document (``NativeDocument``), and that the two ways into each conversion
agree - a document as a ``dict`` or as the typed IR for the writer, a JWW file
read by the native code or the ``dict`` of ``ezjww.read_document`` for the
importer.

The tests with real drawings read the samples of the ``ezjww`` repository next
to this one (``EZJWW_SAMPLES`` names another directory) and are skipped without
them.
"""

from __future__ import annotations

import base64
import copy
import dataclasses
import hashlib
import json
import math
import os
from pathlib import Path
from typing import Any

import cad2d_ir_native as native
import pytest

import cad2d_ir
from cad2d_ir._native import native_core
from cad2d_ir.importers.base import ImportOptions
from cad2d_ir.importers.jww import (
    NativeImportResult,
    convert_jww_file_to_ir,
    import_jww_file,
    jww_document_to_ir,
)

GOLDEN = Path(__file__).resolve().parent / "golden"
DOCUMENTS = sorted(path for path in GOLDEN.glob("*.json") if "." not in path.stem)
SAMPLES = Path(
    os.environ.get(
        "EZJWW_SAMPLES",
        Path(__file__).resolve().parents[3] / "ezjww" / "jww_samples",
    )
)
JWW_FILES = sorted(SAMPLES.rglob("*.jww")) if SAMPLES.is_dir() else []


def _document(entities: list[dict], **tables: Any) -> dict:
    return {
        "format": "cad2d-ir",
        "version": "0.3.0",
        "header": {"units": "mm", "angle_unit": "deg", "coord_space": "world"},
        "source": {"format": "unknown"},
        "tables": {"layers": {"0": {}}, **tables},
        "entities": entities,
    }


def _golden_document(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _diagnostic_rows(diagnostics: list[Any]) -> list[tuple]:
    return [(d.code, d.severity, d.message, d.entity_id, d.action) for d in diagnostics]


def _dicts(diagnostics: list[Any]) -> list[dict]:
    return [dataclasses.asdict(diagnostic) for diagnostic in diagnostics]


# --- the two packages ---------------------------------------------------------


def test_the_native_code_is_of_the_version_of_cad2d_ir() -> None:
    assert native.__version__ == cad2d_ir.__version__
    assert native_core() is native


def test_native_code_of_another_version_is_refused(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import cad2d_ir._native as loader

    monkeypatch.setattr(loader, "_core", None)
    monkeypatch.setattr(native, "__version__", "0.0.1")
    with pytest.raises(ImportError, match="cad2d-ir-native"):
        native_core()
    with pytest.raises(ImportError):
        cad2d_ir.ir_to_dxf(_document([]))


# --- the typed document ---------------------------------------------------------


@pytest.mark.parametrize("path", DOCUMENTS, ids=lambda path: path.stem)
def test_a_document_is_the_same_dict_after_the_typed_ir(path: Path) -> None:
    document = _golden_document(path)
    assert native.document_roundtrip(document) == document
    assert native.NativeDocument.from_dict(document).to_dict() == document
    # the dict is not touched
    assert document == _golden_document(path)


@pytest.mark.parametrize("path", DOCUMENTS, ids=lambda path: path.stem)
def test_the_typed_document_is_written_like_the_dict(path: Path) -> None:
    document = _golden_document(path)
    held = native.NativeDocument.from_dict(document)
    for version in ("AC1024", "AC1009"):
        diagnostics: list[Any] = []
        entity_map: list[Any] = []
        text = cad2d_ir.ir_to_dxf(
            copy.deepcopy(document),
            validate=False,
            target_version=version,
            diagnostics=diagnostics,
            entity_map=entity_map,
        )
        from_typed = held.to_dxf(target_version=version, entity_map=True)
        assert from_typed[0] == text
        assert from_typed[1] == _diagnostic_rows(diagnostics)
        assert from_typed[2] == entity_map
        if version == "AC1024":
            data, rows = held.to_dxf_bytes()
            assert data == text.encode("utf-8")
            assert rows == _diagnostic_rows(diagnostics)
    # other options reach the writer the same way
    text = cad2d_ir.ir_to_dxf(
        copy.deepcopy(document),
        validate=False,
        target_version="AC1009",
        curve_segments=16,
        generic_dimensions="skip",
    )
    assert (
        held.to_dxf(
            target_version="AC1009", curve_segments=16, generic_dimensions="skip"
        )[0]
        == text
    )
    assert held.to_dxf_bytes(curve_segments=16, generic_dimensions="skip")[
        0
    ] == cad2d_ir.ir_to_dxf(
        copy.deepcopy(document),
        validate=False,
        curve_segments=16,
        generic_dimensions="skip",
    ).encode("utf-8")


def test_a_document_that_is_used_up_is_written_the_same() -> None:
    document = _golden_document(GOLDEN / "entity_kinds.json")
    text = cad2d_ir.ir_to_dxf(copy.deepcopy(document), validate=False)
    used_up = copy.deepcopy(document)
    assert cad2d_ir.ir_to_dxf(used_up, validate=False, consume=True) == text
    assert used_up["entities"] == []
    assert used_up["tables"] == document["tables"]


PLAIN_KINDS = {
    "LINE",
    "CIRCLE",
    "ARC",
    "POINT",
    "ELLIPSE",
    "TEXT",
    "INSERT",
    "HATCH",
    "LWPOLYLINE",
}


def _entity_lists(document: dict) -> list[list]:
    """The entities of model space and of the blocks."""
    lists = []
    if isinstance(document.get("entities"), list):
        lists.append(document["entities"])
    tables = document.get("tables")
    blocks = tables.get("blocks") if isinstance(tables, dict) else None
    for block in blocks.values() if isinstance(blocks, dict) else ():
        if isinstance(block, dict) and isinstance(block.get("entities"), list):
            lists.append(block["entities"])
    return lists


def _with_provenance(document: dict) -> dict:
    """The document with what an importer leaves on every entity: where it came
    from (``source``) and ``metadata`` - here with the keys the writer reads of
    an MTEXT and of an IMAGE, on entities of every kind."""
    marked = copy.deepcopy(document)
    for entities in _entity_lists(marked):
        for index, entity in enumerate(entities):
            if not isinstance(entity, dict):
                continue
            entity["source"] = {"format": "dwg", "id": f"0x{index:X}", "kind": "CIRCLE"}
            if entity.get("kind") in PLAIN_KINDS:
                entity["metadata"] = {
                    "dwg": {"handle": f"0x{index:X}", "raw_text": "\\fArial;stored"},
                    "dxf": {
                        "image": {"size": [8, 8], "fade": 50},
                        "layer": "elsewhere",
                    },
                    "jww": {"pen_color": 3, "pen_width": 9},
                }
    return marked


@pytest.mark.parametrize("path", DOCUMENTS, ids=lambda path: path.stem)
def test_the_writer_reads_no_provenance_of_an_entity(path: Path) -> None:
    """A ``dict`` that is only written is converted without the ``source`` of
    its entities and without the ``metadata`` of the kinds that are written
    from their fields alone (it is a large part of an imported drawing). The
    typed document keeps both, and the two are written the same."""
    document = _golden_document(path)
    marked = _with_provenance(document)
    assert marked != document or not any(_entity_lists(document))
    for version in ("AC1024", "AC1009"):
        plain = native.write_dxf(
            copy.deepcopy(document), target_version=version, entity_map=True
        )
        from_dict = native.write_dxf(
            copy.deepcopy(marked), target_version=version, entity_map=True
        )
        from_typed = native.NativeDocument.from_dict(marked).to_dxf(
            target_version=version, entity_map=True
        )
        assert from_dict == from_typed
        assert from_dict == plain
    # the typed document does keep them
    assert native.NativeDocument.from_dict(marked).to_dict() == marked


@pytest.mark.parametrize(
    "tables",
    [
        # a block without entities next to one with them
        {
            "blocks": {
                "B": {
                    "entities": [
                        {"id": "L", "kind": "LINE", "p1": [0.0, 0.0], "p2": [1.0, 1.0]}
                    ]
                },
                "C": {"base_point": [0.0, 0.0], "note": "empty"},
            }
        },
        # entities that are no list, or no list of dicts
        {"blocks": {"B": {"entities": "none"}}},
        {"blocks": {"B": {"entities": [1, {"kind": "LINE"}]}, "C": {"entities": []}}},
        # a block that is no dict, a block table that is none
        {"blocks": {"B": 5, "C": {"entities": []}}},
        {"blocks": []},
        {"layers": {"0": {}}},
    ],
)
def test_odd_block_tables_survive_the_typed_ir(tables: dict) -> None:
    line = {"id": "L1", "kind": "LINE", "p1": [0.0, 0.0], "p2": [1.0, 1.0]}
    document = _document([line])
    document["tables"] = tables
    expected = copy.deepcopy(document)
    assert native.document_roundtrip(document) == expected
    assert native.NativeDocument.from_dict(document).to_dict() == expected
    assert document == expected


@pytest.mark.parametrize(
    "entities", ["none", [1, 2], [{"kind": "LINE"}, None], {"a": 1}]
)
def test_entities_that_are_no_list_of_dicts_survive_the_typed_ir(entities: Any) -> None:
    document = _document([])
    document["entities"] = entities
    expected = copy.deepcopy(document)
    assert native.document_roundtrip(document) == expected
    with pytest.raises(ValueError):
        native.write_dxf(document)


def test_values_of_any_size_and_kind_survive_the_typed_ir() -> None:
    line = {"id": "L1", "kind": "LINE", "p1": [0.0, 0.0], "p2": [1.0, 1.0]}
    document = _document(
        [
            # integers Rust has no type for, and values that are not of the
            # type the schema gives them: all kept as they are
            {
                **line,
                "metadata": {
                    "large": 2**70,
                    "negative": -(2**65),
                    "nested": [None, True, 1.5],
                },
            },
            {**line, "id": "L2", "p1": "nowhere", "layer": None, "color": [1, 2, 3]},
            {"id": "X1", "kind": "FUTURE_ENTITY", "anything": {"a": [1, {"b": None}]}},
        ]
    )
    assert native.document_roundtrip(document) == document
    kept = native.NativeDocument.from_dict(document).to_dict()
    assert kept == document
    assert kept["entities"][0]["metadata"]["large"] == 2**70


def test_the_writer_passes_over_values_json_does_not_have() -> None:
    """The typed document refuses a value it could not give back; the writer,
    which only reads, ignores it where it is not what is written."""
    document = _document(
        [
            {
                "id": "L1",
                "kind": "LINE",
                "p1": [0.0, 0.0],
                "p2": [1.0, 1.0],
                "metadata": {
                    "raw": b"bytes",
                    "set": {1, 2},
                    7: "a key that is no text",
                },
            }
        ]
    )
    plain = _document(
        [{"id": "L1", "kind": "LINE", "p1": [0.0, 0.0], "p2": [1.0, 1.0]}]
    )
    assert cad2d_ir.ir_to_dxf(document, validate=False) == cad2d_ir.ir_to_dxf(plain)
    with pytest.raises(TypeError):
        native.NativeDocument.from_dict(document)
    with pytest.raises(TypeError):
        native.document_roundtrip(document)
    with pytest.raises(TypeError):
        native.write_dxf([])


def test_halves_of_surrogate_pairs_do_not_stop_the_writer() -> None:
    """A text from a lenient decoder can hold the two halves of a character as
    two code points, or one half alone. Rust has no such text: the pair is the
    character it stands for, and a half alone the replacement character."""
    text = "a😀b \ud800 c"
    document = _document(
        [
            {
                "id": "T1",
                "kind": "TEXT",
                "insert": [0.0, 0.0],
                "height": 2.5,
                "text": text,
            }
        ]
    )
    written = cad2d_ir.ir_to_dxf(document, validate=False)
    assert "\na\U0001f600b � c\n" in written
    assert (
        native.document_roundtrip(document)["entities"][0]["text"] == "a\U0001f600b � c"
    )


def test_entities_of_a_kind_can_be_read_and_changed() -> None:
    image = {
        "id": "IM1",
        "kind": "IMAGE",
        "insert": [0.0, 0.0],
        "width": 4.0,
        "height": 2.0,
    }
    line = {"id": "L1", "kind": "LINE", "p1": [0.0, 0.0], "p2": [1.0, 1.0]}
    document = _document([line, {**image, "name": "a.png"}])
    document["tables"]["blocks"] = {
        "b": {
            "base_point": [0.0, 0.0],
            "entities": [{**image, "id": "IM2", "name": "b.png"}],
        }
    }
    held = native.NativeDocument.from_dict(document)
    assert held.entity_count() == 2
    found = held.entities_of_kind("IMAGE")
    assert [(block, index, entity["name"]) for block, index, entity in found] == [
        (None, 1, "a.png"),
        ("b", 0, "b.png"),
    ]
    assert held.entities_of_kind("LINE")[0][2] == line
    held.set_entity_value(None, 1, "name", "plan_a.png")
    held.set_entity_value("b", 0, "name", "plan_b.png")
    held.set_entity_value(None, 0, "layer", "線")
    changed = held.to_dict()
    assert changed["entities"][1]["name"] == "plan_a.png"
    assert changed["tables"]["blocks"]["b"]["entities"][0]["name"] == "plan_b.png"
    assert changed["entities"][0] == {**line, "layer": "線"}
    with pytest.raises(ValueError):
        held.set_entity_value("missing", 0, "name", "x")
    with pytest.raises(ValueError):
        held.set_entity_value(None, 9, "name", "x")


def test_the_bytes_of_an_image_are_read_without_its_text() -> None:
    """``data`` can be left out of the dicts and read as bytes: an image can be very large."""
    raw = bytes(range(256)) * 5 + b"xy"
    image = {
        "id": "IM1",
        "kind": "IMAGE",
        "insert": [0.0, 0.0],
        "width": 4.0,
        "height": 2.0,
    }
    document = _document(
        [
            {**image, "data": base64.b64encode(raw).decode("ascii"), "name": "a.png"},
            {**image, "id": "IM2", "data": "not base64!", "name": "b.png"},
            {**image, "id": "IM3", "href": "c.png"},
            {**image, "id": "IM4", "data": ""},
        ]
    )
    held = native.NativeDocument.from_dict(document)
    found = held.entities_of_kind("IMAGE", without=["data", "width"])
    assert [sorted(entity) for _, _, entity in found] == [
        ["height", "id", "insert", "kind", "name"],
        ["height", "id", "insert", "kind", "name"],
        ["height", "href", "id", "insert", "kind"],
        ["height", "id", "insert", "kind"],
    ]
    assert held.entity_bytes(None, 0, "data") == raw
    with pytest.raises(ValueError):
        held.entity_bytes(None, 1, "data")
    assert held.entity_bytes(None, 2, "data") is None
    assert held.entity_bytes(None, 3, "data") == b""
    # what base64.b64decode(validate=True) reads and raises on
    texts = (
        "",
        "QQ==",
        "QUI=",
        "QUJD",
        "QR==",
        "QUJDRA==",
        "QUJD\n",
        "QU-D",
        "Q===",
        "QUI",
        "=QUJ",
    )
    for text in texts:
        held = native.NativeDocument.from_dict(_document([{**image, "data": text}]))
        try:
            expected = base64.b64decode(text, validate=True)
        except ValueError:
            with pytest.raises(ValueError):
                held.entity_bytes(None, 0, "data")
        else:
            assert held.entity_bytes(None, 0, "data") == expected, text


def test_absurd_numbers_are_counted_outside_metadata() -> None:
    document = native.NativeDocument.from_dict(
        _document(
            [
                {"kind": "LINE", "p1": [0.0, 0.0], "p2": [1e16, 0.0]},
                {"kind": "LINE", "p1": [0.0, 0.0], "p2": [1.0, float("nan")]},
                {
                    "kind": "LINE",
                    "p1": [0.0, 0.0],
                    "p2": [1.0, 2.0],
                    "metadata": {"x": 1e300},
                },
                {
                    "kind": "TEXT",
                    "insert": [0.0, 0.0],
                    "height": 2.5,
                    "text": "a",
                    "note": [1e300],
                },
            ]
        )
    )
    assert document.entity_count() == 4
    assert document.absurd_entities(1e15) == 3


def test_curves_are_sampled_for_the_readers_of_cad2d_ir() -> None:
    whole = native.sample_ellipse([0.0, 0.0], [2.0, 0.0], 0.5, 0.0, 2 * math.pi, 8)
    assert len(whole) == 8 and whole[0] == [2.0, 0.0]
    part = native.sample_ellipse([0.0, 0.0], [2.0, 0.0], 0.5, 0.0, 1.0, 96)
    assert len(part) == 17
    points = native.sample_spline(
        [[0.0, 0.0], [1.0, 2.0], [3.0, 2.0], [4.0, 0.0]], 3, None, None, 8
    )
    assert len(points) == 9 and points[0] == [0.0, 0.0] and points[-1] == [4.0, 0.0]
    with pytest.raises(ValueError):
        native.sample_spline([[0.0, 0.0], [1.0, 1.0]], 1, [0.0, 0.0, 0.0, 0.0], None, 8)


# --- the JWW importer: a file, or the dict of ezjww ----------------------------


def _base(
    layer: int = 0, group: int = 0, color: int = 2, style: int = 1, width: int = 0
) -> dict:
    return {
        "flag": 0,
        "group": 0,
        "layer": layer,
        "layer_group": group,
        "pen_color": color,
        "pen_style": style,
        "pen_width": width,
    }


def _circle(
    base: dict, center: tuple[float, float], radius: float, **shape: Any
) -> dict:
    return {
        "type": "CIRCLE",
        "base": base,
        "center_x": center[0],
        "center_y": center[1],
        "radius": radius,
        "start_angle": 0.0,
        "arc_angle": 2 * math.pi,
        "tilt_angle": 0.0,
        "flatness": 1.0,
        "is_full_circle": True,
        **shape,
    }


def _text(
    base: dict,
    start: tuple[float, float],
    end: tuple[float, float],
    content: str,
    **shape: Any,
) -> dict:
    return {
        "type": "TEXT",
        "base": base,
        "start_x": start[0],
        "start_y": start[1],
        "end_x": end[0],
        "end_y": end[1],
        "text_type": 0,
        "size_x": 4.0,
        "size_y": 5.0,
        "spacing": 0.5,
        "angle": 0.0,
        "font_name": "ＭＳ ゴシック",
        "content": content,
        **shape,
    }


@pytest.fixture
def jww_file(tmp_path: Path) -> Path:
    """A small drawing with every kind of entity ``ezjww`` writes."""
    ezjww = pytest.importorskip("ezjww")
    document = ezjww.new_jww_document()
    document["options"]["memo"] = "ファイルと辞書"
    document["entities"] = [
        {
            "type": "LINE",
            "base": _base(1),
            "start_x": 0.0,
            "start_y": 0.0,
            "end_x": 100.0,
            "end_y": 50.0,
        },
        {
            "type": "LINE",
            "base": _base(2, 1, 5, 3, 4),
            "start_x": -10.5,
            "start_y": 1 / 3,
            "end_x": 0.1,
            "end_y": 0.2,
        },
        _circle(_base(3), (10.0, 20.0), 5.0),
        _circle(
            _base(3, 2, 1),
            (-4.0, 8.0),
            7.5,
            type="ARC",
            start_angle=0.5,
            arc_angle=1.25,
            is_full_circle=False,
        ),
        # an ellipse and a part of one
        _circle(_base(4), (30.0, 0.0), 12.0, tilt_angle=0.3, flatness=0.5),
        _circle(
            _base(4),
            (60.0, 0.0),
            9.0,
            type="ARC",
            start_angle=1.0,
            arc_angle=2.0,
            tilt_angle=-0.7,
            flatness=0.25,
            is_full_circle=False,
        ),
        {
            "type": "POINT",
            "base": _base(5, 0, 5),
            "x": 1.0,
            "y": 2.0,
            "is_temporary": False,
            "code": 0,
            "angle": 0.0,
            "scale": 0.0,
        },
        _text(_base(6, 0, 5), (5.0, 6.0), (45.0, 6.0), "図面 A-1 ｱｲｳ"),
        _text(
            _base(6, 0, 5),
            (0.0, -20.0),
            (0.0, 10.0),
            "縦 90°",
            size_x=3.0,
            size_y=3.0,
            spacing=0.0,
            angle=90.0,
            font_name="ＭＳ 明朝",
        ),
        {
            "type": "SOLID",
            "base": _base(7, 0, 10),
            "color": 12632256,
            "point1_x": 0.0,
            "point1_y": 6.0,
            "point2_x": 6.0,
            "point2_y": 0.0,
            "point3_x": 6.0,
            "point3_y": 6.0,
            "point4_x": 0.0,
            "point4_y": 0.0,
        },
        {
            "type": "BLOCK",
            "base": _base(8),
            "def_number": 0,
            "ref_x": 50.0,
            "ref_y": -25.0,
            "rotation": 0.5,
            "scale_x": 2.0,
            "scale_y": 1.0,
        },
        {
            "type": "BLOCK",
            "base": _base(8),
            "def_number": 0,
            "ref_x": -50.0,
            "ref_y": 25.0,
            "rotation": 0.0,
            "scale_x": 1.0,
            "scale_y": 1.0,
        },
    ]
    document["block_defs"] = [
        {
            "base": _base(),
            "number": 0,
            "is_referenced": True,
            "name": "部品",
            "entities": [
                {
                    "type": "LINE",
                    "base": _base(1),
                    "start_x": 0.0,
                    "start_y": 0.0,
                    "end_x": 1.0,
                    "end_y": 0.0,
                },
                _circle(_base(1), (0.0, 0.0), 1.0),
            ],
        }
    ]
    path = tmp_path / "図面.jww"
    path.write_bytes(ezjww.to_jww_bytes(document))
    return path


def _from_dict(path: Path, options: ImportOptions) -> Any:
    """The import of a file through the ``dict`` of ``ezjww.read_document``."""
    import ezjww

    return jww_document_to_ir(
        ezjww.read_document(str(path)),
        source_name=path.name,
        source_sha256=hashlib.sha256(path.read_bytes()).hexdigest(),
        options=options,
    )


@pytest.mark.parametrize(
    "options",
    [
        ImportOptions(strict=False),
        ImportOptions(strict=False, entity_provenance=False),
        ImportOptions(strict=True),
    ],
    ids=["lenient", "no provenance", "strict"],
)
def test_a_jww_file_and_its_dict_give_the_same_import(
    jww_file: Path, options: ImportOptions
) -> None:
    from_file = convert_jww_file_to_ir(jww_file, options=options)
    from_dict = _from_dict(jww_file, options)
    assert from_file.document == from_dict.document
    assert _dicts(from_file.diagnostics) == _dicts(from_dict.diagnostics)
    assert from_file.statistics == from_dict.statistics

    statistics = from_file.statistics
    assert statistics["source_entities"] == 12
    assert statistics["skipped_entities"] == 0
    assert statistics["converted_entity_counts"] == {
        "ARC": 1,
        "CIRCLE": 2,
        "ELLIPSE": 2,
        "HATCH": 1,
        "INSERT": 2,
        "LINE": 3,
        "POINT": 1,
        "TEXT": 2,
    }
    document = from_file.document
    assert document["source"]["name"] == jww_file.name
    assert document["header"]["metadata"]["jww"]["memo"] == "ファイルと辞書"
    assert [
        entity["kind"] for entity in document["tables"]["blocks"]["部品"]["entities"]
    ] == [
        "LINE",
        "CIRCLE",
    ]


def test_an_import_kept_in_native_code_is_the_same_document(jww_file: Path) -> None:
    options = ImportOptions(strict=False, entity_provenance=False)
    reference = convert_jww_file_to_ir(jww_file, options=options)
    held = import_jww_file(jww_file, options=options)
    assert isinstance(held, NativeImportResult)
    assert isinstance(held.document, native.NativeDocument)
    assert held.document.entity_count() == len(reference.document["entities"])
    assert _dicts(held.diagnostics) == _dicts(reference.diagnostics)
    assert held.statistics == reference.statistics
    assert held.document.to_dict() == reference.document

    # written from native code without the entities becoming dicts
    diagnostics: list[Any] = []
    text = cad2d_ir.ir_to_dxf(reference.document, diagnostics=diagnostics)
    data, rows = held.document.to_dxf_bytes()
    assert data == text.encode("utf-8")
    assert rows == _diagnostic_rows(diagnostics)


def test_a_file_that_is_no_drawing_is_an_error(tmp_path: Path) -> None:
    pytest.importorskip("ezjww")
    path = tmp_path / "broken.jww"
    path.write_bytes(b"not a drawing")
    with pytest.raises(ValueError):
        import_jww_file(path)
    with pytest.raises(ValueError):
        convert_jww_file_to_ir(path)
    with pytest.raises(OSError):
        import_jww_file(tmp_path / "missing.jww")


needs_samples = pytest.mark.skipif(
    not JWW_FILES, reason="no JWW samples (ezjww/jww_samples)"
)


@needs_samples
def test_sample_drawings_and_their_dicts_give_the_same_import() -> None:
    pytest.importorskip("ezjww")
    options = ImportOptions(strict=False)
    for path in JWW_FILES:
        from_file = convert_jww_file_to_ir(path, options=options)
        from_dict = _from_dict(path, options)
        assert from_file.document == from_dict.document, path.name
        assert _dicts(from_file.diagnostics) == _dicts(from_dict.diagnostics), path.name
        assert from_file.statistics == from_dict.statistics, path.name


@needs_samples
def test_sample_drawings_are_written_from_native_code_like_from_their_dicts() -> None:
    pytest.importorskip("ezjww")
    options = ImportOptions(strict=False, entity_provenance=False)
    for path in JWW_FILES:
        held = import_jww_file(path, options=options)
        document = held.document.to_dict()
        diagnostics: list[Any] = []
        text = cad2d_ir.ir_to_dxf(document, validate=False, diagnostics=diagnostics)
        data, rows = held.document.to_dxf_bytes()
        assert data == text.encode("utf-8"), path.name
        assert rows == _diagnostic_rows(diagnostics), path.name
