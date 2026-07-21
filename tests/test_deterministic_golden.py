from __future__ import annotations

import json
from pathlib import Path

from cad2d_ir import ir_to_dxf


def test_minimal_ac1024_output_matches_golden_file() -> None:
    root = Path(__file__).resolve().parents[1]
    document = json.loads(
        (root / "examples" / "ir" / "minimal.json").read_text(encoding="utf-8")
    )
    expected = (root / "tests" / "golden" / "minimal_ac1024.dxf").read_text(
        encoding="utf-8"
    )

    assert ir_to_dxf(document, target_version="AC1024") == expected
