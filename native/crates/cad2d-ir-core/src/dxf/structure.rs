//! What AutoCAD and ODA read in a DXF R2000+ file besides the drawing: subclass
//! markers, handles, every symbol table, block records and owners.

use std::collections::{HashMap, HashSet};

use indexmap::IndexMap;

use super::pairs::{first_string, py_strip, Pair, Val};
use super::HandleAllocator;
use crate::pystr::casefold;

const TABLE_ORDER: [&str; 9] = [
    "VPORT",
    "LTYPE",
    "LAYER",
    "STYLE",
    "VIEW",
    "UCS",
    "APPID",
    "DIMSTYLE",
    "BLOCK_RECORD",
];
const SPACES: [&str; 2] = ["*Model_Space", "*Paper_Space"];

fn is_entity_common_code(code: i32) -> bool {
    matches!(code, 6 | 8 | 48 | 60 | 62 | 67 | 370 | 420)
}

fn is_handle_excluded(kind: &str) -> bool {
    matches!(kind, "SECTION" | "ENDSEC" | "ENDTAB" | "EOF")
}

fn table_record_subclass(kind: &str) -> Option<&'static str> {
    Some(match kind {
        "LTYPE" => "AcDbLinetypeTableRecord",
        "LAYER" => "AcDbLayerTableRecord",
        "STYLE" => "AcDbTextStyleTableRecord",
        "APPID" => "AcDbRegAppTableRecord",
        _ => return None,
    })
}

fn entity_subclasses(kind: &str) -> Option<&'static [&'static str]> {
    Some(match kind {
        "LINE" => &["AcDbLine"],
        "CIRCLE" => &["AcDbCircle"],
        "ARC" => &["AcDbCircle", "AcDbArc"],
        "POINT" => &["AcDbPoint"],
        "ELLIPSE" => &["AcDbEllipse"],
        "LWPOLYLINE" => &["AcDbPolyline"],
        "TEXT" => &["AcDbText"],
        "MTEXT" => &["AcDbMText"],
        "INSERT" => &["AcDbBlockReference"],
        "HATCH" => &["AcDbHatch"],
        "SPLINE" => &["AcDbSpline"],
        "IMAGE" => &["AcDbRasterImage"],
        "BLOCK" => &["AcDbBlockBegin"],
        "ENDBLK" => &["AcDbBlockEnd"],
        "ATTRIB" => &["AcDbText", "AcDbAttribute"],
        "SEQEND" => &["AcDbSequenceEnd"],
        "VIEWPORT" => &["AcDbViewport"],
        _ => return None,
    })
}

/// ``_r2010_record_body`` for the records this writer makes before its entities
/// (table records, ``BLOCK`` and ``ENDBLK``). The entities get their markers as
/// they are written.
fn record_body(kind: &str, body: Vec<Pair>) -> Vec<Pair> {
    if is_handle_excluded(kind) {
        return body;
    }
    if kind == "TABLE" {
        let mut result = Vec::with_capacity(body.len() + 1);
        let mut rest = body.into_iter();
        let mut first = rest.next();
        if let Some((2, _)) = &first {
            result.push(first.take().expect("checked"));
        }
        result.push((100, Val::S("AcDbSymbolTable")));
        result.extend(first);
        result.extend(rest);
        return result;
    }
    if let Some(subclass) = table_record_subclass(kind) {
        let mut result = Vec::with_capacity(body.len() + 2);
        result.push((100, Val::S("AcDbSymbolTableRecord")));
        result.push((100, Val::S(subclass)));
        result.extend(body);
        return result;
    }
    let Some(subclasses) = entity_subclasses(kind) else {
        return body;
    };
    let split = body
        .iter()
        .take_while(|(code, _)| is_entity_common_code(*code))
        .count();
    let mut result = Vec::with_capacity(body.len() + 1 + subclasses.len());
    let mut rest = body.into_iter();
    result.push((100, Val::S("AcDbEntity")));
    result.extend(rest.by_ref().take(split));
    result.extend(subclasses.iter().map(|subclass| (100, Val::S(subclass))));
    result.extend(rest);
    result
}

/// ``_add_r2010_subclasses``.
pub(crate) fn add_r2010_subclasses(pairs: Vec<Pair>) -> Vec<Pair> {
    let mut result: Vec<Pair> = Vec::with_capacity(pairs.len() + pairs.len() / 4);
    let mut pending: Option<(String, Vec<Pair>)> = None;
    for pair in pairs {
        if pair.0 == 0 {
            if let Some((kind, body)) = pending.take() {
                result.extend(record_body(&kind, body));
            }
            let kind = py_strip(pair.1.as_str()).to_ascii_uppercase();
            result.push(pair);
            pending = Some((kind, Vec::new()));
        } else if let Some((_, body)) = pending.as_mut() {
            body.push(pair);
        } else {
            result.push(pair);
        }
    }
    if let Some((kind, body)) = pending.take() {
        result.extend(record_body(&kind, body));
    }
    result
}

/// ``_add_record_handles``: a handle (group 5) for every record, behind the
/// name of a table. Returns the pairs and the first handle given.
pub(crate) fn add_record_handles(
    pairs: Vec<Pair>,
    allocator: &mut HandleAllocator,
) -> (Vec<Pair>, Option<Val>) {
    let mut result: Vec<Pair> = Vec::with_capacity(pairs.len() + pairs.len() / 4);
    let mut primary: Option<Val> = None;
    let mut pending_table: Option<Val> = None;
    for (code, value) in pairs {
        let record = if code == 0 {
            Some(py_strip(value.as_str()).to_ascii_uppercase())
        } else {
            None
        };
        result.push((code, value));
        if pending_table.is_some() && code == 2 {
            let handle = pending_table.take().expect("checked");
            if primary.is_none() {
                primary = Some(handle.clone());
            }
            result.push((5, handle));
            continue;
        }
        let Some(record) = record else { continue };
        if is_handle_excluded(&record) {
            continue;
        }
        let handle = allocator.allocate();
        if record == "TABLE" {
            pending_table = Some(handle);
            continue;
        }
        if primary.is_none() {
            primary = Some(handle.clone());
        }
        result.push((5, handle));
    }
    (result, primary)
}

type Record = (String, Vec<Pair>);

/// ``_pairs_to_records``: the records of ``pairs`` by kind, without their group 0.
fn pairs_to_records(pairs: Vec<Pair>) -> Vec<Record> {
    let mut records: Vec<Record> = Vec::new();
    let mut current: Option<Record> = None;
    for pair in pairs {
        if pair.0 == 0 {
            if let Some(record) = current.take() {
                records.push(record);
            }
            current = Some((py_strip(pair.1.as_str()).to_ascii_uppercase(), Vec::new()));
        } else if let Some((_, body)) = current.as_mut() {
            body.push(pair);
        }
    }
    if let Some(record) = current.take() {
        records.push(record);
    }
    records
}

/// ``_r2010_owned``: ``body`` with its owner (group 330) behind its handle.
fn push_owned(output: &mut Vec<Pair>, body: Vec<Pair>, owner: &Val) {
    if owner.as_str().is_empty() || body.iter().any(|(code, _)| *code == 330) {
        output.extend(body);
        return;
    }
    let mut placed = false;
    for pair in body {
        let is_handle = matches!(pair.0, 5 | 105);
        output.push(pair);
        if is_handle && !placed {
            output.push((330, owner.clone()));
            placed = true;
        }
    }
}

/// The ``DIMENSION`` records of ``pairs`` name dimension styles in group 3.
fn dimension_styles_of(pairs: &[Pair], styles: &mut Vec<String>) {
    let mut in_dimension = false;
    let mut named = false;
    for (code, value) in pairs {
        if *code == 0 {
            in_dimension = py_strip(value.as_str()).eq_ignore_ascii_case("DIMENSION");
            named = false;
        } else if in_dimension && *code == 3 && !named {
            named = true;
            let style = py_strip(value.as_str());
            if !style.is_empty() && !styles.iter().any(|known| known == style) {
                styles.push(style.to_string());
            }
        }
    }
}

/// ``_r2010_structure``: complete the tables, blocks and entities of the file.
///
/// The tables and blocks are returned; the entities are completed in place.
/// A model space entity was written with a placeholder for its owner behind its
/// handle (the handle of the model space block record is given here, after the
/// handles of the entities), which is what ``_r2010_owned`` puts there.
pub(crate) fn r2010_structure(
    table_pairs: Vec<Pair>,
    block_pairs: Vec<Pair>,
    entity_pairs: &mut [Pair],
    allocator: &mut HandleAllocator,
) -> (Vec<Pair>, Vec<Pair>) {
    // name -> (head, records)
    let mut tables: IndexMap<String, (Vec<Pair>, Vec<Record>)> = IndexMap::new();
    let mut current: Option<String> = None;
    for (kind, body) in pairs_to_records(table_pairs) {
        if kind == "TABLE" {
            let name = first_string(&body, 2).unwrap_or("").to_uppercase();
            tables.insert(name.clone(), (body, Vec::new()));
            current = Some(name);
        } else if kind == "ENDTAB" {
            current = None;
        } else if let Some(name) = &current {
            tables
                .get_mut(name)
                .expect("the current table exists")
                .1
                .push((kind, body));
        }
    }
    for name in TABLE_ORDER {
        if !tables.contains_key(name) {
            let head: Vec<Pair> = vec![
                (2, Val::S(name)),
                (5, allocator.allocate()),
                (100, Val::S("AcDbSymbolTable")),
                (70, Val::S("0")),
            ];
            tables.insert(name.to_string(), (head, Vec::new()));
        }
    }

    fn names(tables: &IndexMap<String, (Vec<Pair>, Vec<Record>)>, table: &str) -> HashSet<String> {
        tables[table]
            .1
            .iter()
            .map(|(_, body)| casefold(first_string(body, 2).unwrap_or("")))
            .collect()
    }

    fn add(
        tables: &mut IndexMap<String, (Vec<Pair>, Vec<Record>)>,
        allocator: &mut HandleAllocator,
        table: &'static str,
        subclass: &'static str,
        name: Val,
        fields: Vec<Pair>,
    ) -> Val {
        let handle = allocator.allocate();
        let code = if table == "DIMSTYLE" { 105 } else { 5 };
        let mut body: Vec<Pair> = vec![
            (code, handle.clone()),
            (100, Val::S("AcDbSymbolTableRecord")),
            (100, Val::S(subclass)),
            (2, name),
            (70, Val::S("0")),
        ];
        body.extend(fields);
        tables
            .get_mut(table)
            .expect("every table exists")
            .1
            .push((table.to_string(), body));
        handle
    }

    let linetypes = names(&tables, "LTYPE");
    for (name, description) in [
        ("ByBlock", ""),
        ("ByLayer", ""),
        ("Continuous", "Solid line"),
    ] {
        if !linetypes.contains(&casefold(name)) {
            add(
                &mut tables,
                allocator,
                "LTYPE",
                "AcDbLinetypeTableRecord",
                Val::S(name),
                vec![
                    (3, Val::S(description)),
                    (72, Val::S("65")),
                    (73, Val::S("0")),
                    (40, Val::S("0")),
                ],
            );
        }
    }
    if !names(&tables, "APPID").contains("acad") {
        add(
            &mut tables,
            allocator,
            "APPID",
            "AcDbRegAppTableRecord",
            Val::S("ACAD"),
            Vec::new(),
        );
    }
    let standard_style: Option<Val> = tables["STYLE"]
        .1
        .iter()
        .find(|(_, body)| casefold(first_string(body, 2).unwrap_or("")) == "standard")
        .and_then(|(_, body)| first_string(body, 5).map(|handle| Val::owned(handle.to_string())));

    let mut dimension_styles: Vec<String> = vec!["Standard".to_string()];
    dimension_styles_of(&block_pairs, &mut dimension_styles);
    dimension_styles_of(entity_pairs, &mut dimension_styles);
    let block_records = pairs_to_records(block_pairs);
    let mut known_styles = names(&tables, "DIMSTYLE");
    dimension_styles.sort_by_cached_key(|name| casefold(name));
    for name in dimension_styles {
        let folded = casefold(&name);
        if !known_styles.contains(&folded) {
            known_styles.insert(folded);
            let fields = match &standard_style {
                Some(handle) if !handle.as_str().is_empty() => vec![(340, handle.clone())],
                _ => Vec::new(),
            };
            add(
                &mut tables,
                allocator,
                "DIMSTYLE",
                "AcDbDimStyleTableRecord",
                Val::owned(name),
                fields,
            );
        }
    }

    let block_names: Vec<String> = block_records
        .iter()
        .filter(|(kind, _)| kind == "BLOCK")
        .map(|(_, body)| first_string(body, 2).unwrap_or("").to_string())
        .collect();
    let mut record_handles: HashMap<String, Val> = tables["BLOCK_RECORD"]
        .1
        .iter()
        .map(|(_, body)| {
            (
                casefold(first_string(body, 2).unwrap_or("")),
                Val::owned(first_string(body, 5).unwrap_or("").to_string()),
            )
        })
        .collect();
    for name in SPACES
        .iter()
        .map(|space| space.to_string())
        .chain(block_names.iter().cloned())
    {
        let folded = casefold(&name);
        record_handles.entry(folded).or_insert_with(|| {
            add(
                &mut tables,
                allocator,
                "BLOCK_RECORD",
                "AcDbBlockTableRecord",
                Val::owned(name),
                vec![(280, Val::S("1")), (281, Val::S("0"))],
            )
        });
    }

    let mut table_out: Vec<Pair> = vec![(0, Val::S("SECTION")), (2, Val::S("TABLES"))];
    let mut order: Vec<String> = TABLE_ORDER.iter().map(|name| name.to_string()).collect();
    order.extend(
        tables
            .keys()
            .filter(|name| !TABLE_ORDER.contains(&name.as_str()))
            .cloned(),
    );
    for name in order {
        let (head, records) = tables.swap_remove(&name).expect("listed tables exist");
        let table_handle = match first_string(&head, 5) {
            Some(handle) if !handle.is_empty() => Val::owned(handle.to_string()),
            _ => allocator.allocate(),
        };
        let mut head: Vec<Pair> = head
            .into_iter()
            .filter(|(code, _)| !matches!(code, 5 | 70 | 330))
            .collect();
        let position = usize::from(matches!(head.first(), Some((2, _))));
        head.splice(
            position..position,
            [(5, table_handle.clone()), (330, Val::S("0"))],
        );
        head.push((70, Val::owned(records.len().to_string())));
        if name == "DIMSTYLE" {
            head.push((100, Val::S("AcDbDimStyleTable")));
            head.push((71, Val::owned(records.len().to_string())));
            for (_, body) in &records {
                head.push((
                    340,
                    Val::owned(first_string(body, 105).unwrap_or("").to_string()),
                ));
            }
        }
        table_out.push((0, Val::S("TABLE")));
        table_out.extend(head);
        for (kind, body) in records {
            table_out.push((0, Val::owned(kind)));
            push_owned(&mut table_out, body, &table_handle);
        }
        table_out.push((0, Val::S("ENDTAB")));
    }
    table_out.push((0, Val::S("ENDSEC")));

    let empty = Val::S("");
    let mut block_out: Vec<Pair> = vec![(0, Val::S("SECTION")), (2, Val::S("BLOCKS"))];
    let written: HashSet<String> = block_names.iter().map(|name| casefold(name)).collect();
    for space in SPACES {
        if written.contains(&casefold(space)) {
            continue;
        }
        let owner = record_handles[&casefold(space)].clone();
        let paper = space == "*Paper_Space";
        block_out.push((0, Val::S("BLOCK")));
        block_out.push((5, allocator.allocate()));
        block_out.push((330, owner.clone()));
        block_out.push((100, Val::S("AcDbEntity")));
        if paper {
            block_out.push((67, Val::S("1")));
        }
        block_out.push((8, Val::S("0")));
        block_out.push((100, Val::S("AcDbBlockBegin")));
        block_out.push((2, Val::S(space)));
        block_out.push((70, Val::S("0")));
        block_out.push((10, Val::S("0")));
        block_out.push((20, Val::S("0")));
        block_out.push((30, Val::S("0")));
        block_out.push((3, Val::S(space)));
        block_out.push((1, Val::S("")));
        block_out.push((0, Val::S("ENDBLK")));
        block_out.push((5, allocator.allocate()));
        block_out.push((330, owner));
        block_out.push((100, Val::S("AcDbEntity")));
        if paper {
            block_out.push((67, Val::S("1")));
        }
        block_out.push((8, Val::S("0")));
        block_out.push((100, Val::S("AcDbBlockEnd")));
    }
    let mut owner: Val = empty.clone();
    let mut parent: Val = empty.clone();
    for (kind, mut body) in block_records {
        if kind == "SECTION" || kind == "ENDSEC" {
            continue;
        }
        if kind == "BLOCK" {
            let name = first_string(&body, 2).unwrap_or("").to_string();
            owner = record_handles
                .get(&casefold(&name))
                .cloned()
                .unwrap_or_else(|| empty.clone());
            if name.starts_with('*') {
                // an anonymous block (*D for the geometry of a dimension)
                for pair in body.iter_mut() {
                    if pair.0 == 70 {
                        pair.1 = Val::S("1");
                    }
                }
            }
        }
        let is_subrecord = matches!(kind.as_str(), "ATTRIB" | "VERTEX" | "SEQEND");
        let record_owner = if is_subrecord && !parent.as_str().is_empty() {
            parent.clone()
        } else {
            owner.clone()
        };
        if matches!(kind.as_str(), "INSERT" | "POLYLINE") {
            parent = Val::owned(first_string(&body, 5).unwrap_or("").to_string());
        } else if !is_subrecord {
            parent = empty.clone();
        }
        block_out.push((0, Val::owned(kind)));
        push_owned(&mut block_out, body, &record_owner);
    }
    block_out.push((0, Val::S("ENDSEC")));

    // shared: every model space entity names the one, every paper space entity
    // the other
    let model = Val::A(std::sync::Arc::from(
        record_handles["*model_space"].as_str(),
    ));
    let paper = Val::A(std::sync::Arc::from(
        record_handles["*paper_space"].as_str(),
    ));
    for pair in entity_pairs.iter_mut() {
        if pair.0 != 330 {
            continue;
        }
        if pair.1.is_model_space_owner() {
            pair.1 = model.clone();
        } else if pair.1.is_paper_space_owner() {
            pair.1 = paper.clone();
        }
    }
    (table_out, block_out)
}

/// ``_layer_record_handles``: the handles of the written LAYER records by layer
/// name.
pub(crate) fn layer_record_handles(table_pairs: &[Pair]) -> HashMap<String, Val> {
    let mut handles: HashMap<String, Val> = HashMap::new();
    let mut in_layer = false;
    let mut name: Option<&str> = None;
    let mut handle: Option<&Val> = None;
    let mut flush = |name: Option<&str>, handle: Option<&Val>| {
        if let (Some(name), Some(handle)) = (name, handle) {
            if !name.is_empty() && !py_strip(handle.as_str()).is_empty() {
                handles.insert(
                    name.to_string(),
                    Val::owned(py_strip(handle.as_str()).to_string()),
                );
            }
        }
    };
    for (code, value) in table_pairs {
        if *code == 0 {
            if in_layer {
                flush(name, handle);
            }
            in_layer = py_strip(value.as_str()).eq_ignore_ascii_case("LAYER");
            name = None;
            handle = None;
        } else if in_layer {
            if *code == 2 && name.is_none() {
                name = Some(py_strip(value.as_str()));
            } else if *code == 5 && handle.is_none() {
                handle = Some(value);
            }
        }
    }
    if in_layer {
        flush(name, handle);
    }
    handles
}
