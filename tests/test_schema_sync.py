from __future__ import annotations

import json
from pathlib import Path


def test_packaged_schema_matches_root_schema() -> None:
    root = Path(__file__).resolve().parents[1]
    canonical = json.loads((root / "ir_schema.json").read_text(encoding="utf-8"))
    packaged = json.loads(
        (root / "src" / "cad2d_ir" / "data" / "ir_schema.json").read_text(
            encoding="utf-8"
        )
    )
    assert canonical == packaged
