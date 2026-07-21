from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any

from cad2d_ir.codecs.dxf import dxf_to_ir, ir_to_dxf, read_dxf_file
from cad2d_ir.constants import CURRENT_IR_VERSION
from cad2d_ir.importers.base import ImportOptions, ImportResult
from cad2d_ir.importers.registry import import_file
from cad2d_ir.schema import validate_ir


@dataclass(slots=True)
class DxfToIrResult:
    document: dict[str, Any]
    warnings: list[str]


@dataclass(slots=True)
class IrToDxfResult:
    dxf_text: str
    warnings: list[str]


def convert_dxf_text_to_ir(
    dxf_text: str,
    *,
    ir_version: str = CURRENT_IR_VERSION,
    validate: bool = True,
) -> DxfToIrResult:
    warnings: list[str] = []
    document = dxf_to_ir(
        dxf_text,
        ir_version=ir_version,
        validate=validate,
        warnings=warnings,
    )
    return DxfToIrResult(document=document, warnings=warnings)


def convert_dxf_file_to_ir(
    path: str | Path,
    *,
    ir_version: str = CURRENT_IR_VERSION,
    validate: bool = True,
) -> DxfToIrResult:
    warnings: list[str] = []
    document = read_dxf_file(
        path,
        ir_version=ir_version,
        validate=validate,
        warnings=warnings,
    )
    return DxfToIrResult(document=document, warnings=warnings)


def convert_file_to_ir(
    path: str | Path,
    *,
    source_format: str = "auto",
    ir_version: str = CURRENT_IR_VERSION,
    validate: bool = True,
    strict: bool = True,
    curve_segments: int = 96,
) -> ImportResult:
    """Convert a supported CAD file to IR using format detection or an explicit adapter."""
    return import_file(
        path,
        source_format=source_format,
        options=ImportOptions(
            ir_version=ir_version,
            validate=validate,
            strict=strict,
            curve_segments=curve_segments,
        ),
    )


def convert_jww_file_to_ir(
    path: str | Path,
    *,
    ir_version: str = CURRENT_IR_VERSION,
    validate: bool = True,
    strict: bool = True,
    curve_segments: int = 96,
) -> ImportResult:
    """Convert a JWW file directly to IR without an intermediate DXF representation."""
    from cad2d_ir.importers.jww import convert_jww_file_to_ir as import_jww

    return import_jww(
        path,
        options=ImportOptions(
            ir_version=ir_version,
            validate=validate,
            strict=strict,
            curve_segments=curve_segments,
        ),
    )


def convert_dwg_file_to_ir(
    path: str | Path,
    *,
    ir_version: str = CURRENT_IR_VERSION,
    validate: bool = True,
    strict: bool = True,
    curve_segments: int = 96,
) -> ImportResult:
    """Convert a DWG file directly to IR without an intermediate DXF."""
    from cad2d_ir.importers.dwg import convert_dwg_file_to_ir as import_dwg

    return import_dwg(
        path,
        options=ImportOptions(
            ir_version=ir_version,
            validate=validate,
            strict=strict,
            curve_segments=curve_segments,
        ),
    )


def convert_sxf_file_to_ir(
    path: str | Path,
    *,
    ir_version: str = CURRENT_IR_VERSION,
    validate: bool = True,
    strict: bool = True,
    curve_segments: int = 96,
) -> ImportResult:
    """Convert an SXF SFC/P21 file directly to IR without an intermediate DXF."""
    from cad2d_ir.importers.sxf import convert_sxf_file_to_ir as import_sxf

    return import_sxf(
        path,
        options=ImportOptions(
            ir_version=ir_version,
            validate=validate,
            strict=strict,
            curve_segments=curve_segments,
        ),
    )


def convert_ir_to_dxf_text(
    document: dict[str, Any],
    *,
    validate: bool = True,
) -> IrToDxfResult:
    warnings: list[str] = []
    dxf_text = ir_to_dxf(document, validate=validate, warnings=warnings)
    return IrToDxfResult(dxf_text=dxf_text, warnings=warnings)


def convert_ir_file_to_dxf(
    path: str | Path,
    *,
    validate: bool = True,
) -> IrToDxfResult:
    document = load_ir_json(path, validate=validate)
    return convert_ir_to_dxf_text(document, validate=validate)


def load_ir_json(path: str | Path, *, validate: bool = True) -> dict[str, Any]:
    document = json.loads(Path(path).read_text(encoding="utf-8"))
    if validate:
        validate_ir(document)
    return document


def dump_ir_json(
    document: dict[str, Any],
    path: str | Path,
    *,
    pretty: bool = True,
    validate: bool = True,
) -> None:
    if validate:
        validate_ir(document)
    indent = 2 if pretty else None
    text = json.dumps(document, ensure_ascii=False, indent=indent)
    if pretty:
        text += "\n"
    Path(path).write_text(text, encoding="utf-8")
