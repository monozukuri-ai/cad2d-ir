from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from pathlib import Path

from cad2d_ir.api import (
    convert_dxf_file_to_ir,
    convert_file_to_ir,
    convert_hpgl_file_to_dxf,
    convert_hpgl_file_to_ir,
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
        help=(
            "Import a supported CAD file "
            "(DXF, DWG, DGN, DWF, EXB, HP-GL, IDW, JWW, JWS, MI/BI, PDF, or SXF) to IR JSON"
        ),
    )
    import_parser.add_argument("input", type=Path, help="Path to source CAD file")
    import_parser.add_argument("-o", "--output", type=Path, help="Output IR JSON path")
    import_parser.add_argument(
        "--format",
        dest="source_format",
        default="auto",
        choices=(
            "auto",
            "dxf",
            "dwg",
            "dgn",
            "dwf",
            "edrw",
            "exb",
            "hpgl",
            "icd",
            "idw",
            "jww",
            "jws",
            "mi",
            "pdf",
            "sxf",
        ),
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
        help="Recover/report supported errors; HP-GL stops at the first uncertain command",
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
        help="DXF, DGN, or MI text encoding (default: auto)",
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
    hpgl = subparsers.add_parser(
        "hpgl2dxf", help="Convert one HP-GL page through IR to paper-space DXF"
    )
    hpgl.add_argument("input", type=Path)
    hpgl.add_argument("-o", "--output", type=Path, required=True)
    hpgl.add_argument(
        "--page", type=int, help="1-based page; required for multi-page input"
    )
    hpgl.add_argument("--lenient", action="store_true")
    hpgl.add_argument("--plot-size", type=float, nargs=2, metavar=("WIDTH", "HEIGHT"))
    hpgl.add_argument("--paper-size", type=float, nargs=2, metavar=("WIDTH", "HEIGHT"))
    hpgl.add_argument(
        "--target-version", choices=SUPPORTED_DXF_TARGET_VERSIONS, default="AC1024"
    )
    hpgl.add_argument(
        "--report", type=Path, help="JSON diagnostics for both import and DXF export"
    )
    hpgl.add_argument("--ir-output", type=Path, help="Save all pages as IR JSON")
    for command in (hpgl, import_parser):
        command.add_argument(
            "--max-input-bytes",
            type=int,
            default=67_108_864,
            help="HP-GL input byte cap",
        )
        command.add_argument(
            "--max-entities",
            type=int,
            default=200_000,
            help="HP-GL IR entity cap, including block contents",
        )
    import_parser.add_argument(
        "--plot-size",
        type=float,
        nargs=2,
        metavar=("WIDTH", "HEIGHT"),
        help="HP-GL plot extents in mm",
    )
    import_parser.add_argument(
        "--paper-size",
        type=float,
        nargs=2,
        metavar=("WIDTH", "HEIGHT"),
        help="HP-GL paper metadata in mm",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    try:
        if args.command == "hpgl2dxf":
            from cad2d_ir.importers.hpgl import _backend

            backend = _backend()
            if args.page is not None and args.page < 1:
                raise ValueError("--page must be positive")
            if args.max_input_bytes < 0:
                raise ValueError("--max-input-bytes must be non-negative")
            outputs = [
                p.resolve()
                for p in (args.output, args.report, args.ir_output)
                if p is not None
            ]
            if len(set(outputs)) != len(outputs) or args.input.resolve() in outputs:
                raise ValueError(
                    "Input, DXF output, report and IR output must have distinct paths"
                )
            result = convert_hpgl_file_to_dxf(
                args.input,
                page=args.page - 1 if args.page is not None else None,
                strict=not args.lenient,
                target_version=args.target_version,
                profile=backend.DeviceProfile(
                    plot_size_mm=tuple(args.plot_size) if args.plot_size else None,
                    paper_size_mm=tuple(args.paper_size) if args.paper_size else None,
                ),
                limits=backend.Limits(max_input_bytes=args.max_input_bytes),
                max_entities=args.max_entities,
            )
            report = {
                "page": result.page + 1,
                "import_statistics": result.imported.statistics,
                "import_diagnostics": [
                    d.as_dict() for d in result.imported.diagnostics
                ],
                "export_diagnostics": [
                    d.as_dict() for d in result.exported.diagnostics
                ],
            }
            partial = result.imported.statistics["completeness"] != "complete" or any(
                d.severity in {"warning", "error"} for d in result.exported.diagnostics
            )
            report["completeness"] = "partial" if partial else "complete"
            encoding = resolve_dxf_output_encoding(
                target_version=args.target_version, encoding="auto"
            )
            # Complete conversion and encoding before opening any output.
            encoded = result.exported.dxf_text.encode(encoding)
            ir_json = json.dumps(result.imported.document, ensure_ascii=False)
            report_json = json.dumps(report, ensure_ascii=False, indent=2)
            args.output.write_bytes(encoded)
            if args.ir_output:
                _write_text(args.ir_output, ir_json)
            if args.report:
                _write_text(args.report, report_json + "\n")
            print(report_json)
            return 1 if partial else 0
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
            from cad2d_ir.importers import detect_source_format

            source_format = (
                detect_source_format(args.input)
                if args.source_format == "auto"
                else args.source_format
            )
            if source_format == "hpgl":
                from cad2d_ir.importers.hpgl import _backend

                backend = _backend()
                if args.max_input_bytes < 0:
                    raise ValueError("--max-input-bytes must be non-negative")
                result = convert_hpgl_file_to_ir(
                    args.input,
                    ir_version=args.ir_version,
                    strict=not args.lenient,
                    profile=backend.DeviceProfile(
                        plot_size_mm=tuple(args.plot_size) if args.plot_size else None,
                        paper_size_mm=tuple(args.paper_size)
                        if args.paper_size
                        else None,
                    ),
                    limits=backend.Limits(max_input_bytes=args.max_input_bytes),
                    max_entities=args.max_entities,
                )
            else:
                if args.plot_size or args.paper_size:
                    raise ValueError("--plot-size/--paper-size apply to HP-GL only")
                result = convert_file_to_ir(
                    args.input,
                    source_format=source_format,
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
            return 1 if result.statistics.get("completeness") == "partial" else 0

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
