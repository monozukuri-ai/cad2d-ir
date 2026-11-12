//! The LTYPE, LAYER and STYLE tables: ``_normalized_tables`` and ``_tables_to_pairs``.

use std::collections::BTreeSet;

use indexmap::IndexMap;

use super::common::{
    dxf_lineweight, hex_color, layer_val, lineweight_is_normalized, lineweight_of, nearest_aci,
    py_str,
};
use super::dimension::{self, GeometryBlocks};
use super::layout::LayoutScope;
use super::pairs::{Pair, Val};
use super::{Context, WriteError};
use crate::model::{Document, Entity, Shape};
use crate::num::{num, py_sum};
use crate::pystr::{casefold, repr, upper};
use crate::value::{Map, Value};

/// A table of the document: its records by name.
pub type Table = IndexMap<String, Map>;

pub(crate) struct Normalized {
    pub(crate) layers: Table,
    pub(crate) linetypes: Table,
    pub(crate) text_styles: Table,
}

fn definitions(raw: Option<&Value>) -> Table {
    let Some(Value::Map(raw)) = raw else {
        return Table::new();
    };
    raw.iter()
        .map(|(key, value)| {
            let definition = match value {
                Value::Map(map) => map.clone(),
                _ => Map::new(),
            };
            (key.clone(), definition)
        })
        .collect()
}

fn map_of(entries: Vec<(&str, Value)>) -> Map {
    entries
        .into_iter()
        .map(|(key, value)| (key.to_string(), value))
        .collect()
}

/// ``sorted(names, key=str.casefold)``.
fn sorted_casefold(names: BTreeSet<String>) -> Vec<String> {
    let mut names: Vec<String> = names.into_iter().collect();
    names.sort_by_cached_key(|name| casefold(name));
    names
}

fn ensure_table_record(
    table: &mut Table,
    name: &str,
    default: Map,
    table_name: &str,
    context: &mut Context<'_>,
) {
    let wanted = upper(name);
    if table.keys().any(|existing| upper(existing) == wanted) {
        return;
    }
    table.insert(name.to_string(), default);
    context.diagnose(
        "DXF_TABLE_DEFAULTED",
        "info",
        format!(
            "DXF {table_name} record {} was synthesized from defaults.",
            repr(name)
        ),
        None,
        Some("normalized"),
    );
}

fn layer_default() -> Map {
    map_of(vec![
        ("color", Value::Int(7)),
        ("linetype", Value::Str("CONTINUOUS".to_string())),
        ("plot", Value::Bool(true)),
    ])
}

/// What the entities of a document name: layers, linetypes and text styles.
#[derive(Default)]
struct Referenced {
    layers: BTreeSet<String>,
    linetypes: BTreeSet<String>,
    styles: BTreeSet<String>,
    /// Line weights DXF has no value for, of entities.
    normalized_lineweights: u64,
}

impl Referenced {
    fn visit(&mut self, entity: &Entity) -> Result<(), WriteError> {
        if lineweight_of(entity).is_some_and(lineweight_is_normalized) {
            self.normalized_lineweights += 1;
        }
        match &entity.layer {
            Some(layer) => {
                if !self.layers.contains(layer.as_ref()) {
                    self.layers.insert(layer.to_string());
                }
            }
            None => {
                let layer = layer_val(entity)?;
                if !self.layers.contains(layer.as_str()) {
                    self.layers.insert(layer.as_str().to_string());
                }
            }
        }
        if let Some(linetype) = &entity.linetype {
            if !self.linetypes.contains(linetype.as_ref()) {
                self.linetypes.insert(linetype.to_string());
            }
        }
        match &entity.shape {
            Shape::Text(text) => {
                if let Some(style) = &text.style {
                    if !self.styles.contains(style.as_ref()) {
                        self.styles.insert(style.to_string());
                    }
                }
            }
            Shape::Other => match entity.kind() {
                Some("DIMENSION") => {
                    for style in dimension::referenced_styles(entity)? {
                        self.styles.insert(style);
                    }
                }
                Some("TEXT" | "MTEXT") => {
                    if let Some(Value::Str(style)) = entity.extra_get("style") {
                        if !self.styles.contains(style.as_str()) {
                            self.styles.insert(style.clone());
                        }
                    }
                }
                _ => {}
            },
            _ => {}
        }
        Ok(())
    }
}

/// ``_normalized_tables``: the three tables with a record for every layer,
/// linetype and text style the entities name.
pub(crate) fn normalized_tables(
    document: &Document,
    layouts: &[LayoutScope<'_>],
    generated: &GeometryBlocks,
    context: &mut Context<'_>,
) -> Result<Normalized, WriteError> {
    let tables = document.tables.as_ref();
    let mut layers = definitions(tables.and_then(|t| t.layers.as_ref()));
    let mut linetypes = definitions(tables.and_then(|t| t.linetypes.as_ref()));
    let mut text_styles = definitions(tables.and_then(|t| t.text_styles.as_ref()));

    let mut referenced = Referenced::default();
    for entity in &document.entities {
        referenced.visit(entity)?;
    }
    let blocks = tables.and_then(|t| t.blocks.as_ref());
    for block in blocks
        .into_iter()
        .flat_map(|blocks| blocks.values())
        .chain(generated.blocks.values())
    {
        for entity in &block.entities {
            referenced.visit(entity)?;
        }
    }
    for layout in layouts {
        for entity in layout.entities.iter().flatten() {
            referenced.visit(entity)?;
        }
    }
    // the layers of the viewports, and the ones they freeze
    for layout in layouts {
        let Some(Value::List(viewports)) = layout.raw.get("viewports") else {
            continue;
        };
        for viewport in viewports {
            let Value::Map(viewport) = viewport else {
                continue;
            };
            let layer = match viewport.get("layer") {
                Some(value) if value.truthy() => py_str(value, "viewport layer")?.into_owned(),
                _ => "0".to_string(),
            };
            referenced.layers.insert(layer);
            match viewport.get("frozen_layers") {
                Some(Value::List(names)) => {
                    for name in names {
                        referenced
                            .layers
                            .insert(py_str(name, "viewport frozen layer")?.into_owned());
                    }
                }
                Some(Value::Str(names)) => {
                    // a string is walked a character at a time, as Python walks it
                    for name in names.chars() {
                        referenced.layers.insert(name.to_string());
                    }
                }
                _ => {}
            }
        }
    }
    for definition in layers.values() {
        if let Some(Value::Str(linetype)) = definition.get("linetype") {
            referenced.linetypes.insert(linetype.clone());
        }
    }

    ensure_table_record(&mut layers, "0", layer_default(), "LAYER", context);
    for name in sorted_casefold(referenced.layers) {
        ensure_table_record(&mut layers, &name, layer_default(), "LAYER", context);
    }

    ensure_table_record(
        &mut linetypes,
        "CONTINUOUS",
        map_of(vec![
            ("description", Value::Str("Continuous line".to_string())),
            ("pattern_mm", Value::List(Vec::new())),
        ]),
        "LTYPE",
        context,
    );
    for name in sorted_casefold(referenced.linetypes) {
        let default = map_of(vec![
            (
                "description",
                Value::Str(format!("Defaulted linetype {name}")),
            ),
            ("pattern_mm", Value::List(Vec::new())),
        ]);
        ensure_table_record(&mut linetypes, &name, default, "LTYPE", context);
    }

    ensure_table_record(
        &mut text_styles,
        "STANDARD",
        map_of(vec![("font", Value::Str("txt".to_string()))]),
        "STYLE",
        context,
    );
    for name in sorted_casefold(referenced.styles) {
        let default = map_of(vec![("font", Value::Str(name.clone()))]);
        ensure_table_record(&mut text_styles, &name, default, "STYLE", context);
    }

    if context.r2010() {
        // DXF R12 has no line weights at all (they are reported one by one).
        let mut normalized_lineweights = referenced.normalized_lineweights;
        for definition in layers.values() {
            let lineweight = definition
                .get("lineweight_mm")
                .and_then(Value::as_python_number);
            if lineweight.is_some_and(lineweight_is_normalized) {
                normalized_lineweights += 1;
            }
        }
        if normalized_lineweights > 0 {
            context.diagnose(
                "DXF_LINEWEIGHT_NORMALIZED",
                "info",
                format!(
                    "{normalized_lineweights} line weight(s) DXF has no value for were written \
                     as the nearest line weight it has."
                ),
                None,
                Some("normalized"),
            );
        }
    }

    Ok(Normalized {
        layers,
        linetypes,
        text_styles,
    })
}

/// The names of a table in the order of ``sorted(table, key=str.casefold)``.
fn sorted_names(table: &Table) -> Vec<&String> {
    let mut names: Vec<&String> = table.keys().collect();
    names.sort_by_cached_key(|name| casefold(name));
    names
}

/// ``str(definition.get(key, default))``.
fn str_field(definition: &Map, key: &str, default: &str) -> Result<String, WriteError> {
    match definition.get(key) {
        None => Ok(default.to_string()),
        Some(value) => Ok(py_str(value, "a table field")?.into_owned()),
    }
}

fn num_field(definition: &Map, key: &str, default: f64) -> Result<f64, WriteError> {
    match definition.get(key) {
        None => Ok(default),
        Some(value) => value
            .as_python_number()
            .ok_or_else(|| WriteError::invalid(format!("table field {key} is not a number"))),
    }
}

fn optional_str<'a>(definition: &'a Map, key: &str) -> Result<Option<&'a str>, WriteError> {
    match definition.get(key) {
        None | Some(Value::Null) => Ok(None),
        Some(Value::Str(text)) => Ok(Some(text)),
        Some(_) => Err(WriteError::invalid(format!(
            "table field {key} is not a string"
        ))),
    }
}

/// ``_tables_to_pairs``.
pub(crate) fn tables_to_pairs(
    tables: &Normalized,
    context: &mut Context<'_>,
) -> Result<Vec<Pair>, WriteError> {
    let r2010 = context.r2010();
    let mut pairs: Vec<Pair> = vec![(0, Val::S("SECTION")), (2, Val::S("TABLES"))];

    let linetypes = &tables.linetypes;
    pairs.push((0, Val::S("TABLE")));
    pairs.push((2, Val::S("LTYPE")));
    pairs.push((70, Val::owned(linetypes.len().to_string())));
    for name in sorted_names(linetypes) {
        let definition = &linetypes[name.as_str()];
        let pattern: Vec<f64> = match definition.get("pattern_mm") {
            Some(Value::List(values)) => {
                values.iter().filter_map(Value::as_python_number).collect()
            }
            _ => Vec::new(),
        };
        let total = py_sum(pattern.iter().map(|value| value.abs()));
        pairs.push((0, Val::S("LTYPE")));
        pairs.push((2, Val::owned(name.clone())));
        pairs.push((70, Val::S("0")));
        pairs.push((3, Val::owned(str_field(definition, "description", "")?)));
        pairs.push((72, Val::S("65")));
        pairs.push((73, Val::owned(pattern.len().to_string())));
        pairs.push((40, Val::owned(num(total))));
        for value in &pattern {
            pairs.push((49, Val::owned(num(*value))));
            pairs.push((74, Val::S("0")));
        }
    }
    pairs.push((0, Val::S("ENDTAB")));

    let layers = &tables.layers;
    pairs.push((0, Val::S("TABLE")));
    pairs.push((2, Val::S("LAYER")));
    pairs.push((70, Val::owned(layers.len().to_string())));
    for name in sorted_names(layers) {
        let definition = &layers[name.as_str()];
        pairs.push((0, Val::S("LAYER")));
        pairs.push((2, Val::owned(name.clone())));
        pairs.push((70, Val::S("0")));
        let color_start = pairs.len();
        match definition.get("color") {
            None => pairs.push((62, Val::S("7"))),
            Some(Value::Int(color)) => {
                pairs.push((62, Val::owned((*color).clamp(0, 256).to_string())));
            }
            Some(Value::Bool(_)) => {
                return Err(WriteError::invalid("layer color is a bool"));
            }
            Some(Value::Str(color)) => match hex_color(color) {
                Some(rgb) if r2010 => {
                    pairs.push((62, Val::S("7")));
                    pairs.push((420, Val::owned(rgb.to_string())));
                }
                Some(rgb) => {
                    let aci = nearest_aci(rgb);
                    pairs.push((62, Val::integer(aci)));
                    context.diagnose(
                        "DXF_R12_TRUE_COLOR_APPROXIMATED",
                        "warning",
                        format!(
                            "Layer {} true color {color} was approximated as ACI {aci} for DXF \
                             R12.",
                            repr(name)
                        ),
                        None,
                        Some("approximated"),
                    );
                }
                None => pairs.push((62, Val::S("7"))),
            },
            Some(_) => pairs.push((62, Val::S("7"))),
        }
        if let Some(Value::Bool(false)) = definition.get("visible") {
            // a layer that is off has a negative colour number
            for pair in pairs[color_start..].iter_mut() {
                if pair.0 == 62 {
                    let index: i64 = pair.1.as_str().parse().unwrap_or(0);
                    let index = if index == 0 { 7 } else { index };
                    pair.1 = Val::owned((-index).to_string());
                }
            }
        }
        pairs.push((
            6,
            Val::owned(str_field(definition, "linetype", "CONTINUOUS")?),
        ));
        if let Some(lineweight) = definition
            .get("lineweight_mm")
            .and_then(Value::as_python_number)
        {
            if r2010 {
                pairs.push((370, Val::owned(dxf_lineweight(lineweight).to_string())));
            } else {
                context.diagnose(
                    "DXF_R12_LINEWEIGHT_OMITTED",
                    "warning",
                    format!("Layer {} lineweight was omitted from DXF R12.", repr(name)),
                    None,
                    Some("skipped"),
                );
            }
        }
        if r2010 {
            let plot = definition.get("plot").is_none_or(Value::truthy);
            pairs.push((290, Val::S(if plot { "1" } else { "0" })));
        }
    }
    pairs.push((0, Val::S("ENDTAB")));

    let styles = &tables.text_styles;
    let mut uses_xdata = false;
    pairs.push((0, Val::S("TABLE")));
    pairs.push((2, Val::S("STYLE")));
    pairs.push((70, Val::owned(styles.len().to_string())));
    for name in sorted_names(styles) {
        let definition = &styles[name.as_str()];
        let font = context.host.style_font(
            optional_str(definition, "font")?,
            optional_str(definition, "family")?,
        )?;
        pairs.push((0, Val::S("STYLE")));
        pairs.push((2, Val::owned(name.clone())));
        pairs.push((70, Val::S("0")));
        pairs.push((40, Val::owned(num(num_field(definition, "height", 0.0)?))));
        pairs.push((
            41,
            Val::owned(num(num_field(definition, "width_factor", 1.0)?)),
        ));
        pairs.push((
            50,
            Val::owned(num(num_field(definition, "oblique_deg", 0.0)?)),
        ));
        pairs.push((71, Val::S("0")));
        pairs.push((42, Val::S("0")));
        pairs.push((3, Val::owned(font.file)));
        pairs.push((4, Val::owned(str_field(definition, "big_font", "")?)));
        if let Some(family) = font.family {
            if !family.is_empty() && r2010 {
                // AutoCAD finds the face inside a .ttc, and fonts it has no file
                // for, by this family
                pairs.push((1001, Val::S("ACAD")));
                pairs.push((1000, Val::owned(family)));
                pairs.push((1071, Val::owned(font.flags.to_string())));
                uses_xdata = true;
            }
        }
    }
    pairs.push((0, Val::S("ENDTAB")));
    if uses_xdata {
        // xdata names a registered application
        pairs.push((0, Val::S("TABLE")));
        pairs.push((2, Val::S("APPID")));
        pairs.push((70, Val::S("1")));
        pairs.push((0, Val::S("APPID")));
        pairs.push((2, Val::S("ACAD")));
        pairs.push((70, Val::S("0")));
        pairs.push((0, Val::S("ENDTAB")));
    }
    pairs.push((0, Val::S("ENDSEC")));
    Ok(pairs)
}
