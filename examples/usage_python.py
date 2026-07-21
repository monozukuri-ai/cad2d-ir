from __future__ import annotations

from pathlib import Path

from cad2d_ir.api import (
    convert_file_to_ir,
    convert_ir_to_dxf_text,
    dump_ir_json,
)


def main() -> None:
    dxf_path = Path("examples/dxf/simple_line.dxf")
    to_ir = convert_file_to_ir(dxf_path)
    print("entities:", len(to_ir.document["entities"]))
    print("warnings:", to_ir.warnings)
    print("statistics:", to_ir.statistics)

    out_ir = Path("/tmp/cad2d-ir-example.json")
    dump_ir_json(to_ir.document, out_ir, pretty=True, validate=True)
    print("wrote:", out_ir)

    to_dxf = convert_ir_to_dxf_text(to_ir.document)
    print("dxf warnings:", to_dxf.warnings)
    print("dxf length:", len(to_dxf.dxf_text))


if __name__ == "__main__":
    main()
