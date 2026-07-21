"""Public importer contracts and registry functions."""

from cad2d_ir.importers.base import (
    DiagnosticSeverity,
    ImportDiagnostic,
    ImporterError,
    ImportOptions,
    ImportResult,
    MissingOptionalDependencyError,
    UnsupportedSourceFormatError,
)
from cad2d_ir.importers.registry import detect_source_format, import_file

__all__ = [
    "DiagnosticSeverity",
    "ImportDiagnostic",
    "ImporterError",
    "ImportOptions",
    "ImportResult",
    "MissingOptionalDependencyError",
    "UnsupportedSourceFormatError",
    "detect_source_format",
    "import_file",
]
