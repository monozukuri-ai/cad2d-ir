"""Paper-space layouts and viewports through the schema, the DXF codec and the DWG importer."""

from __future__ import annotations

import copy
from dataclasses import dataclass
from typing import Any

import pytest

from cad2d_ir.codecs.dxf import dxf_to_ir, ir_to_dxf
from cad2d_ir.diagnostics import ExportDiagnostic
from cad2d_ir.importers.base import ImportDiagnostic
from cad2d_ir.importers.dwg import dwg_document_to_ir
from cad2d_ir.layouts import (
    promote_layout_when_model_is_empty,
    viewport_from_dxf_values,
    viewport_to_dxf_view,
)
from cad2d_ir.schema import IRValidationError, validate_ir

Pairs = list[tuple[int, Any]]


def _dxf(
    entities: list[Pairs],
    *,
    tables: Pairs | None = None,
    blocks: Pairs | None = None,
    objects: Pairs | None = None,
) -> str:
    pairs: Pairs = []
    if tables:
        pairs += [(0, "SECTION"), (2, "TABLES"), *tables, (0, "ENDSEC")]
    if blocks:
        pairs += [(0, "SECTION"), (2, "BLOCKS"), *blocks, (0, "ENDSEC")]
    pairs += [(0, "SECTION"), (2, "ENTITIES")]
    for record in entities:
        pairs += record
    pairs += [(0, "ENDSEC")]
    if objects:
        pairs += [(0, "SECTION"), (2, "OBJECTS"), *objects, (0, "ENDSEC")]
    pairs += [(0, "EOF")]
    return "\n".join(f"{code}\n{value}" for code, value in pairs) + "\n"


def _line(handle: str, x: float, *common: tuple[int, Any]) -> Pairs:
    return [
        (0, "LINE"),
        (5, handle),
        *common,
        (8, "0"),
        (10, x),
        (20, 0),
        (11, x + 1),
        (21, 0),
    ]


def _viewport(handle: str, viewport_id: int, *extra: tuple[int, Any]) -> Pairs:
    return [
        (0, "VIEWPORT"),
        (5, handle),
        (100, "AcDbEntity"),
        (67, 1),
        (8, "VP"),
        (100, "AcDbViewport"),
        (10, 100),
        (20, 80),
        (30, 0),
        (40, 120),
        (41, 60),
        (68, viewport_id),
        (69, viewport_id),
        *extra,
    ]


_LAYER_TABLE: Pairs = [
    (0, "TABLE"),
    (2, "LAYER"),
    (0, "LAYER"),
    (5, "10"),
    (2, "0"),
    (70, 0),
    (62, 7),
    (0, "LAYER"),
    (5, "11"),
    (2, "HIDDEN_IN_VIEW"),
    (70, 0),
    (62, 7),
    (0, "ENDTAB"),
]


def _block_records(*records: tuple[str, str]) -> Pairs:
    pairs: Pairs = [(0, "TABLE"), (2, "BLOCK_RECORD")]
    for handle, name in records:
        pairs += [(0, "BLOCK_RECORD"), (5, handle), (2, name)]
    return [*pairs, (0, "ENDTAB")]


def _layout_object(name: str, tab_order: int, block_record: str) -> Pairs:
    return [
        (0, "LAYOUT"),
        (5, f"L{tab_order}"),
        (330, "1A"),
        (100, "AcDbPlotSettings"),
        (1, ""),
        (4, "ISO_A3_(420.00_x_297.00_MM)"),
        (40, 7.5),
        (41, 20),
        (42, 7.5),
        (43, 20),
        (44, 420),
        (45, 297),
        (72, 1),
        (73, 1),
        (100, "AcDbLayout"),
        (1, name),
        (70, 1),
        (71, tab_order),
        (330, block_record),
    ]


def test_paper_space_entities_are_layouts_not_model_space() -> None:
    diagnostics: list[ImportDiagnostic] = []
    document = dxf_to_ir(
        _dxf(
            [
                _line("A1", 0),
                _line("A2", 500, (67, 1)),
                # A polyline keeps its vertices, whatever they say themselves.
                [
                    (0, "POLYLINE"),
                    (5, "A3"),
                    (67, 1),
                    (8, "0"),
                    (70, 1),
                    (0, "VERTEX"),
                    (8, "0"),
                    (10, 0),
                    (20, 0),
                    (0, "VERTEX"),
                    (8, "0"),
                    (10, 420),
                    (20, 0),
                    (0, "VERTEX"),
                    (8, "0"),
                    (10, 420),
                    (20, 297),
                    (0, "SEQEND"),
                ],
                _line("A4", 2),
            ]
        ),
        diagnostics=diagnostics,
    )

    assert [entity["id"] for entity in document["entities"]] == ["EA1", "EA4"]
    (layout,) = document["layouts"]
    assert (layout["name"], layout["active"]) == ("Layout1", True)
    assert [entity["id"] for entity in layout["entities"]] == ["EA2", "EA3"]
    assert layout["entities"][1]["closed"] is True
    kept = [d for d in diagnostics if d.code == "DXF_PAPERSPACE_LAYOUT_PRESERVED"]
    assert len(kept) == 1 and "Kept 2 paper-space" in kept[0].message
    validate_ir(document, strict_jsonschema=True)


def test_group_67_behind_a_subclass_marker_is_not_the_paper_space_flag() -> None:
    document = dxf_to_ir(
        _dxf(
            [
                [
                    (0, "LINE"),
                    (5, "A1"),
                    (100, "AcDbEntity"),
                    (8, "0"),
                    (100, "AcDbLine"),
                    (10, 0),
                    (20, 0),
                    (11, 1),
                    (21, 0),
                    (67, 1),
                ]
            ]
        )
    )
    assert len(document["entities"]) == 1
    assert "layouts" not in document


def test_layout_objects_name_the_sheets_and_describe_their_paper() -> None:
    document = dxf_to_ir(
        _dxf(
            [_line("A1", 0), _line("A2", 500, (67, 1))],
            tables=[
                *_LAYER_TABLE,
                *_block_records(
                    ("1F", "*Model_Space"),
                    ("58", "*Paper_Space"),
                    ("5D", "*Paper_Space0"),
                ),
            ],
            blocks=[
                (0, "BLOCK"),
                (2, "*Paper_Space"),
                (10, 0),
                (20, 0),
                (0, "ENDBLK"),
                (0, "BLOCK"),
                (2, "*Paper_Space0"),
                (10, 0),
                (20, 0),
                *_line("B1", 10),
                (0, "ENDBLK"),
                (0, "BLOCK"),
                (2, "PART"),
                (10, 0),
                (20, 0),
                *_line("B2", 0),
                (0, "ENDBLK"),
            ],
            objects=[
                *_layout_object("Model", 0, "1F"),
                *_layout_object("Sheet B", 2, "58"),
                *_layout_object("Sheet A", 1, "5D"),
            ],
        )
    )

    # Tab order, not file order; the sheet blocks are no block definitions.
    assert [layout["name"] for layout in document["layouts"]] == ["Sheet A", "Sheet B"]
    sheet_a, sheet_b = document["layouts"]
    assert [entity["id"] for entity in sheet_a["entities"]] == ["EB1"]
    assert "active" not in sheet_a
    assert sheet_b["active"] is True
    assert sheet_b["tab_order"] == 2
    assert sheet_b["paper"] == {
        "name": "ISO_A3_(420.00_x_297.00_MM)",
        "size_mm": [420.0, 297.0],
        "margins_mm": [7.5, 20.0, 7.5, 20.0],
        "units": "mm",
        "rotation": 90,
    }
    assert set(document["tables"]["blocks"]) == {"PART"}
    validate_ir(document, strict_jsonschema=True)


def test_viewports_state_the_model_point_at_their_center() -> None:
    view = [
        (12, 30),
        (22, 10),
        (45, 600),
        (16, 0),
        (26, 0),
        (36, 1),
        (17, 0),
        (27, 0),
        (37, 0),
    ]
    document = dxf_to_ir(
        _dxf(
            [
                _line("A1", 0),
                # Viewport 1 stands for the sheet itself.
                _viewport("V1", 1, (12, 100), (22, 80), (45, 60)),
                _viewport("V2", 2, *view, (51, 0), (90, 0x8060), (331, "11")),
                _viewport("V3", 3, *view, (51, 90), (90, 0x8060 | 0x20000)),
                # A view from the side has no 2D mapping.
                _viewport(
                    "V4", 4, (12, 0), (22, 0), (45, 50), (16, 1), (26, 0), (36, 0)
                ),
            ],
            tables=_LAYER_TABLE,
        )
    )

    (layout,) = document["layouts"]
    assert layout["entities"] == []
    plain, twisted, side = layout["viewports"]
    assert plain == {
        "center": [100.0, 80.0],
        "width": 120.0,
        "height": 60.0,
        "id": "VV2",
        "source": {"format": "dxf", "kind": "VIEWPORT", "id": "V2"},
        "layer": "VP",
        "view_center": [30.0, 10.0],
        "view_height": 600.0,
        "frozen_layers": ["HIDDEN_IN_VIEW"],
        "metadata": {"dxf": {"status_flags": 0x8060}},
    }
    # The display frame of a twisted view is rotated: (30, 10) there is the
    # model point (10, -30).
    assert twisted["view_center"] == pytest.approx([10.0, -30.0])
    assert twisted["rotation"] == 90.0
    assert twisted["visible"] is False
    assert "view_center" not in side
    assert side["metadata"]["dxf"]["view"]["direction"] == [1.0, 0.0, 0.0]
    validate_ir(document, strict_jsonschema=True)


def test_viewports_of_a_sheet_that_was_not_current() -> None:
    # Such a sheet is the body of a *Paper_Space<n> block, and its viewports
    # carry status 0 and id 0. The first one is the sheet itself: its view is
    # its own window.
    sheet = [(12, 100), (22, 80), (45, 60), (90, 0x8060)]
    window = [(12, 30), (22, 10), (45, 600), (90, 0x8060)]
    document = dxf_to_ir(
        _dxf(
            [_line("A1", 0)],
            blocks=[
                (0, "BLOCK"),
                (2, "*Paper_Space0"),
                (10, 0),
                (20, 0),
                *_viewport("V1", 0, *sheet),
                *_viewport("V2", 0, *window),
                (0, "ENDBLK"),
                # A first viewport that shows model space is no sheet viewport.
                (0, "BLOCK"),
                (2, "*Paper_Space1"),
                (10, 0),
                (20, 0),
                *_viewport("V3", 0, *window),
                *_viewport("V4", 0, *window[:-1], (90, 0x8060 | 0x20000)),
                (0, "ENDBLK"),
            ],
        )
    )

    first, second = document["layouts"]
    assert [viewport["id"] for viewport in first["viewports"]] == ["VV2"]
    # Status 0 does not switch a viewport off; the status flags do.
    assert "visible" not in first["viewports"][0]
    assert [viewport["id"] for viewport in second["viewports"]] == ["VV3", "VV4"]
    assert [viewport.get("visible", True) for viewport in second["viewports"]] == [
        True,
        False,
    ]


def test_viewport_of_size_zero_is_left_out_without_an_error() -> None:
    diagnostics: list[ImportDiagnostic] = []
    warnings: list[str] = []
    document = dxf_to_ir(
        _dxf(
            [
                _line("A1", 0),
                _line("A2", 5, (67, 1)),
                _viewport("V1", 1),
                [
                    *_viewport("V2", 2)[:9],
                    (40, 0),
                    (41, 0),
                    (68, 0),
                    (69, 2),
                    (90, 0x28020),
                ],
            ]
        ),
        diagnostics=diagnostics,
        warnings=warnings,
    )

    (layout,) = document["layouts"]
    assert "viewports" not in layout
    assert not [d for d in diagnostics if d.severity == "error"]
    assert any("VIEWPORT without a window" in message for message in warnings)


def test_viewport_without_status_flags_is_off_at_status_zero() -> None:
    # R12 files have no status flags; there the status field tells.
    document = dxf_to_ir(
        _dxf(
            [
                _line("A1", 0),
                _viewport("V1", 1),
                [*_viewport("V2", 2)[:-2], (68, 0), (69, 2)],
            ]
        )
    )
    (viewport,) = document["layouts"][0]["viewports"]
    assert viewport["visible"] is False


def test_r12_viewport_keeps_its_view_in_extended_data() -> None:
    def mview(height: float, center_x: float, center_y: float, twist: float) -> Pairs:
        return [
            (1001, "ACAD"),
            (1000, "MVIEW"),
            (1002, "{"),
            (1070, 16),
            (1010, 0),
            (1020, 0),
            (1030, 0),
            (1010, 0),
            (1020, 0),
            (1030, 1),
            (1040, twist),
            (1040, height),
            (1040, center_x),
            (1040, center_y),
            (1040, 50),
            (1040, 0),
            (1040, 0),
            (1070, 0),
            (1070, 100),
            (1002, "{"),
            (1003, "HIDDEN_IN_VIEW"),
            (1002, "}"),
            (1002, "}"),
        ]

    def r12_viewport(handle: str, viewport_id: int, *xdata: tuple[int, Any]) -> Pairs:
        return [
            (0, "VIEWPORT"),
            (5, handle),
            (8, "VP"),
            (67, 1),
            (10, 100),
            (20, 80),
            (30, 0),
            (40, 120),
            (41, 60),
            (68, viewport_id),
            (69, viewport_id),
            *xdata,
        ]

    document = dxf_to_ir(
        _dxf(
            [
                _line("A1", 0),
                r12_viewport("V1", 1, *mview(60, 100, 80, 0)),
                # A quarter turn of twist, in radians.
                r12_viewport("V2", 2, *mview(600, 30, 10, 1.5707963267948966)),
            ]
        )
    )

    (viewport,) = document["layouts"][0]["viewports"]
    assert viewport["id"] == "VV2"
    assert viewport["view_height"] == 600.0
    assert viewport["view_center"] == pytest.approx([10.0, -30.0])
    assert viewport["rotation"] == pytest.approx(90.0)
    assert viewport["frozen_layers"] == ["HIDDEN_IN_VIEW"]


def test_viewport_view_round_trips_through_the_display_frame() -> None:
    viewport = viewport_from_dxf_values(
        source_format="dxf",
        handle="2A",
        center=[100.0, 80.0],
        width=120.0,
        height=60.0,
        view_center=[30.0, 10.0],
        view_height=600.0,
        twist_deg=30.0,
        target=[5.0, 7.0, 0.0],
    )
    scale = viewport["height"] / viewport["view_height"]
    assert scale == pytest.approx(0.1)
    back = viewport_to_dxf_view(viewport)
    assert back is not None
    # The target of the written view is the origin; the same model point stays
    # at the center of the window.
    import math

    angle = math.radians(30.0)
    x, y = viewport["view_center"]
    assert back["view_center"] == pytest.approx(
        [
            x * math.cos(angle) - y * math.sin(angle),
            x * math.sin(angle) + y * math.cos(angle),
        ]
    )
    assert (back["view_height"], back["twist_deg"]) == (600.0, 30.0)

    with pytest.raises(ValueError):
        viewport_from_dxf_values(
            source_format="dxf", handle=None, center=[0.0, 0.0], width=0.0, height=1.0
        )


def test_empty_model_space_makes_the_sheet_the_drawing() -> None:
    diagnostics: list[ImportDiagnostic] = []
    document = dxf_to_ir(
        _dxf(
            [
                _line("A1", 0, (67, 1)),
                _viewport("V1", 1),
                _viewport("V2", 2, (12, 0), (22, 0), (45, 50)),
            ],
            blocks=[
                (0, "BLOCK"),
                (2, "*Paper_Space0"),
                (10, 0),
                (20, 0),
                *_line("B1", 10),
                (0, "ENDBLK"),
            ],
        ),
        diagnostics=diagnostics,
    )

    assert [entity["id"] for entity in document["entities"]] == ["EA1"]
    assert [layout["name"] for layout in document["layouts"]] == ["Layout2"]
    assert document["header"]["metadata"] == {
        "dxf": {"promoted_layout": {"name": "Layout1"}}
    }
    promoted = [d for d in diagnostics if d.code == "DXF_LAYOUT_PROMOTED"]
    assert len(promoted) == 1 and promoted[0].details == {"layout": "Layout1"}
    validate_ir(document, strict_jsonschema=True)


def test_promotion_leaves_a_drawing_with_model_space_alone() -> None:
    document: dict[str, Any] = {
        "header": {},
        "entities": [{"id": "A", "kind": "POINT", "position": [0.0, 0.0]}],
        "layouts": [{"name": "S", "entities": [{"id": "B"}]}],
    }
    before = copy.deepcopy(document)
    assert promote_layout_when_model_is_empty(document, metadata_key="dxf") is None
    assert document == before


def _sheet_document() -> dict[str, Any]:
    return {
        "format": "cad2d-ir",
        "version": "0.2.0",
        "header": {"units": "mm", "angle_unit": "deg", "coord_space": "world"},
        "tables": {"layers": {"0": {}, "VP": {"plot": False}, "HIDDEN_IN_VIEW": {}}},
        "entities": [
            {
                "id": "M1",
                "kind": "LINE",
                "layer": "0",
                "p1": [0.0, 0.0],
                "p2": [5000.0, 0.0],
            }
        ],
        "layouts": [
            {
                "name": "Sheet A",
                "tab_order": 1,
                "entities": [
                    {
                        "id": "F1",
                        "kind": "LWPOLYLINE",
                        "layer": "0",
                        "vertices": [
                            [0.0, 0.0],
                            [420.0, 0.0],
                            [420.0, 297.0],
                            [0.0, 297.0],
                        ],
                        "closed": True,
                    },
                    {
                        "id": "T1",
                        "kind": "TEXT",
                        "layer": "0",
                        "insert": [300.0, 20.0],
                        "height": 5.0,
                        "text": "A-001",
                    },
                ],
                "viewports": [
                    {
                        "center": [200.0, 150.0],
                        "width": 300.0,
                        "height": 200.0,
                        "view_center": [2500.0, 0.0],
                        "view_height": 4000.0,
                        "rotation": 90.0,
                        "layer": "VP",
                        "frozen_layers": ["HIDDEN_IN_VIEW"],
                    }
                ],
            },
            {
                "name": "Sheet B",
                "tab_order": 2,
                "active": True,
                "entities": [
                    {
                        "id": "G1",
                        "kind": "LINE",
                        "layer": "0",
                        "p1": [0.0, 0.0],
                        "p2": [1.0, 1.0],
                    }
                ],
            },
        ],
    }


def test_export_writes_the_active_layout_and_reports_the_others() -> None:
    diagnostics: list[ExportDiagnostic] = []
    text = ir_to_dxf(_sheet_document(), diagnostics=diagnostics)
    back = dxf_to_ir(text)

    assert [entity["kind"] for entity in back["entities"]] == ["LINE"]
    (layout,) = back["layouts"]
    assert [entity["kind"] for entity in layout["entities"]] == ["LINE"]
    assert layout["entities"][0]["p2"] == [1.0, 1.0]
    omitted = [d for d in diagnostics if d.code == "DXF_LAYOUT_OMITTED"]
    assert len(omitted) == 1 and "'Sheet A'" in omitted[0].message
    assert omitted[0].action == "skipped"


def test_export_round_trips_a_layout_with_its_viewport() -> None:
    document = _sheet_document()
    del document["layouts"][1]
    diagnostics: list[ExportDiagnostic] = []
    entity_map: list[dict[str, Any]] = []
    text = ir_to_dxf(document, diagnostics=diagnostics, entity_map=entity_map)

    assert not [d for d in diagnostics if d.code == "DXF_LAYOUT_OMITTED"]
    assert {entry["scope"] for entry in entity_map} == {"modelspace", "layout:Sheet A"}
    back = dxf_to_ir(text)
    assert [entity["kind"] for entity in back["entities"]] == ["LINE"]
    (layout,) = back["layouts"]
    assert [entity["kind"] for entity in layout["entities"]] == ["LWPOLYLINE", "TEXT"]
    # The sheet's own viewport (id 1) is written and read back as no window.
    (viewport,) = layout["viewports"]
    assert viewport["center"] == [200.0, 150.0]
    assert (viewport["width"], viewport["height"]) == (300.0, 200.0)
    assert viewport["view_center"] == pytest.approx([2500.0, 0.0])
    assert viewport["view_height"] == 4000.0
    assert viewport["rotation"] == 90.0
    assert viewport["layer"] == "VP"
    assert viewport["frozen_layers"] == ["HIDDEN_IN_VIEW"]
    validate_ir(back, strict_jsonschema=True)


def test_r12_export_keeps_the_sheet_entities_without_viewports() -> None:
    document = _sheet_document()
    del document["layouts"][1]
    diagnostics: list[ExportDiagnostic] = []
    text = ir_to_dxf(document, target_version="AC1009", diagnostics=diagnostics)

    assert "VIEWPORT" not in text
    assert [d.code for d in diagnostics if "VIEWPORT" in d.code] == [
        "DXF_R12_VIEWPORT_OMITTED"
    ]
    back = dxf_to_ir(text)
    (layout,) = back["layouts"]
    # R12 has no LWPOLYLINE: the frame is a POLYLINE with its vertices.
    assert [entity["kind"] for entity in layout["entities"]] == ["LWPOLYLINE", "TEXT"]
    assert len(layout["entities"][0]["vertices"]) == 4
    assert len(back["entities"]) == 1


@pytest.mark.parametrize(
    ("layouts", "message"),
    [
        ({}, "layouts must be an array"),
        ([{"entities": []}], "layouts[0].name must be a non-empty string"),
        ([{"name": "A"}], "layouts[0].entities must be an array"),
        (
            [{"name": "A", "entities": []}, {"name": "A", "entities": []}],
            "layouts[1].name duplicates layouts[0].name: 'A'",
        ),
        ([{"name": "A", "entities": [], "sheet": 1}], "unknown properties: ['sheet']"),
        (
            [
                {
                    "name": "A",
                    "entities": [],
                    "viewports": [{"center": [0, 0], "width": 1}],
                }
            ],
            "layouts[0].viewports[0].height must be a number",
        ),
        (
            [
                {
                    "name": "A",
                    "entities": [],
                    "viewports": [
                        {"center": [0, 0], "width": 1, "height": 1, "view_height": 0}
                    ],
                }
            ],
            "layouts[0].viewports[0].view_height must be > 0",
        ),
        (
            [{"name": "A", "entities": [], "paper": {"rotation": 45}}],
            "layouts[0].paper.rotation must be 0, 90, 180 or 270",
        ),
    ],
)
def test_layout_validation(layouts: Any, message: str) -> None:
    document = _sheet_document()
    document["layouts"] = layouts
    with pytest.raises(IRValidationError) as error:
        validate_ir(document)
    assert message in str(error.value)


def test_entity_ids_are_unique_per_layout_only() -> None:
    document = _sheet_document()
    document["layouts"][1]["entities"][0]["id"] = "M1"
    validate_ir(document, strict_jsonschema=True)
    document["layouts"][0]["entities"][1]["id"] = "F1"
    with pytest.raises(IRValidationError):
        validate_ir(document)


# --- DWG


@dataclass
class _Entity:
    dxftype: str
    handle: int
    dxf: dict[str, Any]


class _Query:
    def __init__(self, entities: list[_Entity]) -> None:
        self._entities = entities

    def query(self) -> list[_Entity]:
        return self._entities


class _DwgDocument:
    version = "AC1032"

    def __init__(
        self,
        entities: list[_Entity],
        placements: dict[int, tuple[int, int | None]],
        layouts: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        self._entities = entities
        self._placements = placements
        self._layouts = layouts

    def entities(self) -> _Query:
        return _Query(self._entities)

    def entity_placement(self, handle: int) -> tuple[int, int | None] | None:
        return self._placements.get(handle)

    def layouts(self) -> dict[str, dict[str, Any]]:
        if self._layouts is None:
            raise RuntimeError("no layout objects")
        return self._layouts


def _style(layer_handle: int = 16) -> dict[str, Any]:
    return {"layer_handle": layer_handle, "resolved_color_index": 7}


def _dwg_line(handle: int, x: float) -> _Entity:
    return _Entity(
        "LINE", handle, {"start": (x, 0.0, 0.0), "end": (x + 1.0, 0.0, 0.0), **_style()}
    )


def _dwg_viewport(handle: int, **dxf: Any) -> _Entity:
    return _Entity(
        "VIEWPORT",
        handle,
        {
            "center": (100.0, 80.0, 0.0),
            "width": 120.0,
            "height": 60.0,
            "view_center": (30.0, 10.0),
            "view_height": 600.0,
            "view_target": (0.0, 0.0, 0.0),
            "view_direction": (0.0, 0.0, 1.0),
            "view_twist_angle": 0.0,
            "status_flags": 0x8060,
            "frozen_layers": [],
            **_style(17),
            **dxf,
        },
    )


def _dwg_sheet_viewport(handle: int) -> _Entity:
    """The viewport that stands for the sheet: its view is its own window."""
    return _dwg_viewport(handle, view_center=(100.0, 80.0), view_height=60.0)


_DWG_LAYOUTS = {
    "Model": {
        "handle": 0x22,
        "tab_order": 0,
        "model": True,
        "block_record_handle": 0x1F,
    },
    "Sheet A": {
        "handle": 0x59,
        "tab_order": 1,
        "model": False,
        "active": False,
        "block_record_handle": 0x58,
        "paper_width": 420.0,
        "paper_height": 297.0,
        "margins": (7.5, 20.0, 7.5, 20.0),
        "paper_size": "ISO_A3",
        "paper_units": 1,
        "plot_rotation": 0,
        "viewport_handles": [40, 41],
    },
    "Sheet B": {
        "handle": 0x5E,
        "tab_order": 2,
        "model": False,
        "active": True,
        "block_record_handle": 0x5D,
        "paper_width": 210.0,
        "paper_height": 297.0,
        "margins": (0.0, 0.0, 0.0, 0.0),
        "paper_size": "",
        "paper_units": 0,
        "plot_rotation": 1,
        "viewport_handles": [],
    },
}


def test_dwg_sheets_are_named_by_their_layout_objects() -> None:
    document = _DwgDocument(
        [
            _dwg_line(1, 0.0),
            # Sheet A names its block record; Sheet B was current at save time.
            _dwg_line(2, 10.0),
            _dwg_viewport(40),
            _dwg_viewport(41, frozen_layer_handles=[18], clip_boundary_handle=0x77),
            _dwg_line(3, 20.0),
            _dwg_sheet_viewport(50),
        ],
        placements={
            1: (2, None),
            2: (0, 0x58),
            40: (0, 0x58),
            41: (0, 0x58),
            3: (1, None),
            50: (1, None),
        },
        layouts=_DWG_LAYOUTS,
    )
    result = dwg_document_to_ir(
        document,
        layer_names_by_handle={16: "0", 17: "VP", 18: "HIDDEN_IN_VIEW"},
        block_names_by_handle={
            0x1F: "*Model_Space",
            0x58: "*Paper_Space",
            0x5D: "*Paper_Space",
        },
    )
    ir = result.document

    assert [entity["kind"] for entity in ir["entities"]] == ["LINE"]
    sheet_a, sheet_b = ir["layouts"]
    assert (sheet_a["name"], sheet_a["tab_order"]) == ("Sheet A", 1)
    assert "active" not in sheet_a
    assert sheet_a["paper"] == {
        "name": "ISO_A3",
        "size_mm": [420.0, 297.0],
        "margins_mm": [7.5, 20.0, 7.5, 20.0],
        "units": "mm",
        "rotation": 0,
    }
    assert sheet_a["metadata"] == {
        "dwg": {"block_record_handle": "0x58", "layout_handle": "0x59"}
    }
    # The first viewport of the list is the sheet itself.
    (viewport,) = sheet_a["viewports"]
    assert viewport["id"] == "V0x29"
    assert viewport["layer"] == "VP"
    assert viewport["view_center"] == [30.0, 10.0]
    assert viewport["frozen_layers"] == ["HIDDEN_IN_VIEW"]
    assert "clip_boundary" not in viewport["metadata"]["dwg"]

    assert (sheet_b["name"], sheet_b["active"]) == ("Sheet B", True)
    assert [entity["p1"] for entity in sheet_b["entities"]] == [[20.0, 0.0]]
    # Its only viewport is the sheet itself.
    assert "viewports" not in sheet_b
    assert sheet_b["paper"]["units"] == "inch" and sheet_b["paper"]["rotation"] == 90

    ids = [
        entity["id"]
        for scope in (ir["entities"], sheet_a["entities"], sheet_b["entities"])
        for entity in scope
    ]
    assert len(ids) == len(set(ids)) == 3
    assert result.statistics["layouts"] == 2
    assert result.statistics["converted_layout_entities"] == 2
    validate_ir(ir, strict_jsonschema=True)


def test_dwg_viewports_without_geometry_are_reported_as_unsupported() -> None:
    # ezdwg before 0.12.12 decodes no viewport body.
    document = _DwgDocument(
        [
            _dwg_line(1, 0.0),
            _dwg_line(2, 10.0),
            _Entity("VIEWPORT", 40, _style()),
            _Entity("VIEWPORT", 41, _style()),
        ],
        placements={1: (2, None), 2: (1, None), 40: (1, None), 41: (1, None)},
    )
    result = dwg_document_to_ir(document, layer_names_by_handle={16: "0"})

    (layout,) = result.document["layouts"]
    assert "viewports" not in layout
    assert result.statistics["skipped_entity_counts"] == {"VIEWPORT": 1}


def test_dwg_viewport_without_a_window_does_not_stop_a_strict_conversion() -> None:
    document = _DwgDocument(
        [
            _dwg_line(1, 0.0),
            _dwg_line(2, 10.0),
            _dwg_sheet_viewport(40),
            _dwg_viewport(41, width=0.0),
            _dwg_viewport(42),
        ],
        placements={
            1: (2, None),
            2: (1, None),
            40: (1, None),
            41: (1, None),
            42: (1, None),
        },
    )
    result = dwg_document_to_ir(document, layer_names_by_handle={16: "0", 17: "VP"})

    (layout,) = result.document["layouts"]
    assert [viewport["id"] for viewport in layout["viewports"]] == ["V0x2A"]
    assert result.statistics["skipped_entity_counts"] == {"VIEWPORT": 1}
    assert not [d for d in result.diagnostics if d.severity == "error"]


def test_dwg_first_viewport_is_kept_when_it_shows_model_space() -> None:
    # Without a viewport list (files before R2004), the viewport with the
    # lowest handle is taken for the sheet itself only when its view is its
    # own window.
    document = _DwgDocument(
        [_dwg_line(1, 0.0), _dwg_viewport(40), _dwg_viewport(41)],
        placements={1: (2, None), 40: (1, None), 41: (1, None)},
    )
    result = dwg_document_to_ir(document, layer_names_by_handle={16: "0", 17: "VP"})

    (layout,) = result.document["layouts"]
    assert [viewport["id"] for viewport in layout["viewports"]] == ["V0x28", "V0x29"]


def test_dwg_empty_model_space_makes_the_sheet_the_drawing() -> None:
    document = _DwgDocument(
        [_dwg_line(1, 0.0), _dwg_line(2, 5.0), _dwg_viewport(40)],
        placements={1: (1, None), 2: (1, None), 40: (1, None)},
    )
    result = dwg_document_to_ir(document, layer_names_by_handle={16: "0"})
    ir = result.document

    assert [entity["p1"] for entity in ir["entities"]] == [[0.0, 0.0], [5.0, 0.0]]
    assert "layouts" not in ir
    assert ir["header"]["metadata"]["dwg"]["promoted_layout"] == {"name": "Layout1"}
    assert result.statistics["converted_entities"] == 2
    assert result.statistics["converted_layout_entities"] == 0
    codes = [diagnostic.code for diagnostic in result.diagnostics]
    assert "DWG_LAYOUT_PROMOTED" in codes
    validate_ir(ir, strict_jsonschema=True)


def test_dwg_file_tells_the_sheets_of_other_layouts_from_model_space(
    monkeypatch, tmp_path
) -> None:
    import sys
    from types import SimpleNamespace

    from cad2d_ir.importers.dwg import convert_dwg_file_to_ir

    raw = SimpleNamespace(
        decode_layer_names=lambda _path: [(16, "0")],
        decode_layer_colors=lambda _path: [(16, 7, None)],
        # A DWG names every paper-space block record "*Paper_Space".
        decode_block_header_names=lambda _path: [
            (0x1F, "*Model_Space"),
            (0x58, "*Paper_Space"),
            (0x5D, "*Paper_Space"),
            (0x64, "SYMBOL"),
        ],
    )
    document = _DwgDocument(
        [
            _dwg_line(1, 0.0),
            # Model space by owner: the model-space block record.
            _dwg_line(2, 1.0),
            # Sheets that were not current at save time name their block record.
            _dwg_line(3, 2.0),
            _dwg_line(4, 3.0),
            # The current sheet stores no owner.
            _dwg_line(5, 4.0),
            _dwg_line(6, 5.0),
        ],
        placements={
            1: (2, None),
            2: (0, 0x1F),
            3: (0, 0x58),
            4: (0, 0x5D),
            5: (1, None),
            6: (0, 0x64),
        },
    )
    monkeypatch.setitem(
        sys.modules, "ezdwg", SimpleNamespace(read=lambda _path: document, raw=raw)
    )
    source = tmp_path / "fixture.dwg"
    source.write_bytes(b"fixture")

    result = convert_dwg_file_to_ir(source)
    ir = result.document

    assert [entity["p1"] for entity in ir["entities"]] == [[0.0, 0.0], [1.0, 0.0]]
    assert [
        (layout["name"], layout.get("active", False), layout["entities"][0]["p1"])
        for layout in ir["layouts"]
    ] == [
        ("Layout1", True, [4.0, 0.0]),
        ("Layout2", False, [2.0, 0.0]),
        ("Layout3", False, [3.0, 0.0]),
    ]
    assert list(ir["tables"]["blocks"]) == ["SYMBOL"]
    assert result.statistics["source_block_definitions"] == 1
    validate_ir(ir, strict_jsonschema=True)
