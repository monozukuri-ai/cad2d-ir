"""Shared contracts for source-format importers."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from cad2d_ir.constants import CURRENT_IR_VERSION
from cad2d_ir.diagnostics import DiagnosticSeverity


class ImporterError(ValueError):
    """Base class for importer failures."""


class UnsupportedSourceFormatError(ImporterError):
    """Raised when no importer is registered for a source format."""


class MissingOptionalDependencyError(ImporterError, ImportError):
    """Raised when an importer-specific optional dependency is unavailable."""


@dataclass(frozen=True, slots=True)
class ImportDiagnostic:
    """A structured message emitted while importing source CAD data."""

    code: str
    severity: DiagnosticSeverity
    message: str
    source_id: str | None = None
    source_kind: str | None = None
    action: str | None = None
    details: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable representation without empty fields."""
        result = {
            "code": self.code,
            "severity": self.severity,
            "message": self.message,
        }
        if self.source_id is not None:
            result["source_id"] = self.source_id
        if self.source_kind is not None:
            result["source_kind"] = self.source_kind
        if self.action is not None:
            result["action"] = self.action
        if self.details is not None:
            result["details"] = self.details
        return result


@dataclass(frozen=True, slots=True)
class ImportOptions:
    """Options shared by file importers."""

    ir_version: str = CURRENT_IR_VERSION
    validate: bool = True
    strict: bool = True
    curve_segments: int = 96
    encoding: str = "auto"

    def __post_init__(self) -> None:
        if not 8 <= self.curve_segments <= 4096:
            raise ValueError("curve_segments must be in [8, 4096]")


@dataclass(slots=True)
class ImportResult:
    """Imported IR document plus diagnostics and conversion statistics."""

    document: dict[str, Any]
    diagnostics: list[ImportDiagnostic] = field(default_factory=list)
    statistics: dict[str, Any] = field(default_factory=dict)

    @property
    def warnings(self) -> list[str]:
        """Return warning/error messages for callers using the older result style."""
        return [
            diagnostic.message
            for diagnostic in self.diagnostics
            if diagnostic.severity in {"warning", "error"}
        ]
