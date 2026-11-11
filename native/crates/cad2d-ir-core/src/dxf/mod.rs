//! DXF R2010 (AC1024) writer for the typed IR.
//!
//! A port of ``ir_to_dxf`` in ``cad2d_ir/codecs/dxf.py`` that keeps its stages
//! (tables, blocks, entities, the R2000+ structure, header, name and text
//! passes), so that the text it writes is the text the Python writer writes.
//! What it has not learnt yet it does not guess: the writer returns
//! [`WriteError::Unsupported`] and the caller uses the Python writer instead.

mod dimension;
mod image;
mod pairs;
mod structure;
mod tables;

use std::sync::Arc;

use crate::model::{Color, Document, Entity, InsertScale, Shape, Vertex};
use crate::num::num;
use crate::value::Value;

pub use pairs::{Pair, Val};

/// A message the writer leaves for the caller (``ExportDiagnostic`` in Python).
#[derive(Clone, Debug, PartialEq)]
pub struct ExportDiagnostic {
    pub code: &'static str,
    pub severity: &'static str,
    pub message: String,
    pub entity_id: Option<String>,
    pub action: Option<&'static str>,
}

/// How a text style names its font (``cad2d_ir.fonts.StyleFont``).
#[derive(Clone, Debug, PartialEq)]
pub struct StyleFont {
    pub file: String,
    pub family: Option<String>,
    pub flags: i64,
}

/// What the writer asks its caller for: lookups that live in Python for now
/// (the font table and the legacy code pages). Each is asked a few times for a
/// document, never once per entity.
pub trait Host {
    /// ``cad2d_ir.fonts.style_font``.
    fn style_font(&self, font: Option<&str>, family: Option<&str>)
        -> Result<StyleFont, WriteError>;
    /// ``_select_r12_codepage`` for the non-ASCII characters of the file.
    fn select_codepage(&self, characters: &[(char, u64)]) -> Result<String, WriteError>;
}

#[derive(Clone, Debug, PartialEq)]
pub enum WriteError {
    /// The document needs something this writer has not been taught.
    Unsupported(String),
    /// The document is not a writable IR document.
    Invalid(String),
}

impl WriteError {
    pub(crate) fn unsupported(reason: impl Into<String>) -> Self {
        WriteError::Unsupported(reason.into())
    }
}

impl std::fmt::Display for WriteError {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            WriteError::Unsupported(reason) => write!(formatter, "unsupported: {reason}"),
            WriteError::Invalid(reason) => write!(formatter, "invalid: {reason}"),
        }
    }
}

impl std::error::Error for WriteError {}

pub struct DxfOutput {
    pub text: String,
    pub diagnostics: Vec<ExportDiagnostic>,
}

/// Where an entity is written: model space (its owner is filled in once the
/// block record of model space has a handle) or a block.
#[derive(Clone, Copy, PartialEq)]
pub(crate) enum Scope {
    Model,
    Block,
}

const HANDLE_START: u64 = 0x100;

pub(crate) struct HandleAllocator {
    next_value: u64,
}

impl HandleAllocator {
    fn new() -> Self {
        HandleAllocator {
            next_value: HANDLE_START,
        }
    }

    pub(crate) fn allocate(&mut self) -> Val {
        let handle = format!("{:X}", self.next_value);
        self.next_value += 1;
        Val::owned(handle)
    }

    fn handseed(&self) -> String {
        format!("{:X}", self.next_value)
    }
}

pub(crate) struct Context<'a> {
    pub(crate) host: &'a dyn Host,
    pub(crate) allocator: HandleAllocator,
    pub(crate) diagnostics: Vec<ExportDiagnostic>,
    /// The image definitions and reactors of the IMAGE entities written so far.
    pub(crate) images: image::ImageRegistry,
}

impl Context<'_> {
    pub(crate) fn diagnose(
        &mut self,
        code: &'static str,
        severity: &'static str,
        message: String,
        entity_id: Option<String>,
        action: Option<&'static str>,
    ) {
        self.diagnostics.push(ExportDiagnostic {
            code,
            severity,
            message,
            entity_id,
            action,
        });
    }
}

/// Write ``document`` as DXF R2010 text.
pub fn write_r2010(document: &Document, host: &dyn Host) -> Result<DxfOutput, WriteError> {
    let mut context = Context {
        host,
        allocator: HandleAllocator::new(),
        diagnostics: Vec::new(),
        images: image::ImageRegistry::default(),
    };
    check_document(document)?;

    let normalized = tables::normalized_tables(document, &mut context)?;
    let mut table_pairs = tables::tables_to_pairs(&normalized, &mut context)?;
    table_pairs = structure::add_r2010_subclasses(table_pairs);
    table_pairs = structure::add_record_handles(table_pairs, &mut context.allocator).0;

    let mut block_pairs = blocks_to_pairs(document, &mut context)?;

    if let Some(Value::List(constraints)) = document.extra.get("constraints") {
        if !constraints.is_empty() {
            context.diagnose(
                "DXF_CONSTRAINTS_OMITTED",
                "warning",
                "IR constraints are not representable in plain DXF and were omitted.".to_string(),
                None,
                Some("skipped"),
            );
        }
    }

    let mut entity_pairs: Vec<Pair> = Vec::with_capacity(document.entities.len() * 14 + 4);
    entity_pairs.push((0, Val::S("SECTION")));
    entity_pairs.push((2, Val::S("ENTITIES")));
    for entity in &document.entities {
        append_entity_output(&mut entity_pairs, entity, &mut context, Scope::Model)?;
    }
    entity_pairs.push((0, Val::S("ENDSEC")));

    let (table_out, block_out) = structure::r2010_structure(
        table_pairs,
        std::mem::take(&mut block_pairs),
        &mut entity_pairs,
        &mut context.allocator,
    );
    let entity_out = entity_pairs;
    // CLASSES (the raster image classes when there are images) and the named
    // object dictionary in OBJECTS
    let classes_pairs = context.images.classes_pairs();
    let units_name = header_units(document)?;
    let objects_pairs = context
        .images
        .objects_pairs(&mut context.allocator, &units_name);

    let characters = pairs::non_ascii_characters(
        table_out
            .iter()
            .chain(block_out.iter())
            .chain(entity_out.iter()),
    );
    let codepage = context.host.select_codepage(&characters)?;
    let header_pairs = header_to_pairs(
        document,
        &units_name,
        &context.allocator.handseed(),
        codepage,
    )?;

    // the parts of the file in the order they are written; the passes below see
    // them as one list of pairs
    let mut sections: [Vec<Pair>; 7] = [
        header_pairs,
        classes_pairs,
        table_out,
        block_out,
        entity_out,
        objects_pairs,
        vec![(0, Val::S("EOF"))],
    ];
    pairs::valid_symbol_names(&mut sections, &mut context);
    pairs::escape_literal_percents(&mut sections);
    Ok(DxfOutput {
        text: pairs::format_pairs(&sections),
        diagnostics: context.diagnostics,
    })
}

/// What the Python writer reads from a document besides its tables and
/// entities, and this writer does not write.
fn check_document(document: &Document) -> Result<(), WriteError> {
    for key in ["header", "tables", "entities"] {
        // a value of another type than the IR has for it was left as it came
        if document.extra.contains_key(key) {
            return Err(WriteError::unsupported(format!(
                "document {key} has an unexpected type"
            )));
        }
    }
    if let Some(Value::List(layouts)) = document.extra.get("layouts") {
        for layout in layouts {
            let Value::Map(layout) = layout else { continue };
            // a layout with an entity list is a scope of its own
            if layout.contains_key("entities") || layout.contains_key("viewports") {
                return Err(WriteError::unsupported("paper space layouts"));
            }
        }
    }
    Ok(())
}

fn header_units(document: &Document) -> Result<String, WriteError> {
    let Some(header) = &document.header else {
        return Ok("mm".to_string());
    };
    match header.get("units") {
        None => Ok("mm".to_string()),
        Some(Value::Str(units)) => Ok(units.clone()),
        Some(_) => Err(WriteError::unsupported("header.units is not a string")),
    }
}

fn insunits(units_name: &str) -> i64 {
    match units_name {
        "unitless" | "unknown" => 0,
        "inch" => 1,
        "ft" => 2,
        "mm" => 4,
        "cm" => 5,
        "m" => 6,
        _ => 4,
    }
}

/// ``_linetype_scale_value``: a positive number other than 1.
fn linetype_scale_value(value: Option<&Value>) -> Option<f64> {
    let scale = value?.as_number()?;
    if !scale.is_finite() || scale <= 0.0 || (scale - 1.0).abs() < 1e-12 {
        return None;
    }
    Some(scale)
}

fn header_to_pairs(
    document: &Document,
    units_name: &str,
    handseed: &str,
    codepage: String,
) -> Result<Vec<Pair>, WriteError> {
    let mut pairs: Vec<Pair> = vec![
        (0, Val::S("SECTION")),
        (2, Val::S("HEADER")),
        (9, Val::S("$ACADVER")),
        (1, Val::S("AC1024")),
        (9, Val::S("$DWGCODEPAGE")),
        (3, Val::owned(codepage)),
    ];
    let scale = document
        .header
        .as_ref()
        .and_then(|header| linetype_scale_value(header.get("linetype_scale")));
    if let Some(scale) = scale {
        pairs.push((9, Val::S("$LTSCALE")));
        pairs.push((40, Val::owned(num(scale))));
    }
    pairs.push((9, Val::S("$INSUNITS")));
    pairs.push((70, Val::owned(insunits(units_name).to_string())));
    pairs.push((9, Val::S("$HANDSEED")));
    pairs.push((5, Val::owned(handseed.to_string())));
    pairs.push((0, Val::S("ENDSEC")));
    Ok(pairs)
}

fn blocks_to_pairs(
    document: &Document,
    context: &mut Context<'_>,
) -> Result<Vec<Pair>, WriteError> {
    let mut pairs: Vec<Pair> = vec![(0, Val::S("SECTION")), (2, Val::S("BLOCKS"))];
    let blocks = document
        .tables
        .as_ref()
        .and_then(|tables| tables.blocks.as_ref());
    if let Some(blocks) = blocks {
        let mut names: Vec<&String> = blocks.keys().collect();
        names.sort_by_cached_key(|name| crate::pystr::casefold(name));
        for name in names {
            let block = &blocks[name.as_str()];
            if block.extra.contains_key("metadata") {
                // ``generated_for_entity_id`` only names the entity in the entity map
            }
            let base = block.base_point.unwrap_or([0.0, 0.0]);
            if block.extra.contains_key("base_point") {
                return Err(WriteError::unsupported(
                    "block base_point is not a 2D point",
                ));
            }
            let shared: Arc<str> = Arc::from(name.as_str());
            let block_start: Vec<Pair> = vec![
                (0, Val::S("BLOCK")),
                (8, Val::S("0")),
                (2, Val::A(Arc::clone(&shared))),
                (70, Val::S("0")),
                (10, Val::owned(num(base[0]))),
                (20, Val::owned(num(base[1]))),
                (30, Val::S("0")),
                (3, Val::A(shared)),
                (1, Val::S("")),
            ];
            let block_start = structure::add_r2010_subclasses(block_start);
            let block_start = structure::add_record_handles(block_start, &mut context.allocator).0;
            pairs.extend(block_start);
            if block.extra.contains_key("entities") {
                return Err(WriteError::unsupported(
                    "block entities are not a list of entities",
                ));
            }
            for entity in &block.entities {
                append_entity_output(&mut pairs, entity, context, Scope::Block)?;
            }
            let block_end: Vec<Pair> = vec![(0, Val::S("ENDBLK")), (8, Val::S("0"))];
            let block_end = structure::add_r2010_subclasses(block_end);
            let block_end = structure::add_record_handles(block_end, &mut context.allocator).0;
            pairs.extend(block_end);
        }
    } else if let Some(tables) = &document.tables {
        if tables.extra.contains_key("blocks") {
            return Err(WriteError::unsupported(
                "tables.blocks is not a table of blocks",
            ));
        }
    }
    pairs.push((0, Val::S("ENDSEC")));
    Ok(pairs)
}

/// Keys the Python writer reads from every entity. One of them in ``extra``
/// (a value this crate has no typed field for) is something to understand.
const COMMON_READ_KEYS: [&str; 4] = ["layer", "linetype", "color", "lineweight_mm"];

fn check_common(entity: &Entity) -> Result<(), WriteError> {
    let Some(extra) = &entity.extra else {
        return Ok(());
    };
    for key in COMMON_READ_KEYS {
        if let Some(value) = extra.get(key) {
            // ``linetype: None`` and ``color: None`` are read as absent
            if matches!(value, Value::Null) && key != "layer" {
                continue;
            }
            return Err(WriteError::unsupported(format!(
                "entity {key} has an unexpected type"
            )));
        }
    }
    Ok(())
}

fn reject_keys(entity: &Entity, keys: &[&str]) -> Result<(), WriteError> {
    let Some(extra) = &entity.extra else {
        return Ok(());
    };
    for key in keys {
        if extra.contains_key(*key) {
            return Err(WriteError::unsupported(format!(
                "{} entity key {key}",
                entity.kind().unwrap_or("?")
            )));
        }
    }
    Ok(())
}

/// ``_common_to_pairs``.
fn common_to_pairs(entity: &Entity, pairs: &mut Vec<Pair>) -> Result<(), WriteError> {
    match &entity.layer {
        Some(layer) => pairs.push((8, Val::A(Arc::clone(layer)))),
        None => pairs.push((8, Val::S("0"))),
    }
    if let Some(linetype) = &entity.linetype {
        if !linetype.is_empty() {
            pairs.push((6, Val::A(Arc::clone(linetype))));
        }
    }
    if let Some(scale) = linetype_scale_value(entity.extra_get("linetype_scale")) {
        pairs.push((48, Val::owned(num(scale))));
    } else if let Some(Value::Bool(_)) = entity.extra_get("linetype_scale") {
        // a bool is no linetype scale
    }
    match &entity.color {
        Some(Color::Index(index)) => pairs.push((62, Val::integer(*index))),
        Some(Color::Text(text)) => {
            if let Some(rgb) = hex_color(text) {
                pairs.push((420, Val::owned(rgb.to_string())));
            }
        }
        None => {}
    }
    if let Some(lineweight) = entity.lineweight_mm {
        pairs.push((370, Val::integer(dxf_lineweight(lineweight))));
    }
    if let Some(Value::Bool(false)) = entity.extra_get("visible") {
        pairs.push((60, Val::S("1")));
    }
    Ok(())
}

/// The line weights DXF has (group 370, in hundredths of a millimetre). AutoCAD
/// and ODA take any other value for "by layer", so a width in between is written
/// as the nearest of these.
const DXF_LINEWEIGHTS: [i64; 24] = [
    0, 5, 9, 13, 15, 18, 20, 25, 30, 35, 40, 50, 53, 60, 70, 80, 90, 100, 106, 120, 140, 158, 200,
    211,
];

/// ``_dxf_lineweight``: group 370 for a width in millimetres. Like ``min`` in
/// Python, the first of the line weights stays unless a later one is nearer
/// (a width that is not a number is written as 0).
pub(crate) fn dxf_lineweight(lineweight_mm: f64) -> i64 {
    let hundredths = lineweight_mm * 100.0;
    let mut nearest = DXF_LINEWEIGHTS[0];
    let mut distance = (nearest as f64 - hundredths).abs();
    for &weight in &DXF_LINEWEIGHTS[1..] {
        let candidate = (weight as f64 - hundredths).abs();
        if candidate < distance {
            nearest = weight;
            distance = candidate;
        }
    }
    nearest
}

/// ``_lineweight_is_normalized``: whether a width is written as another line
/// weight than it is (by half a hundredth of a millimetre or more).
pub(crate) fn lineweight_is_normalized(lineweight_mm: f64) -> bool {
    (dxf_lineweight(lineweight_mm) as f64 - lineweight_mm * 100.0).abs() >= 0.5
}

/// The RGB number of ``#RRGGBB`` or ``#RRGGBBAA``.
pub(crate) fn hex_color(text: &str) -> Option<u32> {
    let digits = text.strip_prefix('#')?;
    if !(digits.len() == 6 || digits.len() == 8) || !digits.bytes().all(|b| b.is_ascii_hexdigit()) {
        return None;
    }
    u32::from_str_radix(&digits[..6], 16).ok()
}

fn point_pairs(pairs: &mut Vec<Pair>, x_code: i32, y_code: i32, point: [f64; 2]) {
    pairs.push((x_code, Val::owned(num(point[0]))));
    pairs.push((y_code, Val::owned(num(point[1]))));
}

/// ``_append_entity_output`` for AC1024: the pairs of the entity with its
/// subclass markers and a handle.
fn append_entity_output(
    output: &mut Vec<Pair>,
    entity: &Entity,
    context: &mut Context<'_>,
    scope: Scope,
) -> Result<(), WriteError> {
    check_common(entity)?;
    if dimension::is_generic_dimension(entity) {
        // written as the lines, texts and points it was made of, each a record
        // of its own
        for component in dimension::exploded(entity, context)? {
            append_entity_output(output, &component, context, scope)?;
        }
        return Ok(());
    }
    if image::is_image(entity) {
        // the definition and the reactor of the image get their handles first
        let Some(prepared) = image::prepare(entity, context)? else {
            return Ok(());
        };
        let handle = context.allocator.allocate();
        context.images.bind(&prepared, handle.clone());
        let start = output.len();
        image::append_record(output, entity, &prepared, handle)?;
        if scope == Scope::Model {
            output.insert(start + 2, (330, Val::S(pairs::MODEL_SPACE_OWNER)));
        }
        return Ok(());
    }
    if let Shape::Point {
        temporary: Some(true),
        ..
    } = &entity.shape
    {
        // A construction point of the source (a Jw_cad temporary point): it is
        // shown on screen and left out of a plot. DXF has no such point, and
        // written as a POINT it would become part of the drawing.
        let entity_id = entity.id.as_deref().filter(|id| !id.is_empty());
        context.diagnose(
            "DXF_TEMPORARY_POINT_SKIPPED",
            "info",
            format!(
                "[POINT:{}] temporary (construction) point was not written; DXF has no \
                 points that are left out of a plot.",
                entity_id.unwrap_or("?")
            ),
            entity_id.map(str::to_string),
            Some("skipped"),
        );
        return Ok(());
    }
    let handle = context.allocator.allocate();
    let start = output.len();
    append_entity_record(output, entity, handle)?;
    if scope == Scope::Model {
        // ``_r2010_owned``: the owner goes behind the handle. The entities of a
        // block get theirs in ``r2010_structure``, from the record of the block
        output.insert(start + 2, (330, Val::S(pairs::MODEL_SPACE_OWNER)));
    }
    Ok(())
}

fn append_entity_record(
    output: &mut Vec<Pair>,
    entity: &Entity,
    handle: Val,
) -> Result<(), WriteError> {
    match &entity.shape {
        Shape::Line { p1, p2 } => {
            output.push((0, Val::S("LINE")));
            output.push((5, handle));
            output.push((100, Val::S("AcDbEntity")));
            common_to_pairs(entity, output)?;
            output.push((100, Val::S("AcDbLine")));
            point_pairs(output, 10, 20, *p1);
            point_pairs(output, 11, 21, *p2);
        }
        Shape::Circle { center, radius } => {
            output.push((0, Val::S("CIRCLE")));
            output.push((5, handle));
            output.push((100, Val::S("AcDbEntity")));
            common_to_pairs(entity, output)?;
            output.push((100, Val::S("AcDbCircle")));
            point_pairs(output, 10, 20, *center);
            output.push((40, Val::owned(num(*radius))));
        }
        Shape::Arc {
            center,
            radius,
            start_angle,
            end_angle,
            ..
        } => {
            output.push((0, Val::S("ARC")));
            output.push((5, handle));
            output.push((100, Val::S("AcDbEntity")));
            common_to_pairs(entity, output)?;
            output.push((100, Val::S("AcDbCircle")));
            point_pairs(output, 10, 20, *center);
            output.push((40, Val::owned(num(*radius))));
            output.push((100, Val::S("AcDbArc")));
            output.push((50, Val::owned(num(*start_angle))));
            output.push((51, Val::owned(num(*end_angle))));
        }
        Shape::Point { position, .. } => {
            output.push((0, Val::S("POINT")));
            output.push((5, handle));
            output.push((100, Val::S("AcDbEntity")));
            common_to_pairs(entity, output)?;
            output.push((100, Val::S("AcDbPoint")));
            point_pairs(output, 10, 20, *position);
        }
        Shape::Ellipse(ellipse) => {
            let (mut start, mut end) = (ellipse.start_param, ellipse.end_param);
            if ellipse.ccw == Some(false) {
                std::mem::swap(&mut start, &mut end);
            }
            reject_keys(entity, &["ccw"])?;
            output.push((0, Val::S("ELLIPSE")));
            output.push((5, handle));
            output.push((100, Val::S("AcDbEntity")));
            common_to_pairs(entity, output)?;
            output.push((100, Val::S("AcDbEllipse")));
            point_pairs(output, 10, 20, ellipse.center);
            point_pairs(output, 11, 21, ellipse.major_axis);
            output.push((40, Val::owned(num(ellipse.ratio))));
            output.push((41, Val::owned(num(start))));
            output.push((42, Val::owned(num(end))));
        }
        Shape::Lwpolyline(polyline) => {
            reject_keys(entity, &["closed"])?;
            output.push((0, Val::S("LWPOLYLINE")));
            output.push((5, handle));
            output.push((100, Val::S("AcDbEntity")));
            common_to_pairs(entity, output)?;
            output.push((100, Val::S("AcDbPolyline")));
            output.push((90, Val::owned(polyline.vertices.len().to_string())));
            output.push((
                70,
                Val::S(if polyline.closed == Some(true) {
                    "1"
                } else {
                    "0"
                }),
            ));
            for vertex in &polyline.vertices {
                output.push((10, Val::owned(num(vertex.x))));
                output.push((20, Val::owned(num(vertex.y))));
                if let Some(bulge) = vertex.bulge {
                    output.push((42, Val::owned(num(bulge))));
                }
            }
        }
        Shape::Text(text) => {
            reject_keys(
                entity,
                &[
                    "rotation",
                    "halign",
                    "valign",
                    "style",
                    "width_factor",
                    "oblique_deg",
                ],
            )?;
            output.push((0, Val::S("TEXT")));
            output.push((5, handle));
            output.push((100, Val::S("AcDbEntity")));
            common_to_pairs(entity, output)?;
            output.push((100, Val::S("AcDbText")));
            point_pairs(output, 10, 20, text.insert);
            output.push((40, Val::owned(num(text.height))));
            output.push((1, Val::owned(text.text.clone())));
            let rotation = text.rotation.unwrap_or(0.0);
            if rotation != 0.0 {
                output.push((50, Val::owned(num(rotation))));
            }
            if let Some(style) = &text.style {
                if !style.is_empty() {
                    output.push((7, Val::A(Arc::clone(style))));
                }
            }
            if let Some(width_factor) = text.width_factor {
                output.push((41, Val::owned(num(width_factor))));
            }
            // the second AcDbText group holds the vertical justification (none here)
            output.push((100, Val::S("AcDbText")));
        }
        Shape::Insert(insert) => {
            reject_keys(entity, &["transform", "rotation", "scale"])?;
            for key in ["attribute_texts", "attributes"] {
                if entity.extra_get(key).is_some_and(Value::truthy) {
                    return Err(WriteError::unsupported(format!("INSERT entity key {key}")));
                }
            }
            output.push((0, Val::S("INSERT")));
            output.push((5, handle));
            output.push((100, Val::S("AcDbEntity")));
            common_to_pairs(entity, output)?;
            output.push((100, Val::S("AcDbBlockReference")));
            output.push((2, Val::A(Arc::clone(&insert.block))));
            point_pairs(output, 10, 20, insert.insert);
            let rotation = insert.rotation.unwrap_or(0.0);
            if rotation != 0.0 {
                output.push((50, Val::owned(num(rotation))));
            }
            match insert.scale {
                None => {}
                Some(InsertScale::Uniform(scale)) => {
                    if (scale - 1.0).abs() > 1e-12 {
                        output.push((41, Val::owned(num(scale))));
                        output.push((42, Val::owned(num(scale))));
                    }
                }
                Some(InsertScale::Axes(scale)) => {
                    output.push((41, Val::owned(num(scale[0]))));
                    output.push((42, Val::owned(num(scale[1]))));
                }
            }
        }
        Shape::Hatch(hatch) => {
            reject_keys(entity, &["solid"])?;
            if hatch.solid == Some(false) {
                return Err(WriteError::unsupported("pattern HATCH"));
            }
            if hatch.loops.is_empty() {
                return Err(WriteError::Invalid(
                    "HATCH requires non-empty loops".to_string(),
                ));
            }
            output.push((0, Val::S("HATCH")));
            output.push((5, handle));
            output.push((100, Val::S("AcDbEntity")));
            common_to_pairs(entity, output)?;
            output.push((100, Val::S("AcDbHatch")));
            for (code, value) in [
                (10, "0"),
                (20, "0"),
                (30, "0"),
                (210, "0"),
                (220, "0"),
                (230, "1"),
            ] {
                output.push((code, Val::S(value)));
            }
            output.push((2, Val::S("SOLID")));
            output.push((70, Val::S("1")));
            output.push((71, Val::S("0")));
            output.push((91, Val::owned(hatch.loops.len().to_string())));
            for hatch_loop in &hatch.loops {
                let has_bulge = hatch_loop
                    .vertices
                    .iter()
                    .any(|vertex| vertex.bulge.is_some());
                output.push((92, Val::S("2")));
                output.push((72, Val::S(if has_bulge { "1" } else { "0" })));
                output.push((73, Val::S("1")));
                output.push((93, Val::owned(hatch_loop.vertices.len().to_string())));
                for Vertex { x, y, bulge } in &hatch_loop.vertices {
                    output.push((10, Val::owned(num(*x))));
                    output.push((20, Val::owned(num(*y))));
                    if has_bulge {
                        output.push((42, Val::owned(num(bulge.unwrap_or(0.0)))));
                    }
                }
                output.push((97, Val::S("0")));
            }
            output.push((75, Val::S("0")));
            output.push((76, Val::S("1")));
            output.push((98, Val::S("0")));
        }
        Shape::Other => {
            return Err(WriteError::unsupported(format!(
                "entity kind {}",
                entity.kind().unwrap_or("?")
            )));
        }
    }
    Ok(())
}
