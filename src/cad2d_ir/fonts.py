"""Font names across CAD formats.

Jw_cad, SXF and most other sources name a font by its Windows face name
(``ＭＳ ゴシック``). A DXF or DWG text style names the font *file*
(``msgothic.ttc``) and, from AutoCAD 2000 on, keeps the TrueType family in the
style's ``ACAD`` xdata (``1000`` family, ``1071`` charset and pitch). AutoCAD
reads a face name in the file field as the name of a missing SHX font and
substitutes one, and Japanese text then shows as ``?``. This module maps
between the two for the fonts Windows and Office ship.

Lookups ignore case, spaces, hyphens and underscores, fold full-width and
half-width forms (NFKC), drop a PDF subset prefix (``ABCDEF+``) and treat a
leading ``@`` (vertical writing) as part of the name to keep, not to match.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

SHIFTJIS_CHARSET = 128
HANGUL_CHARSET = 129
GB2312_CHARSET = 134

_TRUETYPE_SUFFIXES = (".ttf", ".ttc", ".otf")
_SUBSET_PREFIX = re.compile(r"^[A-Z]{6}\+")
_IGNORED = re.compile(r"[\s\-_]+")
_KANA = re.compile(r"[぀-ヿｦ-ﾟ]")
_HAN = re.compile(r"[㐀-䶿一-鿿]")
_HANGUL = re.compile(r"[가-힯]")
# DWF and PDF name a weight or slant after the family (``Arial Bold``).
_STYLE_SUFFIX = re.compile(
    r"(?:[\s,\-]*(?:bold|italic|oblique|regular))+$", re.IGNORECASE
)


@dataclass(frozen=True, slots=True)
class TrueTypeFont:
    """A Windows font: its family, its file and the face name users see."""

    family: str
    """English family name, the one AutoCAD keeps in the style xdata."""
    file: str
    """File in ``%WINDIR%\\Fonts``; a ``.ttc`` holds several families."""
    face: str
    """Face name in the font's own locale, the one Jw_cad and SXF store."""
    pitch_and_family: int
    """``LOGFONT.lfPitchAndFamily``."""
    charset: int = 0
    """``LOGFONT.lfCharSet``."""
    aliases: tuple[str, ...] = ()
    """PostScript and other names the font is known by."""

    @property
    def xdata_flags(self) -> int:
        """The ``1071`` value of a text style's ``ACAD`` xdata."""
        return self.charset << 8 | self.pitch_and_family


_FIXED, _VARIABLE = 0x01, 0x02
_ROMAN, _SWISS, _MODERN = 0x10, 0x20, 0x30

# The first family of a .ttc comes first: a file name alone resolves to it.
_FONTS: tuple[TrueTypeFont, ...] = (
    TrueTypeFont("MS Gothic", "msgothic.ttc", "ＭＳ ゴシック", _FIXED | _MODERN, SHIFTJIS_CHARSET),
    TrueTypeFont("MS PGothic", "msgothic.ttc", "ＭＳ Ｐゴシック", _VARIABLE | _MODERN, SHIFTJIS_CHARSET),
    TrueTypeFont("MS UI Gothic", "msgothic.ttc", "MS UI Gothic", _VARIABLE | _MODERN, SHIFTJIS_CHARSET),
    TrueTypeFont("MS Mincho", "msmincho.ttc", "ＭＳ 明朝", _FIXED | _ROMAN, SHIFTJIS_CHARSET),
    TrueTypeFont("MS PMincho", "msmincho.ttc", "ＭＳ Ｐ明朝", _VARIABLE | _ROMAN, SHIFTJIS_CHARSET),
    TrueTypeFont("Meiryo", "meiryo.ttc", "メイリオ", _VARIABLE | _MODERN, SHIFTJIS_CHARSET),
    TrueTypeFont("Meiryo UI", "meiryo.ttc", "Meiryo UI", _VARIABLE | _MODERN, SHIFTJIS_CHARSET),
    TrueTypeFont("Yu Gothic", "YuGothR.ttc", "游ゴシック", _VARIABLE | _MODERN, SHIFTJIS_CHARSET),
    TrueTypeFont("Yu Gothic UI", "YuGothR.ttc", "Yu Gothic UI", _VARIABLE | _MODERN, SHIFTJIS_CHARSET),
    TrueTypeFont("Yu Gothic Medium", "YuGothM.ttc", "游ゴシック Medium", _VARIABLE | _MODERN, SHIFTJIS_CHARSET),
    TrueTypeFont("Yu Mincho", "yumin.ttf", "游明朝", _VARIABLE | _ROMAN, SHIFTJIS_CHARSET),
    TrueTypeFont("BIZ UDGothic", "BIZ-UDGothicR.ttc", "BIZ UDゴシック", _FIXED | _MODERN, SHIFTJIS_CHARSET),
    TrueTypeFont("BIZ UDPGothic", "BIZ-UDGothicR.ttc", "BIZ UDPゴシック", _VARIABLE | _MODERN, SHIFTJIS_CHARSET),
    TrueTypeFont("BIZ UDMincho Medium", "BIZ-UDMinchoM.ttc", "BIZ UD明朝 Medium", _FIXED | _ROMAN, SHIFTJIS_CHARSET),
    TrueTypeFont("BIZ UDPMincho Medium", "BIZ-UDMinchoM.ttc", "BIZ UDP明朝 Medium", _VARIABLE | _ROMAN, SHIFTJIS_CHARSET),
    # Microsoft Office (common on Jw_cad users' machines).
    TrueTypeFont("HGGothicM", "HGRGM.TTC", "HGｺﾞｼｯｸM", _FIXED | _MODERN, SHIFTJIS_CHARSET),
    TrueTypeFont("HGPGothicM", "HGRGM.TTC", "HGPｺﾞｼｯｸM", _VARIABLE | _MODERN, SHIFTJIS_CHARSET),
    TrueTypeFont("HGSGothicM", "HGRGM.TTC", "HGSｺﾞｼｯｸM", _VARIABLE | _MODERN, SHIFTJIS_CHARSET),
    TrueTypeFont("HGGothicE", "HGRGE.TTC", "HGｺﾞｼｯｸE", _FIXED | _MODERN, SHIFTJIS_CHARSET),
    TrueTypeFont("HGPGothicE", "HGRGE.TTC", "HGPｺﾞｼｯｸE", _VARIABLE | _MODERN, SHIFTJIS_CHARSET),
    TrueTypeFont("HGSGothicE", "HGRGE.TTC", "HGSｺﾞｼｯｸE", _VARIABLE | _MODERN, SHIFTJIS_CHARSET),
    TrueTypeFont("HGMinchoE", "HGRME.TTC", "HG明朝E", _FIXED | _ROMAN, SHIFTJIS_CHARSET),
    TrueTypeFont("HGPMinchoE", "HGRME.TTC", "HGP明朝E", _VARIABLE | _ROMAN, SHIFTJIS_CHARSET),
    TrueTypeFont("HGSMinchoE", "HGRME.TTC", "HGS明朝E", _VARIABLE | _ROMAN, SHIFTJIS_CHARSET),
    TrueTypeFont("HGMinchoB", "HGRMB.TTC", "HG明朝B", _FIXED | _ROMAN, SHIFTJIS_CHARSET),
    TrueTypeFont("HGPMinchoB", "HGRMB.TTC", "HGP明朝B", _VARIABLE | _ROMAN, SHIFTJIS_CHARSET),
    TrueTypeFont("HGSMinchoB", "HGRMB.TTC", "HGS明朝B", _VARIABLE | _ROMAN, SHIFTJIS_CHARSET),
    TrueTypeFont("HGMaruGothicMPRO","HGRSMP.TTF", "HG丸ｺﾞｼｯｸM-PRO", _VARIABLE | _MODERN, SHIFTJIS_CHARSET),
    TrueTypeFont("HGSoeiKakugothicUB", "HGRSGU.TTC", "HG創英角ｺﾞｼｯｸUB", _FIXED | _MODERN, SHIFTJIS_CHARSET),
    TrueTypeFont("Arial", "arial.ttf", "Arial", _VARIABLE | _SWISS, aliases=("ArialMT", "Helvetica")),
    TrueTypeFont("Arial Narrow", "ARIALN.TTF", "Arial Narrow", _VARIABLE | _SWISS),
    TrueTypeFont("Arial Black", "ariblk.ttf", "Arial Black", _VARIABLE | _SWISS),
    TrueTypeFont("Times New Roman","times.ttf", "Times New Roman", _VARIABLE | _ROMAN, aliases=("TimesNewRomanPSMT", "Times", "Times-Roman")),
    TrueTypeFont("Courier New", "cour.ttf", "Courier New", _FIXED | _MODERN, aliases=("CourierNewPSMT", "Courier")),
    TrueTypeFont("Century", "CENTURY.TTF", "Century", _VARIABLE | _ROMAN),
    TrueTypeFont("Century Gothic", "GOTHIC.TTF", "Century Gothic", _VARIABLE | _SWISS),
    TrueTypeFont("Tahoma","tahoma.ttf", "Tahoma", _VARIABLE | _SWISS),
    TrueTypeFont("Verdana", "verdana.ttf", "Verdana", _VARIABLE | _SWISS),
    TrueTypeFont("Calibri", "calibri.ttf", "Calibri", _VARIABLE | _SWISS),
    TrueTypeFont("Segoe UI", "segoeui.ttf", "Segoe UI", _VARIABLE | _SWISS),
    TrueTypeFont("ISOCPEUR", "isocpeur.ttf", "ISOCPEUR", _VARIABLE | _SWISS),
    TrueTypeFont("SimSun", "simsun.ttc", "宋体", _FIXED | _MODERN, GB2312_CHARSET),
    TrueTypeFont("NSimSun", "simsun.ttc", "新宋体", _FIXED | _MODERN, GB2312_CHARSET),
    TrueTypeFont("SimHei", "simhei.ttf", "黑体", _FIXED | _MODERN, GB2312_CHARSET),
    TrueTypeFont("FangSong", "simfang.ttf", "仿宋", _FIXED | _MODERN, GB2312_CHARSET, ("仿宋_GB2312", "FangSong_GB2312")),
    TrueTypeFont("KaiTi", "simkai.ttf", "楷体", _FIXED | _MODERN, GB2312_CHARSET, ("楷体_GB2312", "KaiTi_GB2312")),
    TrueTypeFont("Microsoft YaHei", "msyh.ttc", "微软雅黑", _VARIABLE | _SWISS, GB2312_CHARSET),
    TrueTypeFont("Gulim", "gulim.ttc", "굴림", _VARIABLE | _SWISS, HANGUL_CHARSET),
    TrueTypeFont("Batang", "batang.ttc", "바탕", _VARIABLE | _ROMAN, HANGUL_CHARSET),
    TrueTypeFont("Malgun Gothic", "malgun.ttf", "맑은 고딕", _VARIABLE | _SWISS, HANGUL_CHARSET),
)  # fmt: skip


def _key(name: str) -> str:
    return _IGNORED.sub("", unicodedata.normalize("NFKC", name)).casefold()


def _build_index() -> tuple[dict[str, TrueTypeFont], dict[str, TrueTypeFont]]:
    by_name: dict[str, TrueTypeFont] = {}
    by_file: dict[str, TrueTypeFont] = {}
    for font in _FONTS:
        for name in (font.family, font.face, *font.aliases):
            by_name.setdefault(_key(name), font)
        by_file.setdefault(font.file.casefold(), font)
    return by_name, by_file


_BY_NAME, _BY_FILE = _build_index()


def _split_vertical(name: str) -> tuple[str, str]:
    """``("@", "MS Gothic")`` for ``"@MS Gothic"``, ``("", name)`` otherwise."""
    name = _SUBSET_PREFIX.sub("", name.strip())
    if name.startswith("@"):
        return "@", name[1:].strip()
    return "", name


def _has_suffix(name: str, suffixes: tuple[str, ...]) -> bool:
    return name.casefold().endswith(suffixes)


def lookup(name: str | None) -> TrueTypeFont | None:
    """The known font a face, family, PostScript or file name refers to.

    A file name resolves to the first family of that file, and a file name
    built from a family (``MS PGothic.ttf``, as some CAD programs write it)
    to that family.
    """
    _, bare = _split_vertical(name or "")
    if not bare:
        return None
    if _has_suffix(bare, _TRUETYPE_SUFFIXES):
        found = _BY_FILE.get(bare.casefold())
        if found is not None:
            return found
        bare = bare.rsplit(".", 1)[0]
    found = _BY_NAME.get(_key(bare))
    if found is None:
        found = _BY_NAME.get(_key(_STYLE_SUFFIX.sub("", bare)))
    return found


def _fallback_file(face: str) -> tuple[str, int]:
    """A file that draws the script of an unknown face, and that file's charset."""
    if _KANA.search(face) or re.search(r"[！-～]", face):
        return "msgothic.ttc", SHIFTJIS_CHARSET
    if _HANGUL.search(face):
        return "gulim.ttc", HANGUL_CHARSET
    if _HAN.search(face):
        return "simsun.ttc", GB2312_CHARSET
    return f"{face}.ttf", 0


@dataclass(frozen=True, slots=True)
class StyleFont:
    """How a DXF or DWG text style names its font."""

    file: str
    """The style's font file (DXF group 3)."""
    family: str | None = None
    """TrueType family for the style's ``ACAD`` xdata; ``None`` for SHX fonts."""
    flags: int = 0
    """The xdata ``1071`` value (charset and pitch) that goes with ``family``."""


def style_font(font: str | None, family: str | None = None) -> StyleFont:
    """The font file and TrueType family a DXF/DWG text style should carry.

    ``font`` is an IR text style's ``font``: a face name (Jw_cad, SXF, ...), a
    font file or a bare SHX name (DXF, DWG). ``family`` is the IR ``family``
    a DXF style's xdata named, if any. Known fonts get their real file, so a
    DWG, which has no room for the family, still finds them. An unknown face
    keeps its name as the family and gets a file that at least draws its
    script. SHX fonts pass through unchanged; ``STANDARD`` and no font at all
    become ``txt``.
    """
    name = (font or "").strip()
    family = (family or "").strip() or None
    if name.upper() == "STANDARD":
        name = ""
    if not name and family is None:
        return StyleFont("txt")
    if _has_suffix(name, (".shx",)):
        return StyleFont(name)
    known = lookup(family) if family else None
    if known is None:
        known = lookup(name)
    if known is not None:
        if family is None:
            vertical, _ = _split_vertical(name)
            family = vertical + known.family
        return StyleFont(known.file, family, known.xdata_flags)
    if _has_suffix(name, _TRUETYPE_SUFFIXES):
        return StyleFont(name, family, 0)
    if family is None and name.isascii() and " " not in name:
        # A bare SHX name such as ``txt``, ``simplex`` or ``romans``.
        return StyleFont(name)
    face = family or name
    _, bare = _split_vertical(face)
    file, charset = _fallback_file(bare)
    return StyleFont(file, face, charset << 8)


def face_name(font: str | None, family: str | None = None) -> str | None:
    """The Windows face name for formats that name fonts that way (JWW, SXF).

    The inverse of :func:`style_font`: a known font file or family gives its
    face in the font's own locale (``msgothic.ttc`` -> ``ＭＳ ゴシック``), a
    face name passes through, and an SHX font gives ``None`` so the writer
    picks its own default.
    """
    for name in (family, font):
        known = lookup(name)
        if known is not None:
            vertical, _ = _split_vertical(name or "")
            return vertical + known.face
    if family and family.strip():
        return family.strip()
    name = (font or "").strip()
    if not name or name.upper() == "STANDARD" or _has_suffix(name, (".shx",)):
        return None
    if _has_suffix(name, _TRUETYPE_SUFFIXES):
        return name.rsplit(".", 1)[0]
    if name.isascii() and " " not in name:
        return None
    return name


__all__ = [
    "StyleFont",
    "TrueTypeFont",
    "face_name",
    "lookup",
    "style_font",
]
