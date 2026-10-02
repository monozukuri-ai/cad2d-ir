from __future__ import annotations

from dataclasses import dataclass
from typing import Any
import weakref

import pytest

from cad2d_ir.importers import ImportOptions
from cad2d_ir.importers.dwg import convert_dwg_file_to_ir, dwg_document_to_ir
from cad2d_ir.schema import validate_ir


@dataclass(frozen=True)
class _Entity:
    dxftype: str
    handle: int
    dxf: dict[str, Any]


class _Layout:
    def __init__(self, entities: list[_Entity]) -> None:
        self.entities = entities

    def query(self) -> list[_Entity]:
        return self.entities


class _Document:
    version = "AC1027"

    def __init__(self, entities: list[_Entity]) -> None:
        self.entities = entities

    def modelspace(self) -> _Layout:
        return _Layout(self.entities)


def _style(*, layer_handle: int = 16, owner_handle: int | None = None) -> dict:
    return {
        "layer_handle": layer_handle,
        "owner_handle": owner_handle,
        "resolved_color_index": 7,
        "resolved_true_color": None,
    }


def _document() -> _Document:
    return _Document(
        [
            _Entity(
                "LINE",
                1,
                {"start": (0.0, 0.0, 0.0), "end": (10.0, 0.0, 0.0), **_style()},
            ),
            _Entity(
                "CIRCLE",
                2,
                {"center": (2.0, 2.0, 0.0), "radius": 1.0, **_style()},
            ),
            _Entity(
                "ELLIPSE",
                3,
                {
                    "center": (5.0, 5.0, 0.0),
                    "major_axis": (3.0, 0.0, 0.0),
                    "axis_ratio": 0.5,
                    "start_angle": 0.0,
                    "end_angle": 6.283185307179586,
                    "extrusion": (0.0, 0.0, 1.0),
                    **_style(),
                },
            ),
            _Entity(
                "LWPOLYLINE",
                4,
                {
                    "points": [(0.0, 0.0, 0.0), (1.0, 1.0, 0.0), (2.0, 0.0, 0.0)],
                    "bulges": [0.0, 0.25, 0.0],
                    "widths": [(0.0, 0.0)] * 3,
                    "flags": 0,
                    "closed": False,
                    **_style(),
                },
            ),
            _Entity(
                "POINT",
                5,
                {"location": (1.0, 2.0, 3.0), "x_axis_angle": 0.0, **_style()},
            ),
            _Entity(
                "TEXT",
                6,
                {
                    "insert": (1.0, 1.0, 0.0),
                    "height": 2.5,
                    "text": "note",
                    "rotation": 15.0,
                    "width": 1.0,
                    "halign": 0,
                    "valign": 0,
                    **_style(),
                },
            ),
            _Entity(
                "SPLINE",
                7,
                {
                    "degree": 3,
                    "control_points": [
                        (0.0, 0.0, 0.0),
                        (1.0, 2.0, 0.0),
                        (2.0, 2.0, 0.0),
                        (3.0, 0.0, 0.0),
                    ],
                    "knots": [0.0, 0.0, 0.0, 0.0, 1.0, 1.0, 1.0, 1.0],
                    "weights": [],
                    "closed": False,
                    **_style(),
                },
            ),
            _Entity(
                "HATCH",
                8,
                {
                    "pattern_name": "SOLID",
                    "solid_fill": True,
                    "associative": False,
                    "paths": [
                        {
                            "closed": True,
                            "points": [
                                (0.0, 0.0, 0.0),
                                (2.0, 0.0, 0.0),
                                (2.0, 2.0, 0.0),
                                (0.0, 2.0, 0.0),
                                (0.0, 0.0, 0.0),
                            ],
                        }
                    ],
                    **_style(),
                },
            ),
            _Entity(
                "INSERT",
                9,
                {
                    "insert": (20.0, 30.0, 0.0),
                    "xscale": -1.0,
                    "yscale": 2.0,
                    "rotation": 90.0,
                    "name": "SYMBOL",
                    **_style(),
                },
            ),
            _Entity(
                "DIMENSION",
                10,
                {
                    "dimtype": "ALIGNED",
                    "defpoint": (0.0, 0.0, 0.0),
                    "defpoint2": (0.0, 0.0, 0.0),
                    "defpoint3": (10.0, 0.0, 0.0),
                    "text_midpoint": (5.0, 1.0, 0.0),
                    "text": "10",
                    "actual_measurement": 10.0,
                    **_style(),
                },
            ),
            _Entity("VIEWPORT", 11, _style()),
            _Entity(
                "LINE",
                12,
                {
                    "start": (0.0, 0.0, 0.0),
                    "end": (5.0, 0.0, 0.0),
                    **_style(owner_handle=100),
                },
            ),
        ]
    )


def test_dwg_document_to_ir_preserves_native_entities_and_blocks() -> None:
    result = dwg_document_to_ir(
        _document(),
        source_name="fixture.dwg",
        source_sha256="a" * 64,
        layer_names_by_handle={16: "Geometry"},
        layer_colors_by_handle={16: (7, None)},
        block_names_by_handle={100: "SYMBOL"},
    )
    document = result.document

    assert document["source"] == {
        "format": "dwg",
        "version": "AC1027",
        "name": "fixture.dwg",
        "sha256": "a" * 64,
    }
    assert document["header"]["units"] == "unknown"
    assert set(document["tables"]["layers"]) == {"0", "Geometry"}
    assert len(document["tables"]["blocks"]["SYMBOL"]["entities"]) == 1

    kinds = [entity["kind"] for entity in document["entities"]]
    assert kinds == [
        "LINE",
        "CIRCLE",
        "ELLIPSE",
        "LWPOLYLINE",
        "POINT",
        "TEXT",
        "SPLINE",
        "HATCH",
        "INSERT",
        "DIMENSION",
    ]
    assert document["entities"][3]["vertices"][1] == [1.0, 1.0, 0.25]
    assert document["entities"][8]["scale"] == [-1.0, 2.0]
    assert document["entities"][9]["dim_kind"] == "ALIGNED"
    assert result.statistics["preserved_dimensions"] == 1
    assert result.statistics["projected_entities"] == 1
    assert result.statistics["skipped_entity_counts"] == {"VIEWPORT": 1}
    assert {diagnostic.code for diagnostic in result.diagnostics} == {
        "DWG_NONPLANAR_PROJECTED",
        "DWG_UNSUPPORTED_ENTITY",
    }
    validate_ir(document, strict_jsonschema=True)


def test_dwg_document_to_ir_leniently_skips_malformed_entities() -> None:
    document = _Document(
        [_Entity("CIRCLE", 1, {"center": (0.0, 0.0, 0.0), "radius": 0.0})]
    )

    result = dwg_document_to_ir(
        document,
        options=ImportOptions(strict=False),
    )

    assert result.document["entities"] == []
    assert result.statistics["skipped_entity_counts"] == {"CIRCLE": 1}
    assert result.diagnostics[0].code == "DWG_ENTITY_CONVERSION_FAILED"

    with pytest.raises(ValueError, match="DWG CIRCLE"):
        dwg_document_to_ir(document)


class _DocumentWithHeader(_Document):
    def __init__(
        self, entities: list[_Entity], header_variables: dict[str, Any] | Exception
    ) -> None:
        super().__init__(entities)
        self._header_variables = header_variables

    def header_variables(self) -> dict[str, Any]:
        if isinstance(self._header_variables, Exception):
            raise self._header_variables
        return self._header_variables


def _line_entities() -> list[_Entity]:
    return [
        _Entity(
            "LINE",
            1,
            {"start": (0.0, 0.0, 0.0), "end": (10.0, 0.0, 0.0), **_style()},
        )
    ]


def test_dwg_units_resolved_from_insunits() -> None:
    result = dwg_document_to_ir(
        _DocumentWithHeader(_line_entities(), {"insunits": 4})
    )

    header = result.document["header"]
    assert header["units"] == "mm"
    assert header["metadata"]["dwg"]["insunits"] == 4
    assert "units_status" not in header["metadata"]["dwg"]
    assert not [d for d in result.diagnostics if "UNITS" in d.code]
    validate_ir(result.document, strict_jsonschema=True)


def test_dwg_units_imperial_code_maps_to_inch() -> None:
    result = dwg_document_to_ir(
        _DocumentWithHeader(_line_entities(), {"insunits": 1})
    )

    assert result.document["header"]["units"] == "inch"


def test_dwg_units_r14_without_insunits_stays_unknown() -> None:
    result = dwg_document_to_ir(
        _DocumentWithHeader(_line_entities(), {"insunits": None})
    )

    header = result.document["header"]
    assert header["units"] == "unknown"
    assert header["metadata"]["dwg"]["units_status"] == "INSUNITS not present (R14)"


def test_dwg_units_unsupported_code_falls_back_with_diagnostic() -> None:
    result = dwg_document_to_ir(
        _DocumentWithHeader(_line_entities(), {"insunits": 3})
    )

    header = result.document["header"]
    assert header["units"] == "unknown"
    assert header["metadata"]["dwg"]["insunits"] == 3
    assert header["metadata"]["dwg"]["units_status"] == "unsupported INSUNITS code"
    codes = [diagnostic.code for diagnostic in result.diagnostics]
    assert "DWG_UNSUPPORTED_INSUNITS" in codes


def test_dwg_ltscale_becomes_header_linetype_scale() -> None:
    result = dwg_document_to_ir(
        _DocumentWithHeader(_line_entities(), {"insunits": 4, "ltscale": 100.0})
    )

    assert result.document["header"]["linetype_scale"] == 100.0
    validate_ir(result.document, strict_jsonschema=True)


def test_dwg_default_or_unusable_ltscale_is_omitted() -> None:
    for value in (1.0, 0.0, -5.0, None, "100"):
        result = dwg_document_to_ir(
            _DocumentWithHeader(_line_entities(), {"insunits": 4, "ltscale": value})
        )
        assert "linetype_scale" not in result.document["header"]

    absent = dwg_document_to_ir(_DocumentWithHeader(_line_entities(), {"insunits": 4}))
    assert "linetype_scale" not in absent.document["header"]


def test_dwg_units_absent_api_keeps_previous_behavior() -> None:
    result = dwg_document_to_ir(_document())

    header = result.document["header"]
    assert header["units"] == "unknown"
    assert header["metadata"]["dwg"]["units_status"] == "not exposed by ezdwg"


def test_dwg_units_header_decode_failure_is_lenient() -> None:
    result = dwg_document_to_ir(
        _DocumentWithHeader(_line_entities(), ValueError("corrupt header"))
    )

    header = result.document["header"]
    assert header["units"] == "unknown"
    assert header["metadata"]["dwg"]["units_status"] == "header variables unreadable"
    codes = [diagnostic.code for diagnostic in result.diagnostics]
    assert "DWG_HEADER_UNITS_UNREADABLE" in codes
    assert result.document["entities"], "import itself must still succeed"


class _PlacementDocument:
    """ezdwg >= 0.12.1 shape: entities() spans everything, entity_placement() tells where."""

    version = "AC1032"

    def __init__(self, entities: list[_Entity], placements: dict[int, tuple[int, int | None]]):
        self._entities = entities
        self._placements = placements

    def modelspace(self) -> _Layout:  # filtered view; the adapter must not rely on it
        return _Layout([e for e in self._entities if self._placements.get(e.handle, (2, None))[0] == 2])

    def entities(self) -> _Layout:
        return _Layout(self._entities)

    def entity_placement(self, handle: int) -> tuple[int, int | None] | None:
        return self._placements.get(handle)


def test_dwg_document_to_ir_uses_entities_and_skips_paper_space() -> None:
    document = _PlacementDocument(
        [
            _Entity("LINE", 1, {"start": (0.0, 0.0, 0.0), "end": (1.0, 0.0, 0.0), **_style()}),
            # block-definition content, only reachable through entities()
            _Entity(
                "LINE",
                2,
                {"start": (0.0, 0.0, 0.0), "end": (0.0, 1.0, 0.0), **_style(owner_handle=100)},
            ),
            _Entity("INSERT", 3, {"insert": (5.0, 5.0, 0.0), "name": "SYMBOL", **_style()}),
            # paper-space title-block line: entmode 1
            _Entity("LINE", 4, {"start": (0.0, 0.0, 0.0), "end": (9.0, 0.0, 0.0), **_style()}),
        ],
        placements={1: (2, None), 2: (0, 100), 3: (2, None), 4: (1, None)},
    )
    result = dwg_document_to_ir(document, block_names_by_handle={100: "SYMBOL", 31: "*Model_Space"})
    kinds = [entity["kind"] for entity in result.document["entities"]]
    assert kinds == ["LINE", "INSERT"]
    assert len(result.document["tables"]["blocks"]["SYMBOL"]["entities"]) == 1
    codes = [diagnostic.code for diagnostic in result.diagnostics]
    assert "DWG_PAPERSPACE_ENTITY_SKIPPED" in codes


class _StreamingLayout:
    def query(self):
        alive: list[weakref.ReferenceType[_Entity]] = []
        for handle in range(1, 6):
            alive = [reference for reference in alive if reference() is not None]
            if len(alive) > 1:
                raise RuntimeError("source entities were buffered")
            entity = _Entity(
                "LINE",
                handle,
                {
                    "start": (float(handle), 0.0, 0.0),
                    "end": (float(handle), 1.0, 0.0),
                    **_style(),
                },
            )
            alive.append(weakref.ref(entity))
            yield entity


class _StreamingDocument:
    version = "AC1027"

    def entities(self) -> _StreamingLayout:
        return _StreamingLayout()


def test_dwg_document_to_ir_consumes_source_entities_as_a_stream() -> None:
    result = dwg_document_to_ir(_StreamingDocument())

    assert len(result.document["entities"]) == 5
    assert result.statistics["source_entities"] == 5
    assert result.statistics["source_entity_counts"] == {"LINE": 5}


def test_convert_dwg_file_releases_ezdwg_decode_caches(monkeypatch, tmp_path) -> None:
    import sys
    from types import SimpleNamespace

    cleared = 0

    def clear_decode_caches() -> None:
        nonlocal cleared
        cleared += 1

    fake_ezdwg = SimpleNamespace(
        read=lambda _path: _Document(_line_entities()),
        raw=None,
        clear_decode_caches=clear_decode_caches,
    )
    monkeypatch.setitem(sys.modules, "ezdwg", fake_ezdwg)
    source = tmp_path / "fixture.dwg"
    source.write_bytes(b"fixture")

    result = convert_dwg_file_to_ir(source)

    assert result.document["entities"]
    assert cleared == 1


def _line_entity(handle: int, **dxf: Any) -> _Entity:
    return _Entity(
        "LINE",
        handle,
        {"start": (0.0, float(handle), 0.0), "end": (10.0, float(handle), 0.0), **dxf},
    )


_LINETYPES = {
    0x14: ("ByBlock", "", []),
    0x15: ("ByLayer", "", []),
    0x16: ("Continuous", "Solid line", []),
    0x30: ("CENTER", "Center ____ _ ____", [31.75, -6.35, 6.35, -6.35]),
    0x31: ("HIDDEN", "", [6.35, -3.175]),
}


def test_dwg_linetypes_reach_the_tables_layers_and_entities() -> None:
    document = _Document(
        [
            _line_entity(1, **_style(layer_handle=16)),
            _line_entity(
                2, linetype="BYLAYER", linetype_scale=1.0, **_style(layer_handle=32)
            ),
            _line_entity(
                3, linetype="HIDDEN", linetype_scale=0.5, **_style(layer_handle=16)
            ),
            _line_entity(4, linetype="CONTINUOUS", **_style(layer_handle=32)),
            _line_entity(5, linetype="BYBLOCK", **_style(layer_handle=16)),
            _line_entity(
                6, linetype=None, linetype_scale=1.0, **_style(layer_handle=16)
            ),
        ]
    )
    result = dwg_document_to_ir(
        document,
        layer_names_by_handle={16: "0", 32: "Center"},
        linetypes_by_handle=_LINETYPES,
        layer_linetypes_by_handle={16: 0x16, 32: 0x30},
    )
    ir = result.document

    # The table holds the named linetypes with the DWG dash lengths (drawing units).
    linetypes = ir["tables"]["linetypes"]
    assert list(linetypes) == ["BYLAYER", "CONTINUOUS", "CENTER", "HIDDEN"]
    assert linetypes["CENTER"] == {
        "pattern_mm": [31.75, -6.35, 6.35, -6.35],
        "description": "Center ____ _ ____",
    }
    assert linetypes["HIDDEN"] == {"pattern_mm": [6.35, -3.175]}
    assert linetypes["CONTINUOUS"]["pattern_mm"] == []

    layers = ir["tables"]["layers"]
    assert layers["0"]["linetype"] == "CONTINUOUS"
    assert layers["Center"]["linetype"] == "CENTER"

    names = [entity["linetype"] for entity in ir["entities"]]
    assert names == ["BYLAYER", "BYLAYER", "HIDDEN", "CONTINUOUS", "BYBLOCK", "BYLAYER"]
    scales = [entity.get("linetype_scale") for entity in ir["entities"]]
    assert scales == [None, None, 0.5, None, None, None]
    validate_ir(ir, strict_jsonschema=True)


def test_dwg_entity_linetype_missing_from_the_table_stays_a_valid_reference() -> None:
    document = _Document([_line_entity(1, linetype="DASHDOT2", **_style())])
    ir = dwg_document_to_ir(document, layer_names_by_handle={16: "0"}).document

    assert ir["entities"][0]["linetype"] == "DASHDOT2"
    assert ir["tables"]["linetypes"]["DASHDOT2"] == {"pattern_mm": []}
    validate_ir(ir, strict_jsonschema=True)


def test_dwg_unusable_linetype_scale_is_dropped() -> None:
    document = _Document(
        [
            _line_entity(1, linetype="BYLAYER", linetype_scale=0.0, **_style()),
            _line_entity(
                2, linetype="BYLAYER", linetype_scale=float("nan"), **_style()
            ),
            _line_entity(3, linetype="BYLAYER", linetype_scale="2", **_style()),
            _line_entity(4, linetype="BYLAYER", linetype_scale=4, **_style()),
        ]
    )
    ir = dwg_document_to_ir(document, layer_names_by_handle={16: "0"}).document

    scales = [entity.get("linetype_scale") for entity in ir["entities"]]
    assert scales == [None, None, None, 4.0]


def _hatch_entity(handle: int, **dxf: Any) -> _Entity:
    square = [(0.0, 0.0, 0.0), (10.0, 0.0, 0.0), (10.0, 10.0, 0.0), (0.0, 10.0, 0.0)]
    return _Entity(
        "HATCH",
        handle,
        {
            "pattern_name": "ANSI31",
            "solid_fill": False,
            "associative": False,
            "paths": [{"closed": True, "points": square}],
            **_style(),
            **dxf,
        },
    )


def test_dwg_hatch_pattern_definition_reaches_the_ir() -> None:
    lines = [
        {"angle": 45.0, "base": (0.0, 0.0), "offset": (-2.2451, 2.2451), "dashes": []},
        {"angle": 0.0, "base": (1.0, 2.0), "offset": (3.0, 4.0), "dashes": [6.0, -3.0]},
        # A family whose offset runs along its own lines has no spacing.
        {"angle": 0.0, "base": (0.0, 0.0), "offset": (5.0, 0.0), "dashes": []},
        {"angle": float("nan"), "base": (0.0, 0.0), "offset": (0.0, 1.0), "dashes": []},
    ]
    document = _Document(
        [
            _hatch_entity(
                1, pattern_angle=30.0, pattern_scale=25.4, pattern_lines=lines
            ),
            _hatch_entity(2),
            _hatch_entity(3, solid_fill=True, pattern_lines=lines[:1]),
        ]
    )
    ir = dwg_document_to_ir(document, layer_names_by_handle={16: "0"}).document
    patterned, bare, solid = ir["entities"]

    assert patterned["solid"] is False
    assert patterned["pattern_angle"] == 30.0
    assert patterned["pattern_scale"] == 25.4
    assert patterned["pattern_lines"] == [
        {"angle": 45.0, "base": [0.0, 0.0], "offset": [-2.2451, 2.2451]},
        {"angle": 0.0, "base": [1.0, 2.0], "offset": [3.0, 4.0], "dashes": [6.0, -3.0]},
    ]
    # A pattern fill without a readable definition keeps its name only.
    assert "pattern_lines" not in bare and bare["solid"] is False
    assert "pattern_lines" not in solid and solid["solid"] is True
    validate_ir(ir, strict_jsonschema=True)


def _dimension_entity(handle: int, block_handle: int | None, **dxf: Any) -> _Entity:
    return _Entity(
        "DIMENSION",
        handle,
        {
            "dimtype": "LINEAR",
            "defpoint": (0.0, 5.0, 0.0),
            "defpoint2": (0.0, 0.0, 0.0),
            "defpoint3": (10.0, 0.0, 0.0),
            "text_midpoint": (5.0, 5.0, 0.0),
            "anonymous_block_handle": block_handle,
            **_style(),
            **dxf,
        },
    )


def _block_line(handle: int, owner_handle: int, length: float) -> _Entity:
    return _Entity(
        "LINE",
        handle,
        {
            "start": (0.0, 0.0, 0.0),
            "end": (length, 0.0, 0.0),
            **_style(owner_handle=owner_handle),
        },
    )


def test_dwg_dimension_names_the_block_with_its_saved_graphics() -> None:
    document = _Document(
        [
            _dimension_entity(1, 200, anonymous_block_name="*D7"),
            # No block, an unknown handle, and a block without entities.
            _dimension_entity(2, None),
            _dimension_entity(3, 999),
            _dimension_entity(4, 201, anonymous_block_name="*D8"),
            _block_line(10, 200, 10.0),
        ]
    )
    ir = dwg_document_to_ir(
        document,
        layer_names_by_handle={16: "0"},
        block_names_by_handle={200: "*D7", 201: "*D8"},
    ).document

    assert list(ir["tables"]["blocks"]) == ["*D7"]
    first, *others = ir["entities"]
    assert first["definition"]["block"] == "*D7"
    assert all("block" not in entity["definition"] for entity in others)
    validate_ir(ir, strict_jsonschema=True)


def test_dwg_blocks_sharing_a_name_stay_separate() -> None:
    document = _Document(
        [
            _dimension_entity(1, 200, anonymous_block_name="*D3"),
            _dimension_entity(2, 201, anonymous_block_name="*D3"),
            _Entity(
                "INSERT",
                3,
                {"insert": (0.0, 0.0, 0.0), "name": "SYMBOL", **_style()},
            ),
            _block_line(10, 200, 10.0),
            _block_line(11, 201, 20.0),
            _block_line(12, 202, 30.0),
            _block_line(13, 300, 40.0),
            _block_line(14, 301, 50.0),
        ]
    )
    result = dwg_document_to_ir(
        document,
        layer_names_by_handle={16: "0"},
        block_names_by_handle={
            200: "*D3",
            201: "*D3",
            202: "*D1",
            300: "SYMBOL",
            301: "SYMBOL",
        },
    )
    ir = result.document
    blocks = ir["tables"]["blocks"]

    def length(name: str) -> float:
        (line,) = blocks[name]["entities"]
        return line["p2"][0]

    # The last header keeps the shared name, as a name reference resolved before;
    # the other one gets the next free anonymous number ("*D1" is taken).
    assert set(blocks) == {"*D1", "*D2", "*D3", "SYMBOL", "SYMBOL_12C"}
    assert (length("*D2"), length("*D3")) == (10.0, 20.0)
    assert (length("SYMBOL_12C"), length("SYMBOL")) == (40.0, 50.0)
    assert blocks["*D2"]["metadata"]["dwg"] == {
        "block_header_handle": "0xC8",
        "base_point_status": "not exposed by ezdwg",
        "source_name": "*D3",
    }
    assert "source_name" not in blocks["*D3"]["metadata"]["dwg"]

    # Each dimension reaches its own block through the handle.
    first, second, insert = ir["entities"]
    assert first["definition"]["block"] == "*D2"
    assert second["definition"]["block"] == "*D3"
    assert insert["block"] == "SYMBOL"
    renamed = [
        d for d in result.diagnostics if d.code == "DWG_DUPLICATE_BLOCK_NAME_RENAMED"
    ]
    assert len(renamed) == 1 and "2 DWG blocks" in renamed[0].message
    validate_ir(ir, strict_jsonschema=True)


def test_dwg_stored_placement_decides_the_owner_block() -> None:
    line = {"start": (0.0, 0.0, 0.0), "end": (1.0, 0.0, 0.0)}
    document = _PlacementDocument(
        [
            # Block content whose decoder reports no owner handle.
            _hatch_entity(1),
            # A model-space insert whose decoder names another object as owner.
            _Entity(
                "INSERT",
                2,
                {
                    "insert": (5.0, 5.0, 0.0),
                    "name": "SYMBOL",
                    **_style(owner_handle=100),
                },
            ),
            # The stored owner wins over the reported one.
            _Entity("LINE", 3, {**line, **_style(owner_handle=100)}),
            # Without a stored placement the reported owner is used.
            _Entity("LINE", 4, {**line, **_style(owner_handle=100)}),
            # Stored in a layout block other than the active paper space.
            _Entity("LINE", 5, {**line, **_style()}),
        ],
        placements={1: (0, 100), 2: (2, None), 3: (0, 101), 5: (0, 88)},
    )
    result = dwg_document_to_ir(
        document,
        layer_names_by_handle={16: "0"},
        block_names_by_handle={100: "SYMBOL", 101: "OTHER", 88: "*Paper_Space0"},
    )
    ir = result.document

    assert [entity["kind"] for entity in ir["entities"]] == ["INSERT"]
    blocks = ir["tables"]["blocks"]
    assert [entity["kind"] for entity in blocks["SYMBOL"]["entities"]] == [
        "HATCH",
        "LINE",
    ]
    assert [entity["kind"] for entity in blocks["OTHER"]["entities"]] == ["LINE"]
    assert result.statistics["converted_block_entities"] == 3
    skipped = [
        d for d in result.diagnostics if d.code == "DWG_PAPERSPACE_ENTITY_SKIPPED"
    ]
    assert len(skipped) == 1 and "Skipped 1 paper-space" in skipped[0].message
    validate_ir(ir, strict_jsonschema=True)


def _attribute_entity(
    kind: str,
    handle: int,
    flags: int,
    owner_handle: int | None,
    tag: str = "TAG",
    **dxf: Any,
) -> _Entity:
    return _Entity(
        kind,
        handle,
        {
            "insert": (0.0, float(handle), 0.0),
            "height": 2.5,
            "text": f"{kind}{handle}",
            "tag": tag,
            "attribute_flags": flags,
            **_style(owner_handle=owner_handle),
            **dxf,
        },
    )


def test_dwg_attribute_templates_and_invisible_attributes_are_not_drawn() -> None:
    document = _Document(
        [
            # Attributes whose block reference is unknown: a visible one stays a
            # text, an invisible one is not shown.
            _attribute_entity("ATTRIB", 1, 0, None),
            _attribute_entity("ATTRIB", 2, 1, None),
            # Inside a block: the template is skipped, a constant definition is block content.
            _attribute_entity("ATTDEF", 3, 0, 100),
            _attribute_entity("ATTDEF", 4, 2, 100),
            _attribute_entity("ATTDEF", 5, 3, 100),
            # Outside of a block a definition is shown, by its tag.
            _attribute_entity("ATTDEF", 6, 0, None, "PART_NO"),
        ]
    )
    result = dwg_document_to_ir(
        document, layer_names_by_handle={16: "0"}, block_names_by_handle={100: "TITLE"}
    )
    ir = result.document

    assert [entity["text"] for entity in ir["entities"]] == ["PART_NO", "ATTRIB1"]
    block = ir["tables"]["blocks"]["TITLE"]["entities"]
    assert [entity["text"] for entity in block] == ["ATTDEF4"]
    skipped = [
        d for d in result.diagnostics if d.code == "DWG_HIDDEN_ATTRIBUTE_SKIPPED"
    ]
    assert len(skipped) == 1 and "Skipped 3 DWG attribute" in skipped[0].message
    validate_ir(ir, strict_jsonschema=True)


def test_dwg_attributes_belong_to_their_block_reference() -> None:
    line = {"start": (0.0, 0.0, 0.0), "end": (1.0, 0.0, 0.0)}
    insert = {"insert": (5.0, 5.0, 0.0)}
    document = _PlacementDocument(
        [
            # Attributes can come before their block reference.
            _attribute_entity("ATTRIB", 1, 0, None, "NAME"),
            _attribute_entity(
                "ATTRIB",
                2,
                1,
                None,
                "SECRET",
                halign=1,
                valign=2,
                align_point=(3.0, 4.0, 0.0),
            ),
            _Entity("INSERT", 10, {**insert, "name": "TITLE", **_style()}),
            # A block reference inside a block has attributes of its own.
            _Entity("LINE", 11, {**line, **_style()}),
            _Entity("INSERT", 12, {**insert, "name": "INNER", **_style()}),
            _attribute_entity("ATTRIB", 3, 0, None, "PART"),
            _Entity("LINE", 14, {**line, **_style()}),
            # Paper space: the reference goes, and its attribute with it.
            _Entity("INSERT", 13, {**insert, "name": "TITLE", **_style()}),
            _attribute_entity("ATTRIB", 4, 0, None, "NAME"),
        ],
        placements={
            1: (0, 10),
            2: (0, 10),
            10: (2, None),
            11: (0, 100),
            12: (0, 100),
            3: (0, 12),
            14: (0, 101),
            13: (1, None),
            4: (0, 13),
        },
    )
    result = dwg_document_to_ir(
        document,
        layer_names_by_handle={16: "0"},
        block_names_by_handle={100: "TITLE", 101: "INNER"},
    )
    ir = result.document

    (title,) = ir["entities"]
    assert title["kind"] == "INSERT"
    assert title["attributes"] == {"NAME": "ATTRIB1", "SECRET": "ATTRIB2"}
    assert title["attribute_texts"] == [
        {
            "tag": "NAME",
            "insert": [0.0, 1.0],
            "height": 2.5,
            "rotation": 0.0,
            "text": "ATTRIB1",
            "style": "STANDARD",
            "halign": "left",
            "valign": "baseline",
            "width_factor": 1.0,
            "layer": "0",
            "color": 7,
        },
        {
            "tag": "SECRET",
            # Justified: anchored at the alignment point.
            "insert": [3.0, 4.0],
            "height": 2.5,
            "rotation": 0.0,
            "text": "ATTRIB2",
            "style": "STANDARD",
            "halign": "center",
            "valign": "middle",
            "width_factor": 1.0,
            "layer": "0",
            "color": 7,
            "visible": False,
        },
    ]

    blocks = ir["tables"]["blocks"]
    line_entity, inner = blocks["TITLE"]["entities"]
    assert (line_entity["kind"], inner["kind"]) == ("LINE", "INSERT")
    assert inner["attributes"] == {"PART": "ATTRIB3"}
    assert [text["text"] for text in inner["attribute_texts"]] == ["ATTRIB3"]

    assert result.statistics["attached_attributes"] == 3
    assert result.statistics["converted_entity_counts"] == {"INSERT": 2, "LINE": 2}
    skipped = [
        d for d in result.diagnostics if d.code == "DWG_PAPERSPACE_ENTITY_SKIPPED"
    ]
    assert len(skipped) == 1 and "Skipped 2 paper-space" in skipped[0].message
    assert not [d for d in result.diagnostics if d.severity == "error"]
    validate_ir(ir, strict_jsonschema=True)


def test_dwg_attribute_without_usable_geometry_is_reported() -> None:
    document = _PlacementDocument(
        [
            _Entity("INSERT", 10, {"insert": (5.0, 5.0, 0.0), "name": "T", **_style()}),
            _attribute_entity("ATTRIB", 1, 0, None, "NAME", height=0.0),
        ],
        placements={10: (2, None), 1: (0, 10)},
    )
    with pytest.raises(Exception, match="ATTRIB"):
        dwg_document_to_ir(document, layer_names_by_handle={16: "0"})

    result = dwg_document_to_ir(
        document,
        layer_names_by_handle={16: "0"},
        options=ImportOptions(strict=False),
    )

    (insert,) = result.document["entities"]
    assert "attribute_texts" not in insert and "attributes" not in insert
    failed = [d for d in result.diagnostics if d.code == "DWG_ENTITY_CONVERSION_FAILED"]
    assert len(failed) == 1 and failed[0].source_kind == "ATTRIB"
    assert result.statistics["skipped_entity_counts"] == {"ATTRIB": 1}


def _text_entity(handle: int, **dxf: Any) -> _Entity:
    return _Entity(
        "TEXT",
        handle,
        {
            "insert": (10.0, 20.0, 0.0),
            "height": 2.5,
            "text": f"TEXT{handle}",
            **_style(),
            **dxf,
        },
    )


def test_dwg_justified_text_is_anchored_at_its_alignment_point() -> None:
    document = _Document(
        [
            _text_entity(1),
            # Middle center: the alignment point is where the text is centered.
            _text_entity(2, halign=1, valign=2, align_point=(15.0, 21.0, 0.0)),
            # "Middle" (4) centers the text on the alignment point as well.
            _text_entity(3, halign=4, valign=0, align_point=(15.0, 21.0, 0.0)),
            # "Aligned" and "fit" run from the insertion point to the other one.
            _text_entity(4, halign=3, valign=0, align_point=(30.0, 20.0, 0.0)),
            _text_entity(5, halign=5, valign=0, align_point=(30.0, 20.0, 0.0)),
            # No alignment point stored: the insertion point is all there is.
            _text_entity(6, halign=2, valign=3, align_point=None),
        ]
    )
    ir = dwg_document_to_ir(document, layer_names_by_handle={16: "0"}).document

    placed = [
        (entity["insert"], entity["halign"], entity["valign"])
        for entity in ir["entities"]
    ]
    assert placed == [
        ([10.0, 20.0], "left", "baseline"),
        ([15.0, 21.0], "center", "middle"),
        ([15.0, 21.0], "center", "middle"),
        ([10.0, 20.0], "left", "baseline"),
        ([10.0, 20.0], "left", "baseline"),
        ([10.0, 20.0], "right", "top"),
    ]
    # The stored points stay available.
    assert ir["entities"][1]["metadata"]["dwg"]["align_point"] == [15.0, 21.0, 0.0]
    validate_ir(ir, strict_jsonschema=True)


def test_dwg_layer_states_and_entity_visibility_reach_the_ir() -> None:
    def state(**overrides: Any) -> dict[str, Any]:
        return {
            "frozen": False,
            "off": False,
            "locked": False,
            "plot": True,
            "lineweight": -3,
            **overrides,
        }

    document = _Document(
        [
            _line_entity(1, **_style(layer_handle=16)),
            _line_entity(2, lineweight=50, invisible=True, **_style(layer_handle=32)),
            _line_entity(3, lineweight=-1, invisible=False, **_style(layer_handle=48)),
        ]
    )
    result = dwg_document_to_ir(
        document,
        layer_names_by_handle={16: "0", 32: "Hidden", 48: "Frozen", 64: "Defpoints"},
        layer_states_by_handle={
            16: state(),
            32: state(off=True, locked=True, lineweight=35),
            48: state(frozen=True, lineweight=0),
            64: state(plot=False),
        },
    )
    ir = result.document

    layers = ir["tables"]["layers"]
    assert not {"visible", "plot", "lineweight_mm"} & set(layers["0"])
    assert layers["Hidden"]["visible"] is False
    assert layers["Hidden"]["lineweight_mm"] == 0.35
    assert layers["Hidden"]["metadata"]["dwg"]["off"] is True
    assert layers["Hidden"]["metadata"]["dwg"]["locked"] is True
    assert layers["Frozen"]["visible"] is False
    assert layers["Frozen"]["lineweight_mm"] == 0.0
    assert layers["Frozen"]["metadata"]["dwg"]["frozen"] is True
    assert layers["Defpoints"]["plot"] is False
    assert "visible" not in layers["Defpoints"]

    first, second, third = ir["entities"]
    assert not {"visible", "lineweight_mm"} & set(first)
    assert second["visible"] is False
    assert second["lineweight_mm"] == 0.5
    assert not {"visible", "lineweight_mm"} & set(third)
    validate_ir(ir, strict_jsonschema=True)


def test_convert_dwg_file_reads_the_layer_states(monkeypatch, tmp_path) -> None:
    import sys
    from types import SimpleNamespace

    raw = SimpleNamespace(
        decode_layer_names=lambda _path: [(16, "0"), (32, "Off")],
        decode_layer_colors=lambda _path: [(16, 7, None), (32, 1, None)],
        decode_block_header_names=lambda _path: [],
        # (handle, frozen, off, frozen in new viewports, locked, plot, lineweight)
        decode_layer_states=lambda _path: [
            (16, False, False, False, False, True, -3),
            (32, False, True, False, False, False, 25),
        ],
    )
    fake_ezdwg = SimpleNamespace(
        read=lambda _path: _Document(_line_entities()), raw=raw
    )
    monkeypatch.setitem(sys.modules, "ezdwg", fake_ezdwg)
    source = tmp_path / "fixture.dwg"
    source.write_bytes(b"fixture")

    layers = convert_dwg_file_to_ir(source).document["tables"]["layers"]

    assert "visible" not in layers["0"]
    assert layers["Off"]["visible"] is False
    assert layers["Off"]["plot"] is False
    assert layers["Off"]["lineweight_mm"] == 0.25


def test_dwg_tolerance_without_a_stored_height_is_skipped() -> None:
    def tolerance(handle: int, height: float) -> _Entity:
        return _Entity(
            "TOLERANCE",
            handle,
            {
                "insert": (1.0, 2.0, 0.0),
                "height": height,
                "text": "{\\Fgdt;j}%%v0.1",
                "rotation": 0.0,
                **_style(),
            },
        )

    # Without a stored height and without a readable dimension style.
    result = dwg_document_to_ir(
        _Document([tolerance(1, 0.0), tolerance(2, 2.5)]),
        layer_names_by_handle={16: "0"},
    )

    (entity,) = result.document["entities"]
    assert (entity["kind"], entity["height"]) == ("MTEXT", 2.5)
    assert result.statistics["skipped_entity_counts"] == {"TOLERANCE": 1}
    skipped = [d for d in result.diagnostics if d.code == "DWG_UNSUPPORTED_ENTITY"]
    assert len(skipped) == 1 and skipped[0].source_kind == "TOLERANCE"
