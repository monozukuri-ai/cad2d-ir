//! How wide a text is drawn, for the start of a justified text
//! (``_text_cells`` and ``_justified_text_start``).

/// The characters below U+2E80 that take two bytes in the Windows code page 932
/// (Shift_JIS): Greek and Cyrillic letters, box drawing and the symbols of
/// JIS X 0208. A character in one of these ranges is one cell wide.
///
/// Made from the ``cp932`` codec of Python
/// (``len(chr(code).encode("cp932")) == 2``); ``native/tests/test_text_cells.py``
/// checks every character against the codec.
const WIDE_BELOW_2E80: [(u32, u32); 85] = [
    (0x00A2, 0x00A3),
    (0x00A7, 0x00A8),
    (0x00AC, 0x00AC),
    (0x00B0, 0x00B1),
    (0x00B4, 0x00B4),
    (0x00B6, 0x00B6),
    (0x00D7, 0x00D7),
    (0x00F7, 0x00F7),
    (0x0391, 0x03A1),
    (0x03A3, 0x03A9),
    (0x03B1, 0x03C1),
    (0x03C3, 0x03C9),
    (0x0401, 0x0401),
    (0x0410, 0x044F),
    (0x0451, 0x0451),
    (0x2010, 0x2010),
    (0x2015, 0x2016),
    (0x2018, 0x2019),
    (0x201C, 0x201D),
    (0x2020, 0x2021),
    (0x2025, 0x2026),
    (0x2030, 0x2030),
    (0x2032, 0x2033),
    (0x203B, 0x203B),
    (0x2103, 0x2103),
    (0x2116, 0x2116),
    (0x2121, 0x2121),
    (0x212B, 0x212B),
    (0x2160, 0x2169),
    (0x2170, 0x2179),
    (0x2190, 0x2193),
    (0x21D2, 0x21D2),
    (0x21D4, 0x21D4),
    (0x2200, 0x2200),
    (0x2202, 0x2203),
    (0x2207, 0x2208),
    (0x220B, 0x220B),
    (0x2211, 0x2212),
    (0x221A, 0x221A),
    (0x221D, 0x2220),
    (0x2225, 0x2225),
    (0x2227, 0x222C),
    (0x222E, 0x222E),
    (0x2234, 0x2235),
    (0x223D, 0x223D),
    (0x2252, 0x2252),
    (0x2260, 0x2261),
    (0x2266, 0x2267),
    (0x226A, 0x226B),
    (0x2282, 0x2283),
    (0x2286, 0x2287),
    (0x22A5, 0x22A5),
    (0x22BF, 0x22BF),
    (0x2312, 0x2312),
    (0x2460, 0x2473),
    (0x2500, 0x2503),
    (0x250C, 0x250C),
    (0x250F, 0x2510),
    (0x2513, 0x2514),
    (0x2517, 0x2518),
    (0x251B, 0x251D),
    (0x2520, 0x2520),
    (0x2523, 0x2525),
    (0x2528, 0x2528),
    (0x252B, 0x252C),
    (0x252F, 0x2530),
    (0x2533, 0x2534),
    (0x2537, 0x2538),
    (0x253B, 0x253C),
    (0x253F, 0x253F),
    (0x2542, 0x2542),
    (0x254B, 0x254B),
    (0x25A0, 0x25A1),
    (0x25B2, 0x25B3),
    (0x25BC, 0x25BD),
    (0x25C6, 0x25C7),
    (0x25CB, 0x25CB),
    (0x25CE, 0x25CF),
    (0x25EF, 0x25EF),
    (0x2605, 0x2606),
    (0x2640, 0x2640),
    (0x2642, 0x2642),
    (0x266A, 0x266A),
    (0x266D, 0x266D),
    (0x266F, 0x266F),
];

/// Whether a character is one cell wide (as wide as the text is high): two bytes
/// in code page 932, or a character that code page lacks from U+2E80 on. The
/// others are half a cell: one byte in the code page (ASCII, the half-width
/// katakana and the four characters the code page keeps in the private use
/// area), or a character it lacks below U+2E80.
pub(crate) fn is_wide(character: char) -> bool {
    let code = character as u32;
    if code >= 0x2E80 {
        return !((0xF8F0..=0xF8F3).contains(&code) || (0xFF61..=0xFF9F).contains(&code));
    }
    if code < 0xA2 {
        return false;
    }
    WIDE_BELOW_2E80
        .binary_search_by(|&(start, end)| {
            if code < start {
                std::cmp::Ordering::Greater
            } else if code > end {
                std::cmp::Ordering::Less
            } else {
                std::cmp::Ordering::Equal
            }
        })
        .is_ok()
}

/// The signs a reader draws for ``%%c``, ``%%d`` and ``%%p``, and the ones DXF
/// R12 writes as those codes: one cell each.
fn is_coded_symbol(character: char) -> bool {
    matches!(
        character,
        '\u{2300}' | '\u{2205}' | '\u{00d8}' | '\u{00f8}' | '\u{00b0}' | '\u{00b1}'
    )
}

/// ``text`` without each ``%%`` followed by one of ``letters``; ``removed``
/// counts them.
fn without_codes(text: &str, letters: &[u8], removed: &mut usize) -> String {
    let bytes = text.as_bytes();
    let mut out = String::with_capacity(text.len());
    let mut index = 0;
    while index < bytes.len() {
        if bytes[index] == b'%'
            && index + 2 < bytes.len()
            && bytes[index + 1] == b'%'
            && letters.contains(&bytes[index + 2])
        {
            *removed += 1;
            index += 3;
            continue;
        }
        // one character (the bytes of a UTF-8 sequence stay together)
        let start = index;
        index += 1;
        while index < bytes.len() && (bytes[index] & 0xC0) == 0x80 {
            index += 1;
        }
        out.push_str(&text[start..index]);
    }
    out
}

/// ``_text_cells``: the width of a text in character cells, each as wide as the
/// text is high.
///
/// A character that takes two bytes in Shift_JIS is one cell and any other half
/// a cell, which is how Jw_cad lays a text out. The symbols a reader draws for
/// ``%%c``, ``%%d`` and ``%%p`` count as one cell each, and the toggles
/// (``%%u``, ``%%o``, ``%%k``) as none.
pub fn text_cells(text: &str) -> f64 {
    let mut ignored = 0;
    let text = without_codes(text, b"uokUOK", &mut ignored);
    let mut codes = 0;
    let text = without_codes(&text, b"cdpCDP%", &mut codes);
    let mut cells = codes as f64;
    for character in text.chars() {
        cells += if is_coded_symbol(character) || is_wide(character) {
            1.0
        } else {
            0.5
        };
    }
    cells
}

/// ``_justified_text_start``: the left end of the baseline of a justified text,
/// from its character cells.
///
/// A DXF text carries two points: the first one is where the text starts, the
/// second one what it is justified to. A reader that knows the justification
/// takes the second point and lays the text out itself; one that does not
/// (Jw_cad) draws the text from the first point. So the first point is where a
/// text of that many cells starts, not a copy of the second point.
pub(crate) fn justified_text_start(
    insert: [f64; 2],
    text: &str,
    height: f64,
    rotation: f64,
    width_factor: Option<f64>,
    halign: &str,
    valign: &str,
) -> [f64; 2] {
    let factor = match width_factor {
        Some(factor) if factor.is_finite() && factor > 0.0 => factor,
        _ => 1.0,
    };
    let width = text_cells(text) * height * factor;
    let along = match halign {
        "center" => -width / 2.0,
        "right" => -width,
        _ => 0.0,
    };
    let up = match valign {
        "middle" => -height / 2.0,
        "top" => -height,
        _ => 0.0,
    };
    let angle = rotation.to_radians();
    let (cos, sin) = (angle.cos(), angle.sin());
    [
        insert[0] + along * cos - up * sin,
        insert[1] + along * sin + up * cos,
    ]
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn cells_follow_the_code_page() {
        assert_eq!(text_cells("ABC"), 1.5);
        assert_eq!(text_cells("図面"), 2.0);
        assert_eq!(text_cells("ｱｲｳ"), 1.5);
        assert_eq!(text_cells("%%c10"), 2.0);
        assert_eq!(text_cells("%%uAB%%u"), 1.0);
        assert_eq!(text_cells("±°"), 2.0);
        assert_eq!(text_cells("αβ"), 2.0);
        assert_eq!(text_cells("é"), 0.5);
        // the code page has the minus sign and the full-width hyphen-minus
        assert_eq!(text_cells("\u{2212}"), 1.0);
        assert_eq!(text_cells("\u{ff0d}"), 1.0);
        // one byte in the code page, or none below U+2E80: half a cell
        assert_eq!(text_cells("\u{f8f0}\u{20ac}"), 1.0);
        // "%%%" is a percent sign: one cell for the code, half a cell for the rest
        assert_eq!(text_cells("%%%%"), 1.5);
    }
}
