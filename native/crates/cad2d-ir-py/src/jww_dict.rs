//! The records of a JWW or JWS document held as the ``dict`` of
//! ``ezjww.read_document`` / ``ezjww.read_jws_document``, read into the structs
//! of ``ezjww-core`` for the entity conversion of ``cad2d-ir-jww``.
//!
//! A value is read as the importer of ``cad2d-ir`` always read it: a required
//! number is an ``int`` or a ``float`` under its key, an optional one what
//! ``float`` makes of the value, a text what ``str`` makes of it. A record that
//! cannot be read is kept as what it is (malformed, with the reason), and the
//! conversion reports it like a record that fails to convert.

use ezjww_core::{
    Arc as JwArc, Block as JwBlock, CircleSolid, Dimension, EmbeddedImage, Entity as JwEntity,
    EntityBase, Line as JwLine, Point as JwPoint, Solid, Text as JwText,
};
use pyo3::exceptions::PyTypeError;
use pyo3::prelude::*;
use pyo3::types::{PyBool, PyDict, PyFloat, PyInt, PyList, PyTuple};

const TAU: f64 = 2.0 * std::f64::consts::PI;

/// One record of an entity list (``cad2d_ir_jww::Source``, owned).
pub(crate) enum Record {
    Absent,
    Entity(Box<JwEntity>, String),
    Unknown {
        kind: String,
        base: EntityBase,
    },
    Malformed {
        kind: String,
        base: Option<EntityBase>,
        message: String,
    },
}

impl Record {
    pub(crate) fn source(&self) -> cad2d_ir_jww::Source<'_> {
        match self {
            Record::Absent => cad2d_ir_jww::Source::Absent,
            Record::Entity(entity, kind) => cad2d_ir_jww::Source::Entity(entity, kind),
            Record::Unknown { kind, base } => cad2d_ir_jww::Source::Unknown { kind, base },
            Record::Malformed {
                kind,
                base,
                message,
            } => cad2d_ir_jww::Source::Malformed {
                kind,
                base: base.as_ref(),
                message,
            },
        }
    }
}

/// Why a record cannot be read: the message of the ``KeyError``, ``TypeError``
/// or ``ValueError`` Python raises for reading such a value.
struct Failure(String);

/// What reading a value gives: the value, why the record is malformed, or an
/// error of Python that is no fault of the record.
type Read<T> = PyResult<Result<T, Failure>>;

/// ``?`` for both layers of a [`Read`].
macro_rules! read {
    ($expression:expr) => {
        match $expression? {
            Ok(value) => value,
            Err(failure) => return Ok(Err(failure)),
        }
    };
}

fn failure_of(error: PyErr, py: Python<'_>) -> Failure {
    Failure(
        error
            .value_bound(py)
            .str()
            .map(|text| text.to_string_lossy().into_owned())
            .unwrap_or_default(),
    )
}

/// ``isinstance(value, (int, float))`` (a ``bool`` is an ``int``).
fn is_number(value: &Bound<'_, PyAny>) -> bool {
    value.is_instance_of::<PyInt>() || value.is_instance_of::<PyFloat>()
}

/// ``_number(mapping, key)``: a number that has to be there. One that is not
/// finite is passed on: the conversion rejects it, with the same message.
fn number(mapping: &Bound<'_, PyDict>, key: &str) -> Read<f64> {
    let Some(value) = mapping.get_item(key)? else {
        // ``str(KeyError(key))``
        return Ok(Err(Failure(format!("'{key}'"))));
    };
    if !is_number(&value) {
        return Ok(Err(Failure(format!("{key} must be a finite number"))));
    }
    match value.extract::<f64>() {
        Ok(number) => Ok(Ok(number)),
        Err(error) => Ok(Err(failure_of(error, mapping.py()))),
    }
}

/// A number the conversion reads only for some records (the end of a text,
/// with entity provenance): one that is missing or no number is passed on as
/// a NaN, which the conversion rejects when it reads it.
fn late_number(mapping: &Bound<'_, PyDict>, key: &str) -> PyResult<f64> {
    Ok(match mapping.get_item(key)? {
        Some(value) if is_number(&value) => value.extract::<f64>().unwrap_or(f64::NAN),
        _ => f64::NAN,
    })
}

/// ``float(mapping.get(key, default))``.
fn float_or(mapping: &Bound<'_, PyDict>, key: &str, default: f64) -> Read<f64> {
    let Some(value) = mapping.get_item(key)? else {
        return Ok(Ok(default));
    };
    if let Ok(number) = value.downcast::<PyFloat>() {
        return Ok(Ok(number.value()));
    }
    let py = mapping.py();
    match py.get_type_bound::<PyFloat>().call1((value,)) {
        Ok(number) => Ok(Ok(number.extract()?)),
        Err(error) => Ok(Err(failure_of(error, py))),
    }
}

/// ``int(mapping.get(key, default))`` within the range of the field it fills.
fn int_or<T: TryFrom<i64>>(mapping: &Bound<'_, PyDict>, key: &str, default: i64) -> Read<T> {
    let py = mapping.py();
    let number: i64 = match mapping.get_item(key)? {
        None => default,
        Some(value) => match py.get_type_bound::<PyInt>().call1((value,)) {
            Ok(number) => match number.extract() {
                Ok(number) => number,
                Err(_) => return Ok(Err(Failure(format!("{key} is out of range")))),
            },
            Err(error) => return Ok(Err(failure_of(error, py))),
        },
    };
    match T::try_from(number) {
        Ok(number) => Ok(Ok(number)),
        Err(_) => Ok(Err(Failure(format!("{key} is out of range")))),
    }
}

/// ``str(mapping.get(key, ""))``.
fn text_or_empty(mapping: &Bound<'_, PyDict>, key: &str) -> PyResult<String> {
    match mapping.get_item(key)? {
        None => Ok(String::new()),
        Some(value) => crate::text_from_py(&value.str()?),
    }
}

/// ``bool(mapping.get(key, False))``.
fn flag(mapping: &Bound<'_, PyDict>, key: &str) -> PyResult<bool> {
    match mapping.get_item(key)? {
        None => Ok(false),
        Some(value) => value.is_truthy(),
    }
}

/// ``_mapping(mapping.get(key, {}), path)``.
fn mapping_or_empty<'py>(
    mapping: &Bound<'py, PyDict>,
    key: &str,
    path: &str,
) -> Read<Bound<'py, PyDict>> {
    match mapping.get_item(key)? {
        None => Ok(Ok(PyDict::new_bound(mapping.py()))),
        Some(value) => match value.downcast_into::<PyDict>() {
            Ok(value) => Ok(Ok(value)),
            Err(_) => Ok(Err(Failure(format!("{path} must be a mapping")))),
        },
    }
}

/// The items of ``mapping.get(key, [])`` when it is a sequence of mappings.
fn mappings<'py>(
    mapping: &Bound<'py, PyDict>,
    key: &str,
    path: &str,
) -> Read<Vec<Bound<'py, PyDict>>> {
    let Some(value) = mapping.get_item(key)? else {
        return Ok(Ok(Vec::new()));
    };
    let items: Vec<Bound<'py, PyAny>> = if let Ok(list) = value.downcast::<PyList>() {
        list.iter().collect()
    } else if let Ok(tuple) = value.downcast::<PyTuple>() {
        tuple.iter().collect()
    } else {
        return Ok(Err(Failure(format!("{path} must be a sequence"))));
    };
    let mut result = Vec::with_capacity(items.len());
    for (index, item) in items.into_iter().enumerate() {
        match item.downcast_into::<PyDict>() {
            Ok(item) => result.push(item),
            Err(_) => return Ok(Err(Failure(format!("{path}[{index}] must be a mapping")))),
        }
    }
    Ok(Ok(result))
}

fn base_of(record: &Bound<'_, PyDict>, path: &str) -> Read<EntityBase> {
    let base = read!(mapping_or_empty(record, "base", &format!("{path}.base")));
    Ok(Ok(EntityBase {
        group: read!(int_or(&base, "group", 0)),
        pen_style: read!(int_or(&base, "pen_style", 0)),
        pen_color: read!(int_or(&base, "pen_color", 0)),
        pen_width: read!(int_or(&base, "pen_width", 0)),
        layer: read!(int_or(&base, "layer", 0)),
        layer_group: read!(int_or(&base, "layer_group", 0)),
        flag: read!(int_or(&base, "flag", 0)),
    }))
}

/// The colour of a solid: ``record.get("color")`` when it is an ``int``.
fn custom_color(record: &Bound<'_, PyDict>) -> PyResult<Option<u32>> {
    Ok(match record.get_item("color")? {
        Some(value) if value.is_instance_of::<PyInt>() && !value.is_instance_of::<PyBool>() => {
            value.extract::<u32>().ok()
        }
        _ => None,
    })
}

fn line_of(record: &Bound<'_, PyDict>, base: EntityBase) -> Read<JwLine> {
    Ok(Ok(JwLine {
        base,
        start_x: read!(number(record, "start_x")),
        start_y: read!(number(record, "start_y")),
        end_x: read!(number(record, "end_x")),
        end_y: read!(number(record, "end_y")),
    }))
}

fn point_of(record: &Bound<'_, PyDict>, base: EntityBase) -> Read<JwPoint> {
    Ok(Ok(JwPoint {
        base,
        x: read!(number(record, "x")),
        y: read!(number(record, "y")),
        is_temporary: flag(record, "is_temporary")?,
        code: read!(int_or(record, "code", 0)),
        angle: read!(float_or(record, "angle", 0.0)),
        scale: read!(float_or(record, "scale", 0.0)),
    }))
}

fn text_of(record: &Bound<'_, PyDict>, base: EntityBase) -> Read<JwText> {
    Ok(Ok(JwText {
        base,
        start_x: read!(number(record, "start_x")),
        start_y: read!(number(record, "start_y")),
        end_x: late_number(record, "end_x")?,
        end_y: late_number(record, "end_y")?,
        text_type: read!(int_or(record, "text_type", 0)),
        size_x: read!(float_or(record, "size_x", 0.0)),
        size_y: read!(float_or(record, "size_y", 0.0)),
        spacing: read!(float_or(record, "spacing", 0.0)),
        angle: read!(float_or(record, "angle", 0.0)),
        font_name: text_or_empty(record, "font_name")?,
        content: text_or_empty(record, "content")?,
    }))
}

/// The base of a record inside a dimension (its line, text, auxiliary lines
/// and points): its own when it has one.
fn inner_base(record: &Bound<'_, PyDict>, path: &str) -> Read<EntityBase> {
    base_of(record, path)
}

fn entity_of(
    record: &Bound<'_, PyDict>,
    kind: &str,
    base: EntityBase,
    path: &str,
) -> Read<Option<JwEntity>> {
    Ok(Ok(Some(match kind {
        "LINE" => JwEntity::Line(read!(line_of(record, base))),
        "ARC" | "CIRCLE" => {
            let is_full_circle = flag(record, "is_full_circle")? || kind == "CIRCLE";
            JwEntity::Arc(JwArc {
                base,
                radius: read!(number(record, "radius")),
                center_x: read!(number(record, "center_x")),
                center_y: read!(number(record, "center_y")),
                flatness: read!(float_or(record, "flatness", 1.0)),
                start_angle: read!(float_or(record, "start_angle", 0.0)),
                arc_angle: read!(float_or(
                    record,
                    "arc_angle",
                    if is_full_circle { TAU } else { 0.0 }
                )),
                tilt_angle: read!(float_or(record, "tilt_angle", 0.0)),
                is_full_circle,
            })
        }
        "POINT" => JwEntity::Point(read!(point_of(record, base))),
        "TEXT" => JwEntity::Text(read!(text_of(record, base))),
        "SOLID" => JwEntity::Solid(Solid {
            base,
            point1_x: read!(number(record, "point1_x")),
            point1_y: read!(number(record, "point1_y")),
            point2_x: read!(number(record, "point2_x")),
            point2_y: read!(number(record, "point2_y")),
            point3_x: read!(number(record, "point3_x")),
            point3_y: read!(number(record, "point3_y")),
            point4_x: read!(number(record, "point4_x")),
            point4_y: read!(number(record, "point4_y")),
            color: custom_color(record)?,
        }),
        "CIRCLE_SOLID" => JwEntity::CircleSolid(CircleSolid {
            base,
            radius: read!(number(record, "radius")),
            start_angle: read!(float_or(record, "start_angle", 0.0)),
            arc_angle: read!(float_or(record, "arc_angle", TAU)),
            solid_mode: read!(float_or(record, "solid_mode", 0.0)),
            flatness: read!(float_or(record, "flatness", 1.0)),
            tilt_angle: read!(float_or(record, "tilt_angle", 0.0)),
            center_x: read!(number(record, "center_x")),
            center_y: read!(number(record, "center_y")),
            color: custom_color(record)?,
        }),
        "BLOCK" => JwEntity::Block(JwBlock {
            base,
            def_number: read!(int_or(record, "def_number", 0)),
            scale_x: read!(float_or(record, "scale_x", 1.0)),
            scale_y: read!(float_or(record, "scale_y", 1.0)),
            ref_x: read!(number(record, "ref_x")),
            ref_y: read!(number(record, "ref_y")),
            rotation: read!(float_or(record, "rotation", 0.0)),
        }),
        "DIMENSION" => {
            let line = read!(mapping_or_empty(record, "line", "dimension.line"));
            let text = read!(mapping_or_empty(record, "text", "dimension.text"));
            let mut aux_lines = Vec::new();
            for (index, item) in read!(mappings(record, "aux_lines", "dimension.aux_lines"))
                .iter()
                .enumerate()
            {
                let inner = read!(inner_base(item, &format!("{path}.aux_lines[{index}]")));
                // an auxiliary line is kept as it is, and read when it is written
                aux_lines.push(JwLine {
                    base: inner,
                    start_x: late_number(item, "start_x")?,
                    start_y: late_number(item, "start_y")?,
                    end_x: late_number(item, "end_x")?,
                    end_y: late_number(item, "end_y")?,
                });
            }
            let mut aux_points = Vec::new();
            for (index, item) in read!(mappings(record, "aux_points", "dimension.aux_points"))
                .iter()
                .enumerate()
            {
                let inner = read!(inner_base(item, &format!("{path}.aux_points[{index}]")));
                aux_points.push(JwPoint {
                    base: inner,
                    x: late_number(item, "x")?,
                    y: late_number(item, "y")?,
                    is_temporary: flag(item, "is_temporary")?,
                    code: read!(int_or(item, "code", 0)),
                    angle: read!(float_or(item, "angle", 0.0)),
                    scale: read!(float_or(item, "scale", 0.0)),
                });
            }
            let line_base = read!(inner_base(&line, &format!("{path}.line")));
            let text_base = read!(inner_base(&text, &format!("{path}.text")));
            let sxf_mode = match record.get_item("sxf_mode")? {
                Some(value) if !value.is_none() => value.extract::<u16>().ok(),
                _ => None,
            };
            JwEntity::Dimension(Dimension {
                base,
                line: read!(line_of(&line, line_base)),
                text: read!(text_of(&text, text_base)),
                sxf_mode,
                aux_lines,
                aux_points,
            })
        }
        _ => return Ok(Ok(None)),
    })))
}

/// One record of an entity list of the ``dict``.
fn record_of(item: &Bound<'_, PyAny>, path: &str) -> PyResult<Record> {
    if item.is_none() {
        return Ok(Record::Absent);
    }
    let Ok(record) = item.downcast::<PyDict>() else {
        return Err(PyTypeError::new_err(format!("{path} must be a mapping")));
    };
    // ``str(record.get("type", "UNKNOWN")).upper()``
    let kind: String = match record.get_item("type")? {
        None => "UNKNOWN".to_string(),
        Some(value) => value.str()?.call_method0("upper")?.extract()?,
    };
    let base = match base_of(record, path)? {
        Ok(base) => base,
        Err(Failure(message)) => {
            return Ok(Record::Malformed {
                kind,
                base: None,
                message,
            })
        }
    };
    Ok(match entity_of(record, &kind, base, path)? {
        Ok(Some(entity)) => Record::Entity(Box::new(entity), kind),
        Ok(None) => Record::Unknown { kind, base },
        Err(Failure(message)) => Record::Malformed {
            kind,
            base: Some(base),
            message,
        },
    })
}

/// The items of a sequence, and the list they are in when it is a list.
type Items<'py> = (Vec<Bound<'py, PyAny>>, Option<Bound<'py, PyList>>);

/// The items of a list or a tuple under ``key`` (``[]`` without the key).
fn sequence<'py>(mapping: &Bound<'py, PyDict>, key: &str, path: &str) -> PyResult<Items<'py>> {
    let Some(value) = mapping.get_item(key)? else {
        return Ok((Vec::new(), None));
    };
    if let Ok(list) = value.downcast::<PyList>() {
        return Ok((list.iter().collect(), Some(list.clone())));
    }
    if let Ok(tuple) = value.downcast::<PyTuple>() {
        return Ok((tuple.iter().collect(), None));
    }
    Err(PyTypeError::new_err(format!("{path} must be a sequence")))
}

/// The records of a document ``dict``: its entities, the entities of each
/// block definition and its image archive.
pub(crate) struct Records {
    pub(crate) entities: Vec<Record>,
    pub(crate) blocks: Vec<Vec<Record>>,
    pub(crate) images: Vec<EmbeddedImage>,
}

/// Read the records of ``document``. With ``consume`` each entity of the
/// drawing is let go from its list (``None`` takes its place) once it is read.
pub(crate) fn records_of(document: &Bound<'_, PyDict>, consume: bool) -> PyResult<Records> {
    let py = document.py();
    let (items, list) = sequence(document, "entities", "entities")?;
    let mut entities = Vec::with_capacity(items.len());
    let none = py.None();
    for (index, item) in items.into_iter().enumerate() {
        entities.push(record_of(&item, &format!("entities[{index}]"))?);
        if consume {
            if let Some(list) = &list {
                drop(item);
                list.set_item(index, &none)?;
            }
        }
    }

    let (definitions, _) = sequence(document, "block_defs", "block_defs")?;
    let mut blocks = Vec::with_capacity(definitions.len());
    for (block_index, definition) in definitions.into_iter().enumerate() {
        let Ok(definition) = definition.downcast::<PyDict>() else {
            return Err(PyTypeError::new_err(format!(
                "block_defs[{block_index}] must be a mapping"
            )));
        };
        let path = format!("block_defs[{block_index}].entities");
        let (items, _) = sequence(definition, "entities", &path)?;
        let mut records = Vec::with_capacity(items.len());
        for (index, item) in items.into_iter().enumerate() {
            records.push(record_of(&item, &format!("{path}[{index}]"))?);
        }
        blocks.push(records);
    }

    // ``document.get("images") or []``: the files of the image archive
    let mut images = Vec::new();
    if let Some(value) = document.get_item("images")? {
        if let Ok(list) = value.downcast::<PyList>() {
            for image in list.iter() {
                let Ok(image) = image.downcast::<PyDict>() else {
                    continue;
                };
                let Some(name) = image.get_item("name")? else {
                    continue;
                };
                if !name.is_truthy()? {
                    continue;
                }
                let data: Vec<u8> = match image.get_item("data")? {
                    Some(data) if data.is_truthy()? => data.extract()?,
                    _ => Vec::new(),
                };
                images.push(EmbeddedImage {
                    name: crate::text_from_py(&name.str()?)?,
                    data,
                });
            }
        }
    }
    Ok(Records {
        entities,
        blocks,
        images,
    })
}
