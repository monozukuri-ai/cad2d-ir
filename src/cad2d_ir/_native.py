"""The native code of cad2d-ir (the ``cad2d-ir-native`` package).

The DXF writer and the entity conversion of the JWW importer are native code.
The package is a dependency of this one, of the same version; it is imported
when one of the two is first used.
"""

from __future__ import annotations

from typing import Any

_core: Any = None


def native_core() -> Any:
    """The module of the native code."""
    global _core
    if _core is not None:
        return _core
    try:
        import cad2d_ir_native
    except ImportError as exc:  # pragma: no cover - a dependency of this package
        raise ImportError(
            "cad2d-ir needs its native code (the package cad2d-ir-native), which "
            "could not be imported. It is installed with cad2d-ir from a wheel for "
            "this platform, or built from its source distribution with a Rust "
            "toolchain."
        ) from exc

    from cad2d_ir import __version__

    # The two packages call private functions of each other, so they are one
    # release: ``cad2d-ir`` pins ``cad2d-ir-native`` of its own version.
    found = getattr(cad2d_ir_native, "__version__", None)
    if found != __version__:
        raise ImportError(
            f"cad2d-ir {__version__} needs cad2d-ir-native {__version__}, and "
            f"{found} is installed. Install the two together "
            "(`pip install --upgrade cad2d-ir`)."
        )
    _core = cad2d_ir_native
    return cad2d_ir_native
