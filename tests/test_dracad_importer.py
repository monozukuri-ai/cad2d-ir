from __future__ import annotations

import hashlib
import json
import math
import os
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from cad2d_ir import convert_file_to_ir, detect_source_format, validate_ir
from cad2d_ir.cli import main
from cad2d_ir.importers import (
    ImporterError,
    ImportOptions,
    MissingOptionalDependencyError,
)
from cad2d_ir.importers.dracad import (
    convert_dracad_file_to_ir,
    dracad_drawing_to_ir,
)

FIXTURES = Path(__file__).parent / "data" / "dracad"
OLDER = FIXTURES / "best-effort8.mpz"
UNICODE = FIXTURES / "best-effort25u.mpuw"
EARLY = FIXTURES / "best-effort1.mpz"
SHA256 = {
    "best-effort8.mpz": "c0e7a82267b074882ef85a4f9c87efe3fd681da57f6744a0221107396a640169",
    "best-effort25u.mpuw": (
        "6c8722272d64a509d7b97afd9457af16539e7bdcd3517283bc33f032c9602f2a"
    ),
    "best-effort1.mpz": "a8f6155b3f1e187f5ae051e2a1c673eea43defeea93cd86d1fb1ce40ed399d7a",
}


# ---------------------------------------------------------------------------
# Drawings built in the shape ezdracad.best_effort returns, without the parser


def entity(kind: str, data: dict[str, Any] | None = None, **attributes: Any) -> Any:
    values = {
        "type": kind,
        "layer": 1,
        "color": 257,
        "line_type": 33,
        "width": 17,
        "flags": 0,
        "group": 0,
        "link_id": 0,
        "segment": 0,
        "table": "root",
        "block_index": None,
        "record_index": 0,
        "data": data or {},
        "children": (),
    }
    values.update(attributes)
    values["reversed"] = values["flags"] & 0x18 == 0x18
    return SimpleNamespace(**values)


def line(start: list[float], end: list[float], **attributes: Any) -> Any:
    return entity("LINE", {"start": start, "end": end}, **attributes)


def arc(
    center: list[float], radius: float, start: float, end: float, **attributes: Any
) -> Any:
    data = {
        "center": center,
        "radius": radius,
        "start_degrees": start,
        "end_degrees": end,
    }
    return entity("ARC", data, **attributes)


def text(contents: str, **data: Any) -> Any:
    values = {
        "position": [0.0, 0.0],
        "text": contents,
        "height": 3.0,
        "width": 0.0,
        "spacing": 0.0,
        "output_size": False,
        "rotation_degrees": 0.0,
        "slant_degrees": 0.0,
        "origin": 0,
        "font_name": "ＭＳ ゴシック",
        "numeric": False,
        "number": None,
        "format_codes": False,
    }
    attributes = {
        key: data.pop(key) for key in ("link_id", "layer", "segment") if key in data
    }
    values.update(data)
    return entity("TEXT", values, **attributes)


def polyline(
    members: list[Any], *, closed: bool = False, fill: Any = None, **data: Any
) -> Any:
    values = {"curve_kind": 0, "closed": closed, "fill": fill, "curve_points": None}
    values.update(data)
    return entity("POLYLINE", values, children=tuple(members))


def package(kind: int, members: list[Any], number: int = 0) -> Any:
    data = {"package_kind": kind, "reference_number": number}
    return entity("PACKAGE", data, children=tuple(members))


def fill(
    color: int, *, rgb: tuple[int, int, int] = (0, 0, 0), transparency: int = 0
) -> Any:
    return {
        "color": color,
        "rgb": list(rgb),
        "transparency": transparency,
        "outline": True,
    }


def drawing(
    entities: list[Any],
    *,
    scale: float = 1.0,
    blocks: tuple[Any, ...] = (),
    **attributes: Any,
) -> Any:
    values = {
        "format": "mpz",
        "marker": 25,
        "marker_observed": True,
        "settings": SimpleNamespace(
            drawing_origin=(0.0, 0.0),
            drawing_scale=scale,
            paper_size=(420.0, 297.0),
            paper_origin=(0.0, 297.0 * scale),
            paper_type=4,
            paper_portrait=False,
            scale_ratio=1.0,
            comment="",
            width_by_color=False,
        ),
        "layers": (
            SimpleNamespace(
                index=1, name="壁", flags=0, hidden=False, color=7, line_type=1, width=1
            ),
            SimpleNamespace(
                index=2,
                name="",
                flags=0x0900,
                hidden=True,
                color=2,
                line_type=2,
                width=3,
            ),
            SimpleNamespace(
                index=3,
                name="古い",
                flags=0,
                hidden=False,
                color=4,
                line_type=2,
                width=0,
            ),
        ),
        "colors": (
            SimpleNamespace(index=1, rgb=(0, 0, 255), pen_width=0.1),
            SimpleNamespace(index=2, rgb=(255, 0, 0), pen_width=0.15),
            SimpleNamespace(index=7, rgb=(255, 255, 255), pen_width=0.4),
            SimpleNamespace(index=16, rgb=(0, 0, 0), pen_width=0.2),
        ),
        "line_types": (
            SimpleNamespace(index=1, pattern=()),
            SimpleNamespace(index=2, pattern=(1.0, 2.0, 2.0, 2.0, 1.0)),
        ),
        "widths": (
            SimpleNamespace(index=1, width=0.01),
            SimpleNamespace(index=3, width=0.1),
        ),
        "blocks": blocks,
        "entities": tuple(entities),
        "report": SimpleNamespace(
            lossy=False,
            decoded_counts={},
            unsupported_counts={},
            failed_counts={},
            orphan_records=0,
            clamped_spans=0,
        ),
    }
    values.update(attributes)
    return SimpleNamespace(**values)


def convert(source: Any, **options: Any) -> Any:
    return dracad_drawing_to_ir(source, options=ImportOptions(**options))


def kinds(result: Any) -> list[str]:
    return [item["kind"] for item in result.document["entities"]]


def codes(result: Any) -> list[str]:
    return [diagnostic.code for diagnostic in result.diagnostics]


# ---------------------------------------------------------------------------
# Synthetic files, both record layouts


def test_fixtures_are_the_documented_bytes() -> None:
    for name, sha256 in SHA256.items():
        assert hashlib.sha256((FIXTURES / name).read_bytes()).hexdigest() == sha256


@pytest.fixture(scope="module", params=[OLDER, UNICODE], ids=["older", "unicode"])
def imported(request: pytest.FixtureRequest) -> Any:
    pytest.importorskip("ezdracad")
    return convert_dracad_file_to_ir(request.param, options=ImportOptions(strict=False))


def test_file_is_converted_as_it_is_drawn(imported: Any) -> None:
    document = imported.document
    validate_ir(document)
    unicode = document["header"]["metadata"]["dracad"]["format"] == "mpuw"
    assert document["source"]["format"] == "dracad"
    assert document["source"]["version"] == ("25" if unicode else "8")
    assert document["header"]["units"] == "mm"
    # Line-type patterns are millimetres on the sheet; the drawing is 1:50.
    assert document["header"]["linetype_scale"] == 50.0
    metadata = document["header"]["metadata"]["dracad"]
    assert metadata["paper_scale"] == 50.0
    assert metadata["paper"] == {
        "width_mm": 420.0,
        "height_mm": 297.0,
        "extent": [-100.0, 0.0, 20900.0, 14850.0],
    }
    assert metadata["stated_scale"] == 100.0
    assert metadata["comment"] == "改修工事 A棟"
    # The last object of the file is painted first: a line, the two edges of a
    # face in space, the named area with its fill, outline and label, a line in
    # space, and so on. Cameras, lights and erased objects draw nothing.
    assert kinds(imported)[:9] == [
        "LINE",
        "LWPOLYLINE",
        "LWPOLYLINE",
        "HATCH",
        "LWPOLYLINE",
        "TEXT",
        "LINE",
        "IMAGE",
        "INSERT",
    ]
    assert kinds(imported)[-1] == "LINE"
    assert document["entities"][-1]["p1"] == [1.0, 2.0]
    counts = imported.statistics["converted_entity_counts"]
    assert counts["DIMENSION"] == 1 and counts["IMAGE"] == 1 and counts["INSERT"] == 1
    assert imported.statistics["read_complete"] is False
    assert all(item["id"].startswith("DRA_E") for item in document["entities"])


def test_file_dimension_is_the_saved_package(imported: Any) -> None:
    document = imported.document
    (dimension,) = [e for e in document["entities"] if e["kind"] == "DIMENSION"]
    assert dimension["dim_kind"] == "LINEAR"
    definition = dimension["definition"]
    assert definition["text"] == "40" and definition["measurement"] == 1234.5
    assert definition["points"]["p1"] == [0.0, 40.0]
    block = document["tables"]["blocks"][definition["block"]]
    # Two lines, two filled-circle... the arrowheads of type 5 are closed triangles.
    assert [e["kind"] for e in block["entities"]] == [
        "TEXT",
        "LWPOLYLINE",
        "LWPOLYLINE",
        "LINE",
        "LINE",
    ]
    arrow = block["entities"][1]
    # 1 x 3 millimetres on the sheet at 1:50: 150 long, 50 wide, tip at the line end.
    assert arrow["closed"] and arrow["vertices"][0] == [40.0, 60.0]
    assert arrow["vertices"][1] == pytest.approx([-110.0, 85.0])
    number = block["entities"][0]
    assert number["text"] == "40" and number["height"] == pytest.approx(4.0 * 50.0)
    assert "DRACAD_DIMENSION_FROM_PACKAGE" in codes(imported)
    # The angle dimension and one of the hatches have no saved geometry.
    not_drawn = next(
        d for d in imported.diagnostics if d.code == "DRACAD_DEFINITION_NOT_DRAWN"
    )
    assert not_drawn.details == {"dimensions": 1, "hatches": 2}


def test_file_fills_blocks_texts_and_references(imported: Any) -> None:
    document = imported.document
    entities = document["entities"]
    hatches = [e for e in entities if e["kind"] == "HATCH"]
    assert all(e["solid"] and e["metadata"]["dracad"]["fill"] for e in hatches)
    # The circle is filled with its own colour, 40 of 255 transparent.
    assert hatches[-1]["color"] == "#0A141ED7"
    assert len(hatches[-1]["loops"][0]["vertices"]) == 4
    region = next(e for e in hatches if len(e["loops"]) == 2)
    assert [loop["is_outer"] for loop in region["loops"]] == [True, False]
    # A fill comes before its outline.
    index = entities.index(hatches[-1])
    assert entities[index + 1]["kind"] == "CIRCLE"
    # The ellipse is filled without its outline.
    assert [e["kind"] for e in entities].count("ELLIPSE") == 1

    (insert,) = [e for e in entities if e["kind"] == "INSERT"]
    assert (insert["block"], insert["scale"], insert["rotation"]) == ("SYM", 2.0, 90.0)
    assert [e["kind"] for e in document["tables"]["blocks"]["SYM"]["entities"]] == [
        "CIRCLE",
        "LINE",
    ]
    (image,) = [e for e in entities if e["kind"] == "IMAGE"]
    assert (image["insert"], image["width"], image["height"]) == (
        [335.0, 107.0],
        69.0,
        49.0,
    )
    assert image["href"] == "E:\\Photo\\写真.tif"
    skipped = next(
        d for d in imported.diagnostics if d.code == "DRACAD_EXTERNAL_REFERENCE_SKIPPED"
    )
    # A drawing laid under this one, and a part placed from another file.
    assert skipped.details["files"] == ["図面枠.mps", "椅子.mps"]

    plain = next(e for e in entities if e["kind"] == "MTEXT" and "図面" in e["text"])
    assert plain["text"].endswith("図面 A髙\\P2") and plain["attach"] == "top_center"
    assert plain["style"] == "ＭＳ ゴシック"
    note = next(e for e in entities if e.get("text") == "注記")
    # Two full-width characters, each 2.5 wide in a cell 4 high.
    assert (note["text"], note["width_factor"], note["valign"]) == (
        "注記",
        0.625,
        "bottom",
    )
    assert document["tables"]["text_styles"] == {
        "ＭＳ ゴシック": {"font": "ＭＳ ゴシック"}
    }
    spline = next(e for e in entities if "approximation" in e)
    assert spline["approximation"]["source_kind"] == "CUBIC_SPLINE"
    assert len(spline["vertices"]) == 31
    partial = next(d for d in imported.diagnostics if d.code == "DRACAD_PARTIAL_READ")
    assert partial.details["unsupported_kinds"] == {"46": 1}


def test_file_sheets_areas_and_faces(imported: Any) -> None:
    document = imported.document
    entities = document["entities"]
    # A sheet with a window onto the model and a line of its own; neither is
    # part of the model.
    (layout,) = document["layouts"]
    assert (layout["name"], layout["tab_order"]) == ("配置図", 1)
    assert layout["paper"] == {"size_mm": [297.0, 210.0], "units": "mm", "name": "A4"}
    (sheet_line,) = layout["entities"]
    assert (sheet_line["kind"], sheet_line["p2"]) == ("LINE", [100.0, 50.0])
    assert [100.0, 50.0] not in [e.get("p2") for e in entities]
    (viewport,) = layout["viewports"]
    assert (viewport["center"], viewport["width"], viewport["height"]) == (
        [148.5, 105.0],
        257.0,
        170.0,
    )
    # 1:50: 170 millimetres of sheet show 8500 of the model.
    assert viewport["view_center"] == [-4484.0, 9311.0]
    assert viewport["view_height"] == pytest.approx(8500.0)
    assert "rotation" not in viewport
    sheets = next(d for d in imported.diagnostics if d.code == "DRACAD_LAYOUT_SHEETS")
    assert sheets.details == {"count": 1, "viewports": 1, "entities": 1}

    # A named area: a fill that lets four fifths through, its outline and label.
    label = next(e for e in entities if e.get("text") == "居室")
    index = entities.index(label)
    area, outline = entities[index - 2], entities[index - 1]
    assert (area["kind"], area["color"][7:]) == ("HATCH", "33")
    assert outline["kind"] == "LWPOLYLINE" and outline["closed"]
    assert label["insert"] == [20.0, 20.0]

    # A face in space is drawn by its edges alone, seen from above.
    faces = entities[1:3]
    assert all(e["kind"] == "LWPOLYLINE" and e["closed"] for e in faces)
    assert "DRACAD_3D_PROJECTED" in codes(imported)
    # The drawing prints with the pen width of each colour: colour 7 is 0.35.
    assert entities[-1]["lineweight_mm"] == 0.35
    assert document["header"]["metadata"]["dracad"]["width_by_color"] is True


def test_file_of_the_first_releases() -> None:
    pytest.importorskip("ezdracad")
    result = convert_dracad_file_to_ir(EARLY)
    document = result.document
    validate_ir(document)
    assert document["source"]["version"] == "1"
    assert kinds(result) == ["POINT", "TEXT", "ARC", "CIRCLE", "LINE"]
    assert "DRACAD_GENERATION_UNVERIFIED" not in codes(result)
    assert result.statistics["read_complete"] is True
    line = document["entities"][-1]
    # Layer 30 has no name; the navy of colour 9 comes from the 32 saved colours.
    assert (line["layer"], line["color"], line["lineweight_mm"]) == (
        "030",
        "#000080",
        0.01,
    )
    assert document["tables"]["layers"]["030"]["color"] == 7
    assert document["header"]["metadata"]["dracad"]["comment"] == "300x300mmタイル"


def test_both_record_layouts_convert_to_the_same_entities() -> None:
    pytest.importorskip("ezdracad")

    def stripped(path: Path) -> list[dict[str, Any]]:
        result = convert_dracad_file_to_ir(
            path, options=ImportOptions(strict=False, entity_provenance=False)
        )
        return result.document["entities"]

    assert stripped(OLDER) == stripped(UNICODE)


def test_registry_cli_and_errors(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    pytest.importorskip("ezdracad")
    assert detect_source_format("a.MPZ") == detect_source_format("a.mpuw") == "dracad"
    result = convert_file_to_ir(OLDER, strict=False, entity_provenance=False)
    assert result.statistics["source_format"] == "dracad"
    assert all("source" not in item for item in result.document["entities"])
    # Strict mode stops at nothing here: every decoded object converts.
    assert convert_file_to_ir(UNICODE).statistics["converted_entities"] == len(
        result.document["entities"]
    )
    output = tmp_path / "drawing.json"
    assert main(["import", str(OLDER), "-o", str(output)]) == 0
    assert (
        json.loads(output.read_text(encoding="utf-8"))["source"]["format"] == "dracad"
    )
    capsys.readouterr()

    other = tmp_path / "other.mpz"
    other.write_bytes(b"not a drawing")
    with pytest.raises(ImporterError, match="Failed to read DRA-CAD file"):
        convert_dracad_file_to_ir(other)
    with pytest.raises(ImporterError, match="Failed to read DRA-CAD file"):
        convert_dracad_file_to_ir(tmp_path / "missing.mpz")


def test_missing_dependency_names_the_extra(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setitem(sys.modules, "ezdracad", None)
    with pytest.raises(MissingOptionalDependencyError, match=r"cad2d-ir\[dracad\]"):
        convert_dracad_file_to_ir(OLDER)


# ---------------------------------------------------------------------------
# Mapping rules


def test_sizes_given_on_the_output_sheet_follow_the_drawing_scale() -> None:
    result = convert(
        drawing(
            [
                text("ABCあいう", height=300.0, width=300.0),
                text("ABCあいう", height=3.0, width=3.0, output_size=True),
                text("ABCあいう", height=300.0, width=150.0, origin=5),
                text("ABCあいう", height=300.0),
                entity(
                    "ARROWHEAD",
                    {
                        "position": [0.0, 0.0],
                        "size": [1.0, 3.0],
                        "angle_degrees": 0.0,
                        "arrow_type": 7,
                        "output_size": True,
                    },
                ),
            ],
            scale=100.0,
        )
    )
    arrow, natural, narrow, scaled, wide = result.document["entities"]
    # Three half-width and three full-width characters: six cells of 300 fill 4.5.
    assert (wide["height"], wide["width_factor"]) == (300.0, 1.333333)
    assert (scaled["height"], scaled["width_factor"]) == (300.0, 1.333333)
    assert (narrow["width_factor"], narrow["halign"], narrow["valign"]) == (
        0.666667,
        "center",
        "middle",
    )
    assert "width_factor" not in natural
    assert arrow["kind"] == "HATCH" and arrow["solid"]
    assert arrow["loops"][0]["vertices"] == [
        [0.0, 0.0],
        [-300.0, 50.0],
        [-300.0, -50.0],
    ]
    assert result.document["header"]["linetype_scale"] == 100.0
    assert result.document["header"]["metadata"]["dracad"]["paper"]["extent"] == [
        0.0,
        0.0,
        42000.0,
        29700.0,
    ]


def test_attributes_come_from_the_tables() -> None:
    result = convert(
        drawing(
            [
                line([0.0, 0.0], [1.0, 0.0], color=1, line_type=2, width=3),
                line([0.0, 1.0], [1.0, 1.0], color=7, line_type=1, width=1),
                line([0.0, 2.0], [1.0, 2.0], color=16, layer=2),
                line([0.0, 3.0], [1.0, 3.0], color=258, layer=3),
                line([0.0, 4.0], [1.0, 4.0], layer=9),
            ]
        )
    )
    outside, by_block, black, white, blue = result.document["entities"]
    assert (blue["color"], blue["linetype"], blue["lineweight_mm"]) == (
        "#0000FF",
        "DRACAD_LT02",
        0.1,
    )
    # White and black are the colour that contrasts with the background.
    assert (white["color"], white["linetype"], white["lineweight_mm"]) == (
        7,
        "CONTINUOUS",
        0.01,
    )
    assert black["color"] == 7 and by_block["color"] == 0
    assert "color" not in outside and "linetype" not in outside
    layers = result.document["tables"]["layers"]
    assert list(layers) == ["壁", "002", "古い", "009"]
    assert layers["壁"]["color"] == 7 and layers["壁"]["lineweight_mm"] == 0.01
    # Hidden and not printed; named by its number.
    assert (layers["002"]["visible"], layers["002"]["plot"]) == (False, False)
    assert (layers["002"]["color"], layers["002"]["linetype"]) == (
        "#FF0000",
        "DRACAD_LT02",
    )
    # A layer of an old file with unset attributes reads as colour 7, solid, width 1.
    assert (layers["古い"]["color"], layers["古い"]["linetype"]) == (7, "CONTINUOUS")
    assert result.document["tables"]["linetypes"]["DRACAD_LT02"]["pattern_mm"] == [
        1.0,
        -2.0,
        2.0,
        -2.0,
        1.0,
    ]
    assert black["source"] == {"format": "dracad", "id": "root:0", "kind": "LINE"}
    assert black["metadata"]["dracad"]["color"] == 16


def test_polyline_members_become_vertices_with_bulges() -> None:
    members = [
        line([120.0, 120.0], [140.0, 120.0], flags=0x08),
        arc([140.0, 130.0], 10.0, 270.0, 90.0, flags=0x08),
        # Followed from its end angle to its start angle.
        arc([140.0, 150.0], 10.0, 90.0, 270.0, flags=0x18),
    ]
    square = [
        line([0.0, 0.0], [4.0, 0.0]),
        line([4.0, 4.0], [4.0, 0.0]),  # stored against the direction of the path
        line([4.0, 4.0], [0.0, 4.0]),
        line([0.0, 4.0], [0.0, 0.0]),
    ]
    broken = [line([0.0, 0.0], [1.0, 0.0]), line([5.0, 5.0], [6.0, 5.0])]
    result = convert(
        drawing(
            [
                polyline(broken, fill=fill(1)),
                polyline(square, closed=True, fill=fill(2, transparency=255)),
                polyline(members),
            ]
        )
    )
    path, paint, outline, *exploded = result.document["entities"]
    assert path["vertices"] == [
        [120.0, 120.0],
        pytest.approx([140.0, 120.0, 1.0]),
        pytest.approx([140.0, 140.0, -1.0]),
        pytest.approx([140.0, 160.0]),
    ]
    assert outline["kind"] == "LWPOLYLINE" and outline["closed"]
    assert outline["vertices"] == [[0.0, 0.0], [4.0, 0.0], [4.0, 4.0], [0.0, 4.0]]
    assert paint["kind"] == "HATCH" and paint["color"] == "#FF000000"
    assert paint["loops"] == [{"vertices": outline["vertices"], "is_outer": True}]
    assert [item["kind"] for item in exploded] == ["LINE", "LINE"]
    assert "DRACAD_POLYLINE_EXPLODED" in codes(result)


def test_hatches_and_leaders_are_their_packages() -> None:
    square = [
        line([0.0, 0.0], [4.0, 0.0]),
        line([4.0, 0.0], [4.0, 4.0]),
        line([4.0, 4.0], [0.0, 4.0]),
        line([0.0, 4.0], [0.0, 0.0]),
    ]

    def region(number: int, flags: int = 1, paint: Any = None) -> Any:
        data = {"flags": flags, "fill": paint, "reference_number": number}
        return entity(
            "EXTENDED_POLYGON", data, children=(polyline(square, closed=True),)
        )

    result = convert(
        drawing(
            [
                region(1),
                entity("HATCH", {"hatch_type": 1}),
                region(2),
                entity("HATCH", {"hatch_type": 0}),
                region(0, paint=fill(257, rgb=(255, 255, 255))),
                region(0, flags=3),
                entity("LEADER"),
                package(4, [line([0.0, 9.0], [5.0, 9.0]), text("NOTE")]),
                package(
                    5, [line([1.0, 1.0], [3.0, 3.0]), polyline(square, closed=True)], 1
                ),
            ]
        )
    )
    # The package of region 1, the leader, then the filled region; region 2 has
    # no package and the last region hides its boundary.
    assert kinds(result) == [
        "LWPOLYLINE",
        "LINE",
        "TEXT",
        "LINE",
        "HATCH",
        "LWPOLYLINE",
    ]
    white = result.document["entities"][4]
    assert white["color"] == "#FFFFFF" and white["metadata"]["dracad"]["fill"] is True
    not_drawn = next(
        d for d in result.diagnostics if d.code == "DRACAD_DEFINITION_NOT_DRAWN"
    )
    assert not_drawn.details == {"dimensions": 0, "hatches": 1}


def test_dimension_pairs_with_its_package_by_link() -> None:
    number = text("8,000", numeric=True, number=8000.0, link_id=5)
    definition = entity(
        "DIMENSION",
        {
            "dimension_type": 4,
            "flags": 128,
            "flags2": 2,
            "arrow_styles": [5, 1, 5, 1],
            "text_position": 19,
            "line": [[0.0, 0.0], [8.0, 0.0]],
            "arc": None,
            "extension_origins": [],
        },
        link_id=5,
        children=(number,),
    )
    drawn = package(
        3,
        [
            line([0.0, 0.0], [8.0, 0.0], link_id=5),
            entity("POINT", {"position": [0.0, 0.0], "point_type": 1}),
            number,
        ],
        1,
    )
    orphan = package(3, [line([0.0, 5.0], [8.0, 5.0], link_id=9)], 2)
    result = convert(drawing([definition, drawn, orphan]))
    # A package without a dimension is drawn as its members.
    assert kinds(result) == ["LINE", "DIMENSION"]
    dimension = result.document["entities"][1]
    assert dimension["dim_kind"] == "RADIAL"
    assert dimension["definition"]["measurement"] == 8000.0
    assert dimension["definition"]["points"] == {
        "location": [8.0, 0.0],
        "text_midpoint": [0.0, 0.0],
    }
    block = result.document["tables"]["blocks"][dimension["definition"]["block"]]
    assert [item["kind"] for item in block["entities"]] == ["TEXT", "LINE"]
    assert result.statistics["converted_entity_counts"] == {"DIMENSION": 1, "LINE": 1}


def test_arrowhead_shapes() -> None:
    def arrow(kind: int, angle: float = 0.0) -> Any:
        data = {
            "position": [10.0, 20.0],
            "size": [2.0, 6.0],
            "angle_degrees": angle,
            "arrow_type": kind,
            "output_size": False,
        }
        return entity("ARROWHEAD", data)

    result = convert(drawing([arrow(kind) for kind in (0, 1, 3, 4, 5, 10, 11, 13, 99)]))
    unknown, barb, slash, box, closed, opened, dot, circle = (
        None,
        *result.document["entities"],
    )
    assert unknown is None and "DRACAD_ARROWHEAD_UNKNOWN" in codes(result)
    assert (circle["kind"], circle["center"], circle["radius"]) == (
        "CIRCLE",
        [10.0, 20.0],
        1.0,
    )
    assert dot["kind"] == "HATCH" and len(dot["loops"][0]["vertices"]) == 4
    assert opened["vertices"] == [[4.0, 21.0], [10.0, 20.0], [4.0, 19.0]]
    assert not opened["closed"] and closed["closed"]
    assert closed["vertices"] == [[10.0, 20.0], [4.0, 21.0], [4.0, 19.0]]
    assert box["kind"] == "HATCH" and box["loops"][0]["vertices"][0] == [9.0, 19.0]
    assert (slash["p1"], slash["p2"]) == ([9.0, 19.0], [11.0, 21.0])
    assert (barb["p1"], barb["p2"]) == ([10.0, 20.0], [4.0, 22.0])
    (up,) = convert(drawing([arrow(7, 90.0)])).document["entities"]
    assert up["loops"][0]["vertices"] == [
        [10.0, 20.0],
        pytest.approx([9.0, 14.0]),
        pytest.approx([11.0, 14.0]),
    ]


def test_curves_ellipses_and_unicode_reference_packages() -> None:
    curve = polyline(
        [line([0.0, 0.0], [1.0, 1.0])],
        curve_kind=3,
        curve_points=[[0.0, 0.0], [0.5, 0.2], [1.0, 1.0]],
    )
    saved = polyline([line([0.0, 0.0], [1.0, 1.0])], curve_kind=1)
    tall = entity(
        "ELLIPTICAL_ARC",
        {
            "center": [0.0, 0.0],
            "radius": [2.0, 5.0],
            "rotation_degrees": 0.0,
            "start_degrees": 0.0,
            "end_degrees": 90.0,
        },
    )
    block = SimpleNamespace(
        index=0, name="部品", name_bytes=b"A", entities=(line([0, 0], [1, 0]),)
    )
    placed = entity(
        "INSERT",
        {
            "name": "部品",
            "name_bytes": b"A",
            "reference_number": 1,
            "position": [5.0, 5.0],
            "scale": [1.0, -2.0],
            "rotation_degrees": 0.0,
        },
    )
    missing = entity(
        "INSERT",
        {
            "name": "無い",
            "name_bytes": b"B",
            "reference_number": 2,
            "position": [0.0, 0.0],
            "scale": [1.0, 1.0],
            "rotation_degrees": 0.0,
        },
    )
    result = convert(
        drawing(
            [
                curve,
                saved,
                tall,
                placed,
                package(1, [line([5.0, 5.0], [6.0, 5.0])], 1),
                missing,
                package(1, [line([9.0, 9.0], [9.0, 8.0])], 2),
                entity("LINE3D", {"start": [0.0, 0.0, 3.0], "end": [1.0, 1.0, 4.0]}),
            ],
            blocks=(block,),
            format="mpuw",
        )
    )
    flat, fallback, insert, ellipse, approximated = result.document["entities"]
    assert (flat["p1"], flat["p2"]) == ([0.0, 0.0], [1.0, 1.0])
    # The placed members stand in for a block the file does not define.
    assert fallback["p1"] == [9.0, 9.0]
    assert (insert["block"], insert["scale"]) == ("部品", [1.0, -2.0])
    assert ellipse["major_axis"] == pytest.approx([0.0, 5.0])
    assert ellipse["ratio"] == 0.4
    assert ellipse["start_param"] == pytest.approx(-math.pi / 2.0)
    assert ellipse["end_param"] == pytest.approx(0.0)
    assert approximated["vertices"] == [[0.0, 0.0], [0.5, 0.2], [1.0, 1.0]]
    assert approximated["approximation"]["segments"] == 2
    assert set(codes(result)) >= {
        "DRACAD_CURVE_APPROXIMATED",
        "DRACAD_3D_PROJECTED",
        "DRACAD_UNDEFINED_BLOCK",
    }
    assert list(result.document["tables"]["blocks"]) == ["部品"]


def test_text_details_and_diagnostics() -> None:
    result = convert(
        drawing(
            [
                text("{TCODE:DrawingScale}"),
                text("a{b}\\c\nd", width=0.75, origin=10),
                text("見出し", format_codes=True, slant_degrees=15.0),
                text("縦", font_name="@ＭＳ 明朝"),
                text("  "),
                text("壊", font_name="ＭＳ ゴ\ufffd ック"),
            ],
            marker=77,
            marker_observed=False,
            report=SimpleNamespace(
                lossy=True,
                decoded_counts={"TEXT": 6},
                unsupported_counts={30: 2},
                failed_counts={4: 1},
                orphan_records=3,
                clamped_spans=0,
            ),
        )
    )
    broken, vertical, styled, lines, field = result.document["entities"]
    assert "style" not in broken and vertical["style"] == "ＭＳ 明朝"
    assert styled["oblique_deg"] == 15.0
    assert lines["kind"] == "MTEXT" and lines["attach"] == "top_right"
    # Braces and backslashes of the text are not inline codes.
    assert lines["text"] == "\\W0.5;a\\{b\\}\\\\c\\Pd"
    # A field prints what the drawing says: the scale of this one is 1:1.
    assert field["text"] == "1:1"
    assert field["metadata"]["dracad"]["stored_text"] == "{TCODE:DrawingScale}"
    assert result.statistics["skipped_entity_counts"] == {"TEXT": 1}
    assert codes(result) == [
        "DRACAD_UNITS_ASSUMED_MM",
        "DRACAD_GENERATION_UNVERIFIED",
        "DRACAD_PARTIAL_READ",
        "DRACAD_TEXT_FORMAT_REMOVED",
        "DRACAD_TEXT_FIELD_FILLED",
        "DRACAD_TEXT_VERTICAL_IGNORED",
    ]
    partial = result.diagnostics[2]
    assert partial.details["unsupported_kinds"] == {"30": 2}
    assert partial.details["orphan_records"] == 3


def test_text_fields_print_what_the_drawing_says() -> None:
    source = drawing(
        [
            text("{TCODE:DrawingName}"),
            text('{TCODE:DrawingName@"L4"}|{TCODE:DrawingName@"M6"}'),
            text('{TCODE:DrawingName@"R3"}|{TCODE:DrawingName@"M2,3"}'),
            text("{TCODE:FileName}"),
            text("縮尺 {TCODE:DrawingScale} の図 {TCODE:Comment}"),
            text("{TCODE:PaperScale} {TCODE:PaperSizeName}{TCODE:PaperOrientation}"),
            text("{PSET:会社名}/{PSET:無い}/{TCODE:Nothing}"),
            # Values of the application, the user and the clock are not in the file.
            text("{TCODE:UserName}"),
            text("作成 {TCODE:CreateDate}"),
            text("{not a field}"),
        ],
        scale=1.0,
    )
    source.settings.scale_ratio = 0.01
    source.settings.comment = "XXXX新築工事"
    source.settings.paper_portrait = True
    source.property_set = lambda: {"会社名": "試験設計室"}
    result = dracad_drawing_to_ir(
        source, source_name="AB-12_fields.mpz", options=ImportOptions()
    )
    texts = [item["text"] for item in reversed(result.document["entities"])]
    assert texts == [
        "AB-12_fields",
        "AB-1|_fields",
        "lds|B-1",
        "AB-12_fields.mpz",
        # A sheet drawn at full size for drawings at 1:100 states 1:100.
        "縮尺 1:100 の図 XXXX新築工事",
        "1:1 A3縦",
        # DRA-CAD prints this for a name it does not know.
        "試験設計室/{ERR:無い}/{ERR:Nothing}",
        "作成 ",
        "{not a field}",
    ]
    filled = next(d for d in result.diagnostics if d.code == "DRACAD_TEXT_FIELD_FILLED")
    assert filled.details == {"count": 16}
    blank = next(
        d for d in result.diagnostics if d.code == "DRACAD_TEXT_FIELD_UNRESOLVED"
    )
    # The text that held nothing but the user name is left out.
    assert blank.details == {"fields": ["CreateDate", "UserName"], "texts_left_out": 1}
    assert result.statistics["skipped_entity_counts"] == {"TEXT": 1}
    # Without a file name its fields stay empty and are reported.
    nameless = dracad_drawing_to_ir(
        drawing([text("図 {TCODE:DrawingName}")]), options=ImportOptions()
    )
    assert nameless.document["entities"][0]["text"] == "図 "
    assert "DRACAD_TEXT_FIELD_UNRESOLVED" in codes(nameless)
    # Non-integer scales print without trailing zeros.
    half = drawing([text("{TCODE:PaperScale}/{TCODE:DrawingScale}")], scale=2.5)
    half.settings.scale_ratio = 1 / 7
    assert convert(half).document["entities"][0]["text"] == "1:2.5/1:17.5"


def test_half_arrowheads_stand_on_the_side_the_application_draws() -> None:
    def head(kind: int, angle: float) -> Any:
        return entity(
            "ARROWHEAD",
            {
                "position": [0.0, 0.0],
                "size": [2.0, 6.0],
                "angle_degrees": angle,
                "arrow_type": kind,
                "output_size": False,
            },
        )

    angles = (0.0, 45.0, 90.0, 180.0, 270.0, 315.0)
    result = convert(drawing([head(13, angle) for angle in angles]))
    barbs = [item["p2"] for item in reversed(result.document["entities"])]
    # Left of the direction, and right of it from straight up over left to
    # straight down: on a horizontal line both heads stand above it.
    expected = [
        [-6.0, 2.0],
        [
            -6 * math.sqrt(0.5) - 2 * math.sqrt(0.5),
            -6 * math.sqrt(0.5) + 2 * math.sqrt(0.5),
        ],
        [2.0, -6.0],
        [6.0, 2.0],
        [-2.0, 6.0],
        [
            -6 * math.sqrt(0.5) + 2 * math.sqrt(0.5),
            6 * math.sqrt(0.5) + 2 * math.sqrt(0.5),
        ],
    ]
    for barb, point in zip(barbs, expected, strict=True):
        assert barb == pytest.approx(point, abs=1e-9)
    closed = convert(drawing([head(14, 180.0)])).document["entities"][0]
    assert closed["vertices"] == [
        [0.0, 0.0],
        pytest.approx([6.0, 2.0]),
        pytest.approx([6.0, 0.0]),
    ]


def test_sheets_model_objects_and_widths_by_colour() -> None:
    viewport = entity(
        "VIEWPORT",
        {
            "flags": 0x80,
            "paper_point": [100.0, 100.0],
            "model_point": [5000.0, 2000.0],
            "scale": [0.01, 0.01],
            "rotation_degrees": 90.0,
            "center": [150.0, 100.0],
            "half_size": [100.0, 50.0],
        },
        segment=2,
    )
    odd = entity(
        "VIEWPORT",
        {
            "flags": 0,
            "paper_point": [0.0, 0.0],
            "model_point": [0.0, 0.0],
            "scale": [0.01, 0.01],
            "rotation_degrees": 0.0,
            "center": None,
            "half_size": None,
        },
        segment=2,
    )
    face = polyline(
        [line([0.0, 0.0], [4.0, 0.0]), line([4.0, 0.0], [4.0, 4.0])],
        closed=True,
        fill=fill(2),
        three_d=True,
    )
    source = drawing(
        [
            line([0.0, 0.0], [1.0, 0.0], color=2),
            line([0.0, 0.0], [2.0, 0.0]),
            viewport,
            odd,
            # A note on the sheet, sized on the output sheet: already final there.
            text("図", height=3.0, output_size=True, segment=2),
            entity(
                "LAYOUT",
                {
                    "number": 2,
                    "name": "",
                    "paper_type": 0,
                    "paper_size": [500.0, 300.0],
                },
            ),
            entity("POLYGON_3D", {"hole_count": 0}, children=(face,)),
            entity("CAMERA", {"position": [0, 0, 0], "target": [1, 0, 0]}),
            entity("LIGHT", {"position": [0, 0, 9], "target": [0, 0, 0]}),
        ],
        scale=100.0,
    )
    source.settings.width_by_color = True
    result = convert(source)
    document = result.document
    # The face shows its edges, not its surface; cameras and lights nothing.
    assert kinds(result) == ["LWPOLYLINE", "LINE", "LINE"]
    assert "DRACAD_3D_PROJECTED" in codes(result)
    by_layer, red = document["entities"][1:]
    # A colour prints with its own pen; an object in the colour of its layer
    # takes the width of the layer.
    assert red["lineweight_mm"] == 0.15 and "lineweight_mm" not in by_layer
    assert document["tables"]["layers"]["壁"]["lineweight_mm"] == 0.4
    (layout,) = document["layouts"]
    assert layout["name"] == "Layout2" and layout["tab_order"] == 2
    assert layout["paper"] == {"size_mm": [500.0, 300.0], "units": "mm"}
    (note,) = layout["entities"]
    assert note["height"] == 3.0
    (window,) = layout["viewports"]
    assert (window["width"], window["height"], window["rotation"]) == (
        200.0,
        100.0,
        90.0,
    )
    # The middle of the window lies 50 right of the paper point: with the model
    # turned a quarter, that is 5000 model units below the model point.
    assert window["view_center"] == pytest.approx([5000.0, -3000.0])
    assert window["view_height"] == pytest.approx(10000.0)
    skipped = next(d for d in result.diagnostics if d.code == "DRACAD_VIEWPORT_SKIPPED")
    assert skipped.details == {"count": 1}
    validate_ir(document)


def test_strict_mode_stops_at_an_object_that_cannot_be_converted() -> None:
    source = drawing(
        [
            line([0.0, 0.0], [1.0, 0.0]),
            entity("CIRCLE", {"center": [0.0, 0.0], "radius": 0.0, "fill": None}),
        ]
    )
    with pytest.raises(ImporterError, match="Failed to convert DRA-CAD CIRCLE"):
        convert(source)
    result = convert(source, strict=False)
    assert kinds(result) == ["LINE"]
    assert result.statistics["skipped_entity_counts"] == {"CIRCLE": 1}
    failed = next(
        d for d in result.diagnostics if d.code == "DRACAD_ENTITY_CONVERSION_FAILED"
    )
    assert (failed.severity, failed.source_kind, failed.action) == (
        "error",
        "CIRCLE",
        "skipped",
    )


LOCAL = os.environ.get("CAD2D_IR_DRACAD_FIXTURES")


@pytest.mark.skipif(not LOCAL, reason="CAD2D_IR_DRACAD_FIXTURES is not set")
def test_local_dracad_drawings_convert() -> None:
    pytest.importorskip("ezdracad")
    root = Path(LOCAL or "")
    paths = sorted(
        path
        for path in root.rglob("*")
        if path.suffix.lower() in (".mpz", ".mps", ".mpx", ".mpuw")
    )
    assert paths
    for path in paths:
        result = convert_dracad_file_to_ir(path, options=ImportOptions(strict=False))
        validate_ir(result.document)
        failed = [
            d for d in result.diagnostics if d.code == "DRACAD_ENTITY_CONVERSION_FAILED"
        ]
        assert not failed, (path.name, failed[:3])
