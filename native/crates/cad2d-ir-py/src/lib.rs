// pyo3 0.22 expands `#[pymethods]` and `#[pyfunction]` into conversions clippy
// reports on every function that returns a `PyResult`.
#![allow(clippy::useless_conversion)]

//! Python bindings of the native IR core.
//!
//! The typed IR (a document converted from a ``dict`` and back is an equal
//! ``dict``), the DXF writer behind ``cad2d_ir.codecs.dxf.ir_to_dxf`` and the
//! conversion of the entities of a JWW file.
//!
//! This is a library: an extension module calls [`register`] to get the
//! functions and classes. ``cad2d_ir_native._core`` is that and nothing else.
//! An application with native code of its own that works on the typed IR links
//! this crate into its extension module, registers the bindings there and
//! takes a [`NativeDocument`] as an argument of its own functions. The classes
//! of two extension modules are different types to Python, so such an
//! application uses the bindings of its own module throughout.

mod jww_dict;

use std::collections::HashMap;
use std::sync::Arc;

use cad2d_ir_core::convert::{
    document_from_map, document_to_map, entity_from_map, entity_to_map, entity_typed_map,
};
use cad2d_ir_core::dxf::{
    self, DxfOutput, GenericDimensions, Host, Options, R12TextPlan, StyleFont, Table, Target,
    ViewportView, WriteError,
};
use cad2d_ir_core::{Block, Document, Entity, Interner, Map, Tables, Value};
use cad2d_ir_jww::{ConvertError, Converted, Sources};
use indexmap::IndexMap;
use pyo3::exceptions::{PyOSError, PyRuntimeError, PyTypeError, PyValueError};
use pyo3::prelude::*;
use pyo3::types::{PyBool, PyBytes, PyDict, PyFloat, PyInt, PyList, PyString, PyTuple};

pyo3::create_exception!(
    cad2d_ir_native,
    Unsupported,
    pyo3::exceptions::PyException,
    "Native code of an application has not been taught what the document needs."
);

/// The ``Unsupported`` error with ``reason`` as its message.
///
/// Nothing in these bindings raises it: the DXF writer and the JWW conversion
/// handle every document. It is the exception of an application that works on
/// the typed IR with native code of its own and declines a document (a
/// renderer that draws some entity kinds only), so that its Python side can
/// tell that from a failure.
pub fn unsupported(reason: impl Into<String>) -> PyErr {
    Unsupported::new_err(reason.into())
}

/// The text of a Python ``str``. A ``str`` can hold half of a surrogate pair
/// (text read from UTF-16 a code unit at a time), which no UTF-8 text can:
/// two halves that belong together become their character, and a half on its
/// own becomes U+FFFD.
pub(crate) fn text_from_py(text: &Bound<'_, PyString>) -> PyResult<String> {
    if let Ok(text) = text.to_str() {
        return Ok(text.to_owned());
    }
    text.call_method1("encode", ("utf-16-le", "surrogatepass"))?
        .call_method1("decode", ("utf-16-le", "replace"))?
        .extract()
}

/// The dynamic value of a Python object of the kinds JSON has.
///
/// ``lenient`` is for a document that is only written: a value of another kind
/// (which nothing of the writer reads) becomes ``None``, and a key that is no
/// string its ``str``. Otherwise such a document is an error, since it could
/// not be given back as it came.
fn value_of(object: &Bound<'_, PyAny>, lenient: bool) -> PyResult<Value> {
    if object.is_none() {
        return Ok(Value::Null);
    }
    if let Ok(flag) = object.downcast::<PyBool>() {
        return Ok(Value::Bool(flag.is_true()));
    }
    if object.is_instance_of::<PyInt>() {
        return match object.extract::<i64>() {
            Ok(number) => Ok(Value::Int(number)),
            Err(_) => Ok(Value::BigInt(
                object.str()?.to_str()?.to_owned().into_boxed_str(),
            )),
        };
    }
    if let Ok(number) = object.downcast::<PyFloat>() {
        return Ok(Value::Float(number.value()));
    }
    if let Ok(text) = object.downcast::<PyString>() {
        return Ok(Value::Str(text_from_py(text)?));
    }
    if let Ok(items) = object.downcast::<PyList>() {
        let mut values = Vec::with_capacity(items.len());
        for item in items.iter() {
            values.push(value_of(&item, lenient)?);
        }
        return Ok(Value::List(values));
    }
    if let Ok(items) = object.downcast::<PyTuple>() {
        let mut values = Vec::with_capacity(items.len());
        for item in items.iter() {
            values.push(value_of(&item, lenient)?);
        }
        return Ok(Value::List(values));
    }
    if let Ok(dict) = object.downcast::<PyDict>() {
        return Ok(Value::Map(map_of(dict, lenient)?));
    }
    if lenient {
        return Ok(Value::Null);
    }
    Err(PyTypeError::new_err(format!(
        "an IR document holds a value of type {}, which is none of the types JSON has",
        object.get_type().name()?
    )))
}

fn map_of(dict: &Bound<'_, PyDict>, lenient: bool) -> PyResult<Map> {
    let mut map = Map::with_capacity(dict.len());
    for (key, value) in dict.iter() {
        let key = match key.downcast::<PyString>() {
            Ok(key) => text_from_py(key)?,
            Err(_) if lenient => text_from_py(&key.str()?)?,
            Err(_) => {
                return Err(PyTypeError::new_err(
                    "an IR document holds a dict key that is not a string",
                ))
            }
        };
        map.insert(key, value_of(&value, lenient)?);
    }
    Ok(map)
}

fn value_from_py(object: &Bound<'_, PyAny>) -> PyResult<Value> {
    value_of(object, false)
}

fn map_from_py(dict: &Bound<'_, PyDict>) -> PyResult<Map> {
    map_of(dict, false)
}

fn value_to_py(py: Python<'_>, value: &Value) -> PyResult<PyObject> {
    Ok(match value {
        Value::Null => py.None(),
        Value::Bool(flag) => flag.into_py(py),
        Value::Int(number) => number.into_py(py),
        Value::BigInt(digits) => py
            .get_type_bound::<PyInt>()
            .call1((digits.as_ref(),))?
            .unbind(),
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
        let (quads, _) = text[..whole].as_chunks::<4>();
        let (triples, _) = out.as_chunks_mut::<3>();
        for (group, decoded) in quads.iter().zip(triples.iter_mut()) {
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

/// What the writer asks the Python package for: the font table, the code
/// pages, the inline codes of MTEXT and the view of a viewport.
struct PythonHost<'py> {
    py: Python<'py>,
    style_font: Bound<'py, PyAny>,
    select_codepage: Bound<'py, PyAny>,
    r12_text_plan: Bound<'py, PyAny>,
    mtext_text: Bound<'py, PyAny>,
    explode_mtext: Bound<'py, PyAny>,
    viewport_view: Bound<'py, PyAny>,
}

impl<'py> PythonHost<'py> {
    fn new(py: Python<'py>) -> PyResult<Self> {
        let codec = py.import_bound("cad2d_ir.codecs.dxf")?;
        Ok(PythonHost {
            py,
            style_font: py.import_bound("cad2d_ir.fonts")?.getattr("style_font")?,
            select_codepage: codec.getattr("_select_r12_codepage")?,
            r12_text_plan: codec.getattr("_r12_text_plan")?,
            mtext_text: codec.getattr("_mtext_dxf_text")?,
            explode_mtext: codec.getattr("_explode_mtext")?,
            viewport_view: py
                .import_bound("cad2d_ir.layouts")?
                .getattr("viewport_to_dxf_view")?,
        })
    }

    /// ``{character: count}``.
    fn counts(&self, characters: &[(char, u64)]) -> PyResult<Bound<'py, PyDict>> {
        let counts = PyDict::new_bound(self.py);
        for (character, count) in characters {
            counts.set_item(character.to_string(), count)?;
        }
        Ok(counts)
    }
}

fn host_error(error: PyErr) -> WriteError {
    WriteError::Invalid(error.to_string())
}

/// ``{character: text}`` of Python as pairs.
fn character_table(table: &Bound<'_, PyAny>) -> PyResult<Vec<(char, String)>> {
    let table = table.downcast::<PyDict>()?;
    let mut pairs = Vec::with_capacity(table.len());
    for (key, value) in table.iter() {
        let key: String = key.extract()?;
        let mut characters = key.chars();
        match (characters.next(), characters.next()) {
            (Some(character), None) => pairs.push((character, value.extract()?)),
            _ => return Err(PyValueError::new_err("a key that is not one character")),
        }
    }
    Ok(pairs)
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
        self.counts(characters)
            .and_then(|counts| self.select_codepage.call1((counts,)))
            .and_then(|value| value.extract())
            .map_err(host_error)
    }

    fn r12_text_plan(
        &self,
        text_characters: &[(char, u64)],
        other_characters: &[(char, u64)],
    ) -> Result<R12TextPlan, WriteError> {
        let plan = || -> PyResult<R12TextPlan> {
            let result = self.r12_text_plan.call1((
                self.counts(text_characters)?,
                self.counts(other_characters)?,
            ))?;
            let (codepage, coded, lost, replaced, similar): (
                String,
                Bound<'_, PyAny>,
                Bound<'_, PyAny>,
                u64,
                u64,
            ) = result.extract()?;
            Ok(R12TextPlan {
                codepage,
                coded: character_table(&coded)?,
                lost: character_table(&lost)?,
                replaced,
                similar,
            })
        };
        plan().map_err(host_error)
    }

    fn mtext_text(&self, entity: &Entity) -> Result<String, WriteError> {
        entity_to_py(self.py, entity, &[])
            .and_then(|entity| self.mtext_text.call1((entity,)))
            .and_then(|value| value.extract())
            .map_err(host_error)
    }

    fn explode_mtext(
        &self,
        entity: &Entity,
        text_styles: &Table,
    ) -> Result<(Vec<Entity>, bool), WriteError> {
        let explode = || -> PyResult<(Vec<Entity>, bool)> {
            let styles = PyDict::new_bound(self.py);
            for (name, definition) in text_styles {
                styles.set_item(name, map_to_py(self.py, definition)?)?;
            }
            let result = self
                .explode_mtext
                .call1((entity_to_py(self.py, entity, &[])?, styles))?;
            let (texts, formatting_changed): (Bound<'_, PyList>, bool) = result.extract()?;
            let mut interner = Interner::new();
            let mut entities = Vec::with_capacity(texts.len());
            for text in texts.iter() {
                let text = text.downcast::<PyDict>()?;
                entities.push(entity_from_map(map_from_py(text)?, &mut interner));
            }
            Ok((entities, formatting_changed))
        };
        explode().map_err(host_error)
    }

    fn viewport_view(&self, viewport: &Map) -> Result<Option<ViewportView>, WriteError> {
        let view = || -> PyResult<Option<ViewportView>> {
            let result = self.viewport_view.call1((map_to_py(self.py, viewport)?,))?;
            if result.is_none() {
                return Ok(None);
            }
            let center: Vec<f64> = result.get_item("view_center")?.extract()?;
            if center.len() < 2 {
                return Err(PyValueError::new_err("view_center is not a point"));
            }
            Ok(Some(ViewportView {
                center: [center[0], center[1]],
                height: result.get_item("view_height")?.extract()?,
                twist: result.get_item("twist_deg")?.extract()?,
            }))
        };
        view().map_err(host_error)
    }
}

fn write_error(error: WriteError) -> PyErr {
    match error {
        WriteError::Invalid(reason) => PyValueError::new_err(reason),
    }
}

/// The options of ``ir_to_dxf`` as the writer takes them.
fn dxf_options(
    target_version: &str,
    curve_segments: i64,
    generic_dimensions: &str,
    entity_map: bool,
) -> PyResult<Options> {
    let target = match target_version {
        "AC1009" => Target::R12,
        "AC1024" => Target::R2010,
        _ => {
            return Err(PyValueError::new_err(
                "target_version must be one of: AC1009, AC1024",
            ))
        }
    };
    if !(8..=4096).contains(&curve_segments) {
        return Err(PyValueError::new_err("curve_segments must be in [8, 4096]"));
    }
    let generic_dimensions = match generic_dimensions {
        "explode" => GenericDimensions::Explode,
        "skip" => GenericDimensions::Skip,
        _ => {
            return Err(PyValueError::new_err(
                "generic_dimensions must be 'explode' or 'skip'",
            ))
        }
    };
    Ok(Options {
        target,
        curve_segments: curve_segments as u32,
        generic_dimensions,
        entity_map,
    })
}

/// An export diagnostic: ``(code, severity, message, entity_id, action)``.
type DiagnosticTuple = (
    &'static str,
    &'static str,
    String,
    Option<String>,
    Option<&'static str>,
);

fn diagnostic_tuples(output: &mut DxfOutput) -> Vec<DiagnosticTuple> {
    std::mem::take(&mut output.diagnostics)
        .into_iter()
        .map(|d| (d.code, d.severity, d.message, d.entity_id, d.action))
        .collect()
}

/// The entity map as the ``dict``s ``ir_to_dxf`` collects: ``ir_id``,
/// ``handle``, ``dxf_type``, ``index`` and ``scope``, and ``reason_code`` for
/// an entity nothing was written for.
fn entity_map_to_py<'py>(py: Python<'py>, output: &DxfOutput) -> PyResult<Bound<'py, PyList>> {
    let list = PyList::empty_bound(py);
    let (ir_id, handle, dxf_type, index, scope, reason_code) = (
        pyo3::intern!(py, "ir_id"),
        pyo3::intern!(py, "handle"),
        pyo3::intern!(py, "dxf_type"),
        pyo3::intern!(py, "index"),
        pyo3::intern!(py, "scope"),
        pyo3::intern!(py, "reason_code"),
    );
    // the scope of many entries is one string
    let mut last_scope: Option<(*const u8, Bound<'py, PyString>)> = None;
    for entry in &output.entity_map {
        let row = PyDict::new_bound(py);
        row.set_item(ir_id, &*entry.ir_id)?;
        row.set_item(handle, entry.handle.as_deref())?;
        row.set_item(dxf_type, entry.dxf_type)?;
        row.set_item(index, entry.index)?;
        let scope_text = match &last_scope {
            Some((address, text)) if *address == entry.scope.as_ptr() => text.clone(),
            _ => {
                let text = PyString::new_bound(py, &entry.scope);
                last_scope = Some((entry.scope.as_ptr(), text.clone()));
                text
            }
        };
        row.set_item(scope, scope_text)?;
        if entry.dxf_type.is_none() {
            row.set_item(reason_code, entry.reason_code)?;
        }
        list.append(row)?;
    }
    Ok(list)
}

/// How a document ``dict`` is read into the typed document.
#[derive(Clone, Copy)]
struct Reading {
    /// The document is only written: a value of a kind JSON does not have is
    /// passed over, and what the writer never reads of an entity is left out
    /// (``dxf::reads_entity_key``). Otherwise the typed document is the whole
    /// ``dict``, and such a value an error.
    to_write: bool,
    /// The entities of model space are let go from their list one by one as
    /// they are converted, and the list is empty afterwards.
    consume: bool,
}

const KEEP: Reading = Reading {
    to_write: false,
    consume: false,
};

fn key_text(key: &Bound<'_, PyAny>, lenient: bool) -> PyResult<String> {
    match key.downcast::<PyString>() {
        Ok(key) => text_from_py(key),
        Err(_) if lenient => text_from_py(&key.str()?),
        Err(_) => Err(PyTypeError::new_err(
            "an IR document holds a dict key that is not a string",
        )),
    }
}

/// The typed entity of an entity ``dict``.
fn entity_from_py(
    entity: &Bound<'_, PyDict>,
    reading: Reading,
    interner: &mut Interner,
) -> PyResult<Entity> {
    if !reading.to_write {
        return Ok(entity_from_map(map_of(entity, false)?, interner));
    }
    let kind = match entity.get_item("kind")? {
        Some(kind) => match kind.downcast::<PyString>() {
            Ok(kind) => Some(text_from_py(kind)?),
            Err(_) => None,
        },
        None => None,
    };
    let mut map = Map::with_capacity(entity.len());
    for (key, value) in entity.iter() {
        let key = key_text(&key, true)?;
        if dxf::reads_entity_key(kind.as_deref(), &key) {
            map.insert(key, value_of(&value, true)?);
        }
    }
    Ok(entity_from_map(map, interner))
}

/// The list under ``key`` when every item of it is a ``dict``: a list of
/// entities that is converted one entity at a time.
fn entity_list<'py>(owner: &Bound<'py, PyDict>, key: &str) -> PyResult<Option<Bound<'py, PyList>>> {
    let Some(value) = owner.get_item(key)? else {
        return Ok(None);
    };
    let Ok(list) = value.downcast_into::<PyList>() else {
        return Ok(None);
    };
    if list.iter().all(|item| item.is_instance_of::<PyDict>()) {
        Ok(Some(list))
    } else {
        Ok(None)
    }
}

/// ``owner`` without ``key`` (a new ``dict``; the values are shared).
fn without_key<'py>(owner: &Bound<'py, PyDict>, key: &str) -> PyResult<Bound<'py, PyDict>> {
    let copy = PyDict::new_bound(owner.py());
    for (name, value) in owner.iter() {
        if name.extract::<&str>().is_ok_and(|name| name == key) {
            continue;
        }
        copy.set_item(name, value)?;
    }
    Ok(copy)
}

/// The entity lists of the blocks, taken out of ``tables``: the tables with
/// those blocks lacking their ``entities``, and the lists by block name. ``None``
/// when the block table is not a ``dict`` of ``dict``s with text for names.
type BlockEntities<'py> = Vec<(String, Bound<'py, PyList>)>;

fn blocks_apart<'py>(
    tables: &Bound<'py, PyDict>,
) -> PyResult<Option<(Bound<'py, PyDict>, BlockEntities<'py>)>> {
    let Some(blocks) = tables.get_item("blocks")? else {
        return Ok(None);
    };
    let Ok(blocks) = blocks.downcast_into::<PyDict>() else {
        return Ok(None);
    };
    let mut lists = Vec::new();
    let shells = PyDict::new_bound(tables.py());
    for (name, block) in blocks.iter() {
        let (Ok(name_text), Ok(block)) = (name.downcast::<PyString>(), block.downcast::<PyDict>())
        else {
            return Ok(None);
        };
        match entity_list(block, "entities")? {
            Some(list) => {
                shells.set_item(&name, without_key(block, "entities")?)?;
                lists.push((text_from_py(name_text)?, list));
            }
            None => shells.set_item(&name, block)?,
        }
    }
    if lists.is_empty() {
        return Ok(None);
    }
    let copy = PyDict::new_bound(tables.py());
    for (name, value) in tables.iter() {
        if name.extract::<&str>().is_ok_and(|name| name == "blocks") {
            copy.set_item(name, &shells)?;
        } else {
            copy.set_item(name, value)?;
        }
    }
    Ok(Some((copy, lists)))
}

/// The typed document of a document ``dict``.
///
/// The entities of model space and of the blocks are converted one at a time,
/// so that the dynamic form of an entity (every key and value as an object of
/// its own) exists for one entity only: for a large drawing all of them at
/// once take more memory than the ``dict``.
fn document_from_py(document: &Bound<'_, PyAny>, reading: Reading) -> PyResult<Document> {
    let Ok(dict) = document.downcast::<PyDict>() else {
        return Err(PyTypeError::new_err("an IR document is a dict"));
    };
    let lenient = reading.to_write;
    let entities = entity_list(dict, "entities")?;
    let blocks = match dict.get_item("tables")? {
        Some(tables) => match tables.downcast::<PyDict>() {
            Ok(tables) => blocks_apart(tables)?,
            Err(_) => None,
        },
        None => None,
    };
    if entities.is_none() && blocks.is_none() {
        return Ok(document_from_map(map_of(dict, lenient)?));
    }

    // the document without those lists
    let shell = PyDict::new_bound(dict.py());
    for (key, value) in dict.iter() {
        match key.extract::<&str>() {
            Ok("entities") if entities.is_some() => {}
            Ok("tables") => match &blocks {
                Some((tables, _)) => shell.set_item(key, tables)?,
                None => shell.set_item(key, value)?,
            },
            _ => shell.set_item(key, value)?,
        }
    }
    let mut typed = document_from_map(map_of(&shell, lenient)?);
    let mut interner = Interner::new();

    if let Some((_, lists)) = blocks {
        let Some(typed_blocks) = typed
            .tables
            .as_mut()
            .and_then(|tables| tables.blocks.as_mut())
        else {
            return Err(PyRuntimeError::new_err("the block table was not converted"));
        };
        for (name, list) in lists {
            let Some(block) = typed_blocks.get_mut(&name) else {
                return Err(PyRuntimeError::new_err("a block was not converted"));
            };
            block.entities.reserve(list.len());
            for entity in list.iter() {
                let entity = entity.downcast_into::<PyDict>()?;
                block
                    .entities
                    .push(entity_from_py(&entity, reading, &mut interner)?);
            }
            block.has_entities = true;
        }
    }

    if let Some(entities) = entities {
        let none = dict.py().None();
        typed.entities.reserve(entities.len());
        for index in 0..entities.len() {
            let entity = entities.get_item(index)?;
            if reading.consume {
                // the list lets the entity go before the next one is converted
                entities.set_item(index, &none)?;
            }
            let entity = entity.downcast_into::<PyDict>()?;
            typed
                .entities
                .push(entity_from_py(&entity, reading, &mut interner)?);
        }
        typed.has_entities = true;
        if reading.consume {
            entities.call_method0("clear")?;
        }
    }
    Ok(typed)
}

fn points_to_py(points: Vec<[f64; 2]>) -> Vec<Vec<f64>> {
    points.into_iter().map(|[x, y]| vec![x, y]).collect()
}

/// Points on an ellipse from ``start_param`` to ``end_param`` (radians),
/// ``curve_segments`` of them for a whole turn; the end point is among them
/// unless the ellipse is whole.
#[pyfunction]
fn sample_ellipse(
    center: [f64; 2],
    major_axis: [f64; 2],
    ratio: f64,
    start_param: f64,
    end_param: f64,
    curve_segments: u32,
) -> PyResult<Vec<Vec<f64>>> {
    dxf::sample_ellipse(
        center,
        major_axis,
        ratio,
        start_param,
        end_param,
        curve_segments,
    )
    .map(points_to_py)
    .map_err(write_error)
}

/// The width of a text in character cells, each as wide as the text is high,
/// as the DXF writer measures a justified text: a character that takes two
/// bytes in Shift_JIS is one cell, any other half a cell, and the ``%%`` codes
/// count as what a reader draws for them.
#[pyfunction]
fn text_cells(text: &Bound<'_, PyString>) -> PyResult<f64> {
    Ok(dxf::text_cells(&text_from_py(text)?))
}

/// ``curve_segments + 1`` points on a (rational) B-spline. Knots and weights
/// that do not fit the control points and the degree are replaced by clamped
/// uniform knots and by ones. ``ValueError`` for a spline without a domain.
#[pyfunction]
#[pyo3(signature = (control_points, degree, knots, weights, curve_segments))]
fn sample_spline(
    control_points: Vec<[f64; 2]>,
    degree: i64,
    knots: Option<Vec<f64>>,
    weights: Option<Vec<f64>>,
    curve_segments: u32,
) -> PyResult<Vec<Vec<f64>>> {
    dxf::sample_spline(
        &control_points,
        degree,
        knots.as_deref(),
        weights.as_deref(),
        curve_segments,
    )
    .map(points_to_py)
    .map_err(write_error)
}

/// The document as the typed IR and back (for checking the conversion).
#[pyfunction]
fn document_roundtrip<'py>(
    py: Python<'py>,
    document: &Bound<'py, PyAny>,
) -> PyResult<Bound<'py, PyDict>> {
    let typed = document_from_py(document, KEEP)?;
    document_to_py(py, &typed)
}

/// What ``write_dxf`` returns: the text, the diagnostics and the entity map.
type Written<'py> = (String, Vec<DiagnosticTuple>, Bound<'py, PyList>);

/// The DXF text of an IR document: ``cad2d_ir.codecs.dxf.ir_to_dxf`` without
/// its validation.
///
/// Returns ``(dxf_text, diagnostics, entity_map)``. A diagnostic is
/// ``(code, severity, message, entity_id, action)``; the entity map is filled
/// when ``entity_map`` is true. With ``consume`` the entities of model space
/// are taken out of ``document["entities"]`` as they are read.
#[pyfunction]
#[pyo3(signature = (document, *, target_version = "AC1024", curve_segments = 96, generic_dimensions = "explode", entity_map = false, consume = false))]
fn write_dxf<'py>(
    py: Python<'py>,
    document: &Bound<'py, PyAny>,
    target_version: &str,
    curve_segments: i64,
    generic_dimensions: &str,
    entity_map: bool,
    consume: bool,
) -> PyResult<Written<'py>> {
    let options = dxf_options(
        target_version,
        curve_segments,
        generic_dimensions,
        entity_map,
    )?;
    let typed = document_from_py(
        document,
        Reading {
            to_write: true,
            consume,
        },
    )?;
    let host = PythonHost::new(py)?;
    let mut output = dxf::write(&typed, &options, &host).map_err(write_error)?;
    let diagnostics = diagnostic_tuples(&mut output);
    let map = entity_map_to_py(py, &output)?;
    Ok((output.text, diagnostics, map))
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
            document: document_from_py(document, KEEP)?,
        })
    }

    /// The document as the ``dict`` the Python importers return.
    fn to_dict<'py>(&self, py: Python<'py>) -> PyResult<Bound<'py, PyDict>> {
        document_to_py(py, &self.document)
    }

    /// The DXF text of the document, the export diagnostics and the entity map
    /// (see ``write_dxf``).
    #[pyo3(signature = (*, target_version = "AC1024", curve_segments = 96, generic_dimensions = "explode", entity_map = false))]
    fn to_dxf<'py>(
        &self,
        py: Python<'py>,
        target_version: &str,
        curve_segments: i64,
        generic_dimensions: &str,
        entity_map: bool,
    ) -> PyResult<Written<'py>> {
        let options = dxf_options(
            target_version,
            curve_segments,
            generic_dimensions,
            entity_map,
        )?;
        let host = PythonHost::new(py)?;
        let mut output = dxf::write(&self.document, &options, &host).map_err(write_error)?;
        let diagnostics = diagnostic_tuples(&mut output);
        let map = entity_map_to_py(py, &output)?;
        Ok((output.text, diagnostics, map))
    }

    /// DXF R2010 as the bytes of its file (UTF-8) with the export diagnostics,
    /// without a Python ``str`` of the whole file in between.
    #[pyo3(signature = (*, curve_segments = 96, generic_dimensions = "explode"))]
    fn to_dxf_bytes<'py>(
        &self,
        py: Python<'py>,
        curve_segments: i64,
        generic_dimensions: &str,
    ) -> PyResult<(Bound<'py, pyo3::types::PyBytes>, Vec<DiagnosticTuple>)> {
        let options = dxf_options("AC1024", curve_segments, generic_dimensions, false)?;
        let host = PythonHost::new(py)?;
        let mut output = dxf::write(&self.document, &options, &host).map_err(write_error)?;
        let diagnostics = diagnostic_tuples(&mut output);
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
    #[pyo3(signature = (*, label, source_format, entity_provenance, strict, curve_segments, layer_names, layer_table, block_names, linetype_names, pen_widths, pen_colors))]
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
        layer_table: Vec<String>,
        block_names: HashMap<i64, String>,
        linetype_names: HashMap<i64, String>,
        pen_widths: HashMap<i64, f64>,
        pen_colors: HashMap<i64, i64>,
    ) -> PyResult<ConvertedJww> {
        let context = conversion_context(
            label,
            source_format,
            entity_provenance,
            strict,
            curve_segments,
            layer_names,
            layer_table,
            block_names,
            linetype_names,
            pen_widths,
            pen_colors,
        );
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
            Err(ConvertError(reason)) => Err(PyValueError::new_err(reason)),
        }
    }
}

/// The lookup tables of a conversion as ``cad2d-ir-jww`` takes them.
#[allow(clippy::too_many_arguments)]
fn conversion_context(
    label: String,
    source_format: String,
    entity_provenance: bool,
    strict: bool,
    curve_segments: i64,
    layer_names: HashMap<(i64, i64), String>,
    layer_table: Vec<String>,
    block_names: HashMap<i64, String>,
    linetype_names: HashMap<i64, String>,
    pen_widths: HashMap<i64, f64>,
    pen_colors: HashMap<i64, i64>,
) -> cad2d_ir_jww::Context {
    let shared = |names: HashMap<i64, String>| -> HashMap<i64, Arc<str>> {
        names
            .into_iter()
            .map(|(key, name)| (key, Arc::from(name.as_str())))
            .collect()
    };
    cad2d_ir_jww::Context {
        label,
        source_format,
        entity_provenance,
        strict,
        curve_segments,
        layer_names: layer_names
            .into_iter()
            .map(|(key, name)| (key, Arc::from(name.as_str())))
            .collect(),
        layer_table: layer_table.into_iter().collect(),
        block_names: shared(block_names),
        linetype_names: shared(linetype_names),
        pen_widths,
        pen_colors,
    }
}

/// Convert the entities of a document held as the ``dict`` of
/// ``ezjww.read_document`` or ``ezjww.read_jws_document``, with the lookup
/// tables the importer made from its header.
///
/// With ``consume`` each entity of the drawing is let go from
/// ``document["entities"]`` (``None`` takes its place) once it is read.
#[pyfunction]
#[pyo3(signature = (document, *, consume, label, source_format, entity_provenance, strict, curve_segments, layer_names, layer_table, block_names, linetype_names, pen_widths, pen_colors))]
#[allow(clippy::too_many_arguments)]
fn convert_jw_document(
    py: Python<'_>,
    document: &Bound<'_, PyDict>,
    consume: bool,
    label: String,
    source_format: String,
    entity_provenance: bool,
    strict: bool,
    curve_segments: i64,
    layer_names: HashMap<(i64, i64), String>,
    layer_table: Vec<String>,
    block_names: HashMap<i64, String>,
    linetype_names: HashMap<i64, String>,
    pen_widths: HashMap<i64, f64>,
    pen_colors: HashMap<i64, i64>,
) -> PyResult<ConvertedJww> {
    let context = conversion_context(
        label,
        source_format,
        entity_provenance,
        strict,
        curve_segments,
        layer_names,
        layer_table,
        block_names,
        linetype_names,
        pen_widths,
        pen_colors,
    );
    let records = jww_dict::records_of(document, consume)?;
    let converted = py.allow_threads(|| {
        let sources = Sources {
            entities: records
                .entities
                .iter()
                .map(jww_dict::Record::source)
                .collect(),
            blocks: records
                .blocks
                .iter()
                .map(|block| block.iter().map(jww_dict::Record::source).collect())
                .collect(),
            images: &records.images,
        };
        cad2d_ir_jww::convert_sources(&sources, &context)
    });
    match converted {
        Ok(converted) => Ok(ConvertedJww {
            converted: Some(converted),
        }),
        Err(ConvertError(reason)) => Err(PyValueError::new_err(reason)),
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

    /// The layers made for entities on a layer the header does not have, in
    /// the order they were made: ``((layer_group, layer), name)``.
    fn added_layers(&self) -> PyResult<Vec<((i64, i64), String)>> {
        Ok(self.get()?.added_layers.clone())
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
        let mut document = document_from_py(shell, KEEP)?;
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
    module.add_function(wrap_pyfunction!(write_dxf, module)?)?;
    module.add_function(wrap_pyfunction!(sample_ellipse, module)?)?;
    module.add_function(wrap_pyfunction!(sample_spline, module)?)?;
    module.add_function(wrap_pyfunction!(text_cells, module)?)?;
    module.add_function(wrap_pyfunction!(parse_jww, module)?)?;
    module.add_function(wrap_pyfunction!(convert_jw_document, module)?)?;
    module.add_class::<NativeDocument>()?;
    module.add_class::<ParsedJww>()?;
    module.add_class::<ConvertedJww>()?;
    Ok(())
}
