"""The native IR core against the Python reference.

Run after ``maturin develop`` (or with the built wheel on the path). The JWW
tests read sample drawings from the ``ezjww`` repository next to this one
(``EZJWW_SAMPLES`` names another directory) and are skipped without them.
"""

from __future__ import annotations

import copy
import dataclasses
import os
from pathlib import Path

import pytest

native = pytest.importorskip("cad2d_ir_native")

import cad2d_ir  # noqa: E402
from cad2d_ir.importers.base import ImportOptions  # noqa: E402
from cad2d_ir.importers.jww import convert_jww_file_to_ir  # noqa: E402
from cad2d_ir_native.jww import convert_jww_file  # noqa: E402

SAMPLES = Path(
    os.environ.get(
        "EZJWW_SAMPLES",
        Path(__file__).resolve().parents[3] / "ezjww" / "jww_samples",
    )
)
JWW_FILES = sorted(SAMPLES.rglob("*.jww")) if SAMPLES.is_dir() else []


def _diagnostic_rows(diagnostics) -> list[tuple]:
    return [(d.code, d.severity, d.message, d.entity_id, d.action) for d in diagnostics]


def _document(entities: list[dict], **tables) -> dict:
    return {
        "format": "cad2d-ir",
        "version": "0.3.0",
        "header": {"units": "mm", "angle_unit": "deg", "coord_space": "world"},
        "source": {"format": "unknown"},
        "tables": {"layers": {"0": {}}, **tables},
        "entities": entities,
    }


def _same_as_python(document: dict) -> None:
    assert native.document_roundtrip(document) == document
    reference = cad2d_ir.convert_ir_to_dxf_text(copy.deepcopy(document), target_version="AC1024")
    text, diagnostics = native.ir_to_dxf(document)
    assert text == reference.dxf_text
    assert diagnostics == _diagnostic_rows(reference.diagnostics)
    from_native, _ = native.NativeDocument.from_dict(document).to_dxf()
    assert from_native == reference.dxf_text
    as_bytes, _ = native.NativeDocument.from_dict(document).to_dxf_bytes()
    assert as_bytes == reference.dxf_text.encode("utf-8")


def test_basic_entities_are_written_like_the_python_writer() -> None:
    _same_as_python(
        _document(
            [
                {"id": "L1", "kind": "LINE", "layer": "0", "p1": [0, 0], "p2": [100.5, 1 / 3]},
                {"id": "C1", "kind": "CIRCLE", "layer": "壁", "center": [10, 10], "radius": 2.5},
                {
                    "id": "A1",
                    "kind": "ARC",
                    "layer": "壁",
                    "linetype": "DASHED",
                    "color": 3,
                    "lineweight_mm": 0.25,
                    "center": [0, 0],
                    "radius": 5,
                    "start_angle": 0.0,
                    "end_angle": 123.456789012345,
                    "ccw": True,
                },
                {"id": "P1", "kind": "POINT", "position": [1e-7, -0.0], "temporary": True},
                {
                    "id": "E1",
                    "kind": "ELLIPSE",
                    "center": [0, 0],
                    "major_axis": [10, 0],
                    "ratio": 0.5,
                    "start_param": 0.0,
                    "end_param": 3.0,
                    "ccw": False,
                },
                {
                    "id": "W1",
                    "kind": "LWPOLYLINE",
                    "vertices": [[0, 0], [10, 0, 0.5], [10, 10]],
                    "closed": True,
                },
            ],
            linetypes={"DASHED": {"description": "dashes", "pattern_mm": [3.0, -1.5]}},
        )
    )


def test_text_tables_and_names_are_written_like_the_python_writer() -> None:
    _same_as_python(
        _document(
            [
                {
                    "id": "T1",
                    "kind": "TEXT",
                    "layer": "注記/寸法",  # a name AutoCAD does not allow
                    "insert": [1, 2],
                    "height": 2.5,
                    "text": "割合 5%% と ^ と %%c10",
                    "rotation": 30.0,
                    "style": "ＭＳ ゴシック",
                    "width_factor": 0.8,
                    "color": "#FF8000",
                },
                {"id": "T2", "kind": "TEXT", "layer": "注記_寸法", "insert": [0, 0], "height": 3, "text": "a\nb"},
                {"id": "L1", "kind": "LINE", "layer": "HIDDEN", "linetype": "", "p1": [0, 0], "p2": [1, 1], "visible": False},
            ],
            layers={
                "0": {},
                "HIDDEN": {"color": 5, "visible": False, "lineweight_mm": 0.175, "plot": False},
                "TRUE": {"color": "#102030", "linetype": "CENTER"},
            },
            text_styles={"ＭＳ ゴシック": {"font": "ＭＳ ゴシック"}, "STANDARD": {"font": "txt"}},
        )
    )


def test_blocks_inserts_and_hatches_are_written_like_the_python_writer() -> None:
    document = _document(
        [
            {"id": "I1", "kind": "INSERT", "block": "door", "insert": [5, 5], "rotation": 90.0, "scale": 2.0},
            {"id": "I2", "kind": "INSERT", "block": "DOOR", "insert": [0, 0], "scale": [1.0, -1.0]},
            {
                "id": "H1",
                "kind": "HATCH",
                "solid": True,
                "loops": [
                    {"vertices": [[0, 0], [4, 0], [4, 4], [0, 4]], "is_outer": True},
                    {"vertices": [[1, 1], [2, 1, 1.0], [2, 2]], "is_outer": False},
                ],
            },
        ]
    )
    document["tables"]["blocks"] = {
        "door": {
            "base_point": [0.0, 0.0],
            "entities": [
                {"id": "B1", "kind": "LINE", "layer": "0", "p1": [0, 0], "p2": [0.9, 0]},
                {"id": "B2", "kind": "ARC", "center": [0, 0], "radius": 0.9, "start_angle": 0, "end_angle": 90},
            ],
        }
    }
    document["constraints"] = [{"kind": "COLLINEAR", "refs": [{"entity": "I1", "feature": "line"}, {"entity": "I2", "feature": "line"}]}]
    _same_as_python(document)


def _base64(raw: bytes) -> str:
    import base64

    return base64.b64encode(raw).decode("ascii")


def _png_header(width: int, height: int) -> bytes:
    """The first bytes of a PNG file: enough for the size in pixels."""
    return (
        b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
        + width.to_bytes(4, "big")
        + height.to_bytes(4, "big")
        + b"\x08\x02\x00\x00\x00"
    )


def test_images_are_written_like_the_python_writer() -> None:
    image = {"kind": "IMAGE", "insert": [10.0, 20.0], "width": 40.0, "height": 20.0}
    png = {"data": _base64(_png_header(400, 200)), "mime_type": "image/png"}
    bmp = b"BM" + bytes(12) + (40).to_bytes(4, "little") + (64).to_bytes(4, "little")
    bmp += (-32).to_bytes(4, "little", signed=True)
    gif = b"GIF89a" + (30).to_bytes(2, "little") + (15).to_bytes(2, "little")
    jpeg = b"\xff\xd8\xff\xe0\x00\x04ab\xff\xc0\x00\x0b\x08\x00\x09\x00\x10"
    entities = [
        {**image, **png, "id": "IM1", "name": "logo.png", "rotation": 30.0, "layer": "図"},
        # the same file again: one definition, two reactors
        {**image, **png, "id": "IM2", "name": "logo.png", "insert": [100, 0], "color": 3},
        {**image, **png, "id": "IM3", "name": "dir\\LOGO.bmp", "rotation": 0},
        # a linked image: the size in pixels is assumed
        {**image, "id": "IM4", "href": " C:\\img\\site plan.jpg ", "width": 12.5},
        # no name: the id and the extension of the kind of file
        {**image, "id": "IM5", "data": _base64(bmp), "mime_type": "image/bmp"},
        {**image, "id": "IM6", "data": _base64(gif), "mime_type": "IMAGE/GIF ", "name": "a:b?"},
        {**image, "id": "IM7", "data": _base64(jpeg), "name": "photo", "visible": False},
        {**image, "id": "IM8", "data": _base64(b"not an image"), "name": "x.dat"},
        # the size the source recorded, and its display values
        {
            **image,
            "id": "IM9",
            "name": "scan.tif",
            "metadata": {"dxf": {"image": {"size": [800, 400.4], "brightness": 70, "fade": 200}}},
        },
        {"id": "L1", "kind": "LINE", "p1": [0, 0], "p2": [1, 1]},
    ]
    document = _document(entities)
    document["header"]["units"] = "m"
    document["tables"]["blocks"] = {
        "sheet": {"base_point": [0.0, 0.0], "entities": [{**image, **png, "id": "B1", "name": "b"}]}
    }
    _same_as_python(document)
    text, diagnostics = native.ir_to_dxf(document)
    # one definition for a file, one reactor for an image
    assert text.count("\n0\nIMAGEDEF\n") == 9
    assert text.count("\n0\nIMAGEDEF_REACTOR\n") == 10
    codes = [row[0] for row in diagnostics]
    assert codes.count("DXF_IMAGE_SIZE_ASSUMED") == 2
    assert codes.count("DXF_IMAGE_REFERENCED") == 8
    # a drawing without images has no image classes or objects
    plain, _ = native.ir_to_dxf(_document(entities[-1:]))
    assert "IMAGEDEF" not in plain and "ACAD_IMAGE_DICT" not in plain


def test_entities_of_a_kind_can_be_read_and_changed() -> None:
    image = {"id": "IM1", "kind": "IMAGE", "insert": [0.0, 0.0], "width": 4.0, "height": 2.0}
    line = {"id": "L1", "kind": "LINE", "p1": [0.0, 0.0], "p2": [1.0, 1.0]}
    document = _document([line, {**image, "name": "a.png"}])
    document["tables"]["blocks"] = {
        "b": {"base_point": [0.0, 0.0], "entities": [{**image, "id": "IM2", "name": "b.png"}]}
    }
    held = native.NativeDocument.from_dict(document)
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
    import base64

    raw = bytes(range(256)) * 5 + b"xy"
    image = {"id": "IM1", "kind": "IMAGE", "insert": [0.0, 0.0], "width": 4.0, "height": 2.0}
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
    for text in ("", "QQ==", "QUI=", "QUJD", "QR==", "QUJDRA==", "QUJD\n", "QU-D", "Q===", "QUI", "=QUJ"):
        document = _document([{**image, "data": text}])
        held = native.NativeDocument.from_dict(document)
        try:
            expected = base64.b64decode(text, validate=True)
        except ValueError:
            with pytest.raises(ValueError):
                held.entity_bytes(None, 0, "data")
        else:
            assert held.entity_bytes(None, 0, "data") == expected, text


def _jww_dimension(**source) -> dict:
    """A GENERIC dimension as the JWW importer makes it: the records it was made of."""
    geometry = {
        "line": {"start_x": 0.0, "start_y": 10.0, "end_x": 50.0, "end_y": 10.0},
        "text": {
            "start_x": 20.0,
            "start_y": 11.0,
            "end_x": 30.0,
            "end_y": 11.0,
            "content": "50",
            "size_x": 2.0,
            "size_y": 2.5,
            "angle": 0.0,
            "font_name": "ＭＳ ゴシック",
        },
        "sxf_mode": 1,
        "aux_lines": [
            {"start_x": 0.0, "start_y": 0.0, "end_x": 0.0, "end_y": 12.0},
            {"start_x": 50.0, "start_y": 0.0, "end_x": 50.0, "end_y": 12.0},
            # the dimension line again, the other way round: written once
            {"start_x": 50.0, "start_y": 10.0, "end_x": 0.0, "end_y": 10.0},
        ],
        "aux_points": [{"x": 0.0, "y": 10.0}, {"x": 50.0, "y": 10.0}],
    }
    geometry.update(source)
    return {
        "kind": "DIMENSION",
        "dim_kind": "GENERIC",
        "definition": {
            "points": {"location": [0.0, 10.0], "p1": [0.0, 10.0], "p2": [50.0, 10.0]},
            "text": "50",
            "source_geometry": geometry,
        },
    }


def test_generic_dimensions_are_written_as_what_they_were_made_of() -> None:
    tilted = {"start_x": 5, "start_y": 30, "content": "縦 5%%", "size_x": 3, "size_y": 2, "angle": 90}
    rendered = {
        "rendered_paths": [
            {"points": [[0, 0], [10, 0], [10, 5]], "closed": 1},
            {"points": [[0, 0], [20, 0]]},
            {"points": [[20, 0], [0, 0]]},
            {"points": [[0, 0]]},
            {"points": [[0, 0], [1, 1], "x"]},
        ],
        "rendered_texts": [
            {"anchor": [5, 5], "height": 2.5, "text": "寸法", "angle_deg": 30},
            {"anchor": [5, 9], "height": 0, "text": ""},
            {"anchor": [5], "height": 2, "text": "no anchor"},
        ],
    }
    entities = [
        {
            **_jww_dimension(),
            "id": "D1",
            "layer": "寸法",
            "linetype": "DASHED",
            "linetype_scale": 2.0,
            "color": "#3366cc",
            "lineweight_mm": 0.35,
            "style": "寸法の書体",
        },
        # the auxiliary slots of mode 0 are unused storage
        {**_jww_dimension(sxf_mode=0), "id": "D2", "visible": False},
        {**_jww_dimension(sxf_mode=None, text=tilted), "id": "D3", "color": 3},
        {
            **_jww_dimension(text={"size_y": -1, "size_x": 0, "font_name": "  "}, aux_points="x"),
            "id": "D4",
        },
        {
            **_jww_dimension(
                line={"p1": [1, 2], "p2": [3, 4]}, text=None, aux_lines=[5, {"p1": [1]}]
            ),
            "id": "D5",
        },
        {"id": "D6", "kind": "DIMENSION", "dim_kind": "GENERIC", "definition": rendered},
        # only the definition points: a line and the text at its placement
        {
            "id": "D7",
            "kind": "DIMENSION",
            "dim_kind": "GENERIC",
            "definition": {
                "points": {"p1": [0, 0], "p2": [9, 0], "location": [4, 1]},
                "text": "9",
                "text_height": 3,
            },
        },
        {
            "id": "D8",
            "kind": "DIMENSION",
            "dim_kind": "GENERIC",
            "definition": {"points": {"text_midpoint": None}, "location": [7, 7], "text": "x"},
        },
        # no placement for the text, and nothing to write at all
        {"id": "D9", "kind": "DIMENSION", "dim_kind": "GENERIC", "definition": {"text": "y"}},
        {
            "id": "D10",
            "kind": "DIMENSION",
            "dim_kind": "GENERIC",
            "definition": {"text": "z", "text_height": 0},
        },
        {"id": "D11", "kind": "DIMENSION", "dim_kind": "GENERIC", "definition": {}},
    ]
    document = _document(entities)
    document["tables"]["blocks"] = {
        "detail": {"base_point": [0.0, 0.0], "entities": [{**_jww_dimension(), "id": "B1"}]}
    }
    _same_as_python(document)
    _, diagnostics = native.ir_to_dxf(document)
    codes = [row[0] for row in diagnostics]
    assert codes.count("DXF_GENERIC_DIMENSION_EXPLODED") == 11
    assert codes.count("DXF_GENERIC_DIMENSION_DEFAULTED") == 2
    assert codes.count("DXF_GENERIC_DIMENSION_GEOMETRY_MISSING") == 1


@pytest.mark.parametrize(
    "entity",
    [
        {"kind": "MTEXT", "insert": [0, 0], "height": 2.5, "text": "a"},
        {
            "kind": "DIMENSION",
            "dim_kind": "LINEAR",
            "definition": {"points": {"p1": [0, 0], "p2": [9, 0], "location": [0, 2]}},
        },
        {"kind": "DIMENSION", "dim_kind": "GENERIC", "definition": {"block": "*D1"}},
        {"kind": "SPLINE", "control_points": [[0, 0], [1, 1], [2, 0]], "degree": 2},
        {"kind": "TEXT", "insert": [0, 0], "height": 2.5, "text": "a", "halign": "center"},
        {"kind": "HATCH", "solid": False, "pattern": "ANSI31", "loops": [{"vertices": [[0, 0], [1, 0], [1, 1]]}]},
        {"kind": "LINE", "p1": [0, 0, 0], "p2": [1, 1, 1]},
        {"kind": "LINE", "layer": None, "p1": [0, 0], "p2": [1, 1]},
    ],
)
def test_what_the_writer_has_not_learnt_is_declined(entity: dict) -> None:
    document = _document([entity])
    assert native.document_roundtrip(document) == document
    with pytest.raises(native.Unsupported):
        native.ir_to_dxf(document)


def test_a_document_with_paper_space_is_declined() -> None:
    document = _document([{"kind": "LINE", "p1": [0, 0], "p2": [1, 1]}])
    document["layouts"] = [{"name": "Layout1", "entities": []}]
    with pytest.raises(native.Unsupported):
        native.ir_to_dxf(document)


def test_absurd_numbers_are_counted_outside_metadata() -> None:
    document = native.NativeDocument.from_dict(
        _document(
            [
                {"kind": "LINE", "p1": [0.0, 0.0], "p2": [1e16, 0.0]},
                {"kind": "LINE", "p1": [0.0, 0.0], "p2": [1.0, float("nan")]},
                {"kind": "LINE", "p1": [0.0, 0.0], "p2": [1.0, 2.0], "metadata": {"x": 1e300}},
                {"kind": "TEXT", "insert": [0.0, 0.0], "height": 2.5, "text": "a", "note": [1e300]},
            ]
        )
    )
    assert document.entity_count() == 4
    assert document.absurd_entities(1e15) == 3


needs_samples = pytest.mark.skipif(not JWW_FILES, reason="no JWW samples (ezjww/jww_samples)")


@needs_samples
@pytest.mark.parametrize("provenance", [False, True])
def test_jww_import_gives_the_document_of_the_python_importer(provenance: bool) -> None:
    options = ImportOptions(entity_provenance=provenance, strict=False)
    compared = 0
    for path in JWW_FILES:
        reference = convert_jww_file_to_ir(path, options=options)
        try:
            result = convert_jww_file(path, options=options)
        except native.Unsupported:
            continue
        compared += 1
        assert result.document.to_dict() == reference.document, path.name
        assert [dataclasses.asdict(d) for d in result.diagnostics] == [
            dataclasses.asdict(d) for d in reference.diagnostics
        ], path.name
        assert result.statistics == reference.statistics, path.name
    assert compared >= len(JWW_FILES) // 2


@needs_samples
def test_jww_to_dxf_gives_the_text_of_the_python_pipeline() -> None:
    options = ImportOptions(entity_provenance=False, strict=False)
    compared = 0
    for path in JWW_FILES:
        reference = convert_jww_file_to_ir(path, options=options)
        expected = cad2d_ir.convert_ir_to_dxf_text(reference.document, target_version="AC1024")
        try:
            data, diagnostics = convert_jww_file(path, options=options).document.to_dxf_bytes()
        except native.Unsupported:
            continue
        compared += 1
        assert data == expected.dxf_text.encode("utf-8"), path.name
        assert diagnostics == _diagnostic_rows(expected.diagnostics), path.name
    assert compared >= len(JWW_FILES) // 2
