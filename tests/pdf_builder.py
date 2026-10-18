"""Tiny PDF writer for importer tests: raw content streams, no external library.

It writes an uncompressed PDF 1.7 with one or more pages, optional form XObjects,
image XObjects, optional content groups and standard Type1 or CID fonts, so the
tests can state exactly which operators the importer has to understand.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

PNG_RED_1X1 = b"\xff\x00\x00"


@dataclass
class PdfPage:
    content: bytes
    width: float = 841.89  # A4 landscape in points
    height: float = 595.28
    rotate: int = 0
    cropbox: list[float] | None = None
    annots: int = 0


@dataclass
class PdfSpec:
    pages: list[PdfPage]
    forms: dict[str, tuple[bytes, list[float], list[float]]] = field(
        default_factory=dict
    )
    images: dict[str, tuple[int, int, bytes]] = field(default_factory=dict)
    ocgs: list[str] = field(default_factory=list)
    fonts: dict[str, str] = field(default_factory=lambda: {"F1": "Helvetica"})
    cid_fonts: dict[str, str] = field(
        default_factory=dict
    )  # name -> base font (Japanese)
    producer: str | None = "cad2d-ir tests"


def build_pdf(spec: PdfSpec) -> bytes:
    objs: list[bytes] = []

    def add(body: bytes) -> int:
        objs.append(body)
        return len(objs)

    font_ids: dict[str, int] = {}
    for key, base in spec.fonts.items():
        font_ids[key] = add(
            f"<< /Type /Font /Subtype /Type1 /BaseFont /{base} /Encoding /WinAnsiEncoding >>".encode()
        )
    for key, base in spec.cid_fonts.items():
        descendant = add(
            (
                f"<< /Type /Font /Subtype /CIDFontType0 /BaseFont /{base} "
                "/CIDSystemInfo << /Registry (Adobe) /Ordering (Japan1) /Supplement 6 >> "
                f"/FontDescriptor << /Type /FontDescriptor /FontName /{base} /Flags 4 "
                "/FontBBox [0 -141 1000 859] /ItalicAngle 0 /Ascent 859 /Descent -141 "
                "/CapHeight 700 /StemV 80 >> /DW 1000 >>"
            ).encode()
        )
        font_ids[key] = add(
            (
                f"<< /Type /Font /Subtype /Type0 /BaseFont /{base} /Encoding /UniJIS-UCS2-H "
                f"/DescendantFonts [{descendant} 0 R] >>"
            ).encode()
        )
    font_res = " ".join(f"/{k} {v} 0 R" for k, v in font_ids.items())
    xobject_ids: dict[str, int] = {}
    for name, (stream, bbox, matrix) in spec.forms.items():
        xobject_ids[name] = add(
            (
                f"<< /Type /XObject /Subtype /Form /BBox [{' '.join(map(str, bbox))}] "
                f"/Matrix [{' '.join(map(str, matrix))}] /Resources << /Font << {font_res} >> >> "
                f"/Length {len(stream)} >>\nstream\n"
            ).encode()
            + stream
            + b"\nendstream"
        )
    for name, (width, height, data) in spec.images.items():
        xobject_ids[name] = add(
            (
                f"<< /Type /XObject /Subtype /Image /Width {width} /Height {height} "
                f"/ColorSpace /DeviceRGB /BitsPerComponent 8 /Length {len(data)} >>\nstream\n"
            ).encode()
            + data
            + b"\nendstream"
        )
    ocg_ids = {
        name: add(f"<< /Type /OCG /Name ({name}) >>".encode()) for name in spec.ocgs
    }
    resources = f"<< /Font << {font_res} >>"
    if xobject_ids:
        resources += (
            " /XObject << "
            + " ".join(f"/{k} {v} 0 R" for k, v in xobject_ids.items())
            + " >>"
        )
    if ocg_ids:
        resources += (
            " /Properties << "
            + " ".join(f"/{k} {v} 0 R" for k, v in ocg_ids.items())
            + " >>"
        )
    resources += " >>"

    content_ids = [
        add(
            f"<< /Length {len(p.content)} >>\nstream\n".encode()
            + p.content
            + b"\nendstream"
        )
        for p in spec.pages
    ]
    annot_ids: list[list[int]] = []
    for page in spec.pages:
        ids = []
        for _ in range(page.annots):
            ids.append(
                add(b"<< /Type /Annot /Subtype /Square /Rect [10 10 50 50] /F 4 >>")
            )
        annot_ids.append(ids)
    pages_id = len(objs) + len(spec.pages) + 1
    page_ids = []
    for page, content_id, annots in zip(spec.pages, content_ids, annot_ids):
        extra = ""
        if page.rotate:
            extra += f" /Rotate {page.rotate}"
        if page.cropbox:
            extra += f" /CropBox [{' '.join(map(str, page.cropbox))}]"
        if annots:
            extra += " /Annots [" + " ".join(f"{i} 0 R" for i in annots) + "]"
        page_ids.append(
            add(
                (
                    f"<< /Type /Page /Parent {pages_id} 0 R /MediaBox [0 0 {page.width} {page.height}]{extra} "
                    f"/Resources {resources} /Contents {content_id} 0 R >>"
                ).encode()
            )
        )
    kids = " ".join(f"{i} 0 R" for i in page_ids)
    real_pages_id = add(
        f"<< /Type /Pages /Kids [{kids}] /Count {len(page_ids)} >>".encode()
    )
    assert real_pages_id == pages_id
    catalog = f"<< /Type /Catalog /Pages {pages_id} 0 R"
    if ocg_ids:
        refs = " ".join(f"{i} 0 R" for i in ocg_ids.values())
        catalog += f" /OCProperties << /OCGs [{refs}] /D << /Order [{refs}] >> >>"
    catalog += " >>"
    catalog_id = add(catalog.encode())
    info_id = (
        add(f"<< /Producer ({spec.producer}) /Creator (cad2d-ir) >>".encode())
        if spec.producer
        else None
    )

    out = bytearray(b"%PDF-1.7\n%\xe2\xe3\xcf\xd3\n")
    offsets = []
    for number, body in enumerate(objs, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    trailer = f"trailer\n<< /Size {len(objs) + 1} /Root {catalog_id} 0 R"
    if info_id:
        trailer += f" /Info {info_id} 0 R"
    trailer += f" >>\nstartxref\n{xref}\n%%EOF\n"
    out += trailer.encode()
    return bytes(out)


def pt(mm: float) -> float:
    """Millimetres to PDF points."""
    return mm * 72.0 / 25.4


def arc_bezier(cx: float, cy: float, r: float, a0_deg: float, a1_deg: float) -> bytes:
    """Content-stream operators drawing an arc as the standard Bezier pieces (<= 90 deg each)."""
    import math

    out = []
    a0, a1 = math.radians(a0_deg), math.radians(a1_deg)
    sweep = a1 - a0
    pieces = max(1, int(math.ceil(abs(sweep) / (math.pi / 2) - 1e-9)))
    step = sweep / pieces
    k = 4.0 / 3.0 * math.tan(abs(step) / 4.0) * (1 if step >= 0 else -1)
    for i in range(pieces):
        t0 = a0 + i * step
        t1 = t0 + step
        p0 = (cx + r * math.cos(t0), cy + r * math.sin(t0))
        p3 = (cx + r * math.cos(t1), cy + r * math.sin(t1))
        p1 = (p0[0] - k * r * math.sin(t0), p0[1] + k * r * math.cos(t0))
        p2 = (p3[0] + k * r * math.sin(t1), p3[1] - k * r * math.cos(t1))
        if i == 0:
            out.append(f"{p0[0]:.4f} {p0[1]:.4f} m")
        out.append(
            f"{p1[0]:.4f} {p1[1]:.4f} {p2[0]:.4f} {p2[1]:.4f} {p3[0]:.4f} {p3[1]:.4f} c"
        )
    return ("\n".join(out) + "\n").encode()


def circle_bezier(cx: float, cy: float, r: float) -> bytes:
    return arc_bezier(cx, cy, r, 0.0, 360.0) + b"h\n"


def polyline_arc(
    cx: float, cy: float, r: float, a0_deg: float, a1_deg: float, n: int
) -> bytes:
    import math

    out = []
    for i in range(n + 1):
        a = math.radians(a0_deg + (a1_deg - a0_deg) * i / n)
        out.append(
            f"{cx + r * math.cos(a):.4f} {cy + r * math.sin(a):.4f} {'m' if i == 0 else 'l'}"
        )
    return ("\n".join(out) + "\n").encode()


def text(
    x: float,
    y: float,
    size: float,
    s: str,
    *,
    font: str = "F1",
    angle_deg: float = 0.0,
    mode: int | None = None,
) -> bytes:
    import math

    c, sn = math.cos(math.radians(angle_deg)), math.sin(math.radians(angle_deg))
    escaped = s.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    tr = f"{mode} Tr " if mode is not None else ""
    body = f"BT /{font} {size:.4f} Tf {tr}{c:.6f} {sn:.6f} {-sn:.6f} {c:.6f} {x:.4f} {y:.4f} Tm ({escaped}) Tj ET\n"
    # The render mode is graphics state and would leak into later text: isolate it.
    if mode is not None:
        body = "q " + body.rstrip("\n") + " Q\n"
    return body.encode("latin-1")


def cid_text(x: float, y: float, size: float, s: str, *, font: str = "J1") -> bytes:
    hexstr = s.encode("utf-16-be").hex()
    return f"BT /{font} {size:.4f} Tf 1 0 0 1 {x:.4f} {y:.4f} Tm <{hexstr}> Tj ET\n".encode()


def page_a3() -> dict[str, Any]:
    return {"width": pt(420.0), "height": pt(297.0)}
