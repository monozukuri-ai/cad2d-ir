//! The text of a DXF R12 file (``_store_r12_text``).
//!
//! R12 has no Unicode: the values of the file are stored in one Windows code
//! page, named in ``$DWGCODEPAGE``. Which code page that is, and what a
//! character it lacks becomes, is decided with the codecs of Python (the host);
//! this pass counts the characters and writes what was decided.

use std::collections::{HashMap, HashSet};

use super::pairs::{Pair, Val};
use super::{Context, WriteError};
use crate::pystr::casefold;

/// The records whose group 1 is text a reader draws.
fn is_text_record(record: &str) -> bool {
    matches!(record, "TEXT" | "ATTRIB" | "ATTDEF" | "DIMENSION")
}

/// Groups that hold the name of a layer, block, linetype or text style, or
/// refer to one.
fn is_name_code(code: i32) -> bool {
    matches!(code, 2 | 6 | 7 | 8)
}

/// ``str.translate``: each character that has an entry as that entry.
fn translate(value: &str, table: &HashMap<char, String>) -> String {
    let mut out = String::with_capacity(value.len());
    for character in value.chars() {
        match table.get(&character) {
            Some(replacement) => out.push_str(replacement),
            None => out.push(character),
        }
    }
    out
}

fn count(order: &mut Vec<char>, counts: &mut HashMap<char, u64>, character: char) {
    let entry = counts.entry(character).or_insert(0);
    if *entry == 0 {
        order.push(character);
    }
    *entry += 1;
}

/// ``_store_r12_text``: make the values of a DXF R12 file storable in one
/// Windows code page.
///
/// In the text of TEXT, ATTRIB, ATTDEF and DIMENSION records the diameter sign
/// becomes ``%%c``, and the degree and plus-minus signs become ``%%d`` and
/// ``%%p`` where the code page lacks them. A character the code page still
/// cannot store becomes its compatibility form where the code page has that
/// (``2`` for ``²``), otherwise ``?``, or ``_`` in a name or a font file; two
/// names stay two names (the later one gets a number).
pub(crate) fn store_r12_text(
    sections: &mut [Vec<Pair>],
    context: &mut Context<'_>,
) -> Result<(), WriteError> {
    // the characters outside ASCII: of the text values, and of all the others
    let mut text_order: Vec<char> = Vec::new();
    let mut text_counts: HashMap<char, u64> = HashMap::new();
    let mut other_order: Vec<char> = Vec::new();
    let mut other_counts: HashMap<char, u64> = HashMap::new();
    {
        let mut text_record = false;
        for (code, value) in sections.iter().flatten() {
            let value = value.as_str();
            if *code == 0 {
                text_record = is_text_record(value);
            }
            if value.is_ascii() {
                continue;
            }
            let (order, counts) = if *code == 1 && text_record {
                (&mut text_order, &mut text_counts)
            } else {
                (&mut other_order, &mut other_counts)
            };
            for character in value.chars().filter(|character| *character > '\u{7f}') {
                count(order, counts, character);
            }
        }
    }
    let listed = |order: Vec<char>, counts: &HashMap<char, u64>| -> Vec<(char, u64)> {
        order
            .into_iter()
            .map(|character| (character, counts[&character]))
            .collect()
    };
    let plan = context.host.r12_text_plan(
        &listed(text_order, &text_counts),
        &listed(other_order, &other_counts),
    )?;
    let coded: HashMap<char, String> = plan.coded.into_iter().collect();
    let lost: HashMap<char, String> = plan.lost.into_iter().collect();
    let lost_in_names: HashMap<char, String> = lost
        .iter()
        .map(|(character, replacement)| {
            (
                *character,
                if replacement == "?" {
                    "_".to_string()
                } else {
                    replacement.clone()
                },
            )
        })
        .collect();

    // the names that keep all their characters
    let mut taken: HashSet<String> = HashSet::new();
    if !lost.is_empty() {
        for (code, value) in sections.iter().flatten() {
            let value = value.as_str();
            if (is_name_code(*code) || *code == 3)
                && !value.chars().any(|character| lost.contains_key(&character))
            {
                taken.insert(casefold(value));
            }
        }
    }
    let mut names: HashMap<String, String> = HashMap::new();
    let mut record = String::new();
    let mut after_codepage_name = false;
    for (code, value) in sections.iter_mut().flatten() {
        if *code == 0 {
            record.clear();
            record.push_str(value.as_str());
        }
        let is_codepage = *code == 3 && after_codepage_name;
        after_codepage_name = *code == 9 && value.as_str() == "$DWGCODEPAGE";
        if is_codepage {
            *value = Val::owned(plan.codepage.clone());
            continue;
        }
        if value.as_str().is_ascii() {
            continue;
        }
        let mut stored = value.as_str().to_string();
        if *code == 1 && is_text_record(&record) {
            stored = translate(&stored, &coded);
        }
        if !lost.is_empty() {
            if is_name_code(*code)
                || (*code == 3 && matches!(record.as_str(), "BLOCK" | "DIMENSION"))
            {
                // ``_r12_name``: the name without the characters the code page
                // lacks, as its own name
                stored = match names.get(&stored) {
                    Some(known) => known.clone(),
                    None => {
                        let mut name = translate(&stored, &lost_in_names);
                        if name != stored {
                            let base = name.clone();
                            let mut serial = 1;
                            while taken.contains(&casefold(&name)) {
                                serial += 1;
                                name = format!("{base}_{serial}");
                            }
                            taken.insert(casefold(&name));
                        }
                        names.insert(stored.clone(), name.clone());
                        name
                    }
                };
            } else if *code == 4 || (*code == 3 && record == "STYLE") {
                stored = translate(&stored, &lost_in_names);
            } else {
                stored = translate(&stored, &lost);
            }
        }
        *value = Val::owned(stored);
    }

    if plan.codepage != "ANSI_932" {
        context.diagnose(
            "DXF_R12_CODEPAGE_SELECTED",
            "info",
            format!(
                "DXF R12 text is stored in the code page {}, which the characters of the \
                 drawing ask for.",
                plan.codepage
            ),
            None,
            Some("normalized"),
        );
    }
    if plan.replaced > 0 {
        context.diagnose(
            "DXF_R12_CHARACTERS_REPLACED",
            "warning",
            format!(
                "{} character(s) that the code page {} cannot store were written as a similar \
                 character ({}, such as '2' for '²') or as '?' ('_' in a name) in DXF R12.",
                plan.replaced, plan.codepage, plan.similar
            ),
            None,
            Some("approximated"),
        );
    }
    Ok(())
}
