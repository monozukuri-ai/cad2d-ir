"""Source format detection and importer dispatch."""

from __future__ import annotations

from collections import Counter
from pathlib import Path

from cad2d_ir.codecs.dxf import read_dxf_file
from cad2d_ir.importers.base import (
    ImportDiagnostic,
    ImportOptions,
    ImportResult,
    UnsupportedSourceFormatError,
)
from cad2d_ir.model3d import Model3DCollector

_SUFFIX_TO_FORMAT = {
    ".dxf": "dxf",
    ".dwg": "dwg",
    ".dgn": "dgn",
    ".dwf": "dwf",
    ".dwfx": "dwf",
    ".edrw": "edrw",
    ".exb": "exb",
    ".idw": "idw",
    ".hpgl": "hpgl",
    ".hpg": "hpgl",
    ".icd": "icd",
    ".plt": "hpgl",
    ".pcl": "hpgl",
    ".jww": "jww",
    ".jws": "jws",
    ".mi": "mi",
    ".bi": "mi",
    ".p21": "sxf",
    ".pdf": "pdf",
    ".sfc": "sxf",
    ".sxf": "sxf",
}


def detect_source_format(path: str | Path) -> str:
    """Detect a known source format from a filename suffix."""
    source_path = Path(path)
    source_format = _SUFFIX_TO_FORMAT.get(source_path.suffix.lower())
    if source_format is None:
        suffix = source_path.suffix or "<none>"
        raise UnsupportedSourceFormatError(
            f"Could not detect a supported CAD format from suffix {suffix!r}; "
            "specify an explicit source format."
        )
    return source_format


def import_file(
    path: str | Path,
    *,
    source_format: str = "auto",
    options: ImportOptions | None = None,
) -> ImportResult:
    """Import a CAD file through the registered format adapter."""
    source_path = Path(path)
    normalized_format = source_format.lower()
    if normalized_format == "auto":
        normalized_format = detect_source_format(source_path)

    import_options = options or ImportOptions()
    if normalized_format == "dxf":
        warnings: list[str] = []
        diagnostics: list[ImportDiagnostic] = []
        collector = (
            Model3DCollector(curve_segments=import_options.curve_segments)
            if import_options.keep_3d
            else None
        )
        document = read_dxf_file(
            source_path,
            ir_version=import_options.ir_version,
            validate=import_options.validate,
            warnings=warnings,
            diagnostics=diagnostics,
            encoding=import_options.encoding,
            model3d=collector,
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
        entity_counts = Counter(
            str(entity.get("kind", "UNKNOWN")) for entity in document["entities"]
        )
        metadata = document.get("source", {}).get("metadata", {})
        skipped = sum(
            diagnostic.code in {"DXF_IMPORT_WARNING", "DXF_ENTITY_CONVERSION_FAILED"}
            for diagnostic in diagnostics
        )
        return ImportResult(
            document=document,
            diagnostics=diagnostics,
            model3d=(
                collector.finalize(units=document.get("header", {}).get("units"))
                if collector is not None
                else None
            ),
            statistics={
                "source_format": "dxf",
                "encoding": metadata.get("encoding"),
                "encoding_source": metadata.get("encoding_source"),
                "decode_replacement_characters": metadata.get(
                    "decode_replacement_characters", 0
                ),
                "decode_replacement_lines": metadata.get("decode_replacement_lines", 0),
                "converted_entities": len(document["entities"]),
                "converted_entity_counts": dict(sorted(entity_counts.items())),
                "skipped_entities": skipped,
            },
        )

    if normalized_format == "jww":
        from cad2d_ir.importers.jww import convert_jww_file_to_ir

        return convert_jww_file_to_ir(source_path, options=import_options)

    if normalized_format == "jws":
        from cad2d_ir.importers.jww import convert_jws_file_to_ir

        return convert_jws_file_to_ir(source_path, options=import_options)

    if normalized_format == "dwg":
        from cad2d_ir.importers.dwg import convert_dwg_file_to_ir

        return convert_dwg_file_to_ir(source_path, options=import_options)

    if normalized_format == "dgn":
        from cad2d_ir.importers.dgn import convert_dgn_file_to_ir

        return convert_dgn_file_to_ir(source_path, options=import_options)

    if normalized_format == "dwf":
        from cad2d_ir.importers.dwf import convert_dwf_file_to_ir

        return convert_dwf_file_to_ir(source_path, options=import_options)

    if normalized_format == "exb":
        from cad2d_ir.importers.exb import convert_exb_file_to_ir

        return convert_exb_file_to_ir(source_path, options=import_options)

    if normalized_format == "hpgl":
        from cad2d_ir.importers.hpgl import convert_hpgl_file_to_ir

        return convert_hpgl_file_to_ir(source_path, options=import_options)

    if normalized_format == "icd":
        from cad2d_ir.importers.icad import convert_icad_file_to_ir

        return convert_icad_file_to_ir(source_path, options=import_options)

    if normalized_format == "edrw":
        from cad2d_ir.importers.edrawings import convert_edrw_file_to_ir

        return convert_edrw_file_to_ir(source_path, options=import_options)

    if normalized_format == "idw":
        from cad2d_ir.importers.idw import convert_idw_file_to_ir

        return convert_idw_file_to_ir(source_path, options=import_options)

    if normalized_format == "mi":
        from cad2d_ir.importers.mi import convert_mi_file_to_ir

        return convert_mi_file_to_ir(source_path, options=import_options)

    if normalized_format == "sxf":
        from cad2d_ir.importers.sxf import convert_sxf_file_to_ir

        return convert_sxf_file_to_ir(source_path, options=import_options)

    if normalized_format == "pdf":
        from cad2d_ir.importers.pdf import convert_pdf_file_to_ir

        return convert_pdf_file_to_ir(source_path, options=import_options)

    raise UnsupportedSourceFormatError(f"Unsupported source format: {source_format!r}")
