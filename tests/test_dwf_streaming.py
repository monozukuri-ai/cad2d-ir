"""DWF import from a streaming reader (ezdwf 0.0.13 ``DrawingReader``) and shared metadata."""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from test_dwf_sheets_images import _entity, _line_sheet, _point, _sheet

from cad2d_ir.importers.dwf import dwf_drawing_to_ir
from cad2d_ir.schema import validate_ir


class _Reader:
    """What ``ezdwf.DrawingReader`` offers: sheets without entities, entities per sheet."""

    def __init__(
        self, sheets: list[SimpleNamespace], per_sheet: dict[int, tuple[list, list]]
    ):
        self.sheets = tuple(
            SimpleNamespace(**{**vars(sheet), "entities": (), "markup_entities": ()})
            for sheet in sheets
        )
        self._per_sheet = per_sheet
        self.diagnostics = ()
        self.pulled: list[tuple[str, int]] = []

    def entities(self, index: int):
        self.pulled.append(("entities", index))
        yield from self._per_sheet[index][0]

    def markup_entities(self, index: int):
        self.pulled.append(("markup", index))
        yield from self._per_sheet[index][1]


def test_streaming_reader_converts_like_a_drawing() -> None:
    first = _line_sheet(0)
    second_line = _entity("LINE", 1, 1, points=(_point(10, 10), _point(110, 60)))
    markup = _entity("LINE", 2, 1, is_markup=True, points=(_point(0, 0), _point(1, 1)))
    second = _sheet(1, (second_line,))
    second.markup_entities = (markup,)
    whole = SimpleNamespace(sheets=(first, second), diagnostics=())
    reader = _Reader(
        [first, second], {0: ([first.entities[0]], []), 1: ([second_line], [markup])}
    )

    from_drawing = dwf_drawing_to_ir(whole)
    from_reader = dwf_drawing_to_ir(reader)

    strip = lambda document: {
        key: value for key, value in document.items() if key != "source"
    }
    assert strip(from_reader.document) == strip(from_drawing.document)
    assert from_reader.statistics == from_drawing.statistics
    sheets = from_reader.document["source"]["metadata"]["dwf"]["sheets"]
    assert [(s["entity_count"], s["markup_entity_count"]) for s in sheets] == [
        (1, 0),
        (1, 1),
    ]
    assert from_reader.statistics["source_markup_entities"] == 1
    assert reader.pulled == [
        ("entities", 0),
        ("markup", 0),
        ("entities", 1),
        ("markup", 1),
    ]
    validate_ir(from_reader.document, strict_jsonschema=True)


def test_entities_of_one_stream_share_the_constant_metadata() -> None:
    lines = [
        _entity("LINE", index, points=(_point(0, index), _point(10, index)))
        for index in range(3)
    ]
    text = _entity("TEXT", 9, points=(_point(2, 3),), text="\\A1;formatted")
    strip = _entity(
        "GOURAUD_POLYTRIANGLE",
        10,
        colored_points=(
            SimpleNamespace(point=_point(0, 0), color=(255, 0, 0, 255)),
            SimpleNamespace(point=_point(10, 0), color=(0, 255, 0, 255)),
            SimpleNamespace(point=_point(0, 10), color=(0, 0, 255, 255)),
        ),
    )
    drawing = SimpleNamespace(
        sheets=(_sheet(0, (*lines, text, strip)),), diagnostics=()
    )
    document = dwf_drawing_to_ir(drawing).document
    entities: list[dict[str, Any]] = document["entities"]

    shared = [e["metadata"]["dwf"] for e in entities[:3]]
    assert shared[0] is shared[1] is shared[2]
    assert shared[0]["sheet_index"] == 0 and "colored_points" not in shared[0]
    # Per-entity keys live in a copy; the shared dictionary stays constant
    mtext = entities[3]["metadata"]["dwf"]
    assert mtext["mtext_formatting_detected"] is True and mtext is not shared[0]
    assert "mtext_formatting_detected" not in shared[0]
    hatch = entities[4]["metadata"]["dwf"]
    assert len(hatch["colored_points"]) == 3 and hatch is not shared[0]
    assert hatch["style"] is shared[0]["style"]
    validate_ir(document, strict_jsonschema=True)
