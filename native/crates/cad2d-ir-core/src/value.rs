//! Dynamic values for the parts of the IR that have no typed field: metadata,
//! header settings and whatever a document carries that this crate does not know.

use indexmap::IndexMap;

/// An ordered string-keyed map (the order of a Python ``dict``).
pub type Map = IndexMap<String, Value>;

#[derive(Clone, Debug, PartialEq)]
pub enum Value {
    Null,
    Bool(bool),
    Int(i64),
    Float(f64),
    Str(String),
    List(Vec<Value>),
    Map(Map),
}

impl Value {
    pub fn as_str(&self) -> Option<&str> {
        match self {
            Value::Str(text) => Some(text),
            _ => None,
        }
    }

    pub fn as_map(&self) -> Option<&Map> {
        match self {
            Value::Map(map) => Some(map),
            _ => None,
        }
    }

    pub fn as_list(&self) -> Option<&[Value]> {
        match self {
            Value::List(items) => Some(items),
            _ => None,
        }
    }

    /// A Python ``int`` or ``float`` that is not a ``bool``.
    pub fn as_number(&self) -> Option<f64> {
        match self {
            Value::Int(value) => Some(*value as f64),
            Value::Float(value) => Some(*value),
            _ => None,
        }
    }

    /// ``isinstance(value, (int, float))`` in Python, where a ``bool`` is an ``int``.
    pub fn as_python_number(&self) -> Option<f64> {
        match self {
            Value::Bool(value) => Some(if *value { 1.0 } else { 0.0 }),
            _ => self.as_number(),
        }
    }

    /// Python truthiness.
    pub fn truthy(&self) -> bool {
        match self {
            Value::Null => false,
            Value::Bool(value) => *value,
            Value::Int(value) => *value != 0,
            Value::Float(value) => *value != 0.0,
            Value::Str(text) => !text.is_empty(),
            Value::List(items) => !items.is_empty(),
            Value::Map(map) => !map.is_empty(),
        }
    }
}
