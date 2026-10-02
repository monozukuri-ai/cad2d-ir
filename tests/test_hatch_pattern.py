from __future__ import annotations

import math

import pytest

from cad2d_ir.codecs.dxf import dxf_to_ir, ir_to_dxf
from cad2d_ir.schema import IRValidationError, validate_ir


def _dxf(*entity: str) -> str:
    return "\n".join(
        ["0", "SECTION", "2", "ENTITIES", *entity, "0", "ENDSEC", "0", "EOF"]
    )


def _hatch(*pattern: str, solid: str = "0", name: str = "ANSI31") -> list[str]:
    """A rectangular HATCH (0,0)-(100,50) followed by the given pattern groups."""
    return [
        *("0", "HATCH", "8", "0", "2", name, "70", solid, "71", "0", "91", "1"),
        *("92", "2", "72", "0", "73", "1", "93", "4"),
        *("10", "0", "20", "0", "10", "100", "20", "0"),
        *("10", "100", "20", "50", "10", "0", "20", "50"),
        *("97", "0", "75", "0", "76", "1"),
        *pattern,
        *("98", "0"),
    ]


def _line(
    angle: str, base: tuple[str, str], offset: tuple[str, str], *dashes: str
) -> list[str]:
    groups = [
        "53",
        angle,
        "43",
        base[0],
        "44",
        base[1],
        "45",
        offset[0],
        "46",
        offset[1],
    ]
    groups += ["79", str(len(dashes))]
    for dash in dashes:
        groups += ["49", dash]
    return groups


def _document(**hatch: object) -> dict:
    return {
        "format": "cad2d-ir",
        "version": "0.2.0",
        "header": {"units": "mm", "angle_unit": "deg", "coord_space": "world"},
        "entities": [
            {
                "id": "H1",
                "kind": "HATCH",
                "solid": False,
                "pattern": "ANSI31",
                "loops": [{"vertices": [[0, 0], [100, 0], [100, 50], [0, 50]]}],
                **hatch,
            }
        ],
    }


def test_dxf_import_reads_hatch_pattern_definition_lines() -> None:
    # ANSI31 at scale 20: 45 degree lines, 3.175 * 20 apart.
    pattern = [
        *("52", "0", "41", "20", "77", "0", "78", "1"),
        *_line("45", ("0", "0"), ("-44.9013", "44.9013")),
    ]
    hatch = dxf_to_ir(_dxf(*_hatch(*pattern)))["entities"][0]

    assert hatch["solid"] is False
    assert hatch["pattern"] == "ANSI31"
    assert hatch["pattern_scale"] == pytest.approx(20.0)
    assert "pattern_angle" not in hatch  # 0 is the default
    assert hatch["pattern_lines"] == [
        {"angle": 45.0, "base": [0.0, 0.0], "offset": [-44.9013, 44.9013]}
    ]
    # The spacing of the family is the offset component across the lines.
    (line,) = hatch["pattern_lines"]
    direction = math.radians(line["angle"])
    spacing = line["offset"][1] * math.cos(direction) - line["offset"][0] * math.sin(
        direction
    )
    assert spacing == pytest.approx(3.175 * 20, rel=1e-4)


def test_dxf_import_reads_dashed_and_multiple_pattern_lines() -> None:
    pattern = [
        *("52", "30", "41", "2.5", "77", "0", "78", "2"),
        *_line("30", ("1", "2"), ("-2.5", "4.33"), "3", "-1.5", "0", "-1.5"),
        *_line("120", ("0", "0"), ("-4.33", "-2.5")),
    ]
    hatch = dxf_to_ir(_dxf(*_hatch(*pattern, name="_USER")))["entities"][0]

    assert hatch["pattern_angle"] == pytest.approx(30.0)
    assert hatch["pattern_scale"] == pytest.approx(2.5)
    first, second = hatch["pattern_lines"]
    assert first == {
        "angle": 30.0,
        "base": [1.0, 2.0],
        "offset": [-2.5, 4.33],
        "dashes": [3.0, -1.5, 0.0, -1.5],
    }
    assert second == {"angle": 120.0, "base": [0.0, 0.0], "offset": [-4.33, -2.5]}


def test_dxf_import_ignores_pattern_data_it_cannot_use() -> None:
    # No definition lines: the pattern name is all there is.
    bare = dxf_to_ir(_dxf(*_hatch("78", "0")))["entities"][0]
    assert "pattern_lines" not in bare and bare["solid"] is False

    # A family whose offset runs along its own lines has no spacing.
    degenerate = [
        *("78", "2"),
        *_line("0", ("0", "0"), ("5", "0")),
        *_line("90", ("0", "0"), ("5", "0")),
    ]
    hatch = dxf_to_ir(_dxf(*_hatch(*degenerate)))["entities"][0]
    assert [line["angle"] for line in hatch["pattern_lines"]] == [90.0]

    # Unreadable numbers discard the pattern but keep the hatch.
    broken = [*("78", "1"), *_line("45", ("0", "x"), ("-1", "1"))]
    hatch = dxf_to_ir(_dxf(*_hatch(*broken)))["entities"][0]
    assert "pattern_lines" not in hatch and hatch["loops"]

    # A solid fill has no pattern, whatever follows it.
    stray = [*("78", "1"), *_line("45", ("0", "0"), ("-1", "1"))]
    solid = dxf_to_ir(_dxf(*_hatch(*stray, solid="1", name="SOLID")))["entities"][0]
    assert "pattern_lines" not in solid and "solid" not in solid


def test_hatch_pattern_roundtrips_through_dxf() -> None:
    lines = [
        {"angle": 45.0, "base": [0.0, 0.0], "offset": [-2.0, 2.0]},
        {"angle": 0.0, "base": [1.0, 0.5], "offset": [3.0, 4.0], "dashes": [6.0, -3.0]},
    ]
    document = _document(pattern_angle=15.0, pattern_scale=2.0, pattern_lines=lines)
    text = ir_to_dxf(document)

    groups = text.splitlines()
    start = groups.index("HATCH")
    codes = groups[start + 1 :: 2]
    assert codes.count("53") == 2 and codes.count("49") == 2
    assert groups[start:].index("78") < groups[start:].index("53")

    restored = dxf_to_ir(text)["entities"][0]
    assert restored["pattern"] == "ANSI31"
    assert restored["pattern_angle"] == pytest.approx(15.0)
    assert restored["pattern_scale"] == pytest.approx(2.0)
    assert restored["pattern_lines"] == lines


def test_pattern_hatch_without_definition_lines_still_exports() -> None:
    text = ir_to_dxf(_document())

    groups = text.splitlines()
    start = groups.index("HATCH")
    tail = groups[start:]
    assert tail[tail.index("78") + 1] == "0"
    assert "53" not in tail[::2][1:] or tail.index("53") > tail.index("98")
    restored = dxf_to_ir(text)["entities"][0]
    assert "pattern_lines" not in restored and restored["solid"] is False


@pytest.mark.parametrize("strict", [False, True])
def test_schema_accepts_hatch_pattern_lines(strict: bool) -> None:
    lines = [
        {"angle": 45.0, "base": [0, 0], "offset": [-2, 2]},
        {
            "angle": 0,
            "base": [1.0, 0.5],
            "offset": [3.0, 4.0],
            "dashes": [6, -3, 0, -3],
        },
    ]
    document = _document(pattern_angle=-15, pattern_scale=2.0, pattern_lines=lines)
    validate_ir(document, strict_jsonschema=strict)
    validate_ir(_document(pattern_lines=[]), strict_jsonschema=strict)


@pytest.mark.parametrize("strict", [False, True])
@pytest.mark.parametrize(
    "hatch",
    [
        {"pattern_lines": {"angle": 45}},
        {"pattern_lines": [{"angle": 45, "base": [0, 0]}]},
        {"pattern_lines": [{"angle": "45", "base": [0, 0], "offset": [1, 1]}]},
        {"pattern_lines": [{"angle": 45, "base": [0, 0, 0], "offset": [1, 1]}]},
        {
            "pattern_lines": [
                {"angle": 45, "base": [0, 0], "offset": [1, 1], "dashes": 3}
            ]
        },
        {
            "pattern_lines": [
                {"angle": 45, "base": [0, 0], "offset": [1, 1], "dashes": ["3"]}
            ]
        },
        {
            "pattern_lines": [
                {"angle": 45, "base": [0, 0], "offset": [1, 1], "width": 1}
            ]
        },
        {"pattern_scale": 0},
        {"pattern_scale": "2"},
        {"pattern_angle": "30"},
    ],
)
def test_schema_rejects_invalid_hatch_pattern(hatch: dict, strict: bool) -> None:
    with pytest.raises(IRValidationError):
        validate_ir(_document(**hatch), strict_jsonschema=strict)
