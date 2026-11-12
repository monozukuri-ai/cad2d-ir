"""The DXF the writer makes of the documents under ``golden/``, kept as files.

Every ``<name>.json`` there is an IR document. ``<name>.ac1024.dxf`` and
``<name>.ac1009.dxf`` are what ``ir_to_dxf`` writes of it as DXF R2010 and R12,
and ``<name>.report.json`` holds the export diagnostics and the entity map of
each. A file named for other options (``<name>.ac1009-16.dxf``: 16 segments to
a curve, ``<name>.ac1024-skip.dxf``: generic dimensions left out) is checked
with those.

The writer is native code and has no second implementation to be compared
with: these files are what notices a change of its output. When a change is
meant, write the files again and review the difference::

    CAD2D_IR_UPDATE_GOLDEN=1 python -m pytest native/tests/test_dxf_golden.py

A text is compared line by line. A number may differ in its last digits (it
comes from the sine and cosine of the platform); everything else is the same to
the byte.
"""

from __future__ import annotations

import dataclasses
import json
import math
import os
from pathlib import Path
from typing import Any

import pytest

from cad2d_ir import ir_to_dxf

GOLDEN = Path(__file__).resolve().parent / "golden"
UPDATE = os.environ.get("CAD2D_IR_UPDATE_GOLDEN") == "1"
NAMES = sorted(path.stem for path in GOLDEN.glob("*.json") if "." not in path.stem)
STANDARD = ("ac1024", "ac1009")


def _options(variant: str) -> dict[str, Any]:
    """``ac1009-16-skip`` -> R12, 16 segments to a curve, no generic dimensions."""
    version, *rest = variant.split("-")
    options: dict[str, Any] = {"target_version": version.upper()}
    for item in rest:
        if item == "skip":
            options["generic_dimensions"] = "skip"
        else:
            options["curve_segments"] = int(item)
    return options


def _variants(name: str) -> list[str]:
    found = {
        path.name[len(name) + 1 : -len(".dxf")] for path in GOLDEN.glob(f"{name}.*.dxf")
    }
    return [*STANDARD, *sorted(found.difference(STANDARD))]


def _write(name: str, variant: str) -> tuple[str, dict[str, Any]]:
    document = json.loads((GOLDEN / f"{name}.json").read_text(encoding="utf-8"))
    diagnostics: list[Any] = []
    entity_map: list[Any] = []
    # not validated: the writer has an answer for what the schema refuses too
    # (an entity of an unknown kind, an entity without an id)
    text = ir_to_dxf(
        document,
        validate=False,
        diagnostics=diagnostics,
        entity_map=entity_map,
        **_options(variant),
    )
    report = {
        "diagnostics": [dataclasses.asdict(item) for item in diagnostics],
        "entity_map": [dict(item) for item in entity_map],
    }
    # as JSON has it (a tuple is a list there)
    return text, json.loads(json.dumps(report, ensure_ascii=False))


def _first_difference(expected: str, actual: str) -> str | None:
    """Where two DXF texts differ by more than the last digits of a number."""
    left, right = expected.split("\n"), actual.split("\n")
    for number, (a, b) in enumerate(zip(left, right), start=1):
        if a == b:
            continue
        try:
            x, y = float(a), float(b)
        except ValueError:
            return f"line {number}: {a!r} != {b!r}"
        # the same number written in another way is a difference
        if x == y or not math.isclose(x, y, rel_tol=1e-9, abs_tol=1e-9):
            return f"line {number}: {a!r} != {b!r}"
    if len(left) != len(right):
        return f"{len(left)} lines != {len(right)} lines"
    return None


def test_there_are_golden_documents() -> None:
    assert len(NAMES) >= 20


@pytest.mark.parametrize("name", NAMES)
def test_the_writer_makes_the_golden_files_of_a_document(name: str) -> None:
    report_path = GOLDEN / f"{name}.report.json"
    reports: dict[str, Any] = {}
    for variant in _variants(name):
        text, report = _write(name, variant)
        reports[variant] = report
        path = GOLDEN / f"{name}.{variant}.dxf"
        if UPDATE:
            path.write_text(text, encoding="utf-8", newline="\n")
            continue
        expected = path.read_text(encoding="utf-8")
        assert _first_difference(expected, text) is None, f"{path.name}"
    if UPDATE:
        report_path.write_text(
            json.dumps(reports, ensure_ascii=False, indent=1) + "\n",
            encoding="utf-8",
            newline="\n",
        )
        return
    assert reports == json.loads(report_path.read_text(encoding="utf-8"))


def test_a_number_may_differ_in_its_last_digits_only() -> None:
    assert _first_difference("10\n1.5\n", "10\n1.5\n") is None
    assert _first_difference("10\n0.30000000000000004\n", "10\n0.3\n") is None
    assert _first_difference("10\n6.123233995736766e-17\n", "10\n0.0\n") is None
    assert _first_difference("10\n1.5\n", "10\n1.50\n") is not None
    assert _first_difference("10\n1.5\n", "10\n1.5001\n") is not None
    assert _first_difference("5\n2A\n", "5\n2B\n") is not None
    assert _first_difference("0\nLINE\n", "0\nLINE\n0\nEOF\n") is not None
