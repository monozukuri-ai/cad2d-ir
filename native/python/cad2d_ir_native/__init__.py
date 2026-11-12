"""The native code (Rust) of ``cad2d-ir``.

``cad2d-ir`` installs this package and uses it for what exists here only: the
DXF writer (``cad2d_ir.ir_to_dxf``) and the entity conversion of the JWW and
JWS importers. Those are used through ``cad2d_ir``. What is exported here are
the functions ``cad2d_ir`` calls, and the typed document
(:class:`NativeDocument`) for a caller that keeps a document in native code.

The DXF writer calls back into ``cad2d_ir`` for what is decided with the codecs
and the tables of Python (the code page of the text, the inline codes of MTEXT,
the fonts of the text styles), so the two packages are of one version.
"""

from ._core import (
    ConvertedJww,
    NativeDocument,
    ParsedJww,
    Unsupported,
    __version__,
    convert_jw_document,
    document_roundtrip,
    parse_jww,
    sample_ellipse,
    sample_spline,
    text_cells,
    write_dxf,
)

__all__ = [
    "ConvertedJww",
    "NativeDocument",
    "ParsedJww",
    "Unsupported",
    "__version__",
    "convert_jw_document",
    "document_roundtrip",
    "parse_jww",
    "sample_ellipse",
    "sample_spline",
    "text_cells",
    "write_dxf",
]
