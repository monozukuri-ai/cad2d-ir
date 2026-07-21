from cad2d_ir.api import (
    DxfToIrResult,
    IrToDxfResult,
    convert_dwg_file_to_ir,
    convert_dxf_file_to_ir,
    convert_dxf_text_to_ir,
    convert_file_to_ir,
    convert_ir_file_to_dxf,
    convert_ir_to_dxf_text,
    convert_jww_file_to_ir,
    convert_sxf_file_to_ir,
    dump_ir_json,
    load_ir_json,
)
from cad2d_ir.codecs.dxf import dxf_to_ir, ir_to_dxf, read_dxf_file, write_dxf_file
from cad2d_ir.constants import CURRENT_IR_VERSION
from cad2d_ir.importers import (
    ImportDiagnostic,
    ImportOptions,
    ImportResult,
    ImporterError,
    MissingOptionalDependencyError,
    UnsupportedSourceFormatError,
    detect_source_format,
)
from cad2d_ir.schema import IRValidationError, load_schema, validate_ir

__version__ = "0.2.0"

__all__ = [
    "__version__",
    "DxfToIrResult",
    "CURRENT_IR_VERSION",
    "IRValidationError",
    "ImportDiagnostic",
    "ImporterError",
    "ImportOptions",
    "ImportResult",
    "IrToDxfResult",
    "MissingOptionalDependencyError",
    "UnsupportedSourceFormatError",
    "convert_dwg_file_to_ir",
    "convert_dxf_file_to_ir",
    "convert_dxf_text_to_ir",
    "convert_file_to_ir",
    "convert_ir_file_to_dxf",
    "convert_ir_to_dxf_text",
    "convert_jww_file_to_ir",
    "convert_sxf_file_to_ir",
    "dxf_to_ir",
    "dump_ir_json",
    "detect_source_format",
    "ir_to_dxf",
    "load_ir_json",
    "load_schema",
    "read_dxf_file",
    "validate_ir",
    "write_dxf_file",
]
