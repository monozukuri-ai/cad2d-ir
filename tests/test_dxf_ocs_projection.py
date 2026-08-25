from __future__ import annotations

import pytest

from cad2d_ir.codecs.dxf import dxf_to_ir
from cad2d_ir.importers.base import ImportDiagnostic


def _dxf_with_xz_geometry() -> str:
    return "\n".join(
        [
            "0",
            "SECTION",
            "2",
            "ENTITIES",
            "0",
            "POINT",
            "5",
            "7B",
            "10",
            "0",
            "20",
            "0",
            "30",
            "0",
            "0",
            "LINE",
            "5",
            "7C",
            "10",
            "50",
            "20",
            "0",
            "30",
            "25",
            "11",
            "130",
            "21",
            "0",
            "31",
            "25",
            "0",
            "LINE",
            "5",
            "7D",
            "10",
            "130",
            "20",
            "0",
            "30",
            "25",
            "11",
            "130",
            "21",
            "0",
            "31",
            "225",
            "0",
            "ARC",
            "5",
            "88",
            "10",
            "90",
            "20",
            "125",
            "30",
            "0",
            "40",
            "15",
            "50",
            "0",
            "51",
            "180",
            "210",
            "0",
            "220",
            "-1",
            "230",
            "0",
            "0",
            "ARC",
            "5",
            "8B",
            "10",
            "-90",
            "20",
            "125",
            "30",
            "0",
            "40",
            "13",
            "50",
            "0",
            "51",
            "180",
            "210",
            "0",
            "220",
            "1",
            "230",
            "0",
            "0",
            "ENDSEC",
            "0",
            "EOF",
        ]
    )


def test_axis_aligned_xz_drawing_is_projected_with_ocs_arcs() -> None:
    diagnostics: list[ImportDiagnostic] = []

    document = dxf_to_ir(
        _dxf_with_xz_geometry(), validate=True, diagnostics=diagnostics
    )

    point, horizontal, vertical, lower_arc, upper_arc = document["entities"]
    assert point["position"] == pytest.approx([0.0, 0.0])
    assert horizontal["p1"] == pytest.approx([50.0, 25.0])
    assert horizontal["p2"] == pytest.approx([130.0, 25.0])
    assert vertical["p1"] == pytest.approx([130.0, 25.0])
    assert vertical["p2"] == pytest.approx([130.0, 225.0])

    assert lower_arc["center"] == pytest.approx([90.0, 125.0])
    assert lower_arc["start_angle"] == pytest.approx(0.0)
    assert lower_arc["end_angle"] == pytest.approx(180.0)
    assert lower_arc.get("ccw", True) is True

    assert upper_arc["center"] == pytest.approx([90.0, 125.0])
    assert upper_arc["start_angle"] == pytest.approx(180.0)
    assert upper_arc["end_angle"] == pytest.approx(0.0)
    assert upper_arc["ccw"] is False

    assert all(
        entity["metadata"]["dxf"]["projected_from_plane"] == "XZ"
        for entity in document["entities"]
    )
    assert [diagnostic.code for diagnostic in diagnostics] == [
        "DXF_NON_XY_PLANE_PROJECTED"
    ]


def test_xy_drawing_keeps_existing_coordinates_without_projection_diagnostic() -> None:
    dxf = "\n".join(
        [
            "0",
            "SECTION",
            "2",
            "ENTITIES",
            "0",
            "LINE",
            "10",
            "1",
            "20",
            "2",
            "30",
            "0",
            "11",
            "3",
            "21",
            "4",
            "31",
            "0",
            "0",
            "ENDSEC",
            "0",
            "EOF",
        ]
    )
    diagnostics: list[ImportDiagnostic] = []

    document = dxf_to_ir(dxf, validate=True, diagnostics=diagnostics)

    assert document["entities"][0]["p1"] == pytest.approx([1.0, 2.0])
    assert document["entities"][0]["p2"] == pytest.approx([3.0, 4.0])
    assert "metadata" not in document["entities"][0]
    assert diagnostics == []


def test_axis_aligned_yz_drawing_uses_the_same_projection_contract() -> None:
    dxf = "\n".join(
        [
            "0",
            "SECTION",
            "2",
            "ENTITIES",
            "0",
            "LINE",
            "10",
            "0",
            "20",
            "10",
            "30",
            "20",
            "11",
            "0",
            "21",
            "30",
            "31",
            "40",
            "0",
            "ARC",
            "10",
            "20",
            "20",
            "40",
            "30",
            "0",
            "40",
            "5",
            "50",
            "0",
            "51",
            "90",
            "210",
            "1",
            "220",
            "0",
            "230",
            "0",
            "0",
            "ENDSEC",
            "0",
            "EOF",
        ]
    )
    diagnostics: list[ImportDiagnostic] = []

    document = dxf_to_ir(dxf, validate=True, diagnostics=diagnostics)

    line, arc = document["entities"]
    assert line["p1"] == pytest.approx([10.0, 20.0])
    assert line["p2"] == pytest.approx([30.0, 40.0])
    assert arc["center"] == pytest.approx([20.0, 40.0])
    assert arc["start_angle"] == pytest.approx(0.0)
    assert arc["end_angle"] == pytest.approx(90.0)
    assert arc.get("ccw", True) is True
    assert diagnostics[0].details == {
        "source_plane": "YZ",
        "target_plane": "XY",
        "method": "axis_aligned",
    }
