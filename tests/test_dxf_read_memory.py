"""Reading a DXF lets its pairs go as the IR is built (memory of large drawings).

A synthetic 72 MB DXF peaked at 2.2 GB while read: the whole file split into lines for every
header variable looked up, the pairs copied once to decode escapes and again into
records, and all of them kept until every entity was built. The reader now lets each
go once used; these tests pin that the result is unchanged.
"""

from __future__ import annotations

from pathlib import Path

from cad2d_ir import dxf_to_ir
from cad2d_ir.codecs import dxf as dxf_codec


def _dxf_text() -> str:
    lines = [
        "0", "SECTION", "2", "HEADER",
        "9", "$ACADVER", "1", "AC1015",
        "9", "$DWGCODEPAGE", "3", "ANSI_1252",
        "9", "$INSUNITS", "70", "4",
        "0", "ENDSEC",
        "0", "SECTION", "2", "BLOCKS",
        "0", "BLOCK", "8", "0", "2", "MARK", "70", "2",
        "10", "0", "20", "0", "30", "0",
        "0", "ATTDEF", "8", "0", "10", "0", "20", "0", "40", "2.5",
        "1", "", "3", "Tag", "2", "TAG", "70", "0",
        "0", "LINE", "8", "0", "10", "0", "20", "0", "11", "5", "21", "5",
        "0", "ENDBLK", "8", "0",
        "0", "ENDSEC",
        "0", "SECTION", "2", "ENTITIES",
        "0", "LINE", "5", "A1", "8", "L1", "62", "1",
        "10", "0", "20", "0", "11", "100", "21", "0",
        "0", "POLYLINE", "5", "A2", "8", "L1", "66", "1", "70", "0",
        "0", "VERTEX", "8", "L1", "10", "0", "20", "10",
        "0", "VERTEX", "8", "L1", "10", "50", "20", "10",
        "0", "VERTEX", "8", "L1", "10", "50", "20", "40",
        "0", "SEQEND", "8", "L1",
        "0", "INSERT", "5", "A3", "8", "L2", "66", "1", "2", "MARK",
        "10", "20", "20", "20",
        "0", "ATTRIB", "8", "L2", "10", "20", "20", "18", "40", "2.5",
        "1", "VALUE", "2", "TAG", "70", "0",
        "0", "SEQEND", "8", "L2",
        "0", "TEXT", "5", "A4", "8", "L2", "10", "1", "20", "2", "40", "2.5",
        "1", "\\U+5BF8\\U+6CD5 A",
        "0", "LINE", "5", "A5", "67", "1", "8", "L1",
        "10", "0", "20", "0", "11", "10", "21", "10",
        "0", "ENDSEC",
        "0", "EOF",
    ]  # fmt: skip
    return "\n".join(lines) + "\n"


def test_file_read_builds_the_same_ir_as_text_read(tmp_path: Path) -> None:
    text = _dxf_text()
    path = tmp_path / "drawing.dxf"
    path.write_text(text, encoding="ascii")
    from_file = dxf_codec.read_dxf_file(path)
    from_text = dxf_to_ir(text)
    for key in ("entities", "tables", "layouts", "header"):
        assert from_file.get(key) == from_text.get(key), key
    kinds = [entity["kind"] for entity in from_text["entities"]]
    assert kinds == ["LINE", "LWPOLYLINE", "INSERT", "TEXT"]
    text_entity = from_text["entities"][3]
    assert text_entity["text"] == "寸法 A"  # the escape is decoded
    assert from_text["entities"][2]["attributes"] == {"TAG": "VALUE"}
    assert from_text.get("layouts")  # the paper space line


def test_records_are_let_go_as_they_are_converted() -> None:
    pairs = dxf_codec._parse_pairs(_dxf_text())
    sections = dxf_codec._split_sections(pairs)
    records = dxf_codec._pairs_to_records(sections["ENTITIES"])
    model, _paper = dxf_codec._split_paper_space_records(records)
    kept = dxf_codec._records_to_entities(list(model))
    used = list(model)
    released = dxf_codec._records_to_entities(used, release_records=True)
    assert released == kept
    assert used == []
    # The records hold the pairs themselves, not copies
    assert records[0][1][0] is sections["ENTITIES"][1]


def test_pairs_are_used_up_by_the_ir_build() -> None:
    pairs = dxf_codec._parse_pairs(_dxf_text())
    document = dxf_codec._pairs_to_ir(
        pairs,
        ir_version=dxf_codec.CURRENT_IR_VERSION,
        validate=True,
        warnings=None,
        diagnostics=None,
    )
    assert pairs == []
    assert [entity["kind"] for entity in document["entities"]] == [
        "LINE",
        "LWPOLYLINE",
        "INSERT",
        "TEXT",
    ]


def test_header_variables_are_read_from_the_header_section() -> None:
    raw = _dxf_text().encode("ascii")
    assert dxf_codec._raw_header_string(raw, "$DWGCODEPAGE") == "ANSI_1252"
    assert dxf_codec._raw_header_string(raw, "$ACADVER") == "AC1015"
    assert dxf_codec._raw_header_string(raw, "$NOTTHERE") is None
    # A file without a HEADER section still reads (nothing to find)
    entities_only = b"0\nSECTION\n2\nENTITIES\n0\nENDSEC\n0\nEOF\n"
    assert dxf_codec._raw_header_string(entities_only, "$ACADVER") is None
