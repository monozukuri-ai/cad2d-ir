"""Native (Rust) IR core for ``cad2d-ir`` (experimental).

``cad2d_ir`` stays the reference implementation. What is here is checked against
it: a document converted to the typed IR and back is an equal ``dict``, the DXF
text written here is the text ``ir_to_dxf`` writes, and a JWW file imported here
gives the document, diagnostics and statistics of ``convert_jww_file_to_ir``.
A document the native code has not learnt raises :class:`Unsupported`, and the
caller uses the Python implementation.
"""

from ._core import (
    ConvertedJww,
    NativeDocument,
    ParsedJww,
    Unsupported,
    __version__,
    document_roundtrip,
    ir_to_dxf,
    parse_jww,
)

__all__ = [
    "ConvertedJww",
    "NativeDocument",
    "ParsedJww",
    "Unsupported",
    "__version__",
    "document_roundtrip",
    "ir_to_dxf",
    "parse_jww",
]
