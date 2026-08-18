from __future__ import annotations

import ast
from pathlib import Path

from cad2d_ir.diagnostics import ALL_CODES, DIAGNOSTIC_CODES


def _diagnostic_codes_used_in_source() -> set[str]:
    root = Path(__file__).resolve().parents[1] / "src" / "cad2d_ir"
    result: set[str] = set()
    for path in root.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            function = node.func
            name = function.id if isinstance(function, ast.Name) else None
            if name not in {"ImportDiagnostic", "_diagnose", "_import_diagnose"}:
                continue
            code_keyword = next(
                (keyword for keyword in node.keywords if keyword.arg == "code"),
                None,
            )
            if (
                code_keyword is not None
                and isinstance(code_keyword.value, ast.Constant)
                and isinstance(code_keyword.value.value, str)
            ):
                result.add(code_keyword.value.value)
    return result


def test_diagnostic_catalog_covers_all_source_codes() -> None:
    assert tuple(sorted(set(ALL_CODES))) == ALL_CODES
    assert set(ALL_CODES) == set(DIAGNOSTIC_CODES)
    assert _diagnostic_codes_used_in_source() <= set(ALL_CODES)


def test_diagnostic_catalog_document_lists_every_public_code() -> None:
    path = Path(__file__).resolve().parents[1] / "docs" / "DIAGNOSTICS.md"
    text = path.read_text(encoding="utf-8")

    for code in ALL_CODES:
        assert f"`{code}`" in text
