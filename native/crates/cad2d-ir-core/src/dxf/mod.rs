//! The DXF writer of the IR: DXF R12 (AC1009) and DXF R2010 (AC1024).
//!
//! ``cad2d_ir.codecs.dxf.ir_to_dxf`` is this writer. Its stages: the blocks of
//! the dimensions that have none, the tables, the blocks, the entities of model
//! space and of one paper-space layout, for R2010 the structure AutoCAD and ODA
//! read a file by (subclass markers, handles, owners, every symbol table) with
//! the CLASSES and OBJECTS sections, the header, and the passes over all values
//! (symbol names, ``%%``, the code page of R12).
//!
//! A few things are asked of the caller ([`Host`]): the fonts of the text
//! styles, the code pages, the inline codes of MTEXT and the view of a
//! viewport. They live in the Python package, which other code there uses too.
//!
//! This writer replaced one written in Python (``cad2d_ir/codecs/dxf.py`` up to
//! cad2d-ir 0.17.22) and writes the same text to the byte. The comments name
//! the functions of that writer (``_render_entity``, ``_text_cells``, ...)
//! where a piece of this code took their place.

mod common;
mod curves;
mod dimension;
mod entity;
mod image;
mod layout;
mod pairs;
mod r12;
mod structure;
mod tables;
mod text;

use std::collections::HashMap;
use std::sync::Arc;

use crate::model::{Block, Document, Entity};
use crate::num::num;
use crate::pystr::repr;
use crate::value::{Map, Value};

pub use curves::{sample_ellipse, sample_spline};
pub use pairs::{Pair, Val};
pub use tables::Table;
pub use text::text_cells;

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

/// The view of a viewport in display coordinates
/// (``cad2d_ir.layouts.viewport_to_dxf_view``).
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct ViewportView {
    pub center: [f64; 2],
    pub height: f64,
    pub twist: f64,
}

/// What the text of a DXF R12 file is stored with: its code page, the control
/// codes the signs of its text values become, and what the characters the code
/// page lacks become.
#[derive(Clone, Debug, PartialEq)]
pub struct R12TextPlan {
    pub codepage: String,
    pub coded: Vec<(char, String)>,
    pub lost: Vec<(char, String)>,
    /// How many characters the code page cannot store.
    pub replaced: u64,
    /// How many of them were written as a similar character.
    pub similar: u64,
}

/// What the writer asks its caller for: what lives in the Python package.
pub trait Host {
    /// ``cad2d_ir.fonts.style_font``.
    fn style_font(&self, font: Option<&str>, family: Option<&str>)
        -> Result<StyleFont, WriteError>;
    /// The code page a DXF R2010 file names, for the non-ASCII characters of
    /// the file.
    fn select_codepage(&self, characters: &[(char, u64)]) -> Result<String, WriteError>;
    /// How the text of a DXF R12 file is stored: for the non-ASCII characters
    /// of its text values and of its other values.
    fn r12_text_plan(
        &self,
        text_characters: &[(char, u64)],
        other_characters: &[(char, u64)],
    ) -> Result<R12TextPlan, WriteError>;
    /// The text of an MTEXT entity as DXF holds it (its inline codes, a line
    /// break as ``\P``, the width factor in front).
    fn mtext_text(&self, entity: &Entity) -> Result<String, WriteError>;
    /// The TEXT entities an MTEXT becomes in DXF R12, and whether formatting
    /// was lost.
    fn explode_mtext(
        &self,
        entity: &Entity,
        text_styles: &Table,
    ) -> Result<(Vec<Entity>, bool), WriteError>;
    /// ``cad2d_ir.layouts.viewport_to_dxf_view``.
    fn viewport_view(&self, viewport: &Map) -> Result<Option<ViewportView>, WriteError>;
}

/// The document is not a writable IR document.
#[derive(Clone, Debug, PartialEq)]
pub enum WriteError {
    Invalid(String),
}

impl WriteError {
    pub(crate) fn invalid(reason: impl Into<String>) -> Self {
        WriteError::Invalid(reason.into())
    }
}

impl std::fmt::Display for WriteError {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            WriteError::Invalid(reason) => write!(formatter, "{reason}"),
        }
    }
}

impl std::error::Error for WriteError {}

/// The DXF version a file is written as.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum Target {
    /// AC1009.
    R12,
    /// AC1024.
    R2010,
}

/// What becomes of a dimension whose kind the importer could not tell.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub enum GenericDimensions {
    /// Written as the lines, texts and points it was made of.
    Explode,
    /// Left out.
    Skip,
}

#[derive(Clone, Debug)]
pub struct Options {
    pub target: Target,
    /// The segments of a whole turn when a curve is written as a polyline
    /// (DXF R12 has no ELLIPSE and no SPLINE).
    pub curve_segments: u32,
    pub generic_dimensions: GenericDimensions,
    /// Whether [`DxfOutput::entity_map`] is filled.
    pub entity_map: bool,
}

impl Default for Options {
    fn default() -> Self {
        Options {
            target: Target::R2010,
            curve_segments: 96,
            generic_dimensions: GenericDimensions::Explode,
            entity_map: false,
        }
    }
}

/// What became of an entity of the IR: one entry for every record written for
/// it, or one without a handle and a type when nothing was written.
#[derive(Clone, Debug, PartialEq)]
pub struct EntityMapEntry {
    pub ir_id: Arc<str>,
    pub handle: Option<String>,
    pub dxf_type: Option<&'static str>,
    /// The place of the record among the written ones (of the next one, for an
    /// entity that was not written).
    pub index: u64,
    /// ``modelspace``, ``block:<name>`` or ``layout:<name>``.
    pub scope: Arc<str>,
    /// Why nothing was written, when the writer left the entity out.
    pub reason_code: Option<&'static str>,
}

pub struct DxfOutput {
    pub text: String,
    pub diagnostics: Vec<ExportDiagnostic>,
    pub entity_map: Vec<EntityMapEntry>,
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

/// What changes while a file is written.
pub(crate) struct Context<'a> {
    pub(crate) host: &'a dyn Host,
    pub(crate) options: &'a Options,
    pub(crate) allocator: HandleAllocator,
    pub(crate) diagnostics: Vec<ExportDiagnostic>,
    /// The image definitions and reactors of the IMAGE entities written so far.
    pub(crate) images: image::ImageRegistry,
    pub(crate) entity_map: Vec<EntityMapEntry>,
    /// How many records of entities were written.
    pub(crate) output_index: u64,
    /// How many GENERIC dimensions the one being written is inside of (a block
    /// of a dimension can hold a dimension).
    pub(crate) generic_depth: u32,
    /// How many records of the GENERIC dimension being written wait for their
    /// handle.
    pub(crate) deferred_handles: usize,
}

/// What stands for a handle that is given later (no handle starts with it).
const DEFERRED_HANDLE: char = '\u{2}';

impl Context<'_> {
    pub(crate) fn r2010(&self) -> bool {
        self.options.target == Target::R2010
    }

    /// The handle of the record of an entity.
    ///
    /// The Python writer makes the records of all the parts of a GENERIC
    /// dimension before it gives the first of them a handle, and an image
    /// among the parts takes the handles of its definition and its reactor
    /// while its record is made. So the records of such a dimension get their
    /// handles when all of them are written ([`Context::resolve_handles`]).
    pub(crate) fn entity_handle(&mut self) -> Val {
        if self.generic_depth == 0 {
            return self.allocator.allocate();
        }
        let token = format!("{DEFERRED_HANDLE}{}", self.deferred_handles);
        self.deferred_handles += 1;
        Val::owned(token)
    }

    /// Give the records written since ``start`` (and the entries of the entity
    /// map since ``map_start``) the handles they wait for, in their order.
    pub(crate) fn resolve_handles(&mut self, out: &mut [Pair], start: usize, map_start: usize) {
        let count = std::mem::take(&mut self.deferred_handles);
        if count == 0 {
            return;
        }
        let handles: Vec<Val> = (0..count).map(|_| self.allocator.allocate()).collect();
        let resolve = |text: &str| -> Option<Val> {
            let index: usize = text.strip_prefix(DEFERRED_HANDLE)?.parse().ok()?;
            handles.get(index).cloned()
        };
        for pair in out[start..].iter_mut() {
            if matches!(pair.0, 5 | 330) {
                if let Some(handle) = resolve(pair.1.as_str()) {
                    pair.1 = handle;
                }
            }
        }
        for entry in self.entity_map[map_start..].iter_mut() {
            if let Some(handle) = entry.handle.as_deref().and_then(resolve) {
                entry.handle = Some(handle.as_str().to_string());
            }
        }
        self.images.resolve_owners(&resolve);
    }

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

/// What a file is written from, besides the entity at hand.
pub(crate) struct Env<'a> {
    pub(crate) document: &'a Document,
    /// The blocks made for dimensions that had none.
    pub(crate) generated: &'a dimension::GeometryBlocks,
    /// The text styles of the file (the width factor an MTEXT is drawn with).
    pub(crate) text_styles: &'a Table,
}

impl<'a> Env<'a> {
    /// The block ``name`` of the document, or the one made under that name.
    pub(crate) fn block(&self, name: &str) -> Option<&'a Block> {
        self.document
            .tables
            .as_ref()
            .and_then(|tables| tables.blocks.as_ref())
            .and_then(|blocks| blocks.get(name))
            .or_else(|| self.generated.blocks.get(name))
    }
}

/// Whether the writer looks at ``key`` of an entity of ``kind``.
///
/// A document that is converted only to be written can leave the other keys
/// out, and an entity then takes a fraction of the memory: where an entity
/// came from (``source``) is never read, and ``metadata`` only of the kinds
/// that keep there how they are written (the inline codes of an MTEXT, the
/// size of an IMAGE in pixels, ...). The typed kinds below are written from
/// their fields alone. ``kind`` is ``None`` for an entity without one.
pub fn reads_entity_key(kind: Option<&str>, key: &str) -> bool {
    match key {
        "source" => false,
        "metadata" => !matches!(
            kind,
            Some(
                "LINE"
                    | "CIRCLE"
                    | "ARC"
                    | "POINT"
                    | "ELLIPSE"
                    | "TEXT"
                    | "INSERT"
                    | "HATCH"
                    | "LWPOLYLINE"
            )
        ),
        _ => true,
    }
}

/// Write ``document`` as DXF text.
pub fn write(
    document: &Document,
    options: &Options,
    host: &dyn Host,
) -> Result<DxfOutput, WriteError> {
    if !(8..=4096).contains(&options.curve_segments) {
        return Err(WriteError::invalid("curve_segments must be in [8, 4096]"));
    }
    check_document(document)?;
    let r2010 = options.target == Target::R2010;

    let layouts = layout::layout_scopes(document)?;
    let mut diagnostics = Vec::new();
    let generated = dimension::geometry_blocks(document, &layouts, &mut diagnostics)?;
    let mut context = Context {
        host,
        options,
        allocator: HandleAllocator::new(),
        diagnostics,
        images: image::ImageRegistry::default(),
        entity_map: Vec::new(),
        output_index: 0,
        generic_depth: 0,
        deferred_handles: 0,
    };

    let normalized = tables::normalized_tables(document, &layouts, &generated, &mut context)?;
    let mut table_pairs = tables::tables_to_pairs(&normalized, &mut context)?;
    let mut layer_handles: HashMap<String, Val> = HashMap::new();
    if r2010 {
        table_pairs = structure::add_r2010_subclasses(table_pairs);
        table_pairs = structure::add_record_handles(table_pairs, &mut context.allocator).0;
        layer_handles = structure::layer_record_handles(&table_pairs);
    }
    let env = Env {
        document,
        generated: &generated,
        text_styles: &normalized.text_styles,
    };

    let block_pairs = blocks_to_pairs(&env, &mut context)?;

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
    {
        let name: Arc<str> = Arc::from("modelspace");
        let scope = entity::Scope {
            mode: entity::Mode {
                r2010,
                paper: false,
                owner: entity::Owner::Model,
            },
            name: &name,
            id_override: None,
        };
        for entity in &document.entities {
            entity::append_entity_output(&mut entity_pairs, entity, &scope, &env, &mut context)?;
        }
    }
    layout::append_layout_output(
        &mut entity_pairs,
        &layouts,
        &layer_handles,
        &env,
        &mut context,
    )?;
    entity_pairs.push((0, Val::S("ENDSEC")));

    let units_name = header_units(document)?;
    let (table_out, block_out, classes_pairs, objects_pairs) = if r2010 {
        // DXF R2000+ is read as a database: the tables, blocks and owners it
        // needs besides the drawing, CLASSES (the raster image classes when
        // there are images) and the named object dictionary in OBJECTS.
        let (table_out, block_out) = structure::r2010_structure(
            table_pairs,
            block_pairs,
            &mut entity_pairs,
            &mut context.allocator,
        );
        let classes_pairs = context.images.classes_pairs();
        let objects_pairs = context
            .images
            .objects_pairs(&mut context.allocator, &units_name);
        (table_out, block_out, classes_pairs, objects_pairs)
    } else {
        (table_pairs, block_pairs, Vec::new(), Vec::new())
    };
    let entity_out = entity_pairs;

    let codepage = if r2010 {
        let characters = pairs::non_ascii_characters(
            table_out
                .iter()
                .chain(block_out.iter())
                .chain(entity_out.iter()),
        );
        Some(context.host.select_codepage(&characters)?)
    } else {
        None
    };
    let header_pairs = header_to_pairs(document, &units_name, codepage, &mut context)?;

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
    if !r2010 {
        r12::store_r12_text(&mut sections, &mut context)?;
    }
    Ok(DxfOutput {
        text: pairs::format_pairs(&sections),
        diagnostics: context.diagnostics,
        entity_map: context.entity_map,
    })
}

/// The parts of a document have the types the IR gives them.
fn check_document(document: &Document) -> Result<(), WriteError> {
    for key in ["header", "tables", "entities"] {
        // a value of another type than the IR has for it was left as it came
        if document.extra.contains_key(key) {
            return Err(WriteError::invalid(format!(
                "document {key} is not what the IR has for it"
            )));
        }
    }
    Ok(())
}

/// ``str(header.get("units", "mm"))``.
fn header_units(document: &Document) -> Result<String, WriteError> {
    let Some(header) = &document.header else {
        return Ok("mm".to_string());
    };
    match header.get("units") {
        None => Ok("mm".to_string()),
        Some(value) => Ok(common::py_str(value, "header.units")?.into_owned()),
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

/// ``_header_to_pairs``.
fn header_to_pairs(
    document: &Document,
    units_name: &str,
    codepage: Option<String>,
    context: &mut Context<'_>,
) -> Result<Vec<Pair>, WriteError> {
    let r2010 = context.r2010();
    let mut pairs: Vec<Pair> = vec![
        (0, Val::S("SECTION")),
        (2, Val::S("HEADER")),
        (9, Val::S("$ACADVER")),
        (1, Val::S(if r2010 { "AC1024" } else { "AC1009" })),
    ];
    if let Some(codepage) = codepage {
        // The text is UTF-8; the code page is what a reader converts it to for
        // an older format. ODA garbles MTEXT saved as R2000 without one.
        pairs.push((9, Val::S("$DWGCODEPAGE")));
        pairs.push((3, Val::owned(codepage)));
    }
    let scale = document
        .header
        .as_ref()
        .and_then(|header| common::linetype_scale_value(header.get("linetype_scale")));
    if let Some(scale) = scale {
        pairs.push((9, Val::S("$LTSCALE")));
        pairs.push((40, Val::owned(num(scale))));
    }
    if r2010 {
        pairs.push((9, Val::S("$INSUNITS")));
        pairs.push((70, Val::owned(insunits(units_name).to_string())));
        pairs.push((9, Val::S("$HANDSEED")));
        pairs.push((5, Val::owned(context.allocator.handseed())));
    } else {
        // the code page is the one the text pass chooses
        pairs.push((9, Val::S("$DWGCODEPAGE")));
        pairs.push((3, Val::S("ANSI_932")));
        pairs.push((9, Val::S("$MEASUREMENT")));
        pairs.push((
            70,
            Val::S(if matches!(units_name, "inch" | "ft") {
                "0"
            } else {
                "1"
            }),
        ));
        if !matches!(units_name, "mm" | "unknown" | "unitless") {
            context.diagnose(
                "DXF_R12_INSUNITS_OMITTED",
                "warning",
                format!(
                    "DXF R12 has no $INSUNITS header; IR units {} were reduced to $MEASUREMENT.",
                    repr(units_name)
                ),
                None,
                Some("skipped"),
            );
        }
    }
    pairs.push((0, Val::S("ENDSEC")));
    Ok(pairs)
}

/// ``_blocks_to_pairs``: the blocks of the document and the ones made for its
/// dimensions, in the order of their names.
fn blocks_to_pairs(env: &Env<'_>, context: &mut Context<'_>) -> Result<Vec<Pair>, WriteError> {
    let r2010 = context.r2010();
    let mut pairs: Vec<Pair> = vec![(0, Val::S("SECTION")), (2, Val::S("BLOCKS"))];
    let tables = env.document.tables.as_ref();
    if let Some(Value::Map(_)) = tables.and_then(|tables| tables.extra.get("blocks")) {
        // (a table of another type is no table of blocks, as in Python)
        return Err(WriteError::invalid(
            "tables.blocks holds a block that is not an object",
        ));
    }
    let mut blocks: Vec<(&String, &Block)> = tables
        .and_then(|tables| tables.blocks.as_ref())
        .into_iter()
        .flatten()
        .chain(env.generated.blocks.iter())
        .collect();
    blocks.sort_by_cached_key(|(name, _)| crate::pystr::casefold(name));
    for (name, block) in blocks {
        let base = match (block.base_point, block.extra.get("base_point")) {
            (Some(base), _) => base,
            (None, None) => [0.0, 0.0],
            (None, Some(value)) => common::point_like(Some(value))
                .ok_or_else(|| WriteError::invalid("block base_point is not a point"))?,
        };
        let shared: Arc<str> = Arc::from(name.as_str());
        let mut block_start: Vec<Pair> = vec![
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
        if r2010 {
            block_start = structure::add_r2010_subclasses(block_start);
            block_start = structure::add_record_handles(block_start, &mut context.allocator).0;
        }
        pairs.extend(block_start);

        if block.extra.contains_key("entities") {
            return Err(WriteError::invalid(
                "block entities are not a list of entities",
            ));
        }
        // the entity a generated block stands for
        let generated_for: Option<Arc<str>> = block
            .extra
            .get("metadata")
            .and_then(Value::as_map)
            .and_then(|metadata| metadata.get("cad2d_ir"))
            .and_then(Value::as_map)
            .and_then(|cad2d_ir| cad2d_ir.get("generated_for_entity_id"))
            .and_then(Value::as_str)
            .map(Arc::from);
        let scope_name: Arc<str> = Arc::from(format!("block:{name}").as_str());
        let scope = entity::Scope {
            mode: entity::Mode {
                r2010,
                paper: false,
                owner: entity::Owner::Block,
            },
            name: &scope_name,
            id_override: generated_for.as_ref(),
        };
        for entity in &block.entities {
            entity::append_entity_output(&mut pairs, entity, &scope, env, context)?;
        }

        let mut block_end: Vec<Pair> = vec![(0, Val::S("ENDBLK")), (8, Val::S("0"))];
        if r2010 {
            block_end = structure::add_r2010_subclasses(block_end);
            block_end = structure::add_record_handles(block_end, &mut context.allocator).0;
        }
        pairs.extend(block_end);
    }
    pairs.push((0, Val::S("ENDSEC")));
    Ok(pairs)
}

#[cfg(test)]
mod tests {
    use super::reads_entity_key;

    #[test]
    fn provenance_is_never_read_and_metadata_of_some_kinds_only() {
        for kind in [Some("LINE"), Some("MTEXT"), Some("FUTURE"), None] {
            assert!(!reads_entity_key(kind, "source"));
            assert!(reads_entity_key(kind, "layer"));
            assert!(reads_entity_key(kind, "definition"));
        }
        for kind in [
            "LINE",
            "CIRCLE",
            "ARC",
            "POINT",
            "ELLIPSE",
            "TEXT",
            "INSERT",
            "HATCH",
            "LWPOLYLINE",
        ] {
            assert!(!reads_entity_key(Some(kind), "metadata"), "{kind}");
        }
        // the stored text of an MTEXT, the pixels of an IMAGE, and whatever
        // an entity of a kind without a struct carries
        for kind in [
            Some("MTEXT"),
            Some("IMAGE"),
            Some("DIMENSION"),
            Some("line"),
            None,
        ] {
            assert!(reads_entity_key(kind, "metadata"));
        }
    }
}
