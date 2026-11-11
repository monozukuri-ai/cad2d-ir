//! JWW (Jw_cad) entities to the typed IR.
//!
//! A port of the entity conversion of ``cad2d_ir/importers/jww.py``
//! (``_convert_entity_sequence`` and what it calls). The parts of a document that
//! come from the header (layers, linetypes, pens, metadata) stay in Python and
//! reach this code as the lookup tables of [`Context`]. The result is checked
//! against the Python importer: the same entities, counts and diagnostics.

use std::collections::{BTreeMap, BTreeSet, HashMap};
use std::io::Read;
use std::sync::Arc;

use cad2d_ir_core::{
    Color, EllipseData, Entity, HatchData, HatchLoop, InsertData, InsertScale, Map, Shape,
    TextData, Value, Vertex,
};
use ezjww_core::{
    Arc as JwArc, Block as JwBlock, CircleSolid, Dimension, Entity as JwEntity, EntityBase,
    JwwDocument, Line as JwLine, Point as JwPoint, Solid, Text as JwText,
};

const TAU: f64 = 2.0 * std::f64::consts::PI;
const EPSILON: f64 = 1e-12;
const IMAGE_TEXT_PREFIX: &str = "^@BM";
const IMAGE_TEMP_PREFIX: &str = "%temp%";

/// What the Python importer works out from the header before it converts entities.
pub struct Context {
    /// ``"JWW"``: the label in messages and entity ids.
    pub label: String,
    /// ``"jww"``: ``source.format`` of an entity.
    pub source_format: String,
    pub entity_provenance: bool,
    pub strict: bool,
    pub curve_segments: i64,
    pub layer_names: HashMap<(i64, i64), Arc<str>>,
    pub block_names: HashMap<i64, Arc<str>>,
    pub linetype_names: HashMap<i64, Arc<str>>,
    pub pen_widths: HashMap<i64, f64>,
    pub pen_colors: HashMap<i64, i64>,
}

#[derive(Clone, Debug, PartialEq)]
pub struct ImportDiagnostic {
    pub code: &'static str,
    pub severity: &'static str,
    pub message: String,
    pub source_id: Option<String>,
    pub source_kind: Option<String>,
    pub action: Option<&'static str>,
}

#[derive(Clone, Debug, PartialEq)]
pub struct MetadataSetting {
    pub entity_index: usize,
    pub key: String,
    pub value: String,
    pub raw: String,
}

#[derive(Default)]
pub struct Converted {
    pub entities: Vec<Entity>,
    /// The entities of each block definition, in the order of the definitions.
    pub blocks: Vec<Vec<Entity>>,
    pub diagnostics: Vec<ImportDiagnostic>,
    pub converted_counts: BTreeMap<String, u64>,
    pub skipped_counts: BTreeMap<String, u64>,
    pub approximation_counts: BTreeMap<String, u64>,
    pub preserved_dimensions: u64,
    pub pen_width_entities: u64,
    pub used_linetypes: BTreeSet<String>,
    /// Font names of the texts, for the text style table.
    pub fonts: BTreeSet<String>,
    pub metadata_settings: Vec<MetadataSetting>,
    pub source_entities: usize,
    /// Image placements whose file the drawing carries, and those that only name a path.
    pub embedded_images: u64,
    pub linked_images: u64,
}

#[derive(Clone, Debug, PartialEq)]
pub enum ConvertError {
    /// Something this code has not been taught: the caller uses the Python importer.
    Unsupported(String),
    /// A conversion failure of a strict import (``ImporterError`` in Python).
    Failed(String),
}

/// An entity conversion that fails like a Python ``ValueError``.
struct Rejected(String);

type EntityResult = Result<Option<Entity>, EntityError>;

enum EntityError {
    Rejected(Rejected),
    Unsupported(String),
}

impl From<Rejected> for EntityError {
    fn from(rejected: Rejected) -> Self {
        EntityError::Rejected(rejected)
    }
}

/// ``str.strip`` of Python.
fn py_strip(text: &str) -> &str {
    text.trim_matches(|c: char| c.is_whitespace() || ('\u{1c}'..='\u{1f}').contains(&c))
}

/// ``math.isclose``.
fn isclose(a: f64, b: f64, rel_tol: f64, abs_tol: f64) -> bool {
    if a == b {
        return true;
    }
    if a.is_infinite() || b.is_infinite() {
        return false;
    }
    let difference = (b - a).abs();
    difference <= (rel_tol * b).abs() || difference <= (rel_tol * a).abs() || difference <= abs_tol
}

/// ``_number``: a finite value, or the ``ValueError`` of the key.
fn number(value: f64, key: &str) -> Result<f64, Rejected> {
    if value.is_finite() {
        Ok(value)
    } else {
        Err(Rejected(format!("{key} must be a finite number")))
    }
}

fn ccw_span(start: f64, span: f64) -> (f64, f64) {
    if span < 0.0 {
        (start + span, start)
    } else {
        (start, start + span)
    }
}

/// ``value % 360.0`` of Python, with 360 itself folded to 0.
fn normalize_degrees(value: f64) -> f64 {
    let mut normalized = value % 360.0;
    if normalized != 0.0 && normalized < 0.0 {
        normalized += 360.0;
    }
    if normalized == 0.0 || isclose(normalized, 360.0, 1e-9, 1e-12) {
        0.0
    } else {
        normalized
    }
}

/// ``round`` of Python on a float: to the nearest integer, a tie to the even one.
fn py_round(value: f64) -> f64 {
    value.round_ties_even()
}

fn int(value: i64) -> Value {
    Value::Int(value)
}

fn point_value(point: [f64; 2]) -> Value {
    Value::List(vec![Value::Float(point[0]), Value::Float(point[1])])
}

fn map_of(entries: Vec<(&str, Value)>) -> Map {
    entries
        .into_iter()
        .map(|(key, value)| (key.to_string(), value))
        .collect()
}

fn base_value(base: &EntityBase) -> Value {
    Value::Map(map_of(vec![
        ("group", int(base.group as i64)),
        ("pen_style", int(base.pen_style as i64)),
        ("pen_color", int(base.pen_color as i64)),
        ("pen_width", int(base.pen_width as i64)),
        ("layer", int(base.layer as i64)),
        ("layer_group", int(base.layer_group as i64)),
        ("flag", int(base.flag as i64)),
    ]))
}

fn line_value(line: &JwLine) -> Map {
    map_of(vec![
        ("base", base_value(&line.base)),
        ("start_x", Value::Float(line.start_x)),
        ("start_y", Value::Float(line.start_y)),
        ("end_x", Value::Float(line.end_x)),
        ("end_y", Value::Float(line.end_y)),
    ])
}

fn point_record_value(point: &JwPoint) -> Map {
    map_of(vec![
        ("base", base_value(&point.base)),
        ("x", Value::Float(point.x)),
        ("y", Value::Float(point.y)),
        ("is_temporary", Value::Bool(point.is_temporary)),
        ("code", int(point.code as i64)),
        ("angle", Value::Float(point.angle)),
        ("scale", Value::Float(point.scale)),
    ])
}

fn text_value(text: &JwText) -> Map {
    map_of(vec![
        ("base", base_value(&text.base)),
        ("start_x", Value::Float(text.start_x)),
        ("start_y", Value::Float(text.start_y)),
        ("end_x", Value::Float(text.end_x)),
        ("end_y", Value::Float(text.end_y)),
        ("text_type", int(text.text_type as i64)),
        ("size_x", Value::Float(text.size_x)),
        ("size_y", Value::Float(text.size_y)),
        ("spacing", Value::Float(text.spacing)),
        ("angle", Value::Float(text.angle)),
        ("font_name", Value::Str(text.font_name.clone())),
        ("content", Value::Str(text.content.clone())),
    ])
}

/// A file of the image archive of the drawing.
#[derive(Clone, Copy)]
struct ArchiveImage<'a> {
    name: &'a str,
    data: &'a [u8],
    compressed: bool,
}

/// ``_embedded_images``: the archive by the name a placement refers to (``.gz``
/// removed, lower case).
fn embedded_images(document: &JwwDocument) -> HashMap<String, ArchiveImage<'_>> {
    let mut images = HashMap::new();
    for image in &document.images {
        let name = image.name.as_str();
        if name.is_empty() {
            continue;
        }
        let bytes = name.as_bytes();
        // ``is_compressed`` of the reader: longer than the suffix and ending in it
        let compressed = bytes.len() > 3 && bytes[bytes.len() - 3..].eq_ignore_ascii_case(b".gz");
        let reference = if name.to_lowercase().ends_with(".gz") {
            // the suffix is ASCII, so the cut is between two characters
            &name[..name.len() - 3]
        } else {
            name
        };
        images.insert(
            reference.to_lowercase(),
            ArchiveImage {
                name,
                data: &image.data,
                compressed,
            },
        );
    }
    images
}

/// The bytes of an embedded image as the IR carries them: base64 text, with
/// the length and the first bytes of the file (for its kind).
struct EncodedImage {
    base64: String,
    length: usize,
    head: Vec<u8>,
}

/// Base64 text made piece by piece. An image can be hundreds of megabytes once
/// decompressed; encoding it as it comes keeps the bytes themselves out of
/// memory, and only the text the IR holds is kept.
struct Base64Writer {
    text: String,
    /// Bytes not yet encoded: fewer than three, a group is three bytes.
    pending: Vec<u8>,
    length: usize,
    head: Vec<u8>,
}

impl Base64Writer {
    fn new(expected: usize) -> Self {
        Base64Writer {
            text: String::with_capacity(expected.div_ceil(3) * 4),
            pending: Vec::with_capacity(3),
            length: 0,
            head: Vec::with_capacity(16),
        }
    }

    fn write(&mut self, mut bytes: &[u8]) {
        self.length += bytes.len();
        if self.head.len() < 16 {
            let room = 16 - self.head.len();
            self.head.extend_from_slice(&bytes[..bytes.len().min(room)]);
        }
        if !self.pending.is_empty() {
            let take = (3 - self.pending.len()).min(bytes.len());
            self.pending.extend_from_slice(&bytes[..take]);
            bytes = &bytes[take..];
            if self.pending.len() < 3 {
                return;
            }
            self.text
                .push_str(&ezjww_core::base64::encode(&self.pending));
            self.pending.clear();
        }
        let whole = bytes.len() - bytes.len() % 3;
        self.text
            .push_str(&ezjww_core::base64::encode(&bytes[..whole]));
        self.pending.extend_from_slice(&bytes[whole..]);
    }

    fn finish(mut self) -> EncodedImage {
        // the last group, padded
        self.text
            .push_str(&ezjww_core::base64::encode(&self.pending));
        EncodedImage {
            base64: self.text,
            length: self.length,
            head: self.head,
        }
    }
}

/// ``gzip.decompress`` into base64 text: every member of the stream, with zero
/// bytes allowed between and after them. ``None`` for a stream Python raises on.
fn gunzip_to_base64(mut rest: &[u8]) -> Option<EncodedImage> {
    // the size a member announces in its last four bytes (modulo 2^32), for the
    // room the text takes
    let announced = match rest {
        [.., a, b, c, d] => u32::from_le_bytes([*a, *b, *c, *d]) as usize,
        _ => 0,
    };
    let mut writer = Base64Writer::new(announced.min(1 << 30));
    let mut buffer = vec![0_u8; 3 * (1 << 16)];
    loop {
        if rest.is_empty() {
            return Some(writer.finish());
        }
        if !rest.starts_with(&[0x1f, 0x8b]) {
            return None;
        }
        let mut decoder = flate2::bufread::GzDecoder::new(rest);
        loop {
            match decoder.read(&mut buffer) {
                Ok(0) => break,
                Ok(count) => writer.write(&buffer[..count]),
                Err(error) if error.kind() == std::io::ErrorKind::Interrupted => {}
                Err(_) => return None,
            }
        }
        rest = decoder.into_inner();
        while let [0, tail @ ..] = rest {
            rest = tail;
        }
    }
}

/// The bytes of an archive file that is not compressed, as base64 text.
fn plain_to_base64(data: &[u8]) -> EncodedImage {
    EncodedImage {
        base64: ezjww_core::base64::encode(data),
        length: data.len(),
        head: data[..data.len().min(16)].to_vec(),
    }
}

/// ``_sniff_mime``.
fn sniff_mime(data: &[u8]) -> &'static str {
    if data.starts_with(b"BM") {
        "image/bmp"
    } else if data.starts_with(b"\x89PNG\r\n\x1a\n") {
        "image/png"
    } else if data.starts_with(b"\xff\xd8") {
        "image/jpeg"
    } else if data.starts_with(b"GIF87a") || data.starts_with(b"GIF89a") {
        "image/gif"
    } else if data.starts_with(b"II*\x00") || data.starts_with(b"MM\x00*") {
        "image/tiff"
    } else {
        "application/octet-stream"
    }
}

struct Converter<'a> {
    context: &'a Context,
    out: Converted,
    next_entity_number: u64,
    images: HashMap<String, ArchiveImage<'a>>,
}

impl Converter<'_> {
    fn allocate_id(&mut self) -> Box<str> {
        let id = format!("{}_E{:08}", self.context.label, self.next_entity_number);
        self.next_entity_number += 1;
        id.into_boxed_str()
    }

    fn diagnose(
        &mut self,
        code: &'static str,
        severity: &'static str,
        message: String,
        source_id: &str,
        source_kind: &str,
        action: &'static str,
    ) {
        self.out.diagnostics.push(ImportDiagnostic {
            code,
            severity,
            message,
            source_id: Some(source_id.to_string()),
            source_kind: Some(source_kind.to_string()),
            action: Some(action),
        });
    }

    /// ``_entity_common``: id, layer, linetype, colour, line width and, with
    /// entity provenance, where the entity came from.
    fn common(
        &mut self,
        base: &EntityBase,
        custom_color: Option<u32>,
        source_id: &str,
        source_kind: &str,
    ) -> Result<Entity, EntityError> {
        let layer_group = base.layer_group as i64;
        let layer = base.layer as i64;
        let Some(layer_name) = self.context.layer_names.get(&(layer_group, layer)) else {
            // the Python importer adds a layer to the table here
            return Err(EntityError::Unsupported(format!(
                "an entity on layer {layer_group:X}-{layer:X}, which the header does not have"
            )));
        };
        let pen_style = base.pen_style as i64;
        let pen_width = base.pen_width as i64;
        let pen_color = base.pen_color as i64;
        let linetype: Arc<str> = match self.context.linetype_names.get(&pen_style) {
            Some(name) => Arc::clone(name),
            None => Arc::from("BYLAYER"),
        };
        if !self.out.used_linetypes.contains(linetype.as_ref()) {
            self.out.used_linetypes.insert(linetype.to_string());
        }
        let mut entity = Entity::new(Shape::Other);
        entity.id = Some(self.allocate_id());
        entity.layer = Some(Arc::clone(layer_name));
        entity.linetype = Some(linetype);
        entity.color = Some(Color::Index(
            self.context
                .pen_colors
                .get(&pen_color)
                .copied()
                .unwrap_or(7),
        ));
        if self.context.entity_provenance {
            entity.extra_insert(
                "source",
                Value::Map(map_of(vec![
                    ("format", Value::Str(self.context.source_format.clone())),
                    ("id", Value::Str(source_id.to_string())),
                    ("kind", Value::Str(source_kind.to_string())),
                ])),
            );
            entity.extra_insert(
                "metadata",
                Value::Map(map_of(vec![(
                    "jww",
                    Value::Map(map_of(vec![
                        ("group", int(base.group as i64)),
                        ("pen_style", int(pen_style)),
                        ("pen_color", int(pen_color)),
                        ("pen_width", int(pen_width)),
                        ("layer", int(layer)),
                        ("layer_group", int(layer_group)),
                        ("flag", int(base.flag as i64)),
                    ])),
                )])),
            );
        }
        if pen_width > 0 {
            entity.lineweight_mm = Some(pen_width.min(211) as f64 / 100.0);
        } else if let Some(by_pen) = self.context.pen_widths.get(&pen_color) {
            // the usual case: Jw_cad prints the entity at the width of its pen colour
            entity.lineweight_mm = Some(*by_pen);
            self.out.pen_width_entities += 1;
        }
        if let Some(custom) = custom_color {
            entity.color = Some(Color::Text(Arc::from(
                format!("#{:06X}", custom & 0x00FF_FFFF).as_str(),
            )));
            jww_metadata_insert(&mut entity, "custom_color", int(custom as i64));
        }
        Ok(entity)
    }

    fn convert_entity(
        &mut self,
        source: &JwEntity,
        source_id: &str,
        source_kind: &str,
    ) -> EntityResult {
        let custom_color = match source {
            JwEntity::Solid(solid) => solid.color,
            JwEntity::CircleSolid(solid) => solid.color,
            _ => None,
        };
        let mut entity = self.common(source.base(), custom_color, source_id, source_kind)?;
        match source {
            JwEntity::Line(line) => {
                entity.shape = Shape::Line {
                    p1: [
                        number(line.start_x, "start_x")?,
                        number(line.start_y, "start_y")?,
                    ],
                    p2: [number(line.end_x, "end_x")?, number(line.end_y, "end_y")?],
                };
            }
            JwEntity::Arc(arc) => self.convert_arc(arc, &mut entity, source_id, source_kind)?,
            JwEntity::Point(point) => {
                entity.shape = Shape::Point {
                    position: [number(point.x, "x")?, number(point.y, "y")?],
                    temporary: if point.is_temporary { Some(true) } else { None },
                    marker_code: Some(point.code as i64),
                };
                if point.scale > 0.0 {
                    entity.extra_insert("scale", Value::Float(point.scale));
                }
                if point.angle != 0.0 {
                    entity.extra_insert("rotation", Value::Float(point.angle));
                }
            }
            JwEntity::Text(text) => {
                // a placement text of an image becomes an IMAGE; one that is no
                // image after all stays a text
                if !(text.content.starts_with(IMAGE_TEXT_PREFIX)
                    && self.convert_image(text, &mut entity, source_id)?)
                {
                    self.convert_text(text, &mut entity, source_id)?;
                }
            }
            JwEntity::Solid(solid) => convert_solid(solid, &mut entity)?,
            JwEntity::CircleSolid(solid) => self.convert_circle_solid(solid, &mut entity)?,
            JwEntity::Block(block) => {
                self.convert_block_reference(block, &mut entity, source_id)?
            }
            JwEntity::Dimension(dimension) => {
                self.out.preserved_dimensions += 1;
                convert_dimension(dimension, &mut entity)?;
            }
        }
        Ok(Some(entity))
    }

    /// ``_convert_image``: a ``^@BM`` placement text as an ``IMAGE``. ``false``
    /// keeps it a plain text.
    fn convert_image(
        &mut self,
        source: &JwText,
        entity: &mut Entity,
        source_id: &str,
    ) -> Result<bool, EntityError> {
        let content = source.content.as_str();
        let Some(parsed) = ezjww_core::parse_image_reference(content) else {
            // Without a reference from the reader the Python importer splits
            // the text itself, with ``float``, which reads a few spellings of a
            // number the reader does not. Where that could matter, leave it.
            let mut parts = content[IMAGE_TEXT_PREFIX.len()..].split(',');
            let path_is_empty = parts.next().is_none_or(str::is_empty);
            if !path_is_empty && parts.nth(1).is_some() {
                return Err(EntityError::Unsupported(
                    "an image placement the reader does not parse".to_string(),
                ));
            }
            return Ok(false);
        };
        let (width, height) = (parsed.width, parsed.height);
        if !(width > 0.0 && height > 0.0) {
            return Ok(false);
        }
        let path = parsed.path.as_str();
        let file_name = if parsed.file_name.is_empty() {
            path
        } else {
            parsed.file_name.as_str()
        };
        let insert = point_value([
            number(source.start_x, "start_x")?,
            number(source.start_y, "start_y")?,
        ]);
        entity.shape = Shape::Other;
        entity.extra_insert("kind", Value::Str("IMAGE".to_string()));
        entity.extra_insert("insert", insert);
        entity.extra_insert("width", Value::Float(width));
        entity.extra_insert("height", Value::Float(height));
        entity.extra_insert("name", Value::Str(file_name.to_string()));
        if source.angle != 0.0 {
            entity.extra_insert("rotation", Value::Float(source.angle));
        }
        let embedded = path.to_lowercase().starts_with(IMAGE_TEMP_PREFIX);
        let archive = if embedded {
            self.images.get(&file_name.to_lowercase()).copied()
        } else {
            None
        };
        let data = match archive {
            Some(archive) if archive.compressed => {
                Some(gunzip_to_base64(archive.data).ok_or_else(|| {
                    // Python reports the error of its gzip module in a diagnostic
                    EntityError::Unsupported(
                        "an embedded image that does not decompress".to_string(),
                    )
                })?)
            }
            Some(archive) => Some(plain_to_base64(archive.data)),
            None => None,
        };
        match data.filter(|data| data.length > 0) {
            Some(EncodedImage {
                base64,
                length,
                head,
            }) => {
                entity.extra_insert("data", Value::Str(base64));
                entity.extra_insert("mime_type", Value::Str(sniff_mime(&head).to_string()));
                self.out.embedded_images += 1;
                self.diagnose(
                    "JWW_IMAGE_EMBEDDED",
                    "info",
                    format!(
                        "Image {file_name} ({length} bytes) was decoded from the drawing's archive."
                    ),
                    source_id,
                    "TEXT",
                    "converted",
                );
            }
            None => {
                if self.context.source_format == "jws" {
                    return Err(EntityError::Unsupported(
                        "an image of a JWS symbol".to_string(),
                    ));
                }
                entity.extra_insert("href", Value::Str(path.to_string()));
                self.out.linked_images += 1;
                let reason = if embedded {
                    "is not in the drawing's image archive"
                } else {
                    "is an external file that is not embedded in the drawing"
                };
                self.out.diagnostics.push(ImportDiagnostic {
                    code: "JWW_IMAGE_LINKED",
                    severity: "warning",
                    message: format!(
                        "Image {file_name} {reason}; the placement keeps only the path."
                    ),
                    source_id: Some(source_id.to_string()),
                    source_kind: Some("TEXT".to_string()),
                    action: None,
                });
            }
        }
        if self.context.entity_provenance {
            jww_metadata_insert(entity, "image_content", Value::Str(content.to_string()));
            jww_metadata_insert(
                entity,
                "image_params",
                Value::List(parsed.extra.iter().cloned().map(Value::Str).collect()),
            );
            jww_metadata_insert(
                entity,
                "archive_name",
                match archive {
                    Some(archive) => Value::Str(archive.name.to_string()),
                    None => Value::Null,
                },
            );
            let end = point_value([
                number(source.end_x, "end_x")?,
                number(source.end_y, "end_y")?,
            ]);
            jww_metadata_insert(entity, "end", end);
            jww_metadata_insert(entity, "text_type", int(source.text_type as i64));
            jww_metadata_insert(entity, "size_x", Value::Float(source.size_x));
            jww_metadata_insert(entity, "size_y", Value::Float(source.size_y));
            jww_metadata_insert(entity, "spacing", Value::Float(source.spacing));
            jww_metadata_insert(entity, "font_name", Value::Str(source.font_name.clone()));
        }
        Ok(true)
    }

    /// ``_convert_arc``: a circle, an arc or (with a flatness) an ellipse.
    fn convert_arc(
        &mut self,
        source: &JwArc,
        entity: &mut Entity,
        source_id: &str,
        source_kind: &str,
    ) -> Result<(), Rejected> {
        let radius = number(source.radius, "radius")?.abs();
        if radius <= EPSILON {
            return Err(Rejected("radius must be non-zero".to_string()));
        }
        let center = [
            number(source.center_x, "center_x")?,
            number(source.center_y, "center_y")?,
        ];
        let mut flatness = source.flatness.abs();
        if flatness <= EPSILON {
            flatness = 1.0;
            self.diagnose(
                "JWW_ZERO_FLATNESS_NORMALIZED",
                "warning",
                "Zero arc flatness was normalized to 1.0.".to_string(),
                source_id,
                source_kind,
                "normalized",
            );
        }
        let is_full = source.is_full_circle;
        let start = source.start_angle;
        let span = source.arc_angle;
        if isclose(flatness, 1.0, 1e-12, 1e-12) {
            if is_full {
                entity.shape = Shape::Circle { center, radius };
                return Ok(());
            }
            let (span_start, span_end) = ccw_span(start, span);
            entity.shape = Shape::Arc {
                center,
                radius,
                start_angle: normalize_degrees(span_start.to_degrees()),
                end_angle: normalize_degrees(span_end.to_degrees()),
                ccw: Some(true),
            };
            return Ok(());
        }
        let mut major_radius = radius;
        let mut ratio = flatness;
        let mut tilt = source.tilt_angle;
        if ratio > 1.0 {
            major_radius *= ratio;
            ratio = 1.0 / ratio;
            tilt += std::f64::consts::PI / 2.0;
        }
        let (span_start, span_end) = ccw_span(start, span);
        entity.shape = Shape::Ellipse(Box::new(EllipseData {
            center,
            major_axis: [major_radius * tilt.cos(), major_radius * tilt.sin()],
            ratio,
            start_param: if is_full { 0.0 } else { span_start },
            end_param: if is_full { TAU } else { span_end },
            ccw: Some(true),
        }));
        Ok(())
    }

    /// ``_convert_text``.
    fn convert_text(
        &mut self,
        source: &JwText,
        entity: &mut Entity,
        source_id: &str,
    ) -> Result<(), Rejected> {
        let mut height = source.size_y;
        if height <= 0.0 {
            height = 2.5;
            self.diagnose(
                "JWW_TEXT_HEIGHT_DEFAULTED",
                "warning",
                format!(
                    "Non-positive {} text height was replaced with 2.5 mm.",
                    self.context.label
                ),
                source_id,
                "TEXT",
                "normalized",
            );
        }
        let font_name = py_strip(&source.font_name);
        let insert = [
            number(source.start_x, "start_x")?,
            number(source.start_y, "start_y")?,
        ];
        let style: Arc<str> = Arc::from(if font_name.is_empty() {
            "STANDARD"
        } else {
            font_name
        });
        let rotation = source.angle;
        let size_x = source.size_x;
        entity.shape = Shape::Text(Box::new(TextData {
            insert,
            height,
            text: source.content.clone(),
            rotation: if rotation != 0.0 {
                Some(rotation)
            } else {
                None
            },
            style: Some(style),
            width_factor: if size_x > 0.0 {
                Some(size_x / height)
            } else {
                None
            },
        }));
        if self.context.entity_provenance {
            let end = point_value([
                number(source.end_x, "end_x")?,
                number(source.end_y, "end_y")?,
            ]);
            jww_metadata_insert(entity, "end", end);
            jww_metadata_insert(entity, "text_type", int(source.text_type as i64));
            jww_metadata_insert(entity, "size_x", Value::Float(size_x));
            jww_metadata_insert(entity, "size_y", Value::Float(source.size_y));
            jww_metadata_insert(entity, "spacing", Value::Float(source.spacing));
            jww_metadata_insert(entity, "font_name", Value::Str(source.font_name.clone()));
        }
        Ok(())
    }

    /// ``_convert_circle_solid``: a filled circle, sector or ring as a polygon.
    fn convert_circle_solid(
        &mut self,
        source: &CircleSolid,
        entity: &mut Entity,
    ) -> Result<(), Rejected> {
        let radius = number(source.radius, "radius")?.abs();
        if radius <= EPSILON {
            return Err(Rejected("circle-solid radius must be non-zero".to_string()));
        }
        let mut start = source.start_angle;
        let mut span = source.arc_angle;
        let mode = py_round(source.solid_mode);
        let is_full = mode == 100.0 || isclose(span.abs(), TAU, 0.0, 1e-6);
        if is_full {
            start = 0.0;
            span = TAU;
        }
        let steps = arc_segment_count(span, is_full, self.context.curve_segments);
        let outer = ellipse_arc_points(source, radius, start, span, is_full, steps)?;
        let pen_style = source.base.pen_style as i64;
        let loop_of = |vertices: Vec<Vertex>, is_outer: bool| HatchLoop {
            vertices,
            extra: Some(Box::new(map_of(vec![("is_outer", Value::Bool(is_outer))]))),
        };
        let loops: Vec<HatchLoop> = if pen_style == 105 || pen_style == 106 {
            let inner_radius = source.solid_mode.abs();
            if EPSILON < inner_radius && inner_radius < radius {
                let mut inner =
                    ellipse_arc_points(source, inner_radius, start, span, is_full, steps)?;
                inner.reverse();
                if is_full {
                    vec![loop_of(outer, true), loop_of(inner, false)]
                } else {
                    let mut vertices = outer;
                    vertices.extend(inner);
                    vec![loop_of(vertices, true)]
                }
            } else {
                vec![loop_of(outer, true)]
            }
        } else if mode == -1.0 || mode == 5.0 || mode == 100.0 || is_full {
            vec![loop_of(outer, true)]
        } else {
            let center = Vertex {
                x: number(source.center_x, "center_x")?,
                y: number(source.center_y, "center_y")?,
                bulge: None,
            };
            let mut vertices = Vec::with_capacity(outer.len() + 1);
            vertices.push(center);
            vertices.extend(outer);
            vec![loop_of(vertices, true)]
        };
        if loops.iter().any(|hatch_loop| hatch_loop.vertices.len() < 3) {
            return Err(Rejected(
                "circle-solid approximation produced fewer than three vertices".to_string(),
            ));
        }
        *self
            .out
            .approximation_counts
            .entry("CIRCLE_SOLID".to_string())
            .or_insert(0) += 1;
        entity.shape = Shape::Hatch(Box::new(HatchData {
            loops,
            solid: Some(true),
        }));
        entity.extra_insert(
            "approximation",
            Value::Map(map_of(vec![
                ("method", Value::Str("polyline".to_string())),
                ("source_kind", Value::Str("CIRCLE_SOLID".to_string())),
                ("segments", int(steps)),
                (
                    "metadata",
                    Value::Map(map_of(vec![(
                        "jww_solid_mode",
                        Value::Float(source.solid_mode),
                    )])),
                ),
            ])),
        );
        Ok(())
    }

    /// ``_convert_block_reference``.
    fn convert_block_reference(
        &mut self,
        source: &JwBlock,
        entity: &mut Entity,
        source_id: &str,
    ) -> Result<(), Rejected> {
        let number_of_definition = source.def_number as i64;
        let block_name: Arc<str> = match self.context.block_names.get(&number_of_definition) {
            Some(name) => Arc::clone(name),
            None => Arc::from(format!("BLOCK_{number_of_definition}").as_str()),
        };
        let mut scale_x = source.scale_x;
        let mut scale_y = source.scale_y;
        if scale_x == 0.0 || scale_y == 0.0 {
            if self.context.strict {
                return Err(Rejected(
                    "block reference scale must be non-zero".to_string(),
                ));
            }
            if scale_x == 0.0 {
                scale_x = 1.0;
            }
            if scale_y == 0.0 {
                scale_y = 1.0;
            }
            self.diagnose(
                "JWW_ZERO_BLOCK_SCALE_NORMALIZED",
                "warning",
                format!(
                    "Zero {} block scale was replaced with 1.0.",
                    self.context.label
                ),
                source_id,
                "BLOCK",
                "normalized",
            );
        }
        let insert = [
            number(source.ref_x, "ref_x")?,
            number(source.ref_y, "ref_y")?,
        ];
        let scale = if isclose(scale_x, 1.0, 1e-9, 0.0) && isclose(scale_y, 1.0, 1e-9, 0.0) {
            None
        } else if isclose(scale_x, scale_y, 1e-9, 0.0) {
            Some(InsertScale::Uniform(scale_x))
        } else {
            Some(InsertScale::Axes([scale_x, scale_y]))
        };
        let rotation = source.rotation;
        entity.shape = Shape::Insert(Box::new(InsertData {
            block: block_name,
            insert,
            rotation: if rotation != 0.0 {
                Some(rotation.to_degrees())
            } else {
                None
            },
            scale,
        }));
        jww_metadata_insert(entity, "def_number", int(number_of_definition));
        Ok(())
    }

    /// ``_convert_entity_sequence`` with ``_convert_entity_safe``.
    fn convert_sequence(
        &mut self,
        sources: &[JwEntity],
        source_prefix: &str,
        skipped: Option<&BTreeSet<usize>>,
    ) -> Result<Vec<Entity>, ConvertError> {
        let mut converted: Vec<Entity> = Vec::with_capacity(sources.len());
        for (index, source) in sources.iter().enumerate() {
            if skipped.is_some_and(|skipped| skipped.contains(&index)) {
                continue;
            }
            let source_id = format!("{source_prefix}[{index}]");
            let source_kind = source.entity_type();
            match self.convert_entity(source, &source_id, source_kind) {
                Ok(Some(entity)) => {
                    let kind = entity.kind().unwrap_or("").to_string();
                    *self.out.converted_counts.entry(kind).or_insert(0) += 1;
                    converted.push(entity);
                }
                Ok(None) => {}
                Err(EntityError::Unsupported(reason)) => {
                    return Err(ConvertError::Unsupported(reason))
                }
                Err(EntityError::Rejected(Rejected(reason))) => {
                    if self.context.strict {
                        return Err(ConvertError::Failed(format!(
                            "Failed to convert {} {source_kind} at {source_id}: {reason}",
                            self.context.label
                        )));
                    }
                    *self
                        .out
                        .skipped_counts
                        .entry(source_kind.to_string())
                        .or_insert(0) += 1;
                    self.diagnose(
                        "JWW_ENTITY_CONVERSION_FAILED",
                        "error",
                        format!(
                            "Failed to convert {} {source_kind}: {reason}",
                            self.context.label
                        ),
                        &source_id,
                        source_kind,
                        "skipped",
                    );
                }
            }
        }
        Ok(converted)
    }
}

/// ``entity["metadata"]["jww"][key] = value`` when entity provenance is on.
fn jww_metadata_insert(entity: &mut Entity, key: &str, value: Value) {
    let Some(extra) = entity.extra.as_mut() else {
        return;
    };
    let Some(Value::Map(metadata)) = extra.get_mut("metadata") else {
        return;
    };
    if let Some(Value::Map(jww)) = metadata.get_mut("jww") {
        jww.insert(key.to_string(), value);
    }
}

/// ``_convert_dimension``: a GENERIC dimension that keeps the records it was made of.
fn convert_dimension(source: &Dimension, entity: &mut Entity) -> Result<(), Rejected> {
    let line = &source.line;
    let text = &source.text;
    let line_start = [
        number(line.start_x, "start_x")?,
        number(line.start_y, "start_y")?,
    ];
    let line_end = [number(line.end_x, "end_x")?, number(line.end_y, "end_y")?];
    let text_start = [
        number(text.start_x, "start_x")?,
        number(text.start_y, "start_y")?,
    ];
    let text_end = [number(text.end_x, "end_x")?, number(text.end_y, "end_y")?];
    let sxf_mode = match source.sxf_mode {
        Some(mode) => int(mode as i64),
        None => Value::Null,
    };
    jww_metadata_insert(entity, "sxf_mode", sxf_mode.clone());
    entity.shape = Shape::Other;
    entity.extra_insert("kind", Value::Str("DIMENSION".to_string()));
    entity.extra_insert("dim_kind", Value::Str("GENERIC".to_string()));
    let points = map_of(vec![
        ("location", point_value(line_start)),
        ("p1", point_value(line_start)),
        ("p2", point_value(line_end)),
        (
            "text_midpoint",
            point_value([
                (text_start[0] + text_end[0]) / 2.0,
                (text_start[1] + text_end[1]) / 2.0,
            ]),
        ),
    ]);
    let source_geometry = map_of(vec![
        ("line", Value::Map(line_value(line))),
        ("text", Value::Map(text_value(text))),
        ("sxf_mode", sxf_mode),
        (
            "aux_lines",
            Value::List(
                source
                    .aux_lines
                    .iter()
                    .map(|l| Value::Map(line_value(l)))
                    .collect(),
            ),
        ),
        (
            "aux_points",
            Value::List(
                source
                    .aux_points
                    .iter()
                    .map(|p| Value::Map(point_record_value(p)))
                    .collect(),
            ),
        ),
    ]);
    entity.extra_insert(
        "definition",
        Value::Map(map_of(vec![
            ("points", Value::Map(points)),
            ("text", Value::Str(text.content.clone())),
            ("source_geometry", Value::Map(source_geometry)),
        ])),
    );
    Ok(())
}

/// ``_convert_solid``: a filled quadrilateral.
fn convert_solid(source: &Solid, entity: &mut Entity) -> Result<(), Rejected> {
    let points = [
        [
            number(source.point1_x, "point1_x")?,
            number(source.point1_y, "point1_y")?,
        ],
        [
            number(source.point2_x, "point2_x")?,
            number(source.point2_y, "point2_y")?,
        ],
        [
            number(source.point3_x, "point3_x")?,
            number(source.point3_y, "point3_y")?,
        ],
        [
            number(source.point4_x, "point4_x")?,
            number(source.point4_y, "point4_y")?,
        ],
    ];
    let vertices = order_solid_vertices(points)
        .into_iter()
        .map(|point| Vertex {
            x: point[0],
            y: point[1],
            bulge: None,
        })
        .collect();
    entity.shape = Shape::Hatch(Box::new(HatchData {
        loops: vec![HatchLoop {
            vertices,
            extra: Some(Box::new(map_of(vec![("is_outer", Value::Bool(true))]))),
        }],
        solid: Some(true),
    }));
    Ok(())
}

fn orientation(a: [f64; 2], b: [f64; 2], c: [f64; 2]) -> f64 {
    (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])
}

fn segments_intersect(a: [f64; 2], b: [f64; 2], c: [f64; 2], d: [f64; 2]) -> bool {
    orientation(a, b, c) * orientation(a, b, d) < 0.0
        && orientation(c, d, a) * orientation(c, d, b) < 0.0
}

fn same_point(a: [f64; 2], b: [f64; 2]) -> bool {
    isclose(a[0], b[0], 1e-9, 1e-9) && isclose(a[1], b[1], 1e-9, 1e-9)
}

/// ``_order_solid_vertices``: a crossed quadrilateral in the order round its centre.
fn order_solid_vertices(points: [[f64; 2]; 4]) -> Vec<[f64; 2]> {
    let crosses = segments_intersect(points[0], points[1], points[2], points[3])
        || segments_intersect(points[1], points[2], points[3], points[0]);
    if !crosses {
        return points.to_vec();
    }
    // ``sum()`` of Python: compensated, which can differ in the last bit from
    // adding one after the other, and so can the order of two nearly equal angles
    let center_x = cad2d_ir_core::num::py_sum(points.iter().map(|point| point[0])) / 4.0;
    let center_y = cad2d_ir_core::num::py_sum(points.iter().map(|point| point[1])) / 4.0;
    let mut ordered = points.to_vec();
    ordered.sort_by(|a, b| {
        let angle_a = (a[1] - center_y).atan2(a[0] - center_x);
        let angle_b = (b[1] - center_y).atan2(b[0] - center_x);
        angle_a
            .partial_cmp(&angle_b)
            .unwrap_or(std::cmp::Ordering::Equal)
    });
    let first = ordered
        .iter()
        .position(|point| same_point(*point, points[0]))
        .unwrap_or(0);
    ordered.rotate_left(first);
    ordered
}

/// ``_arc_segment_count``.
fn arc_segment_count(span: f64, is_full: bool, full_segments: i64) -> i64 {
    let minimum = std::f64::consts::PI / 32.0;
    let effective_span = if is_full {
        TAU
    } else if span.abs() >= minimum || span.abs().is_nan() {
        span.abs()
    } else {
        minimum
    };
    let needed = (effective_span / TAU * full_segments as f64).ceil() as i64;
    needed.min(full_segments).max(8)
}

/// ``_ellipse_point``.
fn ellipse_point(source: &CircleSolid, radius: f64, angle: f64) -> Result<Vertex, Rejected> {
    let mut flatness = source.flatness.abs();
    if flatness <= EPSILON {
        flatness = 1.0;
    }
    let mut major_radius = radius.abs();
    let mut ratio = flatness;
    let mut tilt = source.tilt_angle;
    if ratio > 1.0 {
        major_radius *= ratio;
        ratio = 1.0 / ratio;
        tilt += std::f64::consts::PI / 2.0;
    }
    let minor_radius = major_radius * ratio;
    let local_x = major_radius * angle.cos();
    let local_y = minor_radius * angle.sin();
    let cos_tilt = tilt.cos();
    let sin_tilt = tilt.sin();
    Ok(Vertex {
        x: number(source.center_x, "center_x")? + local_x * cos_tilt - local_y * sin_tilt,
        y: number(source.center_y, "center_y")? + local_x * sin_tilt + local_y * cos_tilt,
        bulge: None,
    })
}

/// ``_ellipse_arc_points``.
fn ellipse_arc_points(
    source: &CircleSolid,
    radius: f64,
    start: f64,
    span: f64,
    is_full: bool,
    steps: i64,
) -> Result<Vec<Vertex>, Rejected> {
    let end_index = if is_full { steps } else { steps + 1 };
    (0..end_index)
        .map(|index| ellipse_point(source, radius, start + span * (index as f64 / steps as f64)))
        .collect()
}

/// The font a text names, as the text style table has it.
fn font_of(entity: &JwEntity) -> Option<&str> {
    let font = match entity {
        JwEntity::Text(text) => &text.font_name,
        JwEntity::Dimension(dimension) => &dimension.text.font_name,
        _ => return None,
    };
    let font = py_strip(font);
    Some(if font.is_empty() { "STANDARD" } else { font })
}

/// Convert the entities and block definitions of a parsed JWW document.
pub fn convert(document: &JwwDocument, context: &Context) -> Result<Converted, ConvertError> {
    let mut converter = Converter {
        context,
        out: Converted::default(),
        next_entity_number: 1,
        images: embedded_images(document),
    };
    converter.out.source_entities = document.entities.len();

    let settings = ezjww_core::collect_metadata_settings(&document.entities);
    let skipped: BTreeSet<usize> = settings
        .iter()
        .map(|setting| setting.entity_index)
        .collect();
    converter.out.metadata_settings = settings
        .into_iter()
        .map(|setting| MetadataSetting {
            entity_index: setting.entity_index,
            key: setting.key,
            value: setting.value,
            raw: setting.raw,
        })
        .collect();

    // ``_collect_text_styles``: the fonts of the texts, STANDARD among them
    let mut fonts: BTreeSet<String> = BTreeSet::new();
    fonts.insert("STANDARD".to_string());
    for (index, entity) in document.entities.iter().enumerate() {
        if skipped.contains(&index) {
            continue;
        }
        if let Some(font) = font_of(entity) {
            if !fonts.contains(font) {
                fonts.insert(font.to_string());
            }
        }
    }
    for block_def in &document.block_defs {
        for entity in &block_def.entities {
            if let Some(font) = font_of(entity) {
                if !fonts.contains(font) {
                    fonts.insert(font.to_string());
                }
            }
        }
    }
    converter.out.fonts = fonts;

    converter.out.entities =
        converter.convert_sequence(&document.entities, "entities", Some(&skipped))?;
    for (index, block_def) in document.block_defs.iter().enumerate() {
        let prefix = format!("block_defs[{index}].entities");
        let entities = converter.convert_sequence(&block_def.entities, &prefix, None)?;
        converter.out.blocks.push(entities);
    }
    Ok(converter.out)
}
