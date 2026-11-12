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

/// ``repr`` of a Python ``float`` (what ``str`` gives as well): the shortest
/// digits that read back as the value, in positional notation from 1e-4 up to
/// 1e16 and with an exponent outside of it.
pub fn float_repr(value: f64) -> String {
    if value.is_nan() {
        return "nan".to_string();
    }
    if value.is_infinite() {
        return if value > 0.0 { "inf" } else { "-inf" }.to_string();
    }
    let scientific = format!("{value:e}");
    let (mantissa, exponent) = scientific
        .split_once('e')
        .expect("scientific notation has an exponent");
    let exponent: i32 = exponent.parse().expect("exponent is an integer");
    let negative = mantissa.starts_with('-');
    let digits: String = mantissa.chars().filter(|c| c.is_ascii_digit()).collect();
    let mut out = String::with_capacity(24);
    if negative {
        out.push('-');
    }
    if (-4..16).contains(&exponent) {
        if exponent < 0 {
            out.push_str("0.");
            for _ in 0..(-exponent - 1) {
                out.push('0');
            }
            out.push_str(&digits);
        } else {
            let whole = (exponent + 1) as usize;
            if digits.len() <= whole {
                out.push_str(&digits);
                for _ in digits.len()..whole {
                    out.push('0');
                }
                out.push_str(".0");
            } else {
                out.push_str(&digits[..whole]);
                out.push('.');
                out.push_str(&digits[whole..]);
            }
        }
        return out;
    }
    out.push_str(&digits[..1]);
    if digits.len() > 1 {
        out.push('.');
        out.push_str(&digits[1..]);
    }
    out.push('e');
    out.push(if exponent < 0 { '-' } else { '+' });
    out.push_str(&format!("{:02}", exponent.abs()));
    out
}

/// ``str.strip`` of Python: Unicode white space and the separators U+001C to U+001F.
pub fn strip(text: &str) -> &str {
    text.trim_matches(|c: char| c.is_whitespace() || ('\u{1c}'..='\u{1f}').contains(&c))
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
    fn float_repr_matches_python() {
        // python3 -c "print(repr(0.1), repr(1.0), repr(123456.789), repr(1e16), repr(1.5e-5), repr(0.0001), repr(-0.0), repr(1e15), repr(2.5e-7))"
        assert_eq!(float_repr(0.1), "0.1");
        assert_eq!(float_repr(1.0), "1.0");
        assert_eq!(float_repr(123456.789), "123456.789");
        assert_eq!(float_repr(1e16), "1e+16");
        assert_eq!(float_repr(1.5e-5), "1.5e-05");
        assert_eq!(float_repr(0.0001), "0.0001");
        assert_eq!(float_repr(-0.0), "-0.0");
        assert_eq!(float_repr(1e15), "1000000000000000.0");
        assert_eq!(float_repr(2.5e-7), "2.5e-07");
    }

    #[test]
    fn casefold_and_upper() {
        assert_eq!(casefold("ABC図"), "abc図");
        assert_eq!(casefold("Straße"), "strasse");
        assert_eq!(upper("straße"), "STRASSE");
    }
}
