"""Stable diagnostic contracts and the public diagnostic-code catalog."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

DiagnosticSeverity = Literal["info", "warning", "error"]
ExportAction = Literal["skipped", "approximated", "exploded", "normalized"]


@dataclass(frozen=True, slots=True)
class ExportDiagnostic:
    """A structured message emitted while exporting an IR document."""

    code: str
    severity: DiagnosticSeverity
    message: str
    entity_id: str | None = None
    action: ExportAction | None = None

    def as_dict(self) -> dict[str, str]:
        """Return a JSON-serializable representation without empty fields."""
        result = {
            "code": self.code,
            "severity": self.severity,
            "message": self.message,
        }
        if self.entity_id is not None:
            result["entity_id"] = self.entity_id
        if self.action is not None:
            result["action"] = self.action
        return result


@dataclass(frozen=True, slots=True)
class DiagnosticCode:
    """Catalog metadata for one stable diagnostic identifier."""

    severity: DiagnosticSeverity
    action: str | None
    adapter: str
    condition: str


DIAGNOSTIC_CODES: dict[str, DiagnosticCode] = {
    "DXF_IMPORT_WARNING": DiagnosticCode(
        "warning", None, "DXF import", "Legacy wrapper for a DXF parser warning."
    ),
    "DXF_ENCODING_DETECTED": DiagnosticCode(
        "info", "detected", "DXF import", "The file encoding was selected."
    ),
    "DXF_DECODE_REPLACED": DiagnosticCode(
        "warning",
        "normalized",
        "DXF import",
        "Undecodable input bytes were replaced while decoding.",
    ),
    "JWW_ENTITY_CONVERSION_FAILED": DiagnosticCode(
        "error", "skipped", "JWW import", "A malformed JWW entity was skipped."
    ),
    "JWW_ZERO_FLATNESS_NORMALIZED": DiagnosticCode(
        "warning", "normalized", "JWW import", "A zero ellipse flatness was replaced."
    ),
    "JWW_TEXT_HEIGHT_DEFAULTED": DiagnosticCode(
        "warning",
        "normalized",
        "JWW import",
        "A non-positive text height was replaced.",
    ),
    "JWW_ZERO_BLOCK_SCALE_NORMALIZED": DiagnosticCode(
        "warning", "normalized", "JWW import", "A zero block scale was replaced."
    ),
    "JWW_UNSUPPORTED_ENTITY": DiagnosticCode(
        "warning", "skipped", "JWW import", "An unsupported JWW entity was skipped."
    ),
    "JWW_CURVE_APPROXIMATED": DiagnosticCode(
        "warning", "approximated", "JWW import", "Source geometry was approximated."
    ),
    "JWW_UNRESOLVED_BLOCK_REFERENCE": DiagnosticCode(
        "warning", None, "JWW import", "A referenced JWW block was not resolved."
    ),
    "DWG_ENTITY_CONVERSION_FAILED": DiagnosticCode(
        "error", "skipped", "DWG import", "A malformed DWG entity was skipped."
    ),
    "DWG_ZERO_INSERT_SCALE_NORMALIZED": DiagnosticCode(
        "warning", "normalized", "DWG import", "A zero insert scale was replaced."
    ),
    "DWG_MINSERT_ARRAY_PRESERVED": DiagnosticCode(
        "warning",
        "preserved_metadata",
        "DWG import",
        "MINSERT array data was preserved as metadata.",
    ),
    "DWG_UNRESOLVED_BLOCK_REFERENCE": DiagnosticCode(
        "warning", None, "DWG import", "A referenced DWG block was not resolved."
    ),
    "DWG_UNSUPPORTED_ENTITY": DiagnosticCode(
        "warning", "skipped", "DWG import", "An unsupported DWG entity was skipped."
    ),
    "DWG_CURVE_APPROXIMATED": DiagnosticCode(
        "warning", "approximated", "DWG import", "Source geometry was approximated."
    ),
    "DWG_NONPLANAR_PROJECTED": DiagnosticCode(
        "warning", "projected", "DWG import", "Non-planar geometry was projected to XY."
    ),
    "SXF_DIMENSION_CONVERSION_FAILED": DiagnosticCode(
        "error", "skipped", "SXF import", "A malformed SXF dimension was skipped."
    ),
    "SXF_PRIMITIVE_CONVERSION_FAILED": DiagnosticCode(
        "error", "skipped", "SXF import", "A malformed SXF primitive was skipped."
    ),
    "SXF_DRAWING_WARNING": DiagnosticCode(
        "warning", None, "SXF import", "The SXF drawing backend reported a warning."
    ),
    "SXF_CURVE_APPROXIMATED": DiagnosticCode(
        "warning", "approximated", "SXF import", "Source geometry was approximated."
    ),
    "SXF_PRIMITIVE_SKIPPED": DiagnosticCode(
        "warning", "skipped", "SXF import", "An unsupported SXF primitive was skipped."
    ),
    "SXF_P21_SEMANTICS_FLATTENED": DiagnosticCode(
        "warning",
        "flattened",
        "SXF import",
        "P21 semantics were flattened to drawing primitives.",
    ),
    "DXF_CONSTRAINTS_OMITTED": DiagnosticCode(
        "warning", "skipped", "DXF export", "IR-only constraints were omitted."
    ),
    "DXF_DIMENSION_BLOCK_GENERATED": DiagnosticCode(
        "info",
        "normalized",
        "DXF export",
        "A required DIMENSION geometry block was generated.",
    ),
    "DXF_INSERT_TRANSFORM_OMITTED": DiagnosticCode(
        "warning", "skipped", "DXF export", "An affine INSERT transform was omitted."
    ),
    "DXF_UNKNOWN_ENTITY_SKIPPED": DiagnosticCode(
        "warning", "skipped", "DXF export", "An unknown IR entity kind was skipped."
    ),
    "DXF_GENERIC_DIMENSION_EXPLODED": DiagnosticCode(
        "info",
        "exploded",
        "DXF export",
        "A GENERIC dimension was expanded to primitives.",
    ),
    "DXF_GENERIC_DIMENSION_SKIPPED": DiagnosticCode(
        "warning",
        "skipped",
        "DXF export",
        "A GENERIC dimension was intentionally omitted.",
    ),
    "DXF_GENERIC_DIMENSION_GEOMETRY_MISSING": DiagnosticCode(
        "warning",
        "skipped",
        "DXF export",
        "A GENERIC dimension had no renderable geometry.",
    ),
    "DXF_GENERIC_DIMENSION_DEFAULTED": DiagnosticCode(
        "warning",
        "normalized",
        "DXF export",
        "Missing GENERIC dimension text placement was defaulted.",
    ),
    "DXF_TABLE_DEFAULTED": DiagnosticCode(
        "info", "normalized", "DXF export", "A required table record was synthesized."
    ),
    "DXF_R12_LWPOLYLINE_EXPLODED": DiagnosticCode(
        "info",
        "exploded",
        "DXF R12 export",
        "LWPOLYLINE was emitted as POLYLINE records.",
    ),
    "DXF_R12_MTEXT_EXPLODED": DiagnosticCode(
        "warning",
        "exploded",
        "DXF R12 export",
        "MTEXT was emitted as one or more TEXT entities.",
    ),
    "DXF_R12_MTEXT_FORMATTING_NORMALIZED": DiagnosticCode(
        "warning",
        "normalized",
        "DXF R12 export",
        "MTEXT formatting codes were removed.",
    ),
    "DXF_R12_ELLIPSE_APPROXIMATED": DiagnosticCode(
        "warning",
        "approximated",
        "DXF R12 export",
        "ELLIPSE was approximated by a polyline.",
    ),
    "DXF_R12_SPLINE_APPROXIMATED": DiagnosticCode(
        "warning",
        "approximated",
        "DXF R12 export",
        "SPLINE was approximated by a polyline.",
    ),
    "DXF_R12_HATCH_EXPLODED": DiagnosticCode(
        "warning",
        "exploded",
        "DXF R12 export",
        "HATCH was emitted as boundary polylines.",
    ),
    "DXF_R12_TRUE_COLOR_APPROXIMATED": DiagnosticCode(
        "warning",
        "approximated",
        "DXF R12 export",
        "True color was approximated by an ACI color.",
    ),
    "DXF_R12_LINEWEIGHT_OMITTED": DiagnosticCode(
        "warning",
        "skipped",
        "DXF R12 export",
        "Lineweight was omitted from R12 output.",
    ),
    "DXF_R12_INSUNITS_OMITTED": DiagnosticCode(
        "warning", "skipped", "DXF R12 export", "INSUNITS was omitted from R12 output."
    ),
}

ALL_CODES: tuple[str, ...] = tuple(sorted(DIAGNOSTIC_CODES))


def diagnostic_code_details(code: str) -> dict[str, Any]:
    """Return JSON-friendly catalog metadata for *code*."""
    definition = DIAGNOSTIC_CODES[code]
    return {
        "code": code,
        "severity": definition.severity,
        "action": definition.action,
        "adapter": definition.adapter,
        "condition": definition.condition,
    }
