from __future__ import annotations

from collections import Counter
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from cad2d_ir.importers.base import ImporterError, ImportOptions
from cad2d_ir.importers.dgn_v8 import dgn_v8_document_to_ir

_DATA_DEFAULTS: dict[str, Any] = {
    "vertices": (),
    "orientations": (),
    "origin": None,
    "center": None,
    "anchor": None,
    "font_id": None,
    "justification": None,
    "width_uor": None,
    "height_uor": None,
    "width_master": None,
    "height_master": None,
    "rotation_degrees": None,
    "orientation": (),
    "editable_fields": None,
    "encoding": None,
    "text_bytes": None,
    "text": None,
    "primary_axis_uor": None,
    "secondary_axis_uor": None,
    "primary_axis_master": None,
    "secondary_axis_master": None,
    "start_angle_degrees": None,
    "sweep_angle_degrees": None,
    "child_count": None,
    "node_number": None,
    "boundary_count": None,
    "transform": (),
    "name": None,
    "properties_raw": None,
    "declared_poles": None,
}


def _point(x: float, y: float, z: float = 0.0) -> SimpleNamespace:
    return SimpleNamespace(uor=(x * 1000.0, y * 1000.0, z * 1000.0), master=(x, y, z))


def _element(
    kind: str,
    element_id: int,
    element_type: int,
    *,
    index: int,
    parent_index: int | None = None,
    level: int = 2,
    **data_values: Any,
) -> SimpleNamespace:
    data = dict(_DATA_DEFAULTS)
    data["kind"] = kind
    data.update(data_values)
    return SimpleNamespace(
        index=index,
        raw=SimpleNamespace(
            element_type=element_type,
            role="graphical",
            stream_path="dgn^h/non-model",
            inflated_offset=element_id * 64,
            raw_bytes=b"",
        ),
        common=SimpleNamespace(
            level=level,
            element_id=element_id,
            model_id=1,
            graphic_group=0,
            properties=0,
            geometry_flags=0,
            line_style=1,
            line_weight=2,
            color_index=3,
            stored_dimension=2,
            dimension=2,
        ),
        data=SimpleNamespace(**data),
        parent_index=parent_index,
        child_indices=(),
        linkages=(),
        auxiliary_records=(),
        kind=kind,
        level=level,
    )


class _Model:
    def __init__(
        self,
        entities: tuple[Any, ...],
        *,
        children: dict[int, tuple[Any, ...]] | None = None,
        extra: tuple[Any, ...] = (),
        unknown: tuple[Any, ...] = (),
        dimension: int = 2,
        name: str = "Default",
        master_unit: str | None = "mm",
    ) -> None:
        self._entities = tuple(entities)
        self._children = children or {}
        self.all_entities = self._entities + tuple(extra)
        self.elements = self.all_entities + tuple(unknown)
        self.unknown_elements = tuple(unknown)
        self.metadata = SimpleNamespace(
            index=0,
            storage_path="dgn^h/non-model",
            storage_index=0,
            model_number=0,
            model_id=1,
            index_model_id=1,
            name=name,
            description="",
            dimension=dimension,
            type_and_flags=0,
            model_flags=0,
            uor_per_master=1000.0,
            scale=0.001,
            global_origin_uor=(0.0, 0.0, 0.0),
            master_unit=master_unit,
            sub_unit="um",
            linkages=(),
            raw_header=b"",
        )

    @property
    def entities(self) -> tuple[Any, ...]:
        return self._entities

    def children(self, element: Any) -> tuple[Any, ...]:
        return self._children.get(element.common.element_id, ())


def _document(*models: Any) -> SimpleNamespace:
    return SimpleNamespace(models=tuple(models))


def test_v8_native_entities_map_to_ir() -> None:
    line = _element(
        "LINE", 1, 3, index=0, vertices=(_point(0.0, 0.0), _point(10.0, 5.0))
    )
    shape = _element(
        "SHAPE",
        2,
        6,
        index=1,
        vertices=(
            _point(0.0, 0.0),
            _point(4.0, 0.0),
            _point(4.0, 4.0),
            _point(0.0, 0.0),
        ),
    )
    arc = _element(
        "ARC",
        3,
        16,
        index=2,
        center=_point(5.0, 5.0),
        primary_axis_master=2.0,
        secondary_axis_master=2.0,
        rotation_degrees=0.0,
        start_angle_degrees=10.0,
        sweep_angle_degrees=180.0,
    )
    text = _element(
        "TEXT",
        4,
        17,
        index=3,
        text="図面",
        text_bytes="図面".encode("utf-8"),
        encoding="utf-8",
        origin=_point(1.0, 2.0),
        height_master=2.0,
        width_master=1.5,
        font_id=3,
        justification=8,
        rotation_degrees=0.0,
        editable_fields=0,
    )
    cell_line = _element(
        "LINE",
        6,
        3,
        index=5,
        parent_index=4,
        vertices=(_point(0.0, 0.0), _point(1.0, 1.0)),
    )
    cell = _element(
        "CELL",
        5,
        2,
        index=4,
        origin=_point(3.0, 3.0),
        transform=(1.0, 0.0, 0.0, 1.0, 0.0, 0.0),
    )
    shared = _element(
        "SHARED_CELL_INSTANCE",
        7,
        35,
        index=6,
        name="Named definition",
        origin=_point(0.0, 1.0),
        transform=(1.0, 0.0, 0.0, 1.0, 0.0, 0.0),
    )
    dimension = _element("DIMENSION", 8, 36, index=7, anchor=_point(0.0, 1.0))
    point_string = _element(
        "POINT_STRING",
        9,
        22,
        index=8,
        vertices=(_point(1.0, 1.0), _point(2.0, 2.0)),
        orientations=((1.0, 0.0, 0.0, 0.0), (1.0, 0.0, 0.0, 0.0)),
    )
    model = _Model(
        (line, shape, arc, text, cell, shared, dimension, point_string),
        children={5: (cell_line,)},
        extra=(cell_line,),
    )

    result = dgn_v8_document_to_ir(
        _document(model), source_name="fixture.dgn", parser_version="test"
    )

    document = result.document
    assert document["source"]["format"] == "dgn"
    assert document["source"]["version"] == "V8"
    assert document["source"]["metadata"]["dgn"]["model_name"] == "Default"
    assert document["header"]["units"] == "mm"

    kinds = Counter(entity["kind"] for entity in document["entities"])
    assert kinds == {
        "LINE": 1,
        "LWPOLYLINE": 1,
        "ARC": 1,
        "TEXT": 1,
        "INSERT": 1,
        "POINT": 2,
    }

    polyline = next(e for e in document["entities"] if e["kind"] == "LWPOLYLINE")
    assert polyline["closed"] is True
    assert len(polyline["vertices"]) == 3

    arc_entity = next(e for e in document["entities"] if e["kind"] == "ARC")
    assert arc_entity["radius"] == pytest.approx(2.0)
    assert arc_entity["start_angle"] == pytest.approx(10.0)
    assert arc_entity["end_angle"] == pytest.approx(190.0)

    text_entity = next(e for e in document["entities"] if e["kind"] == "TEXT")
    assert text_entity["text"] == "図面"
    assert text_entity["insert"] == [1.0, 2.0]
    assert text_entity["halign"] == "center"
    assert text_entity["valign"] == "bottom"
    assert text_entity["width_factor"] == pytest.approx(0.75)
    assert text_entity["metadata"]["dgn"]["justification"] == 8
    assert text_entity["layer"] == "DGN_LEVEL_2"
    assert text_entity["linetype"] == "DGN_STYLE_1"

    insert = next(e for e in document["entities"] if e["kind"] == "INSERT")
    block = document["tables"]["blocks"][insert["block"]]
    assert block["base_point"] == [3.0, 3.0]
    assert [entity["kind"] for entity in block["entities"]] == ["LINE"]

    points = [e for e in document["entities"] if e["kind"] == "POINT"]
    assert [p["position"] for p in points] == [[1.0, 1.0], [2.0, 2.0]]
    assert [p["metadata"]["dgn"]["point_index"] for p in points] == [0, 1]

    codes = {diagnostic.code for diagnostic in result.diagnostics}
    assert "DGN_SHARED_CELL_UNRESOLVED" in codes
    assert "DGN_UNSUPPORTED_ENTITY" in codes

    statistics = result.statistics
    assert statistics["source_version"] == "V8"
    assert statistics["source_models"] == 1
    assert statistics["converted_entities"] == 7
    assert statistics["converted_blocks"] == 1
    assert statistics["skipped_entity_counts"] == {
        "DIMENSION": 1,
        "SHARED_CELL_INSTANCE": 1,
    }
    assert statistics["text_encodings"] == ["utf-8"]


@pytest.mark.parametrize(
    ("justification", "halign", "valign"),
    [
        (0, "left", "top"),
        (2, "left", "bottom"),
        (5, "left", "bottom"),
        (7, "center", "middle"),
        (12, "right", "top"),
        (14, "right", "bottom"),
        (16, "center", "bottom"),
        (99, "left", "baseline"),
    ],
)
def test_v8_text_justification_maps_to_both_alignments(
    justification: int, halign: str, valign: str
) -> None:
    text = _element(
        "TEXT",
        1,
        17,
        index=0,
        text="anchor",
        origin=_point(0.0, 0.0),
        height_master=1.0,
        width_master=1.0,
        justification=justification,
    )
    result = dgn_v8_document_to_ir(_document(_Model((text,))))
    entity = result.document["entities"][0]
    assert entity["halign"] == halign
    assert entity["valign"] == valign
    assert entity["metadata"]["dgn"]["justification"] == justification


def test_v8_first_model_with_entities_is_converted() -> None:
    empty = _Model((), name="Empty sheet")
    line = _element(
        "LINE", 1, 3, index=0, vertices=(_point(0.0, 0.0), _point(1.0, 0.0))
    )
    populated = _Model((line,), name="Design")

    result = dgn_v8_document_to_ir(_document(empty, populated))

    assert result.statistics["converted_model"] == "Design"
    assert result.statistics["source_models"] == 2
    diagnostic = next(
        d for d in result.diagnostics if d.code == "DGN_V8_EXTRA_MODELS_SKIPPED"
    )
    assert diagnostic.details == {
        "converted_model": "Design",
        "model_count": 2,
        "skipped_models": ["Empty sheet"],
    }


def test_v8_3d_model_is_projected_with_a_loss_diagnostic() -> None:
    line = _element(
        "LINE",
        1,
        3,
        index=0,
        vertices=(_point(0.0, 0.0, 7.0), _point(1.0, 2.0, 7.0)),
    )
    result = dgn_v8_document_to_ir(_document(_Model((line,), dimension=3)))

    diagnostic = next(d for d in result.diagnostics if d.code == "DGN_3D_FLATTENED")
    assert diagnostic.action == "projected"
    assert result.document["entities"][0]["p2"] == [1.0, 2.0]


def test_v8_bspline_curve_falls_back_to_pole_polyline() -> None:
    pole = _element(
        "BSPLINE_POLE",
        2,
        21,
        index=1,
        parent_index=0,
        vertices=(
            _point(0.0, 0.0),
            _point(1.0, 2.0),
            _point(2.0, 2.0),
            _point(3.0, 0.0),
        ),
    )
    curve = _element(
        "BSPLINE_CURVE", 1, 27, index=0, properties_raw=6291746, declared_poles=4
    )
    model = _Model((curve,), children={1: (pole,)})

    result = dgn_v8_document_to_ir(_document(model))

    entity = result.document["entities"][0]
    assert entity["kind"] == "LWPOLYLINE"
    assert entity["approximation"]["source_kind"] == "DGN_V8_BSPLINE_CURVE"
    assert len(entity["vertices"]) == 4
    diagnostic = next(
        d for d in result.diagnostics if d.code == "DGN_CURVE_APPROXIMATED"
    )
    assert diagnostic.source_kind == "BSPLINE_CURVE"


def test_v8_strict_raises_and_lenient_skips_malformed_entities() -> None:
    broken = _element("LINE", 1, 3, index=0, vertices=(_point(0.0, 0.0),))

    with pytest.raises(ImporterError):
        dgn_v8_document_to_ir(_document(_Model((broken,))))

    result = dgn_v8_document_to_ir(
        _document(_Model((broken,))), options=ImportOptions(strict=False)
    )
    assert result.document["entities"] == []
    diagnostic = next(
        d for d in result.diagnostics if d.code == "DGN_ENTITY_CONVERSION_FAILED"
    )
    assert diagnostic.source_kind == "LINE"
    assert result.statistics["skipped_entity_counts"] == {"LINE": 1}


def test_v8_unknown_graphic_elements_are_counted_as_skipped() -> None:
    line = _element(
        "LINE", 1, 3, index=0, vertices=(_point(0.0, 0.0), _point(1.0, 0.0))
    )
    unknown = _element("UNKNOWN", 2, 106, index=1)
    result = dgn_v8_document_to_ir(_document(_Model((line,), unknown=(unknown,))))

    assert result.statistics["skipped_entity_counts"] == {"TYPE_106": 1}


def test_v8_document_without_models_is_rejected() -> None:
    with pytest.raises(ImporterError):
        dgn_v8_document_to_ir(_document())


def test_v8_real_fixture_converts_end_to_end() -> None:
    pytest.importorskip("ezdgn")
    from cad2d_ir.importers.dgn import convert_dgn_file_to_ir

    fixture = Path(__file__).parent / "data" / "dgn" / "test_dgnv8.dgn"
    result = convert_dgn_file_to_ir(fixture)

    document = result.document
    assert document["source"]["version"] == "V8"
    assert document["header"]["units"] == "m"

    kinds = Counter(entity["kind"] for entity in document["entities"])
    assert kinds["TEXT"] == 4
    texts = {e["text"] for e in document["entities"] if e["kind"] == "TEXT"}
    assert "myTéxt" in texts
    for entity in document["entities"]:
        if entity["kind"] == "TEXT":
            # Every text in the fixture is stored with justification 0
            # (kLeftTop), matching the ODA-generated reference expectations.
            assert entity["halign"] == "left"
            assert entity["valign"] == "top"
    assert kinds["INSERT"] == 2

    codes = {diagnostic.code for diagnostic in result.diagnostics}
    assert "DGN_3D_FLATTENED" in codes
    assert "DGN_SHARED_CELL_UNRESOLVED" in codes
    assert "DGN_ENTITY_CONVERSION_FAILED" not in codes

    statistics = result.statistics
    assert statistics["source_models"] == 1
    assert statistics["skipped_entity_counts"].get("DIMENSION") == 1


def test_v7_real_fixture_still_routes_through_open_document() -> None:
    pytest.importorskip("ezdgn")
    from cad2d_ir.importers.dgn import convert_dgn_file_to_ir

    fixture = Path(__file__).parent / "data" / "dgn" / "smalltest.dgn"
    result = convert_dgn_file_to_ir(fixture)

    document = result.document
    assert document["source"]["version"].startswith("V7")
    text = next(e for e in document["entities"] if e["kind"] == "TEXT")
    assert text["text"] == "Demo Text"
    assert text["halign"] == "left"
