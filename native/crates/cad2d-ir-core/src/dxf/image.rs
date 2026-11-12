//! Raster images: an ``IMAGE`` entity that refers to an ``IMAGEDEF`` object (the
//! file and its size in pixels) and owns an ``IMAGEDEF_REACTOR``; the
//! definitions hang in the ``ACAD_IMAGE_DICT`` dictionary of the named object
//! dictionary, and the classes are declared in ``CLASSES``. A port of
//! ``_render_image``, ``_image_to_pairs`` and ``_ImageRegistry``.

use std::collections::HashSet;

use indexmap::IndexMap;

use super::common::Common;
use super::pairs::{py_strip, Pair, Val};
use super::{Context, HandleAllocator, WriteError};
use crate::model::Entity;
use crate::num::{num, py_round_to_int};
use crate::pystr::{casefold, repr};
use crate::value::{Map, Value};

/// Group 70 of IMAGE: show the image, also when it is not aligned with the screen.
const IMAGE_DISPLAY_FLAGS: &str = "3";
/// How many characters of the base64 text are read for the header of the file.
const PREFIX_LIMIT: usize = 1 << 20;

/// One IMAGEDEF object: the raster file the IMAGE entities showing it share.
struct ImageDefinition {
    handle: Val,
    name: String,
    file_path: String,
    size: (i64, i64),
    pixel_size: (f64, f64),
    /// (IMAGEDEF_REACTOR handle, handle of the IMAGE entity that owns it)
    reactors: Vec<(Val, Val)>,
}

/// The objects behind the IMAGE entities of one file.
#[derive(Default)]
pub(crate) struct ImageRegistry {
    definitions: IndexMap<String, ImageDefinition>,
    /// Names of the definitions, case folded.
    names: HashSet<String>,
}

/// An image about to be written: what ``_render_image`` works out before the
/// entity has its handle.
pub(crate) struct Prepared {
    width: f64,
    height: f64,
    size: (i64, i64),
    file_path: String,
    definition_handle: Val,
    reactor_handle: Val,
}

impl ImageRegistry {
    /// The handle of the definition of ``file_path``, made on first use.
    fn definition(
        &mut self,
        file_path: &str,
        size: (i64, i64),
        pixel_size: (f64, f64),
        allocator: &mut HandleAllocator,
    ) -> Val {
        if let Some(existing) = self.definitions.get(file_path) {
            return existing.handle.clone();
        }
        let name = image_definition_name(file_path, &self.names);
        self.names.insert(casefold(&name));
        let handle = allocator.allocate();
        self.definitions.insert(
            file_path.to_string(),
            ImageDefinition {
                handle: handle.clone(),
                name,
                file_path: file_path.to_string(),
                size,
                pixel_size,
                reactors: Vec::new(),
            },
        );
        handle
    }

    /// The reactor of ``prepared`` belongs to the IMAGE entity with ``handle``.
    pub(crate) fn bind(&mut self, prepared: &Prepared, handle: Val) {
        if let Some(definition) = self.definitions.get_mut(&prepared.file_path) {
            definition
                .reactors
                .push((prepared.reactor_handle.clone(), handle));
        }
    }

    /// The owners of the reactors that were bound before their image had its
    /// handle.
    pub(crate) fn resolve_owners(&mut self, resolve: &dyn Fn(&str) -> Option<Val>) {
        for definition in self.definitions.values_mut() {
            for (_, owner) in definition.reactors.iter_mut() {
                if let Some(handle) = resolve(owner.as_str()) {
                    *owner = handle;
                }
            }
        }
    }

    /// The CLASSES section: the image classes when there are images.
    pub(crate) fn classes_pairs(&self) -> Vec<Pair> {
        let images: usize = self
            .definitions
            .values()
            .map(|definition| definition.reactors.len())
            .sum();
        let mut pairs: Vec<Pair> = vec![(0, Val::S("SECTION")), (2, Val::S("CLASSES"))];
        if !self.definitions.is_empty() {
            let classes: [(
                &'static str,
                &'static str,
                &'static str,
                usize,
                &'static str,
            ); 4] = [
                ("IMAGE", "AcDbRasterImage", "127", images, "1"),
                (
                    "IMAGEDEF",
                    "AcDbRasterImageDef",
                    "0",
                    self.definitions.len(),
                    "0",
                ),
                (
                    "IMAGEDEF_REACTOR",
                    "AcDbRasterImageDefReactor",
                    "1",
                    images,
                    "0",
                ),
                ("RASTERVARIABLES", "AcDbRasterVariables", "0", 1, "0"),
            ];
            for (record, cpp_class, flags, count, is_entity) in classes {
                pairs.push((0, Val::S("CLASS")));
                pairs.push((1, Val::S(record)));
                pairs.push((2, Val::S(cpp_class)));
                pairs.push((3, Val::S("ISM")));
                pairs.push((90, Val::S(flags)));
                pairs.push((91, Val::owned(count.to_string())));
                pairs.push((280, Val::S("0")));
                pairs.push((281, Val::S(is_entity)));
            }
        }
        pairs.push((0, Val::S("ENDSEC")));
        pairs
    }

    /// The OBJECTS section: the named object dictionary with ``ACAD_GROUP``,
    /// which every DXF R2000+ file needs, and the image objects when there are
    /// images.
    pub(crate) fn objects_pairs(
        &self,
        allocator: &mut HandleAllocator,
        units_name: &str,
    ) -> Vec<Pair> {
        let root = allocator.allocate();
        let groups = allocator.allocate();
        let mut pairs: Vec<Pair> = vec![
            (0, Val::S("SECTION")),
            (2, Val::S("OBJECTS")),
            (0, Val::S("DICTIONARY")),
            (5, root.clone()),
            (330, Val::S("0")),
            (100, Val::S("AcDbDictionary")),
            (281, Val::S("1")),
            (3, Val::S("ACAD_GROUP")),
            (350, groups.clone()),
        ];
        let dictionary = |pairs: &mut Vec<Pair>, handle: Val, owner: Val| {
            pairs.push((0, Val::S("DICTIONARY")));
            pairs.push((5, handle));
            pairs.push((330, owner));
            pairs.push((100, Val::S("AcDbDictionary")));
            pairs.push((281, Val::S("1")));
        };
        if self.definitions.is_empty() {
            dictionary(&mut pairs, groups, root);
            pairs.push((0, Val::S("ENDSEC")));
            return pairs;
        }
        let image_dict = allocator.allocate();
        let variables = allocator.allocate();
        pairs.push((3, Val::S("ACAD_IMAGE_DICT")));
        pairs.push((350, image_dict.clone()));
        pairs.push((3, Val::S("ACAD_IMAGE_VARS")));
        pairs.push((350, variables.clone()));
        dictionary(&mut pairs, groups, root.clone());
        dictionary(&mut pairs, image_dict.clone(), root.clone());
        for definition in self.definitions.values() {
            pairs.push((3, Val::owned(definition.name.clone())));
            pairs.push((350, definition.handle.clone()));
        }
        let raster_units = match units_name {
            "mm" => "1",
            "cm" => "2",
            "m" => "3",
            "inch" => "5",
            "ft" => "6",
            _ => "0",
        };
        pairs.push((0, Val::S("RASTERVARIABLES")));
        pairs.push((5, variables));
        pairs.push((330, root));
        pairs.push((100, Val::S("AcDbRasterVariables")));
        pairs.push((90, Val::S("0")));
        pairs.push((70, Val::S("1")));
        pairs.push((71, Val::S("1")));
        pairs.push((72, Val::S(raster_units)));
        for definition in self.definitions.values() {
            pairs.push((0, Val::S("IMAGEDEF")));
            pairs.push((5, definition.handle.clone()));
            pairs.push((102, Val::S("{ACAD_REACTORS")));
            pairs.push((330, image_dict.clone()));
            for (reactor, _owner) in &definition.reactors {
                pairs.push((330, reactor.clone()));
            }
            pairs.push((102, Val::S("}")));
            pairs.push((330, image_dict.clone()));
            pairs.push((100, Val::S("AcDbRasterImageDef")));
            pairs.push((90, Val::S("0")));
            pairs.push((1, Val::owned(definition.file_path.clone())));
            pairs.push((10, Val::owned(num(definition.size.0 as f64))));
            pairs.push((20, Val::owned(num(definition.size.1 as f64))));
            pairs.push((11, Val::owned(num(definition.pixel_size.0))));
            pairs.push((21, Val::owned(num(definition.pixel_size.1))));
            pairs.push((280, Val::S("1")));
            pairs.push((281, Val::S("0")));
        }
        for definition in self.definitions.values() {
            for (reactor, owner) in &definition.reactors {
                pairs.push((0, Val::S("IMAGEDEF_REACTOR")));
                pairs.push((5, reactor.clone()));
                pairs.push((330, owner.clone()));
                pairs.push((100, Val::S("AcDbRasterImageDefReactor")));
                pairs.push((90, Val::S("2")));
                pairs.push((330, owner.clone()));
            }
        }
        pairs.push((0, Val::S("ENDSEC")));
        pairs
    }
}

/// ``_positive_finite``: a number (no ``bool``) that is finite and above zero.
fn positive_finite(value: Option<&Value>) -> Option<f64> {
    let number = value?.as_number()?;
    (number.is_finite() && number > 0.0).then_some(number)
}

/// ``_image_source_metadata``: the DXF or DWG image record the IR carried.
fn image_source_metadata(entity: &Entity) -> Option<&Map> {
    let metadata = entity.extra_get("metadata")?.as_map()?;
    for key in ["dxf", "dwg"] {
        let image = metadata
            .get(key)
            .and_then(Value::as_map)
            .and_then(|section| section.get("image"))
            .and_then(Value::as_map);
        if image.is_some() {
            return image;
        }
    }
    None
}

/// ``_image_display_values``: brightness, contrast and fade, else AutoCAD's.
fn image_display_values(entity: &Entity) -> (i64, i64, i64) {
    let image = image_source_metadata(entity);
    let value = |key: &str, default: i64| match image.and_then(|image| image.get(key)) {
        Some(Value::Int(value)) if (0..=100).contains(value) => *value,
        _ => default,
    };
    (
        value("brightness", 50),
        value("contrast", 50),
        value("fade", 0),
    )
}

/// ``_image_pixel_size``: columns and rows, from the record the IR carried,
/// else from the header of the embedded file.
fn image_pixel_size(entity: &Entity) -> Result<Option<(i64, i64)>, WriteError> {
    let size = image_source_metadata(entity).and_then(|image| image.get("size"));
    if let Some(Value::List(size)) = size {
        if size.len() == 2 {
            let mut rounded = [0_i64; 2];
            for (slot, value) in rounded.iter_mut().zip(size) {
                // a value that is no number, a NaN or an infinity is no size
                *slot = value
                    .as_python_number()
                    .and_then(py_round_to_int)
                    .unwrap_or(0);
            }
            let [columns, rows] = rounded;
            if columns > 0 && rows > 0 {
                return Ok(Some((columns, rows)));
            }
        }
    }
    if let Some(Value::Str(data)) = entity.extra_get("data") {
        if !py_strip(data).is_empty() {
            return Ok(sniff_image_size(&decode_image_prefix(data)));
        }
    }
    Ok(None)
}

fn base64_value(byte: u8) -> Option<u32> {
    match byte {
        b'A'..=b'Z' => Some((byte - b'A') as u32),
        b'a'..=b'z' => Some((byte - b'a') as u32 + 26),
        b'0'..=b'9' => Some((byte - b'0') as u32 + 52),
        b'+' => Some(62),
        b'/' => Some(63),
        _ => None,
    }
}

/// ``_decode_image_prefix``: the bytes of the first characters of base64 text,
/// as ``base64.b64decode(text, validate=False)`` reads them: white space is
/// left out before, a character outside the alphabet is skipped, padding ends
/// the data, and text that is no base64 (a character outside ASCII, a group
/// that is not complete) gives no bytes.
fn decode_image_prefix(data: &str) -> Vec<u8> {
    let mut compact: Vec<char> = Vec::with_capacity(data.len().min(PREFIX_LIMIT));
    for character in data.chars() {
        if character.is_whitespace() || ('\u{1c}'..='\u{1f}').contains(&character) {
            continue;
        }
        if compact.len() == PREFIX_LIMIT {
            break;
        }
        compact.push(character);
    }
    compact.truncate(compact.len() - compact.len() % 4);
    if compact.iter().any(|character| !character.is_ascii()) {
        return Vec::new();
    }
    let mut out = Vec::with_capacity(compact.len() / 4 * 3);
    let mut quad_position = 0;
    let mut left: u32 = 0;
    let mut pads = 0;
    for character in compact {
        let byte = character as u8;
        if byte == b'=' {
            if quad_position >= 2 {
                pads += 1;
                if quad_position + pads >= 4 {
                    // a pad sequence ends the data
                    return out;
                }
            }
            continue;
        }
        let Some(value) = base64_value(byte) else {
            continue;
        };
        pads = 0;
        match quad_position {
            0 => {
                quad_position = 1;
                left = value;
            }
            1 => {
                quad_position = 2;
                out.push(((left << 2) | (value >> 4)) as u8);
                left = value & 0x0f;
            }
            2 => {
                quad_position = 3;
                out.push(((left << 4) | (value >> 2)) as u8);
                left = value & 0x03;
            }
            _ => {
                quad_position = 0;
                out.push(((left << 6) | value) as u8);
                left = 0;
            }
        }
    }
    if quad_position != 0 {
        return Vec::new();
    }
    out
}

fn u16_le(raw: &[u8], at: usize) -> i64 {
    u16::from_le_bytes([raw[at], raw[at + 1]]) as i64
}

fn u16_be(raw: &[u8], at: usize) -> i64 {
    u16::from_be_bytes([raw[at], raw[at + 1]]) as i64
}

/// ``_sniff_image_size``: width and height from the header of a PNG, GIF, BMP
/// or JPEG file.
fn sniff_image_size(raw: &[u8]) -> Option<(i64, i64)> {
    let (width, height) = if raw.starts_with(b"\x89PNG\r\n\x1a\n") && raw.len() >= 24 {
        (
            u32::from_be_bytes([raw[16], raw[17], raw[18], raw[19]]) as i64,
            u32::from_be_bytes([raw[20], raw[21], raw[22], raw[23]]) as i64,
        )
    } else if (raw.starts_with(b"GIF87a") || raw.starts_with(b"GIF89a")) && raw.len() >= 10 {
        (u16_le(raw, 6), u16_le(raw, 8))
    } else if raw.starts_with(b"BM") && raw.len() >= 26 {
        let header_size = u32::from_le_bytes([raw[14], raw[15], raw[16], raw[17]]);
        if header_size == 12 {
            (u16_le(raw, 18), u16_le(raw, 20))
        } else {
            let width = i32::from_le_bytes([raw[18], raw[19], raw[20], raw[21]]) as i64;
            let height = i32::from_le_bytes([raw[22], raw[23], raw[24], raw[25]]) as i64;
            (width, height.abs())
        }
    } else if raw.starts_with(b"\xff\xd8") {
        jpeg_frame_size(raw)?
    } else {
        return None;
    };
    (width > 0 && height > 0).then_some((width, height))
}

/// ``_jpeg_frame_size``: the size in the first frame header of a JPEG file.
fn jpeg_frame_size(raw: &[u8]) -> Option<(i64, i64)> {
    let mut position = 2;
    while position + 4 <= raw.len() {
        if raw[position] != 0xFF {
            position += 1;
            continue;
        }
        let marker = raw[position + 1];
        if marker == 0xFF {
            position += 1;
            continue;
        }
        if marker == 0x01 || marker == 0xD8 || (0xD0..=0xD7).contains(&marker) {
            position += 2;
            continue;
        }
        if marker == 0xD9 || marker == 0xDA {
            return None;
        }
        let length = u16_be(raw, position + 2) as usize;
        if matches!(
            marker,
            0xC0 | 0xC1
                | 0xC2
                | 0xC3
                | 0xC5
                | 0xC6
                | 0xC7
                | 0xC9
                | 0xCA
                | 0xCB
                | 0xCD
                | 0xCE
                | 0xCF
        ) {
            if position + 9 > raw.len() {
                return None;
            }
            let height = u16_be(raw, position + 5);
            let width = u16_be(raw, position + 7);
            return Some((width, height));
        }
        position += 2 + length;
    }
    None
}

/// ``_image_base_name``: the file name of a Windows or POSIX path.
fn image_base_name(path: &str) -> &str {
    let path = py_strip(path);
    py_strip(path.rsplit(['\\', '/']).next().unwrap_or(path))
}

/// ``_image_file_reference``: the path the IMAGEDEF names, and whether the IR
/// carried the pixels.
fn image_file_reference(entity: &Entity, entity_id: &str) -> Result<(String, bool), WriteError> {
    if let Some(Value::Str(href)) = entity.extra_get("href") {
        let href = py_strip(href);
        if !href.is_empty() {
            return Ok((href.to_string(), false));
        }
    }
    let mut base = match entity.extra_get("name") {
        Some(Value::Str(name)) => image_base_name(name).to_string(),
        _ => String::new(),
    };
    let embedded =
        matches!(entity.extra_get("data"), Some(Value::Str(data)) if !py_strip(data).is_empty());
    if base.is_empty() {
        let entity_id = py_strip(entity_id);
        base = if entity_id.is_empty() {
            "image".to_string()
        } else {
            format!("image_{entity_id}")
        };
    }
    if embedded && !base.contains('.') {
        let mime_type = match entity.extra_get("mime_type") {
            None => String::new(),
            Some(value) => {
                py_strip(&super::common::py_str(value, "image mime_type")?).to_lowercase()
            }
        };
        base.push_str(match mime_type.as_str() {
            "image/png" => ".png",
            "image/bmp" | "image/x-ms-bmp" => ".bmp",
            "image/jpeg" => ".jpg",
            "image/gif" => ".gif",
            "image/tiff" => ".tif",
            "image/webp" => ".webp",
            _ => "",
        });
    }
    Ok((base, embedded))
}

/// ``_image_definition_name``: the key of an IMAGEDEF in ``ACAD_IMAGE_DICT``:
/// the stem of the file, made unique.
fn image_definition_name(file_path: &str, taken: &HashSet<String>) -> String {
    let base = image_base_name(file_path);
    // ``"." in base[1:]``: a dot that is not the first character
    let has_suffix = base.chars().skip(1).any(|character| character == '.');
    let stem = if has_suffix {
        base.rsplit_once('.').map_or(base, |(stem, _)| stem)
    } else {
        base
    };
    let replaced: String = stem
        .chars()
        .map(|character| {
            if matches!(
                character,
                '<' | '>' | '/' | '\\' | '"' | ':' | ';' | '?' | '*' | '|' | ',' | '=' | '`'
            ) {
                '_'
            } else {
                character
            }
        })
        .collect();
    let stem = match py_strip(&replaced) {
        "" => "IMAGE",
        stripped => stripped,
    };
    let mut candidate = stem.to_string();
    let mut counter = 2;
    while taken.contains(&casefold(&candidate)) {
        candidate = format!("{stem}_{counter}");
        counter += 1;
    }
    candidate
}

/// ``_render_image`` up to the pairs: the diagnostics, the definition and the
/// reactor of an image. ``None`` when the placement is skipped.
pub(crate) fn prepare(
    entity: &Entity,
    context: &mut Context<'_>,
) -> Result<Option<Prepared>, WriteError> {
    let id = super::common::entity_id(entity)?;
    let id: &str = &id;
    let label = format!("[IMAGE:{}]", if id.is_empty() { "?" } else { id });
    let diagnostic_id = (!id.is_empty()).then(|| id.to_string());
    let (Some(width), Some(height)) = (
        positive_finite(entity.extra_get("width")),
        positive_finite(entity.extra_get("height")),
    ) else {
        context.diagnose(
            "DXF_IMAGE_SKIPPED",
            "warning",
            format!("{label} has no positive width and height; the placement was skipped."),
            diagnostic_id,
            Some("skipped"),
        );
        return Ok(None);
    };
    let size = match image_pixel_size(entity)? {
        Some(size) => size,
        None => {
            // one pixel for a drawing unit keeps the drawn size
            let pixels = |extent: f64| {
                py_round_to_int(extent)
                    .map(|rounded| rounded.max(1))
                    .ok_or_else(|| WriteError::invalid("image extent is not finite"))
            };
            let size = (pixels(width)?, pixels(height)?);
            context.diagnose(
                "DXF_IMAGE_SIZE_ASSUMED",
                "warning",
                format!(
                    "{label} the size of the image in pixels is unknown; {}x{} pixels (one \
                     pixel per drawing unit) were assumed, which keeps the drawn size.",
                    size.0, size.1
                ),
                diagnostic_id.clone(),
                Some("approximated"),
            );
            size
        }
    };
    let (file_path, embedded) = image_file_reference(entity, id)?;
    if embedded {
        context.diagnose(
            "DXF_IMAGE_REFERENCED",
            "warning",
            format!(
                "{label} the embedded image was written as a reference to {}; a DXF never \
                 embeds the pixels, so the file has to be placed next to the DXF.",
                repr(&file_path)
            ),
            diagnostic_id,
            Some("approximated"),
        );
    }
    let pixel_size = (width / size.0 as f64, height / size.1 as f64);
    let definition_handle =
        context
            .images
            .definition(&file_path, size, pixel_size, &mut context.allocator);
    let reactor_handle = context.allocator.allocate();
    Ok(Some(Prepared {
        width,
        height,
        size,
        file_path,
        definition_handle,
        reactor_handle,
    }))
}

/// ``_snap``: a value that is nothing next to ``scale`` is zero.
fn snap(value: f64, scale: f64) -> f64 {
    if value.abs() < 1e-12 * scale {
        0.0
    } else {
        value
    }
}

/// ``_image_to_pairs`` behind the head of the record (its kind, handle, owner
/// and ``AcDbEntity`` marker).
pub(crate) fn append_record(
    output: &mut Vec<Pair>,
    entity: &Entity,
    prepared: &Prepared,
    common: &Common,
) -> Result<(), WriteError> {
    let insert = match entity.extra_get("insert").and_then(Value::as_list) {
        Some([x, y, ..]) => match (x.as_python_number(), y.as_python_number()) {
            (Some(x), Some(y)) => [x, y],
            _ => return Err(WriteError::invalid("image insert is not a point")),
        },
        _ => return Err(WriteError::invalid("image insert is not a point")),
    };
    // ``float(entity.get("rotation", 0.0) or 0.0)``
    let rotation = match entity.extra_get("rotation") {
        None => 0.0,
        Some(value) => match value.as_python_number() {
            Some(rotation) if rotation != 0.0 => rotation,
            Some(_) => 0.0,
            None if !value.truthy() => 0.0,
            None => return Err(WriteError::invalid("image rotation is not a number")),
        },
    };
    let angle = rotation.to_radians();
    let (cos_a, sin_a) = (angle.cos(), angle.sin());
    let pixel_width = prepared.width / prepared.size.0 as f64;
    let pixel_height = prepared.height / prepared.size.1 as f64;
    let u_vector = (
        snap(pixel_width * cos_a, pixel_width),
        snap(pixel_width * sin_a, pixel_width),
    );
    let v_vector = (
        snap(-pixel_height * sin_a, pixel_height),
        snap(pixel_height * cos_a, pixel_height),
    );
    let (brightness, contrast, fade) = image_display_values(entity);
    let (columns, rows) = (prepared.size.0 as f64, prepared.size.1 as f64);

    common.write(output);
    output.push((100, Val::S("AcDbRasterImage")));
    output.push((90, Val::S("0")));
    output.push((10, Val::owned(num(insert[0]))));
    output.push((20, Val::owned(num(insert[1]))));
    output.push((30, Val::S("0")));
    output.push((11, Val::owned(num(u_vector.0))));
    output.push((21, Val::owned(num(u_vector.1))));
    output.push((31, Val::S("0")));
    output.push((12, Val::owned(num(v_vector.0))));
    output.push((22, Val::owned(num(v_vector.1))));
    output.push((32, Val::S("0")));
    output.push((13, Val::owned(num(columns))));
    output.push((23, Val::owned(num(rows))));
    output.push((340, prepared.definition_handle.clone()));
    output.push((70, Val::S(IMAGE_DISPLAY_FLAGS)));
    output.push((280, Val::S("0")));
    output.push((281, Val::integer(brightness)));
    output.push((282, Val::integer(contrast)));
    output.push((283, Val::integer(fade)));
    output.push((360, prepared.reactor_handle.clone()));
    output.push((71, Val::S("1")));
    output.push((91, Val::S("2")));
    output.push((14, Val::S("-0.5")));
    output.push((24, Val::S("-0.5")));
    output.push((14, Val::owned(num(columns - 0.5))));
    output.push((24, Val::owned(num(rows - 0.5))));
    output.push((290, Val::S("0")));
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn definition_names_are_the_stem_made_unique() {
        let mut taken = HashSet::new();
        assert_eq!(
            image_definition_name("C:\\img\\site plan.png", &taken),
            "site plan"
        );
        assert_eq!(image_definition_name(".hidden", &taken), ".hidden");
        assert_eq!(image_definition_name("a:b.tar.gz", &taken), "a_b.tar");
        assert_eq!(image_definition_name(" ?.png ", &taken), "_");
        assert_eq!(image_definition_name("", &taken), "IMAGE");
        taken.insert(casefold("Logo"));
        taken.insert(casefold("LOGO_2"));
        assert_eq!(image_definition_name("dir/logo.bmp", &taken), "logo_3");
    }

    #[test]
    fn sizes_are_read_from_the_file_headers() {
        let mut png = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR".to_vec();
        png.extend_from_slice(&[0, 0, 0, 40, 0, 0, 0, 20]);
        assert_eq!(sniff_image_size(&png), Some((40, 20)));
        assert_eq!(sniff_image_size(&png[..20]), None);
        let mut bmp = b"BM".to_vec();
        bmp.extend_from_slice(&[0; 12]);
        bmp.extend_from_slice(&40_u32.to_le_bytes());
        bmp.extend_from_slice(&7_i32.to_le_bytes());
        bmp.extend_from_slice(&(-5_i32).to_le_bytes());
        assert_eq!(sniff_image_size(&bmp), Some((7, 5)));
        assert_eq!(sniff_image_size(b"GIF89a\x03\x00\x02\x00"), Some((3, 2)));
        let jpeg = b"\xff\xd8\xff\xe0\x00\x04ab\xff\xc0\x00\x0b\x08\x00\x09\x00\x10";
        assert_eq!(sniff_image_size(jpeg), Some((16, 9)));
        assert_eq!(sniff_image_size(b"plain"), None);
    }

    #[test]
    fn base64_prefix_is_decoded() {
        // python3 -c "import base64; print(base64.b64decode('QUJDRA==', validate=False), base64.b64decode('QQ==QUJD', validate=False))"
        assert_eq!(decode_image_prefix("QUJD\nRA=="), b"ABCD");
        // a tail that is no whole group is left out
        assert_eq!(decode_image_prefix("QUJDR"), b"ABC");
        // a character outside the alphabet is skipped; the group it leaves
        // incomplete is no base64
        assert_eq!(decode_image_prefix("QU-D"), b"");
        assert_eq!(decode_image_prefix("QU-DQQ=="), b"");
        // padding ends the data
        assert_eq!(decode_image_prefix("QQ==QUJD"), b"A");
        assert_eq!(decode_image_prefix("QUJD\u{56f3}\u{9762}AA"), b"");
    }
}
