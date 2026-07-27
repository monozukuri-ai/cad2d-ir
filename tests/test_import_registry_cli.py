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
    assert detect_source_format("drawing.DGN") == "dgn"
    assert detect_source_format("drawing.DWF") == "dwf"
    assert detect_source_format("drawing.DWFX") == "dwf"
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
    assert result.document["source"]["format"] == "dxf"
    assert result.document["source"]["name"] == "line.dxf"
    assert result.document["source"]["metadata"]["encoding"] == "utf-8"
    assert result.statistics["converted_entity_counts"] == {"LINE": 1}

    assert main(["import", str(source), "-o", str(output), "--pretty"]) == 0
    written = json.loads(output.read_text(encoding="utf-8"))
    assert written["entities"][0]["kind"] == "LINE"


def test_registry_dispatches_optional_adapters(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from cad2d_ir.importers import dgn, dwf, dwg, sxf

    calls: list[tuple[str, Path]] = []

    def fake_dwg(path: Path, *, options: object) -> str:
        calls.append(("dwg", path))
        return "dwg-result"

    def fake_sxf(path: Path, *, options: object) -> str:
        calls.append(("sxf", path))
        return "sxf-result"

    def fake_dgn(path: Path, *, options: object) -> str:
        calls.append(("dgn", path))
        return "dgn-result"

    def fake_dwf(path: Path, *, options: object) -> str:
        calls.append(("dwf", path))
        return "dwf-result"

    monkeypatch.setattr(dwg, "convert_dwg_file_to_ir", fake_dwg)
    monkeypatch.setattr(dgn, "convert_dgn_file_to_ir", fake_dgn)
    monkeypatch.setattr(dwf, "convert_dwf_file_to_ir", fake_dwf)
    monkeypatch.setattr(sxf, "convert_sxf_file_to_ir", fake_sxf)

    dwg_path = tmp_path / "drawing.dwg"
    dgn_path = tmp_path / "drawing.dgn"
    dwf_path = tmp_path / "drawing.dwfx"
    sxf_path = tmp_path / "drawing.p21"
    assert convert_file_to_ir(dwg_path) == "dwg-result"
    assert convert_file_to_ir(dgn_path) == "dgn-result"
    assert convert_file_to_ir(dwf_path) == "dwf-result"
    assert convert_file_to_ir(sxf_path) == "sxf-result"
    assert calls == [
        ("dwg", dwg_path),
        ("dgn", dgn_path),
        ("dwf", dwf_path),
        ("sxf", sxf_path),
    ]


def test_ir2dxf_cli_accepts_target_version_and_dimension_mode(
    tmp_path: Path,
) -> None:
    source = tmp_path / "drawing.json"
    output = tmp_path / "drawing-r12.dxf"
    source.write_text(
        json.dumps(
            {
                "format": "cad2d-ir",
                "version": "0.2.0",
                "header": {
                    "units": "mm",
                    "angle_unit": "deg",
                    "coord_space": "world",
                },
                "entities": [
                    {
                        "id": "E1",
                        "kind": "LWPOLYLINE",
                        "vertices": [[0, 0], [1, 0], [1, 1]],
                    },
                    {
                        "id": "T1",
                        "kind": "TEXT",
                        "layer": "日本語レイヤ",
                        "insert": [0, 0],
                        "height": 1,
                        "text": "寸法",
                    },
                ],
            }
        ),
        encoding="utf-8",
    )

    assert (
        main(
            [
                "ir2dxf",
                str(source),
                "-o",
                str(output),
                "--target-version",
                "AC1009",
                "--generic-dimensions",
                "explode",
                "--encoding",
                "cp932",
            ]
        )
        == 0
    )
    raw = output.read_bytes()
    dxf_text = raw.decode("cp932")
    assert "日本語レイヤ".encode("utf-8") not in raw
    assert "$ACADVER\n1\nAC1009" in dxf_text
    assert "日本語レイヤ" in dxf_text
    assert "寸法" in dxf_text
    assert "$DWGCODEPAGE\n3\nANSI_932" in dxf_text
    assert "\nPOLYLINE\n" in dxf_text
    assert "\nLWPOLYLINE\n" not in dxf_text
