"""JWW import with the entities converted in native code.

What comes from the header (layers, linetypes, pens, metadata, the document
level diagnostics) is made by the functions of ``cad2d_ir.importers.jww``; the
entities are converted by ``cad2d-ir-jww`` and stay in the typed IR. The result
is the one ``convert_jww_file_to_ir`` gives, with a :class:`NativeDocument` in
place of the document ``dict`` (``result.document.to_dict()`` is that ``dict``).

Not handled here, and left to the Python importer by raising ``Unsupported``:
files whose strings the parser read as UTF-8, an embedded image that does not
decompress, entities on a layer the header does not have, and JWS symbols.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from cad2d_ir.importers import jww as _reference
from cad2d_ir.importers.base import ImportDiagnostic, ImporterError, ImportOptions

from . import _core as _own_core
from ._core import NativeDocument


@dataclass(slots=True)
class NativeImportResult:
    """``ImportResult`` with the document held in native code."""

    document: NativeDocument
    diagnostics: list[ImportDiagnostic]
    statistics: dict[str, Any]


def convert_jww_file(
    path: str | Path,
    *,
    options: ImportOptions | None = None,
    core: Any = None,
) -> NativeImportResult:
    """Read a JWW file and convert it to the typed IR.

    The document level of this function follows ``_jw_document_to_ir`` of the
    Python importer line by line; a change there has to be made here as well
    until the two share it.

    ``core`` is the extension module whose bindings do the work: the one of
    this package, or the module of an application that registers the bindings
    of ``cad2d-ir-py`` next to native code of its own. The document of the
    result and the ``Unsupported`` raised are those of that module.
    """
    import ezjww

    bindings = _own_core if core is None else core
    unsupported = bindings.Unsupported
    import_options = options or ImportOptions()
    source_path = Path(path)
    header = _reference._mapping(ezjww.read_header(str(source_path)), "header")
    parsed = bindings.parse_jww(str(source_path))
    # what the parser left: characters it replaced, a list it could not read to
    # its end. The header above is read on its own, as CP932; a file read as
    # UTF-8 has other strings in the header of the document.
    parser_diagnostics = list(parsed.diagnostics())
    if any(row.get("code") == "TEXT_ENCODING_DETECTED" for row in parser_diagnostics):
        raise unsupported("a file whose strings were read as UTF-8")

    layers, layer_names = _reference._build_layers(header)
    block_defs = parsed.block_defs()
    block_names = _reference._build_block_names(block_defs)
    linetype_names, linetype_table, linetypes_from_file = (
        _reference._document_line_types(header)
    )
    pen_table = _reference._pen_table(header)
    context = _reference._ConversionContext(
        options=import_options,
        layers=layers,
        layer_names=layer_names,
        block_names=block_names,
        linetype_names=linetype_names,
        source_format="jww",
        group_scales=None,
        pen_widths=_reference._pen_widths_mm(pen_table),
        pen_colors=_reference._pen_colors(pen_table),
    )

    source_entity_count = parsed.entity_count()
    source_counts = {str(key): int(value) for key, value in parsed.entity_counts().items()}
    unresolved = parsed.unresolved_def_numbers()
    try:
        converted = parsed.convert(
            label=context.label,
            source_format="jww",
            entity_provenance=import_options.entity_provenance,
            strict=import_options.strict,
            curve_segments=import_options.curve_segments,
            layer_names=layer_names,
            block_names=block_names,
            linetype_names=linetype_names,
            pen_widths=context.pen_widths,
            pen_colors=context.pen_colors,
        )
    except unsupported:
        raise
    except ValueError as exc:
        raise ImporterError(str(exc)) from exc

    context.diagnostics.extend(
        ImportDiagnostic(
            code=code,
            severity=severity,
            message=message,
            source_id=source_id,
            source_kind=source_kind,
            action=action,
        )
        for code, severity, message, source_id, source_kind, action in converted.diagnostics()
    )
    context.converted_counts = Counter(converted.converted_counts())
    context.skipped_counts = Counter(converted.skipped_counts())
    context.approximation_counts = Counter(converted.approximation_counts())
    context.preserved_dimensions = converted.preserved_dimensions()
    context.pen_width_entities = converted.pen_width_entities()
    context.used_linetypes = set(converted.used_linetypes())
    metadata_settings = [
        {"entity_index": index, "key": key, "value": value, "raw": raw}
        for index, key, value, raw in converted.metadata_settings()
    ]
    text_styles = {font: {"font": font} for font in converted.fonts()}

    tables: dict[str, Any] = {
        "layers": context.layers,
        "linetypes": _reference._used_linetype_table(
            linetype_table, context.used_linetypes
        ),
        "text_styles": text_styles,
    }
    block_entity_counts = converted.block_entity_counts()
    block_shells: list[tuple[str, dict[str, Any]]] = []
    block_sizes: dict[str, int] = {}
    for block_def, entity_count in zip(block_defs, block_entity_counts, strict=True):
        number = int(block_def.get("number", 0))
        name = context.block_names[number]
        block_sizes[name] = entity_count
        block_shells.append(
            (
                name,
                {
                    "base_point": [0.0, 0.0],
                    "metadata": {
                        "jww": {
                            "number": number,
                            "is_referenced": bool(block_def.get("is_referenced", False)),
                            "base": dict(
                                _reference._mapping(block_def.get("base", {}), "block base")
                            ),
                        }
                    },
                },
            )
        )

    raw_version = int(header.get("version", 0))
    source: dict[str, Any] = {
        "format": "jww",
        "version": str(raw_version),
        "name": source_path.name,
        "sha256": _reference._sha256_file(source_path),
    }
    format_metadata: dict[str, Any] = {
        "version": raw_version,
        "memo": str(header.get("memo", "")),
        "paper_size": int(header.get("paper_size", 0)),
        "write_layer_group": int(header.get("write_layer_group", 0)),
        "line_type_settings": "file" if linetypes_from_file else "default",
    }
    if pen_table is not None:
        format_metadata["pens"] = pen_table
    if metadata_settings:
        format_metadata["settings"] = metadata_settings

    shell: dict[str, Any] = {
        "format": "cad2d-ir",
        "version": import_options.ir_version,
        "header": {
            "units": "mm",
            "angle_unit": "deg",
            "coord_space": "world",
            "metadata": {"jww": format_metadata},
        },
        "source": source,
        "tables": tables,
    }

    _reference._append_parser_diagnostics({"diagnostics": parser_diagnostics}, context)
    if metadata_settings:
        context.diagnostics.append(
            ImportDiagnostic(
                code="JWW_METADATA_SETTING_EXTRACTED",
                severity="info",
                message=(
                    f"Preserved {len(metadata_settings)} Jw_cad internal setting "
                    "record(s) as document metadata instead of drawing TEXT."
                ),
                source_kind="TEXT",
                action="preserved_metadata",
                details={
                    "count": len(metadata_settings),
                    "keys": sorted({str(item["key"]) for item in metadata_settings}),
                },
            )
        )
    if isinstance(pen_table, dict):
        resolved = sum(
            1
            for table in (pen_table["printer"], pen_table.get("extended", {}))
            for pen in table.values()
            if pen.get("width_mm") is not None
        )
        context.diagnostics.append(
            ImportDiagnostic(
                code="JWW_PEN_TABLE_PRESERVED",
                severity="info",
                message=(
                    "Preserved the printer pen table (line width per pen color) as "
                    "header.metadata.jww.pens; "
                    f"{context.pen_width_entities} entities with pen width 0 took "
                    "their pen color's width as lineweight_mm."
                ),
                action="preserved_metadata",
                details={
                    "unit": pen_table["unit"],
                    "printer_dpi": pen_table["printer_dpi"],
                    "widths_in_mm": resolved,
                    "entities_with_pen_color_width": context.pen_width_entities,
                },
            )
        )
    _reference._append_summary_diagnostics(
        {"validation": {"unresolved_def_numbers": unresolved}}, context
    )

    converted_entities = converted.entity_count()
    statistics: dict[str, Any] = {
        "source_format": "jww",
        "source_entities": source_entity_count,
        "source_entity_counts": dict(sorted(source_counts.items())),
        "metadata_settings": len(metadata_settings),
        "source_block_definitions": len(block_defs),
        "converted_entities": converted_entities,
        "converted_block_entities": sum(block_sizes.values()),
        "converted_entity_counts": dict(sorted(context.converted_counts.items())),
        "skipped_entities": sum(context.skipped_counts.values()),
        "skipped_entity_counts": dict(sorted(context.skipped_counts.items())),
        "approximated_entities": sum(context.approximation_counts.values()),
        "preserved_dimensions": context.preserved_dimensions,
        "embedded_images": converted.embedded_images(),
        "linked_images": converted.linked_images(),
    }
    document = converted.take_document(shell, block_shells)
    return NativeImportResult(
        document=document, diagnostics=context.diagnostics, statistics=statistics
    )


__all__ = ["NativeImportResult", "convert_jww_file"]
