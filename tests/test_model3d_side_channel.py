from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from cad2d_ir import convert_dxf_text_to_ir, convert_file_to_ir
from cad2d_ir.codecs.dxf import dxf_to_ir
from cad2d_ir.importers import ImportOptions
from cad2d_ir.importers.dwg import convert_dwg_file_to_ir, dwg_document_to_ir
from cad2d_ir.model3d import (
    Model3DCollector,
    bulge_polyline_points,
    has_3d_content,
    insert_matrix,
    ocs_to_wcs,
    spline_points,
)


# --- synthetic DWG document (same shape as tests/test_dwg_importer.py) -------


@dataclass(frozen=True)
class _Entity:
    dxftype: str
    handle: int
    dxf: dict[str, Any]


class _Layout:
    def __init__(self, entities: list[_Entity]) -> None:
        self._entities = entities

    def query(self) -> list[_Entity]:
        return self._entities


class _Document:
    version = "AC1032"

    def __init__(
        self, entities: list[_Entity], placements: dict[int, tuple[int, int | None]]
    ) -> None:
        self._entities = entities
        self._placements = placements

    def modelspace(self) -> _Layout:
        return _Layout(self._entities)

    def entities(self) -> _Layout:
        return _Layout(self._entities)

    def entity_placement(self, handle: int) -> tuple[int, int | None] | None:
        return self._placements.get(handle)


def _style(*, owner_handle: int | None = None) -> dict[str, Any]:
    return {
        "layer_handle": 16,
        "owner_handle": owner_handle,
        "resolved_color_index": 7,
        "resolved_true_color": None,
    }


def _dwg_document() -> _Document:
    entities = [
        _Entity(
            "LINE", 1, {"start": (0.0, 0.0, 0.0), "end": (10.0, 0.0, 4.0), **_style()}
        ),
        _Entity(
            "ARC",
            2,
            {
                "center": (0.0, 0.0, 2.0),
                "radius": 1.0,
                "start_angle": 0.0,
                "end_angle": 90.0,
                **_style(),
            },
        ),
        _Entity(
            "LWPOLYLINE",
            3,
            {
                "points": [(0.0, 0.0, 0.0), (2.0, 0.0, 0.0)],
                "bulges": [1.0, 0.0],
                "widths": [(0.0, 0.0)] * 2,
                "flags": 0,
                "closed": False,
                **_style(),
            },
        ),
        _Entity(
            "POLYLINE_3D",
            4,
            {
                "points": [(0.0, 0.0, 0.0), (1.0, 1.0, 1.0), (2.0, 0.0, 3.0)],
                "flags": 8,
                "closed": False,
                **_style(),
            },
        ),
        _Entity(
            "3DFACE",
            5,
            {
                "points": [
                    (0.0, 0.0, 0.0),
                    (1.0, 0.0, 0.0),
                    (1.0, 1.0, 1.0),
                    (0.0, 1.0, 1.0),
                ],
                "invisible_edge_flags": 0,
                **_style(),
            },
        ),
        _Entity(
            "POLYLINE_PFACE",
            6,
            {
                "num_vertices": 4,
                "num_faces": 1,
                "vertices": [
                    (0.0, 0.0, 5.0),
                    (1.0, 0.0, 5.0),
                    (1.0, 1.0, 5.0),
                    (0.0, 1.0, 5.0),
                ],
                "faces": [[1, 2, 3, -4]],
                **_style(),
            },
        ),
        _Entity(
            "POLYLINE_MESH",
            7,
            {
                "points": [
                    (0.0, 0.0, 0.0),
                    (1.0, 0.0, 0.0),
                    (0.0, 1.0, 2.0),
                    (1.0, 1.0, 2.0),
                ],
                "flags": 16,
                "m_vertex_count": 2,
                "n_vertex_count": 2,
                "closed": False,
                **_style(),
            },
        ),
        _Entity(
            "SOLID",
            8,
            {
                "points": [
                    (0.0, 0.0, 7.0),
                    (1.0, 0.0, 7.0),
                    (0.0, 1.0, 7.0),
                    (1.0, 1.0, 7.0),
                ],
                "thickness": 0.0,
                "extrusion": (1.0, 0.0, 0.0),
                **_style(),
            },
        ),
        _Entity(
            "ELLIPSE",
            9,
            {
                "center": (0.0, 0.0, 0.0),
                "major_axis": (2.0, 0.0, 0.0),
                "axis_ratio": 0.5,
                "start_angle": 0.0,
                "end_angle": 6.283185307179586,
                "extrusion": (0.0, -1.0, 0.0),
                **_style(),
            },
        ),
        _Entity(
            "SPLINE",
            10,
            {
                "degree": 3,
                "control_points": [
                    (0.0, 0.0, 0.0),
                    (1.0, 2.0, 1.0),
                    (2.0, 2.0, 2.0),
                    (3.0, 0.0, 3.0),
                ],
                "knots": [0.0, 0.0, 0.0, 0.0, 1.0, 1.0, 1.0, 1.0],
                "weights": [],
                "closed": False,
                **_style(),
            },
        ),
        # block definition content (owner 100) and its reference
        _Entity(
            "LINE",
            11,
            {
                "start": (0.0, 0.0, 0.0),
                "end": (1.0, 0.0, 0.0),
                **_style(owner_handle=100),
            },
        ),
        _Entity(
            "INSERT",
            12,
            {
                "insert": (10.0, 0.0, 5.0),
                "name": "SYMBOL",
                "xscale": 2.0,
                "yscale": 2.0,
                "zscale": 2.0,
                "rotation": 90.0,
                **_style(),
            },
        ),
        _Entity("3DSOLID", 13, {**_style()}),
        _Entity("REGION", 14, {**_style()}),
        # paper space is not collected
        _Entity(
            "LINE", 15, {"start": (0.0, 0.0, 9.0), "end": (1.0, 0.0, 9.0), **_style()}
        ),
    ]
    placements = {handle: (2, None) for handle in range(1, 15)}
    placements[11] = (0, 100)
    placements[15] = (1, None)
    return _Document(entities, placements)


def _by_kind(items: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {item["kind"]: item for item in items}


def test_dwg_side_channel_collects_curves_faces_inserts_and_unsupported() -> None:
    result = dwg_document_to_ir(
        _dwg_document(),
        block_names_by_handle={100: "SYMBOL", 31: "*Model_Space"},
        options=ImportOptions(keep_3d=True, strict=False),
    )
    model3d = result.model3d
    assert model3d is not None
    assert model3d["version"] == 1
    curves = _by_kind(model3d["curves"])
    faces = _by_kind(model3d["faces"])

    line = curves["LINE"]
    assert line["points"] == [[0.0, 0.0, 0.0], [10.0, 0.0, 4.0]]
    assert line["planar"] is False
    entity_ids = {entity["id"] for entity in result.document["entities"]}
    first = result.document["entities"][0]
    assert line["id"] == first["id"] and line["source_id"] == first["source"]["id"]
    assert line["layer"] == first["layer"]

    arc = curves["ARC"]
    assert arc["planar"] is True  # elevation 2, no extrusion: a plane parallel to XY
    assert all(point[2] == 2.0 for point in arc["points"])
    assert arc["points"][0][:2] == pytest.approx([1.0, 0.0])
    assert arc["points"][-1][:2] == pytest.approx([0.0, 1.0])

    polyline = curves["LWPOLYLINE"]
    assert len(polyline["points"]) > 3  # the semicircle bulge was expanded
    assert polyline["points"][0] == [0.0, 0.0, 0.0]
    assert polyline["points"][-1] == [2.0, 0.0, 0.0]
    # a positive bulge runs counter-clockwise from (0, 0) to (2, 0): below the chord
    assert min(point[1] for point in polyline["points"]) == pytest.approx(
        -1.0, abs=1e-6
    )

    assert curves["POLYLINE_3D"]["points"][-1] == [2.0, 0.0, 3.0]
    assert curves["POLYLINE_3D"]["planar"] is False
    ellipse = curves["ELLIPSE"]
    assert ellipse["closed"] is True
    # extrusion -Y: the ellipse lies in the XZ plane
    assert all(abs(point[1]) < 1e-9 for point in ellipse["points"])
    assert max(abs(point[2]) for point in ellipse["points"]) == pytest.approx(
        1.0, abs=1e-6
    )
    spline = curves["SPLINE"]
    assert spline["points"][0] == pytest.approx([0.0, 0.0, 0.0])
    assert spline["points"][-1] == pytest.approx([3.0, 0.0, 3.0])
    assert len(spline["points"]) == 97

    assert len(faces["3DFACE"]["triangles"]) == 2
    assert len(faces["POLYLINE_PFACE"]["triangles"]) == 2
    assert faces["POLYLINE_PFACE"]["id"] is None
    assert len(faces["POLYLINE_MESH"]["triangles"]) == 2
    solid = faces["SOLID"]
    assert len(solid["triangles"]) == 2
    # OCS extrusion +X maps (x, y, z) to world (z, x, y): every corner has x == 7
    assert all(corner[0] == 7.0 for tri in solid["triangles"] for corner in tri)

    (insert,) = model3d["inserts"]
    assert insert["block"] == "SYMBOL"
    assert insert["id"] in entity_ids
    matrix = insert["matrix"]
    # world = T(10, 0, 5) · Rz(90°) · S(2): the block point (1, 0, 0) lands on (10, 2, 5)
    moved = [
        sum(matrix[row][k] * [1.0, 0.0, 0.0, 1.0][k] for k in range(4))
        for row in range(3)
    ]
    assert moved == pytest.approx([10.0, 2.0, 5.0])
    assert model3d["blocks"]["SYMBOL"]["curves"][0]["points"] == [
        [0.0, 0.0, 0.0],
        [1.0, 0.0, 0.0],
    ]
    assert model3d["blocks"]["SYMBOL"]["curves"][0]["id"] in {
        entity["id"]
        for entity in result.document["tables"]["blocks"]["SYMBOL"]["entities"]
    }

    assert model3d["unsupported"] == {"3DSOLID": 1, "REGION": 1}
    # the paper-space line (handle 15, z = 9) is not part of the model
    assert all(
        point[2] != 9.0 for curve in model3d["curves"] for point in curve["points"]
    )
    stats = model3d["stats"]
    assert stats["curves_total"] == 7
    assert stats["curves_3d"] == 4  # LINE, POLYLINE_3D, ELLIPSE, SPLINE
    assert stats["elevations"] == 2  # z = 0 (LWPOLYLINE) and z = 2 (ARC)
    assert stats["faces"] == 8
    assert stats["inserts"] == 1
    assert stats["unsupported"] == 2
    assert has_3d_content(model3d)
    assert model3d["units"] == result.document["header"]["units"]


def test_keep_3d_off_leaves_the_document_untouched() -> None:
    plain = dwg_document_to_ir(
        _dwg_document(),
        block_names_by_handle={100: "SYMBOL", 31: "*Model_Space"},
        options=ImportOptions(strict=False),
    )
    kept = dwg_document_to_ir(
        _dwg_document(),
        block_names_by_handle={100: "SYMBOL", 31: "*Model_Space"},
        options=ImportOptions(keep_3d=True, strict=False),
    )
    assert plain.model3d is None
    assert json.dumps(plain.document, sort_keys=True) == json.dumps(
        kept.document, sort_keys=True
    )
    assert [item.code for item in plain.diagnostics] == [
        item.code for item in kept.diagnostics
    ]
    assert not has_3d_content(None)


# --- DXF text -----------------------------------------------------------------


def _dxf(entities: list[str], blocks: list[str] | None = None) -> str:
    lines = ["0", "SECTION", "2", "HEADER", "9", "$INSUNITS", "70", "4", "0", "ENDSEC"]
    if blocks:
        lines += ["0", "SECTION", "2", "BLOCKS", *blocks, "0", "ENDSEC"]
    lines += ["0", "SECTION", "2", "ENTITIES", *entities, "0", "ENDSEC", "0", "EOF"]
    return "\n".join(lines) + "\n"


def _dxf_fixture() -> str:
    entities = [
        "0",
        "LINE",
        "5",
        "A1",
        "8",
        "L1",
        "10",
        "0",
        "20",
        "0",
        "30",
        "1",
        "11",
        "4",
        "21",
        "0",
        "31",
        "3",
        # circle in the plane x = 2 (extrusion +X, OCS centre z = 2)
        "0",
        "CIRCLE",
        "5",
        "A2",
        "8",
        "L1",
        "10",
        "0",
        "20",
        "0",
        "30",
        "2",
        "40",
        "1",
        "210",
        "1",
        "220",
        "0",
        "230",
        "0",
        "0",
        "LWPOLYLINE",
        "5",
        "A3",
        "8",
        "L1",
        "90",
        "2",
        "70",
        "0",
        "38",
        "5",
        "10",
        "0",
        "20",
        "0",
        "42",
        "1",
        "10",
        "2",
        "20",
        "0",
        "0",
        "POLYLINE",
        "5",
        "A4",
        "8",
        "L1",
        "70",
        "8",
        "0",
        "VERTEX",
        "10",
        "0",
        "20",
        "0",
        "30",
        "0",
        "0",
        "VERTEX",
        "10",
        "1",
        "20",
        "1",
        "30",
        "4",
        "0",
        "SEQEND",
        # polyface mesh: 4 vertices, 1 face
        "0",
        "POLYLINE",
        "5",
        "A5",
        "8",
        "L1",
        "70",
        "64",
        "71",
        "4",
        "72",
        "1",
        "0",
        "VERTEX",
        "70",
        "192",
        "10",
        "0",
        "20",
        "0",
        "30",
        "6",
        "0",
        "VERTEX",
        "70",
        "192",
        "10",
        "1",
        "20",
        "0",
        "30",
        "6",
        "0",
        "VERTEX",
        "70",
        "192",
        "10",
        "1",
        "20",
        "1",
        "30",
        "6",
        "0",
        "VERTEX",
        "70",
        "192",
        "10",
        "0",
        "20",
        "1",
        "30",
        "6",
        "0",
        "VERTEX",
        "70",
        "128",
        "71",
        "1",
        "72",
        "2",
        "73",
        "3",
        "74",
        "4",
        "0",
        "SEQEND",
        "0",
        "3DFACE",
        "5",
        "A6",
        "8",
        "L1",
        "10",
        "0",
        "20",
        "0",
        "30",
        "0",
        "11",
        "1",
        "21",
        "0",
        "31",
        "0",
        "12",
        "1",
        "22",
        "1",
        "32",
        "1",
        "13",
        "0",
        "23",
        "1",
        "33",
        "1",
        "0",
        "MESH",
        "5",
        "A7",
        "8",
        "L1",
        "71",
        "2",
        "72",
        "0",
        "91",
        "0",
        "92",
        "3",
        "10",
        "0",
        "20",
        "0",
        "30",
        "8",
        "10",
        "1",
        "20",
        "0",
        "30",
        "8",
        "10",
        "0",
        "20",
        "1",
        "30",
        "8",
        "93",
        "4",
        "90",
        "3",
        "90",
        "0",
        "90",
        "1",
        "90",
        "2",
        "94",
        "0",
        "95",
        "0",
        "0",
        "SPLINE",
        "5",
        "A8",
        "8",
        "L1",
        "70",
        "0",
        "71",
        "2",
        "40",
        "0",
        "40",
        "0",
        "40",
        "0",
        "40",
        "1",
        "40",
        "1",
        "40",
        "1",
        "10",
        "0",
        "20",
        "0",
        "30",
        "0",
        "10",
        "1",
        "20",
        "2",
        "30",
        "1",
        "10",
        "2",
        "20",
        "0",
        "30",
        "2",
        "0",
        "INSERT",
        "5",
        "A9",
        "8",
        "L1",
        "2",
        "SYM",
        "10",
        "10",
        "20",
        "0",
        "30",
        "5",
        "41",
        "2",
        "42",
        "2",
        "43",
        "2",
        "50",
        "90",
        "70",
        "2",
        "71",
        "1",
        "44",
        "3",
        "45",
        "0",
        "0",
        "3DSOLID",
        "5",
        "AA",
        "8",
        "L1",
        "1",
        "ACIS",
    ]
    blocks = [
        "0",
        "BLOCK",
        "5",
        "B1",
        "8",
        "0",
        "2",
        "SYM",
        "70",
        "0",
        "10",
        "1",
        "20",
        "0",
        "30",
        "0",
        "0",
        "LINE",
        "5",
        "B2",
        "8",
        "0",
        "10",
        "1",
        "20",
        "0",
        "30",
        "0",
        "11",
        "2",
        "21",
        "0",
        "31",
        "0",
        "0",
        "ENDBLK",
        "5",
        "B3",
    ]
    return _dxf(entities, blocks)


def test_dxf_side_channel_collects_entities_and_mesh_variants() -> None:
    collector = Model3DCollector()
    document = dxf_to_ir(_dxf_fixture(), model3d=collector)
    model3d = collector.finalize(units=document["header"]["units"])
    curves = _by_kind(model3d["curves"])
    faces = _by_kind(model3d["faces"])

    assert curves["LINE"]["points"] == [[0.0, 0.0, 1.0], [4.0, 0.0, 3.0]]
    assert curves["LINE"]["id"] == "EA1" and curves["LINE"]["layer"] == "L1"
    circle = curves["CIRCLE"]
    assert circle["closed"] is True and circle["planar"] is False
    assert all(point[0] == pytest.approx(2.0) for point in circle["points"])
    polyline = curves["LWPOLYLINE"]
    assert all(point[2] == 5.0 for point in polyline["points"])
    assert len(polyline["points"]) > 3
    assert curves["POLYLINE"]["points"] == [[0.0, 0.0, 0.0], [1.0, 1.0, 4.0]]
    assert curves["SPLINE"]["points"][0] == pytest.approx([0.0, 0.0, 0.0])
    assert curves["SPLINE"]["points"][-1] == pytest.approx([2.0, 0.0, 2.0])

    assert len(faces["POLYLINE_PFACE"]["triangles"]) == 2
    assert faces["POLYLINE_PFACE"]["source_id"] == "A5"
    assert len(faces["3DFACE"]["triangles"]) == 2
    assert len(faces["MESH"]["triangles"]) == 1
    assert faces["MESH"]["triangles"][0][0] == [0.0, 0.0, 8.0]

    inserts = model3d["inserts"]
    assert len(inserts) == 2  # MINSERT 2 columns × 1 row
    assert [item["cell"] for item in inserts] == [[0, 0], [1, 0]]
    assert inserts[0]["id"] == "EA9"
    # block base point (1, 0, 0): block point (2, 0, 0) is 1 unit along the block X axis
    first = inserts[0]["matrix"]
    moved = [
        sum(first[row][k] * [2.0, 0.0, 0.0, 1.0][k] for k in range(4))
        for row in range(3)
    ]
    assert moved == pytest.approx([10.0, 2.0, 5.0])
    second = inserts[1]["matrix"]
    # the second column is 3 units along the rotated X axis (= world +Y)
    moved = [
        sum(second[row][k] * [2.0, 0.0, 0.0, 1.0][k] for k in range(4))
        for row in range(3)
    ]
    assert moved == pytest.approx([10.0, 5.0, 5.0])
    assert model3d["blocks"]["SYM"]["base_point"] == [1.0, 0.0, 0.0]
    assert model3d["blocks"]["SYM"]["curves"][0]["id"] == "EB2"

    assert model3d["unsupported"] == {"3DSOLID": 1}
    assert model3d["units"] == "mm"
    assert model3d["stats"]["faces"] == 5
    assert model3d["stats"]["inserts"] == 2

    plain = dxf_to_ir(_dxf_fixture())
    assert json.dumps(plain, sort_keys=True) == json.dumps(document, sort_keys=True)


def test_public_api_exposes_the_side_channel(tmp_path: Path) -> None:
    text_result = convert_dxf_text_to_ir(_dxf_fixture(), keep_3d=True)
    assert text_result.model3d is not None
    assert text_result.model3d["stats"]["curves_total"] == 6
    assert convert_dxf_text_to_ir(_dxf_fixture()).model3d is None

    source = tmp_path / "model.dxf"
    source.write_text(_dxf_fixture(), encoding="ascii")
    result = convert_file_to_ir(source, keep_3d=True)
    assert result.model3d is not None
    assert result.model3d["stats"]["unsupported"] == 1
    assert convert_file_to_ir(source).model3d is None


def test_dwg_file_round_trip_through_ezdwg(tmp_path: Path) -> None:
    ezdwg = pytest.importorskip("ezdwg")
    drawing = ezdwg.new("AC1015")
    space = drawing.modelspace()
    space.add_line((0.0, 0.0, 0.0), (5.0, 0.0, 2.0))
    space.add_polyline3d([(0.0, 0.0, 0.0), (1.0, 1.0, 1.0), (2.0, 0.0, 2.0)])
    space.add_3dface(
        [(0.0, 0.0, 3.0), (1.0, 0.0, 3.0), (1.0, 1.0, 4.0), (0.0, 1.0, 4.0)]
    )
    block = drawing.blocks.new("SYM")
    block.add_line((0.0, 0.0, 0.0), (1.0, 0.0, 0.0))
    space.add_blockref("SYM", (10.0, 0.0, 5.0), scale=(2.0, 2.0, 2.0), rotation=90.0)
    path = tmp_path / "model.dwg"
    drawing.save(str(path))

    result = convert_dwg_file_to_ir(
        path, options=ImportOptions(keep_3d=True, strict=False)
    )
    model3d = result.model3d
    assert model3d is not None
    curves = _by_kind(model3d["curves"])
    assert curves["LINE"]["points"] == [[0.0, 0.0, 0.0], [5.0, 0.0, 2.0]]
    assert curves["POLYLINE_3D"]["points"][-1] == [2.0, 0.0, 2.0]
    (face,) = model3d["faces"]
    assert len(face["triangles"]) == 2
    (insert,) = model3d["inserts"]
    assert insert["block"] == "SYM"
    assert [insert["matrix"][row][3] for row in range(3)] == [10.0, 0.0, 5.0]
    assert model3d["blocks"]["SYM"]["curves"][0]["points"][-1] == [1.0, 0.0, 0.0]
    assert model3d["stats"]["curves_3d"] == 2


# --- geometry helpers ----------------------------------------------------------


def test_geometry_helpers() -> None:
    assert ocs_to_wcs((1.0, 2.0, 3.0), (0.0, 0.0, 1.0)) == (1.0, 2.0, 3.0)
    assert ocs_to_wcs((1.0, 2.0, 3.0), (1.0, 0.0, 0.0)) == pytest.approx(
        (3.0, 1.0, 2.0)
    )
    quarter = bulge_polyline_points(
        [(0.0, 0.0, 0.0), (1.0, 1.0, 0.0)],
        [math.tan(math.pi / 8)],
        closed=False,
        extrusion=(0.0, 0.0, 1.0),
        curve_segments=8,
    )
    assert quarter[0] == (0.0, 0.0, 0.0) and quarter[-1] == (1.0, 1.0, 0.0)
    # a counter-clockwise 90° arc from (0,0) to (1,1) is centred at (0, 1)
    assert all(math.hypot(x, y - 1.0) == pytest.approx(1.0) for x, y, _z in quarter)
    clockwise = bulge_polyline_points(
        [(0.0, 0.0, 0.0), (1.0, 1.0, 0.0)],
        [-math.tan(math.pi / 8)],
        closed=False,
        extrusion=(0.0, 0.0, 1.0),
        curve_segments=8,
    )
    assert all(math.hypot(x - 1.0, y) == pytest.approx(1.0) for x, y, _z in clockwise)
    straight = spline_points(
        [(0.0, 0.0, 0.0), (1.0, 1.0, 1.0)],
        1,
        None,
        None,
        closed=False,
        curve_segments=4,
    )
    assert straight[0] == pytest.approx((0.0, 0.0, 0.0))
    assert straight[-1] == pytest.approx((1.0, 1.0, 1.0))
    matrix = insert_matrix(
        insert=(1.0, 2.0, 3.0),
        extrusion=(0.0, 0.0, 1.0),
        scale=(1.0, 1.0, 1.0),
        rotation_deg=0.0,
        base_point=(0.0, 0.0, 0.0),
    )
    assert [row[3] for row in matrix[:3]] == [1.0, 2.0, 3.0]
