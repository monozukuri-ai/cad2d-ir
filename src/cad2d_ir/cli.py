from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Sequence

from cad2d_ir.api import (
    convert_dxf_file_to_ir,
    convert_file_to_ir,
    convert_ir_to_dxf_text,
    load_ir_json,
)
from cad2d_ir.codecs.dxf import (
    SUPPORTED_DXF_TARGET_VERSIONS,
    resolve_dxf_output_encoding,
)
from cad2d_ir.constants import CURRENT_IR_VERSION
from cad2d_ir.importers import ImporterError
from cad2d_ir.schema import IRValidationError, validate_ir


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_text(path: Path | None, text: str, *, encoding: str = "utf-8") -> None:
    if path is None:
        if encoding != "utf-8":
            sys.stdout.buffer.write(text.encode(encoding))
            return
        print(text, end="")
        return
    path.write_bytes(text.encode(encoding))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cad2d-ir")
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate_parser = subparsers.add_parser(
        "validate", help="Validate an IR JSON document"
    )
    validate_parser.add_argument("input", type=Path, help="Path to IR JSON file")

    dxf2ir_parser = subparsers.add_parser("dxf2ir", help="Convert DXF file to IR JSON")
    dxf2ir_parser.add_argument("input", type=Path, help="Path to DXF file")
    dxf2ir_parser.add_argument("-o", "--output", type=Path, help="Output IR JSON path")
    dxf2ir_parser.add_argument(
        "--pretty", action="store_true", help="Pretty-print JSON"
    )
    dxf2ir_parser.add_argument(
        "--ir-version", default=CURRENT_IR_VERSION, help="IR version string"
    )
    dxf2ir_parser.add_argument(
        "--encoding",
        default="auto",
        help="Input encoding (default: BOM/codepage/UTF-8/CP932 auto detection)",
    )

    import_parser = subparsers.add_parser(
        "import",
        help="Import a supported CAD file (DXF, DWG, DGN, DWF, JWW, or SXF) to IR JSON",
    )
    import_parser.add_argument("input", type=Path, help="Path to source CAD file")
    import_parser.add_argument("-o", "--output", type=Path, help="Output IR JSON path")
    import_parser.add_argument(
        "--format",
        dest="source_format",
        default="auto",
        choices=("auto", "dxf", "dwg", "dgn", "dwf", "jww", "sxf"),
        help="Source format (default: detect from filename)",
    )
    import_parser.add_argument(
        "--pretty", action="store_true", help="Pretty-print JSON"
    )
    import_parser.add_argument(
        "--ir-version", default=CURRENT_IR_VERSION, help="IR version string"
    )
    import_parser.add_argument(
        "--lenient",
        action="store_true",
        help="Skip malformed source entities and report diagnostics",
    )
    import_parser.add_argument(
        "--curve-segments",
        type=int,
        default=96,
        help="Segments per full curve when approximation is required (8..4096)",
    )
    import_parser.add_argument(
        "--encoding",
        default="auto",
        help="DXF or DGN text encoding (default: auto)",
    )

    ir2dxf_parser = subparsers.add_parser("ir2dxf", help="Convert IR JSON to DXF file")
    ir2dxf_parser.add_argument("input", type=Path, help="Path to IR JSON file")
    ir2dxf_parser.add_argument("-o", "--output", type=Path, help="Output DXF path")
    ir2dxf_parser.add_argument(
        "--target-version",
        default="AC1024",
        choices=SUPPORTED_DXF_TARGET_VERSIONS,
        help="DXF target version: AC1009 (R12) or AC1024 (R2010)",
    )
    ir2dxf_parser.add_argument(
        "--curve-segments",
        type=int,
        default=96,
        help="Segments per full curve for R12 approximations (8..4096)",
    )
    ir2dxf_parser.add_argument(
        "--generic-dimensions",
        default="explode",
        choices=("explode", "skip"),
        help="Expand GENERIC dimensions to primitives or omit them",
    )
    ir2dxf_parser.add_argument(
        "--encoding",
        default="auto",
        help="Output encoding (auto: CP932 for AC1009, UTF-8 for AC1024)",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        if args.command == "validate":
            validate_ir(_read_json(args.input))
            print("IR document is valid.")
            return 0

        if args.command == "dxf2ir":
            result = convert_dxf_file_to_ir(
                args.input,
                ir_version=args.ir_version,
                validate=True,
                encoding=args.encoding,
            )
            indent = 2 if args.pretty else None
            text = json.dumps(result.document, ensure_ascii=False, indent=indent)
            if indent is not None:
                text += "\n"
            _write_text(args.output, text)
            for diagnostic in result.diagnostics:
                print(
                    f"{diagnostic.severity.title()} "
                    f"[{diagnostic.code}]: {diagnostic.message}",
                    file=sys.stderr,
                )
            return 0

        if args.command == "import":
            result = convert_file_to_ir(
                args.input,
                source_format=args.source_format,
                ir_version=args.ir_version,
                validate=True,
                strict=not args.lenient,
                curve_segments=args.curve_segments,
                encoding=args.encoding,
            )
            indent = 2 if args.pretty else None
            text = json.dumps(result.document, ensure_ascii=False, indent=indent)
            if indent is not None:
                text += "\n"
            _write_text(args.output, text)
            for diagnostic in result.diagnostics:
                print(
                    f"{diagnostic.severity.title()} [{diagnostic.code}]: {diagnostic.message}",
                    file=sys.stderr,
                )
            return 0

        if args.command == "ir2dxf":
            result = convert_ir_to_dxf_text(
                load_ir_json(args.input, validate=True),
                validate=True,
                target_version=args.target_version,
                curve_segments=args.curve_segments,
                generic_dimensions=args.generic_dimensions,
            )
            output_encoding = resolve_dxf_output_encoding(
                target_version=args.target_version,
                encoding=args.encoding,
            )
            _write_text(args.output, result.dxf_text, encoding=output_encoding)
            for diagnostic in result.diagnostics:
                print(
                    f"{diagnostic.severity.title()} "
                    f"[{diagnostic.code}]: {diagnostic.message}",
                    file=sys.stderr,
                )
            return 0
    except (
        IRValidationError,
        ImporterError,
        OSError,
        ValueError,
        json.JSONDecodeError,
    ) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 2

    parser.error(f"Unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
