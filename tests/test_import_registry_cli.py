from __future__ import annotations

import json
from pathlib import Path

import pytest

from cad2d_ir import convert_file_to_ir, detect_source_format
from cad2d_ir.cli import main
from cad2d_ir.importers import UnsupportedSourceFormatError


def test_detect_source_format_is_case_insensitive() -> None:
    assert detect_source_format("drawing.DXF") == "dxf"
    assert detect_source_format("drawing.JWW") == "jww"
    assert detect_source_format("drawing.DWG") == "dwg"
    assert detect_source_format("drawing.SFC") == "sxf"

    with pytest.raises(UnsupportedSourceFormatError):
        detect_source_format("drawing.bin")


def test_generic_file_import_and_cli_support_dxf(tmp_path: Path) -> None:
    source = tmp_path / "line.dxf"
    output = tmp_path / "line.json"
    source.write_text(
        "0\nSECTION\n2\nENTITIES\n0\nLINE\n10\n0\n20\n0\n11\n1\n21\n1\n0\nENDSEC\n0\nEOF\n",
        encoding="utf-8",
    )

    result = convert_file_to_ir(source)
    assert result.document["version"] == "0.2.0"
    assert result.document["source"] == {"format": "dxf", "name": "line.dxf"}
    assert result.statistics["converted_entity_counts"] == {"LINE": 1}

    assert main(["import", str(source), "-o", str(output), "--pretty"]) == 0
    written = json.loads(output.read_text(encoding="utf-8"))
    assert written["entities"][0]["kind"] == "LINE"


def test_registry_dispatches_dwg_and_sxf_adapters(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cad2d_ir.importers import dwg, sxf

    calls: list[tuple[str, Path]] = []

    def fake_dwg(path: Path, *, options: object) -> str:
        calls.append(("dwg", path))
        return "dwg-result"

    def fake_sxf(path: Path, *, options: object) -> str:
        calls.append(("sxf", path))
        return "sxf-result"

    monkeypatch.setattr(dwg, "convert_dwg_file_to_ir", fake_dwg)
    monkeypatch.setattr(sxf, "convert_sxf_file_to_ir", fake_sxf)

    dwg_path = tmp_path / "drawing.dwg"
    sxf_path = tmp_path / "drawing.p21"
    assert convert_file_to_ir(dwg_path) == "dwg-result"
    assert convert_file_to_ir(sxf_path) == "sxf-result"
    assert calls == [("dwg", dwg_path), ("sxf", sxf_path)]
