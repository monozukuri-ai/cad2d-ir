// pyo3 0.22 expands `#[pymethods]` and `#[pyfunction]` into conversions clippy
// reports on every function that returns a `PyResult`.
#![allow(clippy::useless_conversion)]

//! Python bindings of the native IR core (experimental).
//!
//! ``cad2d_ir`` stays the reference implementation. What is here is checked
//! against it: a document converted to the typed IR and back is an equal
//! ``dict``, and the DXF text written here is the text ``ir_to_dxf`` writes.
//! A document the native writer has not learnt raises [`Unsupported`], and the
//! caller uses the Python writer.
//!
//! This is a library: an extension module calls [`register`] to get the
//! functions and classes. ``cad2d_ir_native._core`` is that and nothing else.
//! An application with native code of its own that works on the typed IR links
//! this crate into its extension module, registers the bindings there and
//! takes a [`NativeDocument`] as an argument of its own functions. The classes
//! of two extension modules are different types to Python, so such an
//! application uses the bindings of its own module throughout.

use std::collections::HashMap;
use std::sync::Arc;

use cad2d_ir_core::convert::{
    document_from_map, document_to_map, entity_from_map, entity_to_map, entity_typed_map,
};
use cad2d_ir_core::dxf::{self, Host, StyleFont, WriteError};
use cad2d_ir_core::{Block, Document, Entity, Interner, Map, Tables, Value};
use cad2d_ir_jww::{ConvertError, Converted};
use indexmap::IndexMap;
use pyo3::exceptions::{PyOSError, PyRuntimeError, PyTypeError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::{PyBool, PyBytes, PyDict, PyFloat, PyInt, PyList, PyString, PyTuple};

pyo3::create_exception!(
    cad2d_ir_native,
    Unsupported,
    pyo3::exceptions::PyException,
    "The document needs something the native code has not been taught."
);

/// The ``Unsupported`` error with ``reason`` as its message.
pub fn unsupported(reason: impl Into<String>) -> PyErr {
    Unsupported::new_err(reason.into())
}

fn value_from_py(object: &Bound<'_, PyAny>) -> PyResult<Value> {
    if object.is_none() {
        return Ok(Value::Null);
    }
    if let Ok(flag) = object.downcast::<PyBool>() {
        return Ok(Value::Bool(flag.is_true()));
    }
    if object.is_instance_of::<PyInt>() {
        return match object.extract::<i64>() {
            Ok(number) => Ok(Value::Int(number)),
            Err(_) => Err(Unsupported::new_err("an integer outside 64 bits")),
        };
    }
    if let Ok(number) = object.downcast::<PyFloat>() {
        return Ok(Value::Float(number.value()));
    }
    if let Ok(text) = object.downcast::<PyString>() {
        return Ok(Value::Str(text.to_str()?.to_owned()));
    }
    if let Ok(items) = object.downcast::<PyList>() {
        let mut values = Vec::with_capacity(items.len());
        for item in items.iter() {
            values.push(value_from_py(&item)?);
        }
        return Ok(Value::List(values));
    }
    if let Ok(items) = object.downcast::<PyTuple>() {
        let mut values = Vec::with_capacity(items.len());
        for item in items.iter() {
            values.push(value_from_py(&item)?);
        }
        return Ok(Value::List(values));
    }
    if let Ok(dict) = object.downcast::<PyDict>() {
        return Ok(Value::Map(map_from_py(dict)?));
    }
    Err(Unsupported::new_err(format!(
        "a value of type {}",
        object.get_type().name()?
    )))
}

fn map_from_py(dict: &Bound<'_, PyDict>) -> PyResult<Map> {
    let mut map = Map::with_capacity(dict.len());
    for (key, value) in dict.iter() {
        let Ok(key) = key.downcast::<PyString>() else {
            return Err(Unsupported::new_err("a dict key that is not a string"));
        };
        map.insert(key.to_str()?.to_owned(), value_from_py(&value)?);
    }
    Ok(map)
}

fn value_to_py(py: Python<'_>, value: &Value) -> PyResult<PyObject> {
    Ok(match value {
        Value::Null => py.None(),
        Value::Bool(flag) => flag.into_py(py),
        Value::Int(number) => number.into_py(py),
        Value::Float(number) => number.into_py(py),
        Value::Str(text) => text.into_py(py),
        Value::List(items) => {
            let list = PyList::empty_bound(py);
            for item in items {
                list.append(value_to_py(py, item)?)?;
            }
            list.into_py(py)
        }
        Value::Map(map) => map_to_py(py, map)?.into_py(py),
    })
}

fn map_to_py<'py>(py: Python<'py>, map: &Map) -> PyResult<Bound<'py, PyDict>> {
    let dict = PyDict::new_bound(py);
    for (key, value) in map {
        dict.set_item(key, value_to_py(py, value)?)?;
    }
    Ok(dict)
}

/// The entity ``dict`` of a typed entity, without the keys in ``without``. The
/// values of ``Entity::extra`` go to Python as they are, with no copy in the
/// dynamic form in between.
fn entity_to_py<'py>(
    py: Python<'py>,
    entity: &Entity,
    without: &[String],
) -> PyResult<Bound<'py, PyDict>> {
    let dict = map_to_py(py, &entity_typed_map(entity))?;
    if let Some(extra) = &entity.extra {
        for (key, value) in extra.iter() {
            if !without.contains(key) {
                dict.set_item(key, value_to_py(py, value)?)?;
            }
        }
    }
    for key in without {
        if dict.contains(key)? {
            dict.del_item(key)?;
        }
    }
    Ok(dict)
}

fn entities_to_py<'py>(py: Python<'py>, entities: &[Entity]) -> PyResult<Bound<'py, PyList>> {
    let list = PyList::empty_bound(py);
    for entity in entities {
        // one entity at a time: the dynamic form of all of them at once is as
        // large as the ``dict``s it is made into
        list.append(entity_to_py(py, entity, &[])?)?;
    }
    Ok(list)
}

/// The value of a base64 character, or 255 for a byte outside the alphabet.
const BASE64_VALUES: [u8; 256] = {
    let alphabet = b"ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";
    let mut table = [255_u8; 256];
    let mut index = 0;
    while index < 64 {
        table[alphabet[index] as usize] = index as u8;
        index += 1;
    }
    table
};

/// The bytes of base64 text as Python ``bytes``, decoded into the object
/// itself. ``None`` for text ``base64.b64decode(text, validate=True)`` raises
/// on: a character outside the alphabet, padding that is not at the end, a
/// length that is no multiple of four.
pub fn base64_to_pybytes<'py>(py: Python<'py>, text: &str) -> Option<Bound<'py, PyBytes>> {
    let text = text.as_bytes();
    if !text.len().is_multiple_of(4) {
        return None;
    }
    let padding = text
        .iter()
        .rev()
        .take(3)
        .take_while(|byte| **byte == b'=')
        .count();
    if padding > 2 {
        return None;
    }
    let length = text.len() / 4 * 3 - padding;
    let mut valid = true;
    let bytes = PyBytes::new_bound_with(py, length, |out| {
        // every group but the last is four characters for three bytes
        let whole = if padding == 0 {
            text.len()
        } else {
            text.len() - 4
        };
        let mut invalid = 0_u8;
        for (group, decoded) in text[..whole].chunks_exact(4).zip(out.chunks_exact_mut(3)) {
            let a = BASE64_VALUES[group[0] as usize];
            let b = BASE64_VALUES[group[1] as usize];
            let c = BASE64_VALUES[group[2] as usize];
            let d = BASE64_VALUES[group[3] as usize];
            // 255 has the two high bits no value of the alphabet has
            invalid |= a | b | c | d;
            decoded[0] = (a << 2) | (b >> 4);
            decoded[1] = (b << 4) | (c >> 2);
            decoded[2] = (c << 6) | d;
        }
        if invalid & 0xC0 != 0 {
            valid = false;
            return Ok(());
        }
        if padding > 0 {
            let group = &text[whole..];
            let mut bits = 0_u32;
            for byte in &group[..4 - padding] {
                let value = BASE64_VALUES[*byte as usize];
                if value == 255 {
                    valid = false;
                    return Ok(());
                }
                bits = (bits << 6) | value as u32;
            }
            bits <<= 6 * padding as u32;
            let tail = &mut out[whole / 4 * 3..];
            tail[0] = (bits >> 16) as u8;
            if padding == 1 {
                tail[1] = (bits >> 8) as u8;
            }
        }
        Ok(())
    })
    .ok()?;
    valid.then_some(bytes)
}

/// The document ``dict`` of a typed document.
fn document_to_py<'py>(py: Python<'py>, document: &Document) -> PyResult<Bound<'py, PyDict>> {
    // everything but the entity lists goes through the dynamic form
    let shell = Document {
        header: document.header.clone(),
        tables: document.tables.as_ref().map(|tables| Tables {
            layers: tables.layers.clone(),
            linetypes: tables.linetypes.clone(),
            text_styles: tables.text_styles.clone(),
            blocks: None,
            extra: tables.extra.clone(),
        }),
        entities: Vec::new(),
        has_entities: false,
        extra: document.extra.clone(),
    };
    let dict = map_to_py(py, &document_to_map(&shell))?;
    if let Some(blocks) = document
        .tables
        .as_ref()
        .and_then(|tables| tables.blocks.as_ref())
    {
        let blocks_dict = PyDict::new_bound(py);
        for (name, block) in blocks {
            let block_dict = PyDict::new_bound(py);
            if let Some(base_point) = block.base_point {
                block_dict.set_item("base_point", vec![base_point[0], base_point[1]])?;
            }
            if block.has_entities {
                block_dict.set_item("entities", entities_to_py(py, &block.entities)?)?;
            }
            for (key, value) in &block.extra {
                block_dict.set_item(key, value_to_py(py, value)?)?;
            }
            blocks_dict.set_item(name, block_dict)?;
        }
        let Some(tables) = dict.get_item("tables")? else {
            return Err(PyRuntimeError::new_err("a document with blocks has tables"));
        };
        tables.set_item("blocks", blocks_dict)?;
    }
    if document.has_entities {
        dict.set_item("entities", entities_to_py(py, &document.entities)?)?;
    }
    Ok(dict)
}

/// The lookups that still live in Python: the font table and the code pages.
struct PythonHost<'py> {
    style_font: Bound<'py, PyAny>,
    select_codepage: Bound<'py, PyAny>,
}

impl<'py> PythonHost<'py> {
    fn new(py: Python<'py>) -> PyResult<Self> {
        Ok(PythonHost {
            style_font: py.import_bound("cad2d_ir.fonts")?.getattr("style_font")?,
            select_codepage: py
                .import_bound("cad2d_ir.codecs.dxf")?
                .getattr("_select_r12_codepage")?,
        })
    }
}

fn host_error(error: PyErr) -> WriteError {
    WriteError::Invalid(error.to_string())
}

impl Host for PythonHost<'_> {
    fn style_font(
        &self,
        font: Option<&str>,
        family: Option<&str>,
    ) -> Result<StyleFont, WriteError> {
        let result = self.style_font.call1((font, family)).map_err(host_error)?;
        Ok(StyleFont {
            file: result
                .getattr("file")
                .and_then(|value| value.extract())
                .map_err(host_error)?,
            family: result
                .getattr("family")
                .and_then(|value| value.extract())
                .map_err(host_error)?,
            flags: result
                .getattr("flags")
                .and_then(|value| value.extract())
                .map_err(host_error)?,
        })
    }

    fn select_codepage(&self, characters: &[(char, u64)]) -> Result<String, WriteError> {
        let py = self.select_codepage.py();
        let counts = PyDict::new_bound(py);
        for (character, count) in characters {
            counts
                .set_item(character.to_string(), count)
                .map_err(host_error)?;
        }
        self.select_codepage
            .call1((counts,))
            .and_then(|value| value.extract())
            .map_err(host_error)
    }
}

fn write_error(error: WriteError) -> PyErr {
    match error {
        WriteError::Unsupported(reason) => Unsupported::new_err(reason),
        WriteError::Invalid(reason) => PyValueError::new_err(reason),
    }
}

fn document_from_py(document: &Bound<'_, PyAny>) -> PyResult<Document> {
    let Ok(dict) = document.downcast::<PyDict>() else {
        return Err(PyTypeError::new_err("an IR document is a dict"));
    };
    Ok(document_from_map(map_from_py(dict)?))
}

/// An export diagnostic: ``(code, severity, message, entity_id, action)``.
type DiagnosticTuple = (
    &'static str,
    &'static str,
    String,
    Option<String>,
    Option<&'static str>,
);

/// The document as the typed IR and back (for checking the conversion).
#[pyfunction]
fn document_roundtrip<'py>(
    py: Python<'py>,
    document: &Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyDict>> {
    let typed = document_from_py(document)?;
    document_to_py(py, &typed)
}

/// ``ir_to_dxf(document, target_version="AC1024")`` in native code.
///
/// Returns ``(dxf_text, diagnostics)`` where a diagnostic is
/// ``(code, severity, message, entity_id, action)``. Raises ``Unsupported``
/// for a document the native writer has not learnt.
#[pyfunction]
fn ir_to_dxf<'py>(
    py: Python<'py>,
    document: &Bound<'py, PyAny>,
) -> PyResult<(String, Vec<DiagnosticTuple>)> {
    let typed = document_from_py(document)?;
    let host = PythonHost::new(py)?;
    let output = dxf::write_r2010(&typed, &host).map_err(write_error)?;
    let diagnostics = output
        .diagnostics
        .into_iter()
        .map(|d| (d.code, d.severity, d.message, d.entity_id, d.action))
        .collect();
    Ok((output.text, diagnostics))
}

/// An entity with where it is: the name of its block (``None`` for model
/// space), its place in the entity list and the entity as a ``dict``.
type EntityAt<'py> = (Option<String>, usize, Bound<'py, PyDict>);

/// A document held as the typed IR. Its entities never become Python objects
/// unless ``to_dict`` is asked for.
#[pyclass(module = "cad2d_ir_native")]
pub struct NativeDocument {
    document: Document,
}

impl NativeDocument {
    /// The typed document, for the native code of an application.
    pub fn document(&self) -> &Document {
        &self.document
    }
}

#[pymethods]
impl NativeDocument {
    /// The typed IR of a document ``dict``.
    #[staticmethod]
    fn from_dict(document: &Bound<'_, PyAny>) -> PyResult<Self> {
        Ok(NativeDocument {
            document: document_from_py(document)?,
        })
    }

    /// The document as the ``dict`` the Python importers return.
    fn to_dict<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyDict>> {
        document_to_py(py, &self.document)
    }

    /// DXF R2010 text and the export diagnostics, as ``ir_to_dxf`` returns them.
    fn to_dxf(&self, py: Python<'_>) -> PyResult<(String, Vec<DiagnosticTuple>)> {
        let host = PythonHost::new(py)?;
        let output = dxf::write_r2010(&self.document, &host).map_err(write_error)?;
        let diagnostics = output
            .diagnostics
            .into_iter()
            .map(|d| (d.code, d.severity, d.message, d.entity_id, d.action))
            .collect();
        Ok((output.text, diagnostics))
    }

    /// ``to_dxf`` with the text as UTF-8 bytes (what a DXF R2010 file holds),
    /// without a Python ``str`` of the whole file in between.
    fn to_dxf_bytes<'py>(
        &self,
        py: Python<'py>,
    ) -> PyResult<(Bound<'py, pyo3::types::PyBytes>, Vec<DiagnosticTuple>)> {
        let host = PythonHost::new(py)?;
        let output = dxf::write_r2010(&self.document, &host).map_err(write_error)?;
        let diagnostics = output
            .diagnostics
            .into_iter()
            .map(|d| (d.code, d.severity, d.message, d.entity_id, d.action))
            .collect();
        Ok((
            pyo3::types::PyBytes::new_bound(py, output.text.as_bytes()),
            diagnostics,
        ))
    }

    /// The number of model space entities.
    fn entity_count(&self) -> usize {
        self.document.entities.len()
    }

    /// The entities of ``kind`` as ``(block, index, entity)``: the name of the
    /// block they are in (``None`` for model space), their place in its entity
    /// list and the entity as a ``dict`` (a copy). For the few entities a
    /// caller has to look at in Python, images for one.
    ///
    /// ``without`` names keys to leave out of the ``dict``s: the ``data`` of an
    /// image is as large as its file, and ``entity_bytes`` gives its bytes.
    #[pyo3(signature = (kind, *, without = Vec::new()))]
    fn entities_of_kind<'py>(
        &self,
        py: Python<'py>,
        kind: &str,
        without: Vec<String>,
    ) -> PyResult<Vec<EntityAt<'py>>> {
        let mut found = Vec::new();
        for (index, entity) in self.document.entities.iter().enumerate() {
            if entity.kind() == Some(kind) {
                found.push((None, index, entity_to_py(py, entity, &without)?));
            }
        }
        let blocks = self
            .document
            .tables
            .as_ref()
            .and_then(|tables| tables.blocks.as_ref());
        for (name, block) in blocks.into_iter().flatten() {
            for (index, entity) in block.entities.iter().enumerate() {
                if entity.kind() == Some(kind) {
                    found.push((
                        Some(name.clone()),
                        index,
                        entity_to_py(py, entity, &without)?,
                    ));
                }
            }
        }
        Ok(found)
    }

    /// The bytes of the base64 text under ``key`` of the entity at ``index`` of
    /// model space (``block`` is ``None``) or of a block: ``base64.b64decode(
    /// entity[key], validate=True)`` without the text becoming a Python ``str``.
    /// ``None`` when the entity has no string there; ``ValueError`` for text
    /// that is no base64.
    #[pyo3(signature = (block, index, key))]
    fn entity_bytes<'py>(
        &self,
        py: Python<'py>,
        block: Option<&str>,
        index: usize,
        key: &str,
    ) -> PyResult<Option<Bound<'py, PyBytes>>> {
        let entities = match block {
            None => &self.document.entities,
            Some(name) => {
                let found = self
                    .document
                    .tables
                    .as_ref()
                    .and_then(|tables| tables.blocks.as_ref())
                    .and_then(|blocks| blocks.get(name));
                match found {
                    Some(block) => &block.entities,
                    None => return Err(PyValueError::new_err(format!("no block {name:?}"))),
                }
            }
        };
        let Some(entity) = entities.get(index) else {
            return Err(PyValueError::new_err("entity index out of range"));
        };
        match entity.extra_get(key) {
            Some(Value::Str(text)) => match base64_to_pybytes(py, text) {
                Some(bytes) => Ok(Some(bytes)),
                None => Err(PyValueError::new_err("the text is no base64")),
            },
            _ => Ok(None),
        }
    }

    /// Set ``key`` of the entity at ``index`` of model space (``block`` is
    /// ``None``) or of a block, as ``entity[key] = value`` does on the ``dict``.
    #[pyo3(signature = (block, index, key, value))]
    fn set_entity_value(
        &mut self,
        block: Option<&str>,
        index: usize,
        key: &str,
        value: &Bound<'_, PyAny>,
    ) -> PyResult<()> {
        let value = value_from_py(value)?;
        let entities = match block {
            None => &mut self.document.entities,
            Some(name) => {
                let found = self
                    .document
                    .tables
                    .as_mut()
                    .and_then(|tables| tables.blocks.as_mut())
                    .and_then(|blocks| blocks.get_mut(name));
                match found {
                    Some(block) => &mut block.entities,
                    None => return Err(PyValueError::new_err(format!("no block {name:?}"))),
                }
            }
        };
        let Some(entity) = entities.get_mut(index) else {
            return Err(PyValueError::new_err("entity index out of range"));
        };
        // An entity without a struct keeps every key but the common ones as it
        // comes: the value goes in directly. (The other way is through the
        // ``dict`` form, which copies every value, the text of an image too.)
        let common = ["id", "kind", "layer", "linetype", "color", "lineweight_mm"];
        if matches!(entity.shape, cad2d_ir_core::Shape::Other) && !common.contains(&key) {
            entity.extra_insert(key, value);
            return Ok(());
        }
        let mut map = entity_to_map(entity);
        map.insert(key.to_string(), value);
        *entity = entity_from_map(map, &mut Interner::new());
        Ok(())
    }

    /// How many entities (of model space and of the blocks) hold a number that
    /// is not finite or larger than ``limit``: what ``drop_absurd_geometry`` of
    /// a caller would remove.
    fn absurd_entities(&self, limit: f64) -> usize {
        let blocks = self
            .document
            .tables
            .as_ref()
            .and_then(|tables| tables.blocks.as_ref());
        let block_entities = blocks
            .into_iter()
            .flat_map(|blocks| blocks.values())
            .flat_map(|block| block.entities.iter());
        self.document
            .entities
            .iter()
            .chain(block_entities)
            .filter(|entity| entity.has_absurd_number(limit))
            .count()
    }
}

/// A JWW file read by ``ezjww-core``, before its entities are converted.
#[pyclass(module = "cad2d_ir_native")]
struct ParsedJww {
    parsed: Option<ezjww_core::ParsedJwwDocument>,
}

impl ParsedJww {
    fn get(&self) -> PyResult<&ezjww_core::ParsedJwwDocument> {
        self.parsed
            .as_ref()
            .ok_or_else(|| PyRuntimeError::new_err("the parsed document was converted already"))
    }
}

fn base_to_py<'py>(py: Python<'py>, base: &ezjww_core::EntityBase) -> PyResult<Bound<'py, PyDict>> {
    let dict = PyDict::new_bound(py);
    dict.set_item("group", base.group)?;
    dict.set_item("pen_style", base.pen_style)?;
    dict.set_item("pen_color", base.pen_color)?;
    dict.set_item("pen_width", base.pen_width)?;
    dict.set_item("layer", base.layer)?;
    dict.set_item("layer_group", base.layer_group)?;
    dict.set_item("flag", base.flag)?;
    Ok(dict)
}

#[pymethods]
impl ParsedJww {
    /// The block definitions without their entities:
    /// ``{"number", "is_referenced", "name", "base"}`` as ``ezjww.read_document`` has them.
    fn block_defs<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyList>> {
        let list = PyList::empty_bound(py);
        for block_def in &self.get()?.document.block_defs {
            let dict = PyDict::new_bound(py);
            dict.set_item("number", block_def.number)?;
            dict.set_item("is_referenced", block_def.is_referenced)?;
            dict.set_item("name", &block_def.name)?;
            dict.set_item("base", base_to_py(py, &block_def.base)?)?;
            list.append(dict)?;
        }
        Ok(list)
    }

    /// ``read_document()["entity_counts"]``.
    fn entity_counts<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyDict>> {
        let dict = PyDict::new_bound(py);
        for (kind, count) in ezjww_core::entity_counts(&self.get()?.document.entities) {
            dict.set_item(kind, count)?;
        }
        Ok(dict)
    }

    /// ``read_document()["validation"]["unresolved_def_numbers"]``.
    fn unresolved_def_numbers(&self) -> PyResult<Vec<u32>> {
        Ok(ezjww_core::validate_block_references(&self.get()?.document).unresolved_def_numbers)
    }

    /// How many diagnostics the parser left (a truncated list, replaced characters).
    fn parser_diagnostics(&self) -> PyResult<usize> {
        Ok(self.get()?.diagnostics.len())
    }

    /// ``read_document()["diagnostics"]``: what the parser left, as the dicts
    /// ``ezjww`` gives (``code``, ``severity``, ``message``, ``action``, ``details``).
    fn diagnostics<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyList>> {
        use ezjww_core::DiagnosticDetails;
        let list = PyList::empty_bound(py);
        for diagnostic in &self.get()?.diagnostics {
            let row = PyDict::new_bound(py);
            row.set_item("code", &diagnostic.code)?;
            row.set_item("severity", &diagnostic.severity)?;
            row.set_item("message", &diagnostic.message)?;
            row.set_item("action", &diagnostic.action)?;
            let details = PyDict::new_bound(py);
            match &diagnostic.details {
                DiagnosticDetails::Decode(decode) => {
                    details.set_item("encoding", &decode.encoding)?;
                    details.set_item("field", &decode.field)?;
                    details.set_item("byte_offset", decode.byte_offset)?;
                    details.set_item("byte_length", decode.byte_length)?;
                    details.set_item("replacement_characters", decode.replacement_characters)?;
                    details.set_item("had_errors", decode.had_errors)?;
                }
                DiagnosticDetails::Encoding(encoding) => {
                    details.set_item("encoding", &encoding.encoding)?;
                    details.set_item("non_ascii_strings", encoding.non_ascii_strings)?;
                    details.set_item("utf8_sequences", encoding.utf8_sequences)?;
                    details.set_item("cp932_anomalies", encoding.cp932_anomalies)?;
                }
                DiagnosticDetails::Truncation(truncation) => {
                    details.set_item("byte_offset", truncation.byte_offset)?;
                    details.set_item("expected_entities", truncation.expected_entities)?;
                    details.set_item("parsed_entities", truncation.parsed_entities)?;
                    details.set_item("error", &truncation.error)?;
                }
                DiagnosticDetails::Unverified(unverified) => {
                    details.set_item("field", &unverified.field)?;
                    details.set_item("byte_offset", unverified.byte_offset)?;
                    details.set_item("count", unverified.count)?;
                    details.set_item("values", &unverified.values)?;
                }
                DiagnosticDetails::ImageList(images) => {
                    details.set_item("byte_offset", images.byte_offset)?;
                    details.set_item("expected_images", images.expected_images)?;
                    details.set_item("parsed_images", images.parsed_images)?;
                    details.set_item("error", &images.error)?;
                }
            }
            row.set_item("details", details)?;
            list.append(row)?;
        }
        Ok(list)
    }

    /// How many images the file carries.
    fn images(&self) -> PyResult<usize> {
        Ok(self.get()?.document.images.len())
    }

    fn entity_count(&self) -> PyResult<usize> {
        Ok(self.get()?.document.entities.len())
    }

    /// Convert the entities with the lookup tables the importer made from the header.
    #[pyo3(signature = (*, label, source_format, entity_provenance, strict, curve_segments, layer_names, block_names, linetype_names, pen_widths, pen_colors))]
    #[allow(clippy::too_many_arguments)]
    fn convert(
        &mut self,
        py: Python<'_>,
        label: String,
        source_format: String,
        entity_provenance: bool,
        strict: bool,
        curve_segments: i64,
        layer_names: HashMap<(i64, i64), String>,
        block_names: HashMap<i64, String>,
        linetype_names: HashMap<i64, String>,
        pen_widths: HashMap<i64, f64>,
        pen_colors: HashMap<i64, i64>,
    ) -> PyResult<ConvertedJww> {
        let shared = |names: HashMap<i64, String>| -> HashMap<i64, Arc<str>> {
            names
                .into_iter()
                .map(|(key, name)| (key, Arc::from(name.as_str())))
                .collect()
        };
        let context = cad2d_ir_jww::Context {
            label,
            source_format,
            entity_provenance,
            strict,
            curve_segments,
            layer_names: layer_names
                .into_iter()
                .map(|(key, name)| (key, Arc::from(name.as_str())))
                .collect(),
            block_names: shared(block_names),
            linetype_names: shared(linetype_names),
            pen_widths,
            pen_colors,
        };
        let parsed = self.get()?;
        let converted = py.allow_threads(|| cad2d_ir_jww::convert(&parsed.document, &context));
        match converted {
            Ok(converted) => {
                // the raw entities are not needed any more
                self.parsed = None;
                Ok(ConvertedJww {
                    converted: Some(converted),
                })
            }
            Err(ConvertError::Unsupported(reason)) => Err(Unsupported::new_err(reason)),
            Err(ConvertError::Failed(reason)) => Err(PyValueError::new_err(reason)),
        }
    }
}

/// Read a JWW file with ``ezjww-core``.
#[pyfunction]
fn parse_jww(py: Python<'_>, path: String) -> PyResult<ParsedJww> {
    let parsed = py
        .allow_threads(|| ezjww_core::read_document_from_file_with_diagnostics(&path))
        .map_err(|error| match error {
            ezjww_core::JwwError::Io(error) => PyOSError::new_err(error.to_string()),
            other => PyValueError::new_err(other.to_string()),
        })?;
    Ok(ParsedJww {
        parsed: Some(parsed),
    })
}

/// The entities of a JWW file as the typed IR, with what the importer reports about them.
#[pyclass(module = "cad2d_ir_native")]
struct ConvertedJww {
    converted: Option<Converted>,
}

impl ConvertedJww {
    fn get(&self) -> PyResult<&Converted> {
        self.converted.as_ref().ok_or_else(|| {
            PyRuntimeError::new_err("the entities were moved into a document already")
        })
    }
}

fn counts_to_py<'py>(
    py: Python<'py>,
    counts: &std::collections::BTreeMap<String, u64>,
) -> PyResult<Bound<'py, PyDict>> {
    let dict = PyDict::new_bound(py);
    for (kind, count) in counts {
        dict.set_item(kind, count)?;
    }
    Ok(dict)
}

#[pymethods]
impl ConvertedJww {
    /// ``(code, severity, message, source_id, source_kind, action)`` in the order they arose.
    #[allow(clippy::type_complexity)]
    fn diagnostics(
        &self,
    ) -> PyResult<
        Vec<(
            &'static str,
            &'static str,
            String,
            Option<String>,
            Option<String>,
            Option<&'static str>,
        )>,
    > {
        Ok(self
            .get()?
            .diagnostics
            .iter()
            .map(|d| {
                (
                    d.code,
                    d.severity,
                    d.message.clone(),
                    d.source_id.clone(),
                    d.source_kind.clone(),
                    d.action,
                )
            })
            .collect())
    }

    fn converted_counts<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyDict>> {
        counts_to_py(py, &self.get()?.converted_counts)
    }

    fn skipped_counts<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyDict>> {
        counts_to_py(py, &self.get()?.skipped_counts)
    }

    fn approximation_counts<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyDict>> {
        counts_to_py(py, &self.get()?.approximation_counts)
    }

    fn preserved_dimensions(&self) -> PyResult<u64> {
        Ok(self.get()?.preserved_dimensions)
    }

    fn pen_width_entities(&self) -> PyResult<u64> {
        Ok(self.get()?.pen_width_entities)
    }

    fn used_linetypes(&self) -> PyResult<Vec<String>> {
        Ok(self.get()?.used_linetypes.iter().cloned().collect())
    }

    /// The font names of the texts (sorted), ``STANDARD`` among them.
    fn fonts(&self) -> PyResult<Vec<String>> {
        Ok(self.get()?.fonts.iter().cloned().collect())
    }

    /// ``(entity_index, key, value, raw)`` of the Jw_cad setting records.
    fn metadata_settings(&self) -> PyResult<Vec<(usize, String, String, String)>> {
        Ok(self
            .get()?
            .metadata_settings
            .iter()
            .map(|s| {
                (
                    s.entity_index,
                    s.key.clone(),
                    s.value.clone(),
                    s.raw.clone(),
                )
            })
            .collect())
    }

    fn source_entities(&self) -> PyResult<usize> {
        Ok(self.get()?.source_entities)
    }

    /// Image placements whose file the drawing carries.
    fn embedded_images(&self) -> PyResult<u64> {
        Ok(self.get()?.embedded_images)
    }

    /// Image placements that only name a path.
    fn linked_images(&self) -> PyResult<u64> {
        Ok(self.get()?.linked_images)
    }

    fn entity_count(&self) -> PyResult<usize> {
        Ok(self.get()?.entities.len())
    }

    /// The number of entities of each block definition.
    fn block_entity_counts(&self) -> PyResult<Vec<usize>> {
        Ok(self.get()?.blocks.iter().map(Vec::len).collect())
    }

    /// The document of ``shell`` (a document ``dict`` without entities) with the
    /// converted entities, and ``blocks`` (a name and the rest of the block
    /// ``dict`` for each block definition) with theirs.
    fn take_document(
        &mut self,
        shell: &Bound<'_, PyAny>,
        blocks: Vec<(String, Bound<'_, PyDict>)>,
    ) -> PyResult<NativeDocument> {
        let mut document = document_from_py(shell)?;
        let converted = self.converted.take().ok_or_else(|| {
            PyRuntimeError::new_err("the entities were moved into a document already")
        })?;
        if blocks.len() != converted.blocks.len() {
            self.converted = Some(converted);
            return Err(PyValueError::new_err(
                "one block shell for each block definition",
            ));
        }
        document.entities = converted.entities;
        document.has_entities = true;
        if !blocks.is_empty() {
            let mut table: IndexMap<String, Block> = IndexMap::with_capacity(blocks.len());
            for ((name, shell), entities) in blocks.into_iter().zip(converted.blocks) {
                let mut extra = map_from_py(&shell)?;
                let base_point = match extra.shift_remove("base_point") {
                    Some(Value::List(items)) if items.len() == 2 => {
                        match (items[0].as_number(), items[1].as_number()) {
                            (Some(x), Some(y)) => Some([x, y]),
                            _ => return Err(PyValueError::new_err("base_point is a 2D point")),
                        }
                    }
                    None => None,
                    Some(_) => return Err(PyValueError::new_err("base_point is a 2D point")),
                };
                table.insert(
                    name,
                    Block {
                        base_point,
                        entities,
                        has_entities: true,
                        extra,
                    },
                );
            }
            document.tables.get_or_insert_with(Default::default).blocks = Some(table);
        }
        Ok(NativeDocument { document })
    }
}

/// Add the functions, the classes and ``Unsupported`` to an extension module.
pub fn register(module: &Bound<'_, PyModule>) -> PyResult<()> {
    module.add("Unsupported", module.py().get_type_bound::<Unsupported>())?;
    module.add("__version__", env!("CARGO_PKG_VERSION"))?;
    module.add_function(wrap_pyfunction!(document_roundtrip, module)?)?;
    module.add_function(wrap_pyfunction!(ir_to_dxf, module)?)?;
    module.add_function(wrap_pyfunction!(parse_jww, module)?)?;
    module.add_class::<NativeDocument>()?;
    module.add_class::<ParsedJww>()?;
    module.add_class::<ConvertedJww>()?;
    Ok(())
}
