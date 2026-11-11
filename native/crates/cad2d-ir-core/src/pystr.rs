//! Python string behaviour the writer's output depends on: ``repr``,
//! ``str.casefold`` and ``str.upper``.

use unicode_general_category::{get_general_category, GeneralCategory};

/// ``str.casefold``.
pub fn casefold(text: &str) -> String {
    if text.is_ascii() {
        return text.to_ascii_lowercase();
    }
    caseless::default_case_fold_str(text)
}

/// ``str.upper``.
pub fn upper(text: &str) -> String {
    if text.is_ascii() {
        return text.to_ascii_uppercase();
    }
    text.to_uppercase()
}

/// ``str.isprintable`` for one character.
fn is_printable(character: char) -> bool {
    if character == ' ' {
        return true;
    }
    !matches!(
        get_general_category(character),
        GeneralCategory::Control
            | GeneralCategory::Format
            | GeneralCategory::Surrogate
            | GeneralCategory::PrivateUse
            | GeneralCategory::Unassigned
            | GeneralCategory::LineSeparator
            | GeneralCategory::ParagraphSeparator
            | GeneralCategory::SpaceSeparator
    )
}

/// ``repr`` of a Python ``str``.
pub fn repr(text: &str) -> String {
    let quote = if text.contains('\'') && !text.contains('"') {
        '"'
    } else {
        '\''
    };
    let mut out = String::with_capacity(text.len() + 2);
    out.push(quote);
    for character in text.chars() {
        match character {
            '\\' => out.push_str("\\\\"),
            '\n' => out.push_str("\\n"),
            '\r' => out.push_str("\\r"),
            '\t' => out.push_str("\\t"),
            c if c == quote => {
                out.push('\\');
                out.push(c);
            }
            c if is_printable(c) => out.push(c),
            c => {
                let code = c as u32;
                if code < 0x100 {
                    out.push_str(&format!("\\x{code:02x}"));
                } else if code < 0x10000 {
                    out.push_str(&format!("\\u{code:04x}"));
                } else {
                    out.push_str(&format!("\\U{code:08x}"));
                }
            }
        }
    }
    out.push(quote);
    out
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn repr_matches_python() {
        assert_eq!(repr("0"), "'0'");
        assert_eq!(repr("it's"), "\"it's\"");
        assert_eq!(repr("a\"b'c"), "'a\"b\\'c'");
        assert_eq!(repr("図面"), "'図面'");
        assert_eq!(repr("a\nb"), "'a\\nb'");
        assert_eq!(repr("\u{0}"), "'\\x00'");
        assert_eq!(repr("\u{3000}"), "'\\u3000'");
    }

    #[test]
    fn casefold_and_upper() {
        assert_eq!(casefold("ABC図"), "abc図");
        assert_eq!(casefold("Straße"), "strasse");
        assert_eq!(upper("straße"), "STRASSE");
    }
}
