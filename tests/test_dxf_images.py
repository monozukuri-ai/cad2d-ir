"""Raster images in DXF: IMAGE entities with their IMAGEDEF objects, written and read."""

from __future__ import annotations

import base64
import struct
import zlib
from typing import Any

import pytest

from cad2d_ir import convert_ir_to_dxf_text
from cad2d_ir.codecs.dxf import dxf_to_ir, ir_to_dxf
from cad2d_ir.schema import validate_ir

Pairs = list[tuple[int, str]]


def _png(width: int, height: int) -> bytes:
    def chunk(kind: bytes, payload: bytes) -> bytes:
        crc = zlib.crc32(kind + payload) & 0xFFFFFFFF
        return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", crc)

    rows = b"".join(b"\x00" + b"\xff\x00\x00" * width for _ in range(height))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(rows))
        + chunk(b"IEND", b"")
    )


def _bmp(width: int, height: int) -> bytes:
    """A 24-bit BMP; a negative height is a top-down bitmap."""
    stride = (width * 3 + 3) & ~3
    pixels = stride * abs(height)
    header = b"BM" + struct.pack("<IHHI", 54 + pixels, 0, 0, 54)
    info = struct.pack(
        "<IiiHHIIiiII", 40, width, height, 1, 24, 0, pixels, 2835, 2835, 0, 0
    )
    return header + info + b"\x00" * pixels


def _jpeg(width: int, height: int) -> bytes:
    """SOI, an APP0 segment, a baseline frame header and EOI."""
    app0 = (
        b"\xff\xe0"
        + struct.pack(">H", 16)
        + b"JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    )
    frame = (
        b"\xff\xc0" + struct.pack(">HBHHB", 11, 8, height, width, 1) + b"\x01\x11\x00"
    )
    return b"\xff\xd8" + app0 + frame + b"\xff\xd9"


def _b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode("ascii")


def _image(
    entity_id: str,
    insert: tuple[float, float],
    width: float,
    height: float,
    **extra: Any,
) -> dict[str, Any]:
    return {
        "id": entity_id,
        "kind": "IMAGE",
        "insert": list(insert),
        "width": width,
        "height": height,
        **extra,
    }


def _document(
    *entities: dict[str, Any],
    blocks: dict[str, Any] | None = None,
    layouts: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    document: dict[str, Any] = {
        "format": "cad2d-ir",
        "version": "0.3.0",
        "header": {"units": "mm", "angle_unit": "deg", "coord_space": "world"},
        "entities": list(entities),
    }
    if blocks:
        document["tables"] = {"blocks": blocks}
    if layouts:
        document["layouts"] = layouts
    return document


def _pairs(dxf_text: str) -> Pairs:
    lines = dxf_text.splitlines()
    return [(int(lines[i]), lines[i + 1]) for i in range(0, len(lines) - 1, 2)]


def _records(dxf_text: str, section: str) -> list[tuple[str, Pairs]]:
    """(record name, pairs) of one section of the written text."""
    pairs = _pairs(dxf_text)
    records: list[tuple[str, Pairs]] = []
    inside = False
    current: tuple[str, Pairs] | None = None
    for index, (code, value) in enumerate(pairs):
        if code == 0 and value == "SECTION":
            inside = pairs[index + 1] == (2, section)
            current = None
            continue
        if not inside:
            continue
        if code == 0 and value == "ENDSEC":
            break
        if code == 0:
            current = (value, [])
            records.append(current)
        elif current is not None:
            current[1].append((code, value))
    return records


def _values(pairs: Pairs, code: int) -> list[str]:
    return [value for pair_code, value in pairs if pair_code == code]


def _first(pairs: Pairs, code: int) -> str:
    return _values(pairs, code)[0]


def _text(pairs: list[tuple[int, object]]) -> str:
    return "".join(f"{code}\n{value}\n" for code, value in pairs)


def _import(dxf_text: str) -> tuple[dict[str, Any], list[Any], list[str]]:
    warnings: list[str] = []
    diagnostics: list[Any] = []
    document = dxf_to_ir(dxf_text, warnings=warnings, diagnostics=diagnostics)
    return document, diagnostics, warnings


def test_dxf_images_are_written_with_their_objects_and_round_trip() -> None:
    site = "C:\\pics\\site.jpg"
    document = _document(
        _image(
            "LOGO",
            (10.0, 20.0),
            40.0,
            30.0,
            name="logo",
            mime_type="image/png",
            data=_b64(_png(4, 3)),
            layer="Pictures",
        ),
        _image(
            "SITE",
            (0.0, 0.0),
            64.0,
            48.0,
            rotation=90.0,
            name="site.jpg",
            href=site,
            metadata={
                "dwg": {
                    "image": {
                        "size": [640, 480],
                        "brightness": 60,
                        "contrast": 40,
                        "fade": 10,
                    }
                }
            },
        ),
        _image(
            "SITE2",
            (100.0, 0.0),
            32.0,
            24.0,
            name="site.jpg",
            href=site,
            metadata={"dwg": {"image": {"size": [640, 480]}}},
        ),
        {"id": "L1", "kind": "LINE", "p1": [0.0, 0.0], "p2": [1.0, 0.0]},
    )

    result = convert_ir_to_dxf_text(document)
    text = result.dxf_text

    # CLASSES declares the four classes, before TABLES; OBJECTS follows ENTITIES.
    classes = {
        _first(pairs, 1): pairs
        for name, pairs in _records(text, "CLASSES")
        if name == "CLASS"
    }
    assert set(classes) == {"IMAGE", "IMAGEDEF", "IMAGEDEF_REACTOR", "RASTERVARIABLES"}
    assert _first(classes["IMAGE"], 2) == "AcDbRasterImage"
    assert (_first(classes["IMAGE"], 91), _first(classes["IMAGE"], 281)) == ("3", "1")
    assert _first(classes["IMAGEDEF"], 91) == "2"
    assert _first(classes["IMAGEDEF_REACTOR"], 91) == "3"
    assert text.index("\nCLASSES\n") < text.index("\nTABLES\n")
    assert (
        text.index("\nENTITIES\n") < text.index("\nOBJECTS\n") < text.index("\nEOF\n")
    )

    images = [pairs for name, pairs in _records(text, "ENTITIES") if name == "IMAGE"]
    assert [name for name, _ in _records(text, "ENTITIES")] == [
        "IMAGE",
        "IMAGE",
        "IMAGE",
        "LINE",
    ]
    logo, rotated, second = images
    assert _values(logo, 100) == ["AcDbEntity", "AcDbRasterImage"]
    assert _first(logo, 8) == "Pictures"
    assert (_first(logo, 10), _first(logo, 20), _first(logo, 30)) == ("10", "20", "0")
    # One pixel is 40 / 4 along the columns and 30 / 3 along the rows.
    assert (_first(logo, 11), _first(logo, 21)) == ("10", "0")
    assert (_first(logo, 12), _first(logo, 22)) == ("0", "10")
    assert (_first(logo, 13), _first(logo, 23)) == ("4", "3")
    assert (_first(logo, 70), _first(logo, 280)) == ("3", "0")
    assert (_first(logo, 281), _first(logo, 282), _first(logo, 283)) == (
        "50",
        "50",
        "0",
    )
    assert (_first(logo, 71), _first(logo, 91)) == ("1", "2")
    assert _values(logo, 14) == ["-0.5", "3.5"] and _values(logo, 24) == ["-0.5", "2.5"]
    assert _first(logo, 290) == "0"
    # Turned by 90 degrees the column axis points up and the row axis left.
    assert (_first(rotated, 11), _first(rotated, 21)) == ("0", "0.1")
    assert (_first(rotated, 12), _first(rotated, 22)) == ("-0.1", "0")
    assert (_first(rotated, 13), _first(rotated, 23)) == ("640", "480")
    assert (_first(rotated, 281), _first(rotated, 282), _first(rotated, 283)) == (
        "60",
        "40",
        "10",
    )
    assert (_first(second, 11), _first(second, 21)) == ("0.05", "0")

    objects = _records(text, "OBJECTS")
    definitions = [pairs for name, pairs in objects if name == "IMAGEDEF"]
    reactors = {
        _first(pairs, 5): pairs for name, pairs in objects if name == "IMAGEDEF_REACTOR"
    }
    dictionaries = [pairs for name, pairs in objects if name == "DICTIONARY"]
    variables = next(pairs for name, pairs in objects if name == "RASTERVARIABLES")
    assert len(definitions) == 2 and len(reactors) == 3
    root = dictionaries[0]
    assert _first(root, 330) == "0"
    assert _values(root, 3) == ["ACAD_GROUP", "ACAD_IMAGE_DICT", "ACAD_IMAGE_VARS"]
    image_dict_handle = _values(root, 350)[1]
    image_dict = next(
        pairs for pairs in dictionaries if _first(pairs, 5) == image_dict_handle
    )
    assert _first(image_dict, 330) == _first(root, 5)
    assert _values(image_dict, 3) == ["logo", "site"]
    assert _values(image_dict, 350) == [
        _first(definitions[0], 5),
        _first(definitions[1], 5),
    ]
    assert _values(root, 350)[2] == _first(variables, 5)
    assert (_first(variables, 70), _first(variables, 71), _first(variables, 72)) == (
        "1",
        "1",
        "1",
    )
    by_path = {_first(pairs, 1): pairs for pairs in definitions}
    assert set(by_path) == {"logo.png", site}
    assert (_first(by_path["logo.png"], 10), _first(by_path["logo.png"], 20)) == (
        "4",
        "3",
    )
    assert (_first(by_path["logo.png"], 11), _first(by_path["logo.png"], 21)) == (
        "10",
        "10",
    )
    assert (_first(by_path[site], 10), _first(by_path[site], 20)) == ("640", "480")
    assert (_first(by_path[site], 11), _first(by_path[site], 21)) == ("0.1", "0.1")
    assert (_first(by_path[site], 280), _first(by_path[site], 281)) == ("1", "0")
    # The shared definition lists the dictionary and both reactors, then its owner.
    assert len(_values(by_path[site], 330)) == 4
    assert _values(by_path[site], 102) == ["{ACAD_REACTORS", "}"]
    # Every IMAGE points at its definition and owns the reactor that points back.
    for image in images:
        handle = _first(image, 5)
        reactor = reactors[_first(image, 360)]
        assert _values(reactor, 330) == [handle, handle]
        assert _first(reactor, 90) == "2"
        definition = next(
            pairs for pairs in definitions if _first(pairs, 5) == _first(image, 340)
        )
        assert _first(reactor, 5) in _values(definition, 330)
        assert _first(definition, 330) == image_dict_handle

    # Handles are unique and below $HANDSEED.
    pairs = _pairs(text)
    handles = [
        int(value, 16)
        for index, (code, value) in enumerate(pairs)
        if code == 5 and pairs[index - 1] != (9, "$HANDSEED")
    ]
    assert len(handles) == len(set(handles))
    handseed = next(
        value
        for index, (code, value) in enumerate(pairs)
        if pairs[index - 1] == (9, "$HANDSEED")
    )
    assert max(handles) < int(handseed, 16)

    referenced = [d for d in result.diagnostics if d.code == "DXF_IMAGE_REFERENCED"]
    assert [d.entity_id for d in referenced] == ["LOGO"]
    assert (
        referenced[0].action == "approximated" and "'logo.png'" in referenced[0].message
    )
    codes = {d.code for d in result.diagnostics}
    assert not codes & {
        "DXF_IMAGE_SKIPPED",
        "DXF_IMAGE_SIZE_ASSUMED",
        "DXF_UNKNOWN_ENTITY_SKIPPED",
    }
    entries = {entry["ir_id"]: entry for entry in result.entity_map}
    for ir_id, image in zip(("LOGO", "SITE", "SITE2"), images, strict=True):
        assert entries[ir_id]["dxf_type"] == "IMAGE"
        assert entries[ir_id]["handle"] == _first(image, 5)

    back, diagnostics, warnings = _import(text)
    images_back = [entity for entity in back["entities"] if entity["kind"] == "IMAGE"]
    assert [entity["name"] for entity in images_back] == [
        "logo.png",
        "site.jpg",
        "site.jpg",
    ]
    logo_back, site_back, second_back = images_back
    assert logo_back["insert"] == [10.0, 20.0]
    assert logo_back["width"] == pytest.approx(40.0)
    assert logo_back["height"] == pytest.approx(30.0)
    assert logo_back["href"] == "logo.png" and "rotation" not in logo_back
    assert "data" not in logo_back and logo_back["layer"] == "Pictures"
    assert logo_back["source"] == {
        "format": "dxf",
        "id": _first(logo, 5),
        "kind": "IMAGE",
    }
    logo_meta = logo_back["metadata"]["dxf"]["image"]
    assert logo_meta["size"] == [4, 3] and logo_meta["definition_name"] == "logo"
    assert logo_meta["pixel_size"] == [10.0, 10.0] and logo_meta["clipping"] is False
    assert site_back["rotation"] == pytest.approx(90.0)
    assert site_back["width"] == pytest.approx(64.0)
    assert site_back["height"] == pytest.approx(48.0)
    assert site_back["href"] == site
    assert site_back["metadata"]["dxf"]["image"]["brightness"] == 60
    assert second_back["width"] == pytest.approx(32.0) and second_back["href"] == site
    assert [d.code for d in diagnostics if d.code.startswith("DXF_IMAGE")] == [
        "DXF_IMAGE_LINKED"
    ] * 3
    assert not any("Unsupported DXF entity" in warning for warning in warnings)
    validate_ir(back)


def test_dxf_image_pixel_sizes_come_from_headers_or_are_assumed() -> None:
    document = _document(
        _image(
            "B",
            (0.0, 0.0),
            20.0,
            10.0,
            name="scan",
            mime_type="image/bmp",
            data=_b64(_bmp(200, -100)),
        ),
        _image(
            "J",
            (0.0, 0.0),
            30.0,
            15.0,
            name="photo.jpeg",
            mime_type="image/jpeg",
            data=_b64(_jpeg(300, 150)),
        ),
        _image(
            "G",
            (0.0, 0.0),
            8.0,
            4.0,
            name="anim",
            mime_type="image/gif",
            data=_b64(b"GIF89a" + struct.pack("<HH", 80, 40) + b"\x00" * 3),
        ),
        _image("X", (0.0, 0.0), 12.4, 0.2, name="unknown", href="plans/unknown.tif"),
        _image(
            "N", (0.0, 0.0), 5.0, 5.0, data="not base64 at all!!", mime_type="image/png"
        ),
    )

    result = convert_ir_to_dxf_text(document)
    text = result.dxf_text

    images = [pairs for name, pairs in _records(text, "ENTITIES") if name == "IMAGE"]
    assert [(_first(pairs, 13), _first(pairs, 23)) for pairs in images] == [
        ("200", "100"),
        ("300", "150"),
        ("80", "40"),
        ("12", "1"),
        ("5", "5"),
    ]
    paths = [
        _first(pairs, 1)
        for name, pairs in _records(text, "OBJECTS")
        if name == "IMAGEDEF"
    ]
    assert paths == [
        "scan.bmp",
        "photo.jpeg",
        "anim.gif",
        "plans/unknown.tif",
        "image_N.png",
    ]
    names = [
        _values(pairs, 3)
        for name, pairs in _records(text, "OBJECTS")
        if name == "DICTIONARY" and "ACAD_GROUP" not in _values(pairs, 3)
    ]
    assert names == [[], ["scan", "photo", "anim", "unknown", "image_N"]]

    assumed = [d for d in result.diagnostics if d.code == "DXF_IMAGE_SIZE_ASSUMED"]
    assert sorted(d.entity_id for d in assumed) == ["N", "X"]
    assert "12x1" in next(d.message for d in assumed if d.entity_id == "X")
    referenced = sorted(
        d.entity_id for d in result.diagnostics if d.code == "DXF_IMAGE_REFERENCED"
    )
    assert referenced == ["B", "G", "J", "N"]

    # The drawn size survives an assumed pixel count.
    back, _diagnostics, _warnings = _import(text)
    unknown = next(
        entity
        for entity in back["entities"]
        if entity.get("href") == "plans/unknown.tif"
    )
    assert unknown["width"] == pytest.approx(12.4) and unknown[
        "height"
    ] == pytest.approx(0.2)
    assert unknown["name"] == "unknown.tif"


def test_dxf_image_definition_names_stay_unique_and_valid() -> None:
    document = _document(
        _image(
            "A",
            (0.0, 0.0),
            1.0,
            1.0,
            href="a/plan.png",
            metadata={"dxf": {"image": {"size": [1, 1]}}},
        ),
        _image(
            "B",
            (0.0, 0.0),
            1.0,
            1.0,
            href="b/plan.png",
            metadata={"dxf": {"image": {"size": [1, 1]}}},
        ),
        _image(
            "C",
            (0.0, 0.0),
            1.0,
            1.0,
            href="c/PLAN.png",
            metadata={"dxf": {"image": {"size": [1, 1]}}},
        ),
        _image(
            "D",
            (0.0, 0.0),
            1.0,
            1.0,
            href="what:is*this?.png",
            metadata={"dxf": {"image": {"size": [1, 1]}}},
        ),
    )
    text = convert_ir_to_dxf_text(document).dxf_text
    image_dict = next(
        pairs
        for name, pairs in _records(text, "OBJECTS")
        if name == "DICTIONARY"
        and _values(pairs, 3)
        and "ACAD_GROUP" not in _values(pairs, 3)
    )
    assert _values(image_dict, 3) == ["plan", "plan_2", "PLAN_3", "what_is_this_"]


def test_dxf_r12_skips_images_and_a_file_without_images_keeps_its_sections() -> None:
    line = {"id": "L1", "kind": "LINE", "p1": [0.0, 0.0], "p2": [1.0, 0.0]}
    document = _document(
        _image(
            "I1",
            (0.0, 0.0),
            10.0,
            5.0,
            name="logo.bmp",
            data="Qk0=",
            mime_type="image/bmp",
        ),
        line,
    )

    result = convert_ir_to_dxf_text(document, target_version="AC1009")
    skipped = [d for d in result.diagnostics if d.code == "DXF_IMAGE_SKIPPED"]
    assert [d.entity_id for d in skipped] == ["I1"] and skipped[0].action == "skipped"
    assert "R14" in skipped[0].message
    assert "\nIMAGE\n" not in result.dxf_text and "\nLINE\n" in result.dxf_text
    assert "\nCLASSES\n" not in result.dxf_text and "\nOBJECTS\n" not in result.dxf_text
    entry = next(entry for entry in result.entity_map if entry["ir_id"] == "I1")
    assert entry["reason_code"] == "DXF_IMAGE_SKIPPED" and entry["dxf_type"] is None

    plain = convert_ir_to_dxf_text(_document(line))
    assert "\nCLASSES\n" not in plain.dxf_text and "\nOBJECTS\n" not in plain.dxf_text
    assert "$HANDSEED" in plain.dxf_text

    # Without validation an image of no size is skipped, not written.
    degenerate = _document(_image("Z", (0.0, 0.0), 0.0, 5.0, name="z.png"))
    diagnostics: list[Any] = []
    text = ir_to_dxf(degenerate, validate=False, diagnostics=diagnostics)
    image_diagnostics = [d for d in diagnostics if d.code.startswith("DXF_IMAGE")]
    assert [(d.code, d.entity_id) for d in image_diagnostics] == [
        ("DXF_IMAGE_SKIPPED", "Z")
    ]
    assert "no positive width" in image_diagnostics[0].message
    assert "\nIMAGE\n" not in text and "\nOBJECTS\n" not in text


def test_dxf_images_inside_blocks_and_on_layouts_round_trip() -> None:
    document = _document(
        {"id": "R", "kind": "INSERT", "block": "B", "insert": [5.0, 5.0]},
        blocks={
            "B": {
                "base_point": [0.0, 0.0],
                "entities": [
                    _image(
                        "BI",
                        (1.0, 2.0),
                        10.0,
                        5.0,
                        name="part.png",
                        mime_type="image/png",
                        data=_b64(_png(10, 5)),
                    )
                ],
            }
        },
        layouts=[
            {
                "name": "Sheet",
                "entities": [
                    _image(
                        "LI",
                        (0.0, 0.0),
                        210.0,
                        297.0,
                        name="title.png",
                        href="title.png",
                        metadata={"dxf": {"image": {"size": [2100, 2970]}}},
                    )
                ],
            }
        ],
    )

    result = convert_ir_to_dxf_text(document)
    text = result.dxf_text

    block_images = [
        pairs for name, pairs in _records(text, "BLOCKS") if name == "IMAGE"
    ]
    assert len(block_images) == 1
    assert (_first(block_images[0], 13), _first(block_images[0], 23)) == ("10", "5")
    layout_images = [
        pairs for name, pairs in _records(text, "ENTITIES") if name == "IMAGE"
    ]
    assert len(layout_images) == 1 and _first(layout_images[0], 67) == "1"
    assert _values(layout_images[0], 100) == ["AcDbEntity", "AcDbRasterImage"]
    reactors = {
        _first(pairs, 5): pairs
        for name, pairs in _records(text, "OBJECTS")
        if name == "IMAGEDEF_REACTOR"
    }
    for image in (block_images[0], layout_images[0]):
        assert _first(reactors[_first(image, 360)], 330) == _first(image, 5)
    classes = {
        _first(pairs, 1): _first(pairs, 91)
        for name, pairs in _records(text, "CLASSES")
        if name == "CLASS"
    }
    assert classes["IMAGE"] == "2" and classes["IMAGEDEF"] == "2"
    scopes = {entry["ir_id"]: entry["scope"] for entry in result.entity_map}
    assert scopes["BI"] == "block:B" and scopes["LI"] == "layout:Sheet"

    back, diagnostics, _warnings = _import(text)
    block_back = back["tables"]["blocks"]["B"]["entities"]
    assert [entity["kind"] for entity in block_back] == ["IMAGE"]
    assert block_back[0]["href"] == "part.png" and block_back[0]["insert"] == [1.0, 2.0]
    assert block_back[0]["width"] == pytest.approx(10.0)
    assert block_back[0]["height"] == pytest.approx(5.0)
    layout_back = back["layouts"][0]
    assert [entity["kind"] for entity in layout_back["entities"]] == ["IMAGE"]
    assert layout_back["entities"][0]["width"] == pytest.approx(210.0)
    assert layout_back["entities"][0]["height"] == pytest.approx(297.0)
    assert [d.code for d in diagnostics if d.code.startswith("DXF_IMAGE")] == [
        "DXF_IMAGE_LINKED",
        "DXF_IMAGE_LINKED",
    ]
    validate_ir(back)


def test_dxf_image_import_reports_clips_axes_and_missing_definitions() -> None:
    pairs: list[tuple[int, object]] = [
        (0, "SECTION"),
        (2, "HEADER"),
        (9, "$INSUNITS"),
        (70, 4),
        (0, "ENDSEC"),
        (0, "SECTION"),
        (2, "ENTITIES"),
        # A: turned by 90 degrees, a polygonal clip boundary in use
        (0, "IMAGE"),
        (5, "A1"),
        (8, "0"),
        (100, "AcDbEntity"),
        (100, "AcDbRasterImage"),
        (90, 0),
        (10, 100),
        (20, 50),
        (30, 0),
        (11, 0),
        (21, 0.25),
        (31, 0),
        (12, -0.25),
        (22, 0),
        (32, 0),
        (13, 400),
        (23, 200),
        (340, "D1"),
        (70, 7),
        (280, 1),
        (281, 50),
        (282, 50),
        (283, 0),
        (360, "R1"),
        (71, 2),
        (91, 3),
        (14, 0),
        (24, 0),
        (14, 399.5),
        (24, 0),
        (14, 200),
        (24, 199.5),
        (290, 0),
        # B: a mirrored row axis; the full rectangular boundary is not in use
        (0, "IMAGE"),
        (5, "B2"),
        (8, "0"),
        (10, 0),
        (20, 0),
        (11, 0.5),
        (21, 0),
        (12, 0),
        (22, -0.5),
        (13, 10),
        (23, 20),
        (340, "d1"),
        (70, 3),
        (280, 0),
        (71, 1),
        (91, 2),
        (14, -0.5),
        (24, -0.5),
        (14, 9.5),
        (24, 19.5),
        # C: its definition is missing
        (0, "IMAGE"),
        (5, "C3"),
        (8, "0"),
        (10, 1),
        (20, 1),
        (11, 1),
        (21, 0),
        (12, 0),
        (22, 2),
        (13, 3),
        (23, 4),
        (340, "FFFF"),
        # D: a zero column vector
        (0, "IMAGE"),
        (5, "D4"),
        (8, "0"),
        (10, 0),
        (20, 0),
        (11, 0),
        (21, 0),
        (12, 0),
        (22, 1),
        (13, 3),
        (23, 4),
        (340, "D1"),
        (0, "LINE"),
        (8, "0"),
        (10, 0),
        (20, 0),
        (11, 1),
        (21, 1),
        (0, "ENDSEC"),
        (0, "SECTION"),
        (2, "OBJECTS"),
        (0, "DICTIONARY"),
        (5, "C"),
        (330, 0),
        (100, "AcDbDictionary"),
        (3, "ACAD_IMAGE_DICT"),
        (350, "E"),
        (0, "DICTIONARY"),
        (5, "E"),
        (330, "C"),
        (100, "AcDbDictionary"),
        (3, "aerial"),
        (350, "D1"),
        (0, "IMAGEDEF"),
        (5, "D1"),
        (330, "E"),
        (100, "AcDbRasterImageDef"),
        (90, 0),
        (1, "..\\maps\\aerial.tif"),
        (10, 400),
        (20, 200),
        (11, 0.25),
        (21, 0.25),
        (280, 1),
        (281, 0),
        (0, "ENDSEC"),
        (0, "EOF"),
    ]

    document, diagnostics, warnings = _import(_text(pairs))

    assert [entity["kind"] for entity in document["entities"]] == [
        "IMAGE",
        "IMAGE",
        "IMAGE",
        "LINE",
    ]
    a, b, c = document["entities"][:3]
    assert [entity["source"]["id"] for entity in (a, b, c)] == ["A1", "B2", "C3"]
    assert a["insert"] == [100.0, 50.0]
    assert a["width"] == pytest.approx(100.0) and a["height"] == pytest.approx(50.0)
    assert a["rotation"] == pytest.approx(90.0)
    assert a["href"] == "..\\maps\\aerial.tif" and a["name"] == "aerial.tif"
    assert a["metadata"]["dxf"]["image"] == {
        "size": [400, 200],
        "u_vector": [0.0, 0.25, 0.0],
        "v_vector": [-0.25, 0.0, 0.0],
        "display_props": 7,
        "clipping": True,
        "brightness": 50,
        "contrast": 50,
        "fade": 0,
        "clip_type": 2,
        "clip_vertices": [[0.0, 0.0], [399.5, 0.0], [200.0, 199.5]],
        "clip_mode": 0,
        "file_path": "..\\maps\\aerial.tif",
        "image_def_handle": "D1",
        "definition_name": "aerial",
        "pixel_size": [0.25, 0.25],
    }
    assert b["width"] == pytest.approx(5.0) and b["height"] == pytest.approx(10.0)
    assert "rotation" not in b and b["href"] == "..\\maps\\aerial.tif"
    assert c["name"] == "IMAGE_C3" and "href" not in c
    assert c["width"] == pytest.approx(3.0) and c["height"] == pytest.approx(8.0)
    assert c["metadata"]["dxf"]["image"]["image_def_handle"] == "FFFF"

    by_code: dict[str, list[Any]] = {}
    for diagnostic in diagnostics:
        by_code.setdefault(diagnostic.code, []).append(diagnostic)
    assert [d.source_id for d in by_code["DXF_IMAGE_LINKED"]] == ["A1", "B2"]
    assert all(d.severity == "info" for d in by_code["DXF_IMAGE_LINKED"])
    assert [d.source_id for d in by_code["DXF_IMAGE_CLIP_IGNORED"]] == ["A1"]
    assert by_code["DXF_IMAGE_CLIP_IGNORED"][0].action == "approximated"
    assert [d.source_id for d in by_code["DXF_IMAGE_AXES_APPROXIMATED"]] == ["B2"]
    assert [d.source_id for d in by_code["DXF_IMAGE_DEF_MISSING"]] == ["C3"]
    failed = by_code["DXF_ENTITY_CONVERSION_FAILED"]
    assert [d.source_id for d in failed] == ["D4"] and "non-zero" in failed[0].message
    assert all(
        d.source_kind == "IMAGE"
        for code, items in by_code.items()
        if code.startswith("DXF_IMAGE")
        for d in items
    )
    assert not any(
        "Unsupported DXF entity skipped: IMAGE" in warning for warning in warnings
    )
    validate_ir(document)


def test_dxf_images_follow_an_axis_aligned_drawing_plane() -> None:
    # A drawing in the XZ plane: lines and an image share the projection.
    pairs: list[tuple[int, object]] = [
        (0, "SECTION"),
        (2, "ENTITIES"),
        (0, "LINE"),
        (8, "0"),
        (10, 0),
        (20, 0),
        (30, 0),
        (11, 10),
        (21, 0),
        (31, 10),
        (0, "IMAGE"),
        (5, "I1"),
        (8, "0"),
        (10, 2),
        (20, 0),
        (30, 3),
        (11, 0.5),
        (21, 0),
        (31, 0),
        (12, 0),
        (22, 0),
        (32, 0.5),
        (13, 4),
        (23, 2),
        (0, "ENDSEC"),
        (0, "EOF"),
    ]
    document, diagnostics, _warnings = _import(_text(pairs))
    image = next(entity for entity in document["entities"] if entity["kind"] == "IMAGE")
    assert image["insert"] == [2.0, 3.0]
    assert image["width"] == pytest.approx(2.0) and image["height"] == pytest.approx(
        1.0
    )
    assert "rotation" not in image
    assert image["metadata"]["dxf"]["projected_from_plane"] == "XZ"
    assert {d.code for d in diagnostics} >= {
        "DXF_NON_XY_PLANE_PROJECTED",
        "DXF_IMAGE_DEF_MISSING",
    }
