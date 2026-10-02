"""Linetype dash patterns shared by the importers.

Patterns follow the DXF sign convention used by ``tables.linetypes.*.pattern_mm``:
positive = dash, negative = gap, 0 = dot. All lengths here are millimetres on paper.
"""

from __future__ import annotations

# SXF Ver.3.1 predefined line types (code -> name). Jw_cad numbers its
# SXF-compatible line types as 30 + code.
SXF_LINETYPE_NAMES: dict[int, str] = {
    1: "continuous",
    2: "dashed",
    3: "dashed spaced",
    4: "long dashed dotted",
    5: "long dashed double-dotted",
    6: "long dashed triplicate-dotted",
    7: "dotted",
    8: "chain",
    9: "chain double dash",
    10: "dashed dotted",
    11: "double-dashed dotted",
    12: "dashed double-dotted",
    13: "double-dashed double-dotted",
    14: "dashed triplicate-dotted",
    15: "double-dashed triplicate-dotted",
}

# Reference pitches from the SXF Ver.3.1 common predefined elements. Jw_cad stores
# the same segment lengths in every JWW header for its SXF-compatible line types.
SXF_LINETYPE_PATTERNS_MM: dict[str, tuple[float, ...]] = {
    "continuous": (),
    "dashed": (6.0, -1.5),
    "dashed spaced": (6.0, -6.0),
    "long dashed dotted": (12.0, -1.5, 0.25, -1.5),
    "long dashed double-dotted": (12.0, -1.5, 0.25, -1.5, 0.25, -1.5),
    "long dashed triplicate-dotted": (12.0, -1.5, 0.25, -1.5, 0.25, -1.5, 0.25, -1.5),
    "dotted": (0.25, -1.5),
    "chain": (12.0, -1.5, 3.5, -1.5),
    "chain double dash": (12.0, -1.5, 3.5, -1.5, 3.5, -1.5),
    "dashed dotted": (6.0, -1.5, 0.25, -1.5),
    "double-dashed dotted": (6.0, -1.5, 6.0, -1.5, 0.25, -1.5),
    "dashed double-dotted": (6.0, -1.5, 0.25, -1.5, 0.25, -1.5),
    "double-dashed double-dotted": (6.0, -1.5, 6.0, -1.5, 0.25, -1.5, 0.25, -1.5),
    "dashed triplicate-dotted": (6.0, -1.5, 0.25, -1.5, 0.25, -1.5, 0.25, -1.5),
    "double-dashed triplicate-dotted": (
        6.0,
        -1.5,
        6.0,
        -1.5,
        0.25,
        -1.5,
        0.25,
        -1.5,
        0.25,
        -1.5,
    ),
}


def sxf_linetype_pattern(name: str) -> list[float] | None:
    """Pattern of an SXF predefined line type name, or ``None`` if it is not one."""
    pattern = SXF_LINETYPE_PATTERNS_MM.get(" ".join(str(name).lower().split()))
    return None if pattern is None else list(pattern)
