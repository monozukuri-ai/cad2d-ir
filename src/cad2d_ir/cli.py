from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Sequence

from cad2d_ir.api import convert_dxf_text_to_ir, convert_ir_to_dxf_text, load_ir_json
from cad2d_ir.schema import IRValidationError, validate_ir


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _write_text(path: Path | None, text: str) -> None:
    if path is None:
        print(text, end="")
        return
    path.write_text(text, encoding="utf-8")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cad2d-ir")
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate_parser = subparsers.add_parser("validate", help="Validate an IR JSON document")
    validate_parser.add_argument("input", type=Path, help="Path to IR JSON file")

    dxf2ir_parser = subparsers.add_parser("dxf2ir", help="Convert DXF file to IR JSON")
    dxf2ir_parser.add_argument("input", type=Path, help="Path to DXF file")
    dxf2ir_parser.add_argument("-o", "--output", type=Path, help="Output IR JSON path")
    dxf2ir_parser.add_argument("--pretty", action="store_true", help="Pretty-print JSON")
    dxf2ir_parser.add_argument("--ir-version", default="0.1.0", help="IR version string")

    ir2dxf_parser = subparsers.add_parser("ir2dxf", help="Convert IR JSON to DXF file")
    ir2dxf_parser.add_argument("input", type=Path, help="Path to IR JSON file")
    ir2dxf_parser.add_argument("-o", "--output", type=Path, help="Output DXF path")
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
            result = convert_dxf_text_to_ir(
                args.input.read_text(encoding="utf-8"),
                ir_version=args.ir_version,
                validate=True,
            )
            indent = 2 if args.pretty else None
            text = json.dumps(result.document, ensure_ascii=False, indent=indent)
            if indent is not None:
                text += "\n"
            _write_text(args.output, text)
            for warning in result.warnings:
                print(f"Warning: {warning}", file=sys.stderr)
            return 0

        if args.command == "ir2dxf":
            result = convert_ir_to_dxf_text(load_ir_json(args.input, validate=True), validate=True)
            _write_text(args.output, result.dxf_text)
            for warning in result.warnings:
                print(f"Warning: {warning}", file=sys.stderr)
            return 0
    except (IRValidationError, ValueError, json.JSONDecodeError) as exc:
        print(f"Error: {exc}")
        return 2

    parser.error(f"Unknown command: {args.command}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
