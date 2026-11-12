//! Group code and value pairs, and the passes over all of them.

use std::collections::{HashMap, HashSet};
use std::sync::Arc;

use super::Context;
use crate::pystr::casefold;

/// The value of a pair: a literal of the writer, a text made for this pair or
/// a name many pairs share.
#[derive(Clone, Debug)]
pub enum Val {
    S(&'static str),
    O(Box<str>),
    A(Arc<str>),
}

/// The owner of a model space entity, written before the handle of the model
/// space block record is known. Compared by address, so no value of a drawing
/// can be taken for it.
pub(crate) static MODEL_SPACE_OWNER: &str = "\u{1}*Model_Space";

/// The owner of a paper space entity, in the same way.
pub(crate) static PAPER_SPACE_OWNER: &str = "\u{1}*Paper_Space";

const SMALL_INTEGERS: usize = 512;

fn small_integers() -> &'static [String] {
    static TABLE: std::sync::OnceLock<Vec<String>> = std::sync::OnceLock::new();
    TABLE.get_or_init(|| (0..SMALL_INTEGERS).map(|value| value.to_string()).collect())
}

impl Val {
    pub fn owned(text: String) -> Val {
        Val::O(text.into_boxed_str())
    }

    /// The decimal digits of an integer; the small ones (colour numbers, line
    /// widths, counts) are shared.
    pub fn integer(value: i64) -> Val {
        if (0..SMALL_INTEGERS as i64).contains(&value) {
            return Val::S(small_integers()[value as usize].as_str());
        }
        Val::owned(value.to_string())
    }

    pub(crate) fn is_model_space_owner(&self) -> bool {
        matches!(self, Val::S(text) if std::ptr::eq(*text, MODEL_SPACE_OWNER))
    }

    pub(crate) fn is_paper_space_owner(&self) -> bool {
        matches!(self, Val::S(text) if std::ptr::eq(*text, PAPER_SPACE_OWNER))
    }

    pub fn as_str(&self) -> &str {
        match self {
            Val::S(text) => text,
            Val::O(text) => text,
            Val::A(text) => text,
        }
    }
}

impl PartialEq for Val {
    fn eq(&self, other: &Self) -> bool {
        self.as_str() == other.as_str()
    }
}

pub type Pair = (i32, Val);

pub(crate) use crate::pystr::strip as py_strip;

/// ``_first_string``: the stripped value of the first pair with ``code``.
pub(crate) fn first_string(pairs: &[Pair], code: i32) -> Option<&str> {
    pairs
        .iter()
        .find(|(pair_code, _)| *pair_code == code)
        .map(|(_, value)| py_strip(value.as_str()))
}

/// How often each character outside ASCII occurs in the values of ``pairs``.
pub(crate) fn non_ascii_characters<'a>(pairs: impl Iterator<Item = &'a Pair>) -> Vec<(char, u64)> {
    let mut order: Vec<char> = Vec::new();
    let mut counts: HashMap<char, u64> = HashMap::new();
    for (_, value) in pairs {
        let text = value.as_str();
        if text.is_ascii() {
            continue;
        }
        for character in text.chars() {
            if character > '\u{7f}' {
                let count = counts.entry(character).or_insert(0);
                if *count == 0 {
                    order.push(character);
                }
                *count += 1;
            }
        }
    }
    order
        .into_iter()
        .map(|character| (character, counts[&character]))
        .collect()
}

fn is_invalid_symbol_character(character: char) -> bool {
    matches!(
        character,
        '<' | '>' | '/' | '\\' | '"' | ':' | ';' | '?' | '*' | '|' | ',' | '=' | '`'
    ) || character <= '\u{1f}'
}

fn names_symbol(code: i32, record: &str) -> bool {
    (code == 2 && matches!(record, "LAYER" | "LTYPE" | "STYLE" | "DIMSTYLE"))
        || matches!(code, 6..=8)
        || (code == 3 && record == "DIMENSION")
}

/// ``_valid_symbol_names``: ``_`` for the characters AutoCAD does not allow in
/// the name of a layer, linetype or style. ``sections`` are the parts of the
/// file in the order they are written.
pub(crate) fn valid_symbol_names(sections: &mut [Vec<Pair>], context: &mut Context<'_>) {
    let mut invalid: HashSet<String> = HashSet::new();
    {
        let mut record: &str = "";
        for (code, value) in sections.iter().flatten() {
            let text = value.as_str();
            if *code == 0 {
                record = text;
            } else if names_symbol(*code, record)
                && !text.is_empty()
                && text.chars().any(is_invalid_symbol_character)
                && !invalid.contains(text)
            {
                invalid.insert(text.to_string());
            }
        }
    }
    if invalid.is_empty() {
        return;
    }
    let mut taken: HashSet<String> = HashSet::new();
    for (code, value) in sections.iter().flatten() {
        let text = value.as_str();
        if matches!(code, 2 | 3 | 6 | 7 | 8) && !invalid.contains(text) && !taken.contains(text) {
            taken.insert(casefold(text));
        }
    }
    let mut names: Vec<&String> = invalid.iter().collect();
    names.sort();
    let mut renamed: HashMap<String, Arc<str>> = HashMap::new();
    for name in names {
        let base: String = name
            .chars()
            .map(|c| {
                if is_invalid_symbol_character(c) {
                    '_'
                } else {
                    c
                }
            })
            .collect();
        let mut candidate = base.clone();
        let mut serial = 1;
        while taken.contains(&casefold(&candidate)) {
            serial += 1;
            candidate = format!("{base}_{serial}");
        }
        taken.insert(casefold(&candidate));
        renamed.insert(name.clone(), Arc::from(candidate.as_str()));
    }
    let mut record = String::new();
    for (code, value) in sections.iter_mut().flatten() {
        if *code == 0 {
            record.clear();
            record.push_str(value.as_str());
        } else if names_symbol(*code, &record) {
            if let Some(replacement) = renamed.get(value.as_str()) {
                *value = Val::A(Arc::clone(replacement));
            }
        }
    }
    context.diagnose(
        "DXF_SYMBOL_NAME_NORMALIZED",
        "warning",
        format!(
            "{} layer, linetype or style name(s) with characters AutoCAD does not allow in a name \
             (<>/\\\":;?*|,=`) were written with '_' in their place.",
            renamed.len()
        ),
        None,
        Some("normalized"),
    );
}

/// ``%%`` that starts no control code, written as ``%%%%%%`` (two percent signs).
fn escape_percents(value: &str) -> String {
    let bytes = value.as_bytes();
    let mut out = String::with_capacity(value.len() + 8);
    let mut index = 0;
    while index < bytes.len() {
        if bytes[index] == b'%' && index + 1 < bytes.len() && bytes[index + 1] == b'%' {
            let rest = &bytes[index + 2..];
            if let Some(next) = rest.first() {
                if b"cdpouCDPOU%".contains(next) {
                    out.push_str(&value[index..index + 3]);
                    index += 3;
                    continue;
                }
                if rest.len() >= 3 && rest[..3].iter().all(u8::is_ascii_digit) {
                    out.push_str(&value[index..index + 5]);
                    index += 5;
                    continue;
                }
            }
            out.push_str("%%%%%%");
            index += 2;
            continue;
        }
        // copy one character (the bytes of a UTF-8 sequence stay together)
        let start = index;
        index += 1;
        while index < bytes.len() && (bytes[index] & 0xC0) == 0x80 {
            index += 1;
        }
        out.push_str(&value[start..index]);
    }
    out
}

/// ``_escape_literal_percents``.
pub(crate) fn escape_literal_percents(sections: &mut [Vec<Pair>]) {
    let mut text_record = false;
    for (code, value) in sections.iter_mut().flatten() {
        if *code == 0 {
            text_record = matches!(
                value.as_str(),
                "TEXT" | "MTEXT" | "ATTRIB" | "ATTDEF" | "DIMENSION"
            );
        } else if text_record && matches!(code, 1 | 3) && value.as_str().contains("%%") {
            *value = Val::owned(escape_percents(value.as_str()));
        }
    }
}

/// ``_single_line`` appended to ``out``: a value is one line, in the caret
/// notation of DXF.
fn push_single_line(out: &mut String, value: &str) {
    if !value.contains(['^', '\n', '\r']) {
        out.push_str(value);
        return;
    }
    let mut characters = value.chars().peekable();
    while let Some(character) = characters.next() {
        match character {
            '^' => out.push_str("^ "),
            '\r' => {
                if characters.peek() == Some(&'\n') {
                    characters.next();
                    out.push_str("^J");
                } else {
                    out.push_str("^M");
                }
            }
            '\n' => out.push_str("^J"),
            other => out.push(other),
        }
    }
}

/// ``_format_pairs``: a line for each code and each value.
pub(crate) fn format_pairs(sections: &[Vec<Pair>]) -> String {
    if sections.iter().all(Vec::is_empty) {
        return "\n".to_string();
    }
    let size: usize = sections
        .iter()
        .flatten()
        .map(|(_, value)| value.as_str().len() + 6)
        .sum();
    let mut out = String::with_capacity(size);
    let mut buffer = itoa_buffer();
    for (code, value) in sections.iter().flatten() {
        out.push_str(format_code(&mut buffer, *code));
        out.push('\n');
        push_single_line(&mut out, value.as_str());
        out.push('\n');
    }
    out
}

fn itoa_buffer() -> [u8; 12] {
    [0; 12]
}

/// The decimal digits of a group code.
fn format_code(buffer: &mut [u8; 12], code: i32) -> &str {
    let mut position = buffer.len();
    let negative = code < 0;
    let mut value = code.unsigned_abs();
    loop {
        position -= 1;
        buffer[position] = b'0' + (value % 10) as u8;
        value /= 10;
        if value == 0 {
            break;
        }
    }
    if negative {
        position -= 1;
        buffer[position] = b'-';
    }
    std::str::from_utf8(&buffer[position..]).expect("digits are ASCII")
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn percents_that_are_no_code_are_doubled() {
        assert_eq!(escape_percents("5%%"), "5%%%%%%");
        assert_eq!(escape_percents("%%c10"), "%%c10");
        assert_eq!(escape_percents("%%123"), "%%123");
        assert_eq!(escape_percents("%%%"), "%%%");
        assert_eq!(escape_percents("a%%b%%d"), "a%%%%%%b%%d");
        assert_eq!(escape_percents("図%%面"), "図%%%%%%面");
    }

    #[test]
    fn values_are_single_lines() {
        let mut out = String::new();
        push_single_line(&mut out, "a^b\r\nc\nd\re");
        assert_eq!(out, "a^ b^Jc^Jd^Me");
    }
}
