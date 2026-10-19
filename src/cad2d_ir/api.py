from __future__ import annotations

import copy
import json
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from cad2d_ir.codecs.dxf import (
    EntityMapEntry,
    GenericDimensionMode,
    TargetVersion,
    dxf_to_ir,
    ir_to_dxf,
    read_dxf_file,
)
from cad2d_ir.constants import CURRENT_IR_VERSION
from cad2d_ir.diagnostics import ExportDiagnostic
from cad2d_ir.importers.base import (
    ImportDiagnostic,
    ImportOptions,
    ImportResult,
)
from cad2d_ir.importers.registry import import_file
from cad2d_ir.schema import validate_ir


@dataclass(slots=True)
class DxfToIrResult:
    document: dict[str, Any]
    warnings: list[str]
    diagnostics: list[ImportDiagnostic] = field(default_factory=list)

    @property
    def encoding(self) -> str | None:
        """Return the detected file encoding when this result came from a file."""
        source = self.document.get("source")
        if not isinstance(source, dict):
            return None
        metadata = source.get("metadata")
        if not isinstance(metadata, dict):
            return None
        encoding = metadata.get("encoding")
        return str(encoding) if encoding is not None else None


@dataclass(slots=True)
class IrToDxfResult:
    dxf_text: str
    warnings: list[str]
    diagnostics: list[ExportDiagnostic] = field(default_factory=list)
    entity_map: list[EntityMapEntry] = field(default_factory=list)
    target_version: str = "AC1024"


@dataclass(slots=True)
class HpglToDxfResult:
    """One explicitly selected paper page and both stages' diagnostics."""

    imported: ImportResult
    exported: IrToDxfResult
    page: int  # zero based


def convert_hpgl_file_to_dxf(
    path: str | Path,
    *,
    page: int | None = None,
    strict: bool = True,
    profile: Any = None,
    limits: Any = None,
    geometry_limits: Any = None,
    max_entities: int = 200_000,
    target_version: TargetVersion = "AC1024",
) -> HpglToDxfResult:
    """Convert one page through IR; multi-page input requires a zero-based page.

    All pages remain in ``imported.document``. DXF contains only the selected
    sheet in paper space. Its name/paper setup are not reproduced by the writer.
    """
    imported = convert_hpgl_file_to_ir(
        path,
        strict=strict,
        profile=profile,
        limits=limits,
        geometry_limits=geometry_limits,
        max_entities=max_entities,
    )
    layouts = imported.document["layouts"]
    if page is None:
        if len(layouts) != 1:
            raise ValueError(
                "Multi-page HP-GL requires an explicit page (Python: 0 based; CLI: --page 1 based)"
            )
        page = 0
    if (
        isinstance(page, bool)
        or not isinstance(page, int)
        or not 0 <= page < len(layouts)
    ):
        raise ValueError("HP-GL page index is out of range")
    used_blocks = {
        e["block"] for e in layouts[page]["entities"] if e["kind"] == "INSERT"
    }
    document = copy.deepcopy(
        {
            **imported.document,
            "layouts": [layouts[page]],
            "tables": {
                **imported.document["tables"],
                "blocks": {
                    n: b
                    for n, b in imported.document["tables"]["blocks"].items()
                    if n in used_blocks
                },
            },
        }
    )
    document["layouts"][0]["active"] = True
    extra = [
        ExportDiagnostic(
            code="HPGL_DXF_LAYOUT_METADATA_OMITTED",
            severity="warning",
            action="skipped",
            message="DXF retains paper-space coordinates; layout name and paper setup are not serialized by this writer.",
        )
    ]
    if target_version == "AC1024":
        weights = (
            0,
            5,
            9,
            13,
            15,
            18,
            20,
            25,
            30,
            35,
            40,
            50,
            53,
            60,
            70,
            80,
            90,
            100,
            106,
            120,
            140,
            158,
            200,
            211,
        )
        scopes = [
            document["layouts"][0]["entities"],
            *[b["entities"] for b in document["tables"]["blocks"].values()],
        ]
        changed = 0
        for scope in scopes:
            for entity in scope:
                width = entity.get("lineweight_mm", 0.0)
                normalized = min(weights, key=lambda n: abs(n / 100 - width)) / 100
                if abs(width - normalized) > 1e-12:
                    entity["lineweight_mm"] = normalized
                    changed += 1
        if changed:
            extra.append(
                ExportDiagnostic(
                    code="HPGL_DXF_WIDTH_APPROXIMATED",
                    severity="warning",
                    action="approximated",
                    message=f"{changed} pen widths were mapped to supported DXF lineweights; original widths remain in IR.",
                )
            )
    exported = convert_ir_to_dxf_text(document, target_version=target_version)
    exported.diagnostics.extend(extra)
    exported.warnings.extend(d.message for d in extra)
    return HpglToDxfResult(imported, exported, page)


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
    diagnostics = [
        ImportDiagnostic(
            code="DXF_IMPORT_WARNING",
            severity="warning",
            message=warning,
        )
        for warning in warnings
    ]
    return DxfToIrResult(
        document=document,
        warnings=warnings,
        diagnostics=diagnostics,
    )


def convert_dxf_file_to_ir(
    path: str | Path,
    *,
    ir_version: str = CURRENT_IR_VERSION,
    validate: bool = True,
    encoding: str = "auto",
) -> DxfToIrResult:
    warnings: list[str] = []
    diagnostics: list[ImportDiagnostic] = []
    document = read_dxf_file(
        path,
        ir_version=ir_version,
        validate=validate,
        warnings=warnings,
        diagnostics=diagnostics,
        encoding=encoding,
    )
    diagnosed_messages = {diagnostic.message for diagnostic in diagnostics}
    diagnostics.extend(
        ImportDiagnostic(
            code="DXF_IMPORT_WARNING",
            severity="warning",
            message=warning,
        )
        for warning in warnings
        if warning not in diagnosed_messages
    )
    return DxfToIrResult(
        document=document,
        warnings=warnings,
        diagnostics=diagnostics,
    )


def convert_file_to_ir(
    path: str | Path,
    *,
    source_format: str = "auto",
    ir_version: str = CURRENT_IR_VERSION,
    validate: bool = True,
    strict: bool = True,
    curve_segments: int = 96,
    encoding: str = "auto",
    entity_provenance: bool = True,
) -> ImportResult:
    """Convert a supported CAD file to IR using format detection or an explicit adapter.

    ``entity_provenance=False`` omits per-entity ``source``/``metadata`` (JWW),
    roughly halving peak memory for render-only imports of very large drawings.
    """
    return import_file(
        path,
        source_format=source_format,
        options=ImportOptions(
            ir_version=ir_version,
            validate=validate,
            strict=strict,
            curve_segments=curve_segments,
            encoding=encoding,
            entity_provenance=entity_provenance,
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


def convert_jws_file_to_ir(
    path: str | Path,
    *,
    ir_version: str = CURRENT_IR_VERSION,
    validate: bool = True,
    strict: bool = True,
    curve_segments: int = 96,
) -> ImportResult:
    """Convert a JWS registered symbol (Jw_cad 図形登録) to IR."""
    from cad2d_ir.importers.jww import convert_jws_file_to_ir as import_jws

    return import_jws(
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


def convert_dgn_file_to_ir(
    path: str | Path,
    *,
    ir_version: str = CURRENT_IR_VERSION,
    validate: bool = True,
    strict: bool = True,
    curve_segments: int = 96,
    encoding: str = "auto",
) -> ImportResult:
    """Convert a V7 or V8 DGN file directly to IR without an intermediate DXF."""
    from cad2d_ir.importers.dgn import convert_dgn_file_to_ir as import_dgn

    return import_dgn(
        path,
        options=ImportOptions(
            ir_version=ir_version,
            validate=validate,
            strict=strict,
            curve_segments=curve_segments,
            encoding=encoding,
        ),
    )


def convert_dwf_file_to_ir(
    path: str | Path,
    *,
    ir_version: str = CURRENT_IR_VERSION,
    validate: bool = True,
    strict: bool = True,
    curve_segments: int = 96,
) -> ImportResult:
    """Convert a 2D DWF or DWFx file directly to IR."""
    from cad2d_ir.importers.dwf import convert_dwf_file_to_ir as import_dwf

    return import_dwf(
        path,
        options=ImportOptions(
            ir_version=ir_version,
            validate=validate,
            strict=strict,
            curve_segments=curve_segments,
        ),
    )


def convert_exb_file_to_ir(
    path: str | Path,
    *,
    ir_version: str = CURRENT_IR_VERSION,
    validate: bool = True,
    strict: bool = True,
    curve_segments: int = 96,
    entity_provenance: bool = True,
    parse_options: Any = None,
) -> ImportResult:
    """Convert a CAXA EXB drawing (CAXA CAD / 电子图板) directly to IR.

    Requires the ``exb`` extra (``ezexb``). Coordinates are the stored model
    coordinates taken as millimetres; the drawing scale and the paper detected
    from the frame block are in ``header.metadata.exb``. ``parse_options`` is an
    ``ezexb.ParseOptions``; by default the resource budgets are raised well above
    ``ezexb``'s conservative defaults so that production-size drawings read.
    """
    from cad2d_ir.importers.exb import convert_exb_file_to_ir as import_exb

    return import_exb(
        path,
        options=ImportOptions(
            ir_version=ir_version,
            validate=validate,
            strict=strict,
            curve_segments=curve_segments,
            entity_provenance=entity_provenance,
        ),
        parse_options=parse_options,
    )


def convert_mi_file_to_ir(
    path: str | Path,
    *,
    ir_version: str = CURRENT_IR_VERSION,
    validate: bool = True,
    strict: bool = True,
    curve_segments: int = 96,
    encoding: str = "auto",
) -> ImportResult:
    """Convert an MI text file or supported gzip-wrapped BI file directly to IR."""
    from cad2d_ir.importers.mi import convert_mi_file_to_ir as import_mi

    return import_mi(
        path,
        options=ImportOptions(
            ir_version=ir_version,
            validate=validate,
            strict=strict,
            curve_segments=curve_segments,
            encoding=encoding,
        ),
    )


def convert_hpgl_file_to_ir(
    path: str | Path,
    *,
    ir_version: str = CURRENT_IR_VERSION,
    validate: bool = True,
    strict: bool = True,
    profile: Any = None,
    limits: Any = None,
    geometry_limits: Any = None,
    max_entities: int = 200_000,
    entity_provenance: bool = True,
    label_mode: str = "glyph",
    model_page: int | None = None,
    operation_metadata: bool = True,
) -> ImportResult:
    """Import HP-GL/limited PCL/PJL into separate paper layouts in mm.

    Requires cad2d-ir[hpgl]. Profile and resource arguments are ezhpgl objects.
    Approximation and known omission diagnostics remain nonfatal in strict mode.
    ``label_mode="label"`` emits one TEXT per label line; ``model_page`` (zero
    based) promotes that page to the model entities; ``operation_metadata=False``
    omits the per-operation tables from the page metadata.
    """
    from cad2d_ir.importers.hpgl import convert_hpgl_file_to_ir as import_hpgl

    return import_hpgl(
        path,
        options=ImportOptions(
            ir_version=ir_version,
            validate=validate,
            strict=strict,
            entity_provenance=entity_provenance,
        ),
        profile=profile,
        limits=limits,
        geometry_limits=geometry_limits,
        max_entities=max_entities,
        label_mode=label_mode,
        model_page=model_page,
        operation_metadata=operation_metadata,
    )


def convert_idw_file_to_ir(
    path: str | Path,
    *,
    ir_version: str = CURRENT_IR_VERSION,
    validate: bool = True,
    strict: bool = True,
    curve_segments: int = 96,
    entity_provenance: bool = True,
) -> ImportResult:
    """Convert an Autodesk Inventor IDW drawing's saved display directly to IR.

    Requires the ``idw`` extra (``inventor-kit``). Coordinates are sheet paper
    space scaled from Inventor's internal centimetres to millimetres.
    """
    from cad2d_ir.importers.idw import convert_idw_file_to_ir as import_idw

    return import_idw(
        path,
        options=ImportOptions(
            ir_version=ir_version,
            validate=validate,
            strict=strict,
            curve_segments=curve_segments,
            entity_provenance=entity_provenance,
        ),
    )


def convert_pdf_file_to_ir(
    path: str | Path,
    *,
    ir_version: str = CURRENT_IR_VERSION,
    validate: bool = True,
    strict: bool = True,
    entity_provenance: bool = True,
    pages: Sequence[int] | None = None,
    scale: str | float = "auto",
    embed_images: bool = False,
) -> ImportResult:
    """Rebuild the CAD structure of a vector PDF directly as IR.

    Requires the ``pdf`` extra (``pypdfium2``). Coordinates are paper
    millimetres of the page as displayed; with ``scale="auto"`` the plot scale
    is recovered from the dimension texts and applied when enough of them
    agree. ``pages`` selects 1-based pages (default: all, tiled along +X).
    """
    from cad2d_ir.importers.pdf import convert_pdf_file_to_ir as import_pdf

    return import_pdf(
        path,
        options=ImportOptions(
            ir_version=ir_version,
            validate=validate,
            strict=strict,
            entity_provenance=entity_provenance,
        ),
        pages=pages,
        scale=scale,
        embed_images=embed_images,
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
    target_version: TargetVersion = "AC1024",
    curve_segments: int = 96,
    generic_dimensions: GenericDimensionMode = "explode",
) -> IrToDxfResult:
    warnings: list[str] = []
    diagnostics: list[ExportDiagnostic] = []
    entity_map: list[EntityMapEntry] = []
    dxf_text = ir_to_dxf(
        document,
        validate=validate,
        warnings=warnings,
        diagnostics=diagnostics,
        entity_map=entity_map,
        target_version=target_version,
        curve_segments=curve_segments,
        generic_dimensions=generic_dimensions,
    )
    return IrToDxfResult(
        dxf_text=dxf_text,
        warnings=warnings,
        diagnostics=diagnostics,
        entity_map=entity_map,
        target_version=target_version,
    )


def convert_ir_file_to_dxf(
    path: str | Path,
    *,
    validate: bool = True,
    target_version: TargetVersion = "AC1024",
    curve_segments: int = 96,
    generic_dimensions: GenericDimensionMode = "explode",
) -> IrToDxfResult:
    document = load_ir_json(path, validate=validate)
    return convert_ir_to_dxf_text(
        document,
        validate=validate,
        target_version=target_version,
        curve_segments=curve_segments,
        generic_dimensions=generic_dimensions,
    )


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
