//! The typed IR: what a Python IR ``dict`` holds, as structs.
//!
//! A field is typed when its value has the type the IR schema gives it. Anything
//! else (an unknown key, a value of another type) stays in ``extra`` as a dynamic
//! [`Value`], so that a document converted from a ``dict`` converts back to an
//! equal ``dict``. Code that writes a document reads those keys from ``extra``
//! where a typed field is missing.

use std::collections::HashMap;
use std::sync::Arc;

use indexmap::IndexMap;

use crate::value::{Map, Value};

pub type Point2 = [f64; 2];

/// Shares the strings many entities repeat (layer, linetype and style names).
#[derive(Default)]
pub struct Interner {
    known: HashMap<Box<str>, Arc<str>>,
}

impl Interner {
    pub fn new() -> Self {
        Self::default()
    }

    pub fn intern(&mut self, text: &str) -> Arc<str> {
        if let Some(found) = self.known.get(text) {
            return Arc::clone(found);
        }
        let shared: Arc<str> = Arc::from(text);
        self.known.insert(Box::from(text), Arc::clone(&shared));
        shared
    }
}

/// An entity colour: an AutoCAD colour index or a text such as ``#RRGGBB``.
#[derive(Clone, Debug, PartialEq)]
pub enum Color {
    Index(i64),
    Text(Arc<str>),
}

/// A polyline or hatch vertex: ``[x, y]`` or ``[x, y, bulge]``.
#[derive(Clone, Copy, Debug, PartialEq)]
pub struct Vertex {
    pub x: f64,
    pub y: f64,
    pub bulge: Option<f64>,
}

#[derive(Clone, Debug, PartialEq)]
pub struct TextData {
    pub insert: Point2,
    pub height: f64,
    pub text: String,
    pub rotation: Option<f64>,
    pub style: Option<Arc<str>>,
    pub width_factor: Option<f64>,
}

#[derive(Clone, Debug, PartialEq)]
pub struct EllipseData {
    pub center: Point2,
    pub major_axis: Point2,
    pub ratio: f64,
    pub start_param: f64,
    pub end_param: f64,
    pub ccw: Option<bool>,
}

/// The scale of a block reference: one factor or one for each axis.
#[derive(Clone, Copy, Debug, PartialEq)]
pub enum InsertScale {
    Uniform(f64),
    Axes(Point2),
}

#[derive(Clone, Debug, PartialEq)]
pub struct InsertData {
    pub block: Arc<str>,
    pub insert: Point2,
    pub rotation: Option<f64>,
    pub scale: Option<InsertScale>,
}

#[derive(Clone, Debug, PartialEq)]
pub struct HatchLoop {
    pub vertices: Vec<Vertex>,
    /// Keys of the loop besides ``vertices``.
    pub extra: Option<Box<Map>>,
}

#[derive(Clone, Debug, PartialEq)]
pub struct HatchData {
    pub loops: Vec<HatchLoop>,
    pub solid: Option<bool>,
}

#[derive(Clone, Debug, PartialEq)]
pub struct PolylineData {
    pub vertices: Vec<Vertex>,
    pub closed: Option<bool>,
}

/// The geometry of an entity. ``Other`` is an entity this crate has no struct
/// for (or one whose required fields are not as the schema has them): all of
/// its keys, ``kind`` included, are in ``Entity::extra``.
#[derive(Clone, Debug, PartialEq)]
pub enum Shape {
    Line {
        p1: Point2,
        p2: Point2,
    },
    Circle {
        center: Point2,
        radius: f64,
    },
    Arc {
        center: Point2,
        radius: f64,
        start_angle: f64,
        end_angle: f64,
        ccw: Option<bool>,
    },
    Point {
        position: Point2,
        temporary: Option<bool>,
        marker_code: Option<i64>,
    },
    Ellipse(Box<EllipseData>),
    Text(Box<TextData>),
    Insert(Box<InsertData>),
    Hatch(Box<HatchData>),
    Lwpolyline(Box<PolylineData>),
    Other,
}

impl Shape {
    /// The IR ``kind`` of a typed shape.
    pub fn kind(&self) -> Option<&'static str> {
        Some(match self {
            Shape::Line { .. } => "LINE",
            Shape::Circle { .. } => "CIRCLE",
            Shape::Arc { .. } => "ARC",
            Shape::Point { .. } => "POINT",
            Shape::Ellipse(_) => "ELLIPSE",
            Shape::Text(_) => "TEXT",
            Shape::Insert(_) => "INSERT",
            Shape::Hatch(_) => "HATCH",
            Shape::Lwpolyline(_) => "LWPOLYLINE",
            Shape::Other => return None,
        })
    }
}

#[derive(Clone, Debug, PartialEq)]
pub struct Entity {
    pub id: Option<Box<str>>,
    pub layer: Option<Arc<str>>,
    pub linetype: Option<Arc<str>>,
    pub color: Option<Color>,
    pub lineweight_mm: Option<f64>,
    pub shape: Shape,
    /// Every key without a typed field above or in ``shape``.
    pub extra: Option<Box<Map>>,
}

impl Entity {
    pub fn new(shape: Shape) -> Self {
        Entity {
            id: None,
            layer: None,
            linetype: None,
            color: None,
            lineweight_mm: None,
            shape,
            extra: None,
        }
    }

    pub fn extra_get(&self, key: &str) -> Option<&Value> {
        self.extra.as_ref().and_then(|extra| extra.get(key))
    }

    pub fn extra_insert(&mut self, key: &str, value: Value) {
        self.extra
            .get_or_insert_with(|| Box::new(Map::new()))
            .insert(key.to_string(), value);
    }

    /// Whether a number of the entity is not finite or larger than ``limit``:
    /// every float it holds, except under ``metadata`` and ``source``.
    pub fn has_absurd_number(&self, limit: f64) -> bool {
        let bad = |value: f64| !(-limit <= value && value <= limit);
        let bad_point = |point: &Point2| bad(point[0]) || bad(point[1]);
        let bad_vertex =
            |vertex: &Vertex| bad(vertex.x) || bad(vertex.y) || vertex.bulge.is_some_and(bad);
        if self.lineweight_mm.is_some_and(bad) {
            return true;
        }
        let shape_is_bad = match &self.shape {
            Shape::Line { p1, p2 } => bad_point(p1) || bad_point(p2),
            Shape::Circle { center, radius } => bad_point(center) || bad(*radius),
            Shape::Arc {
                center,
                radius,
                start_angle,
                end_angle,
                ..
            } => bad_point(center) || bad(*radius) || bad(*start_angle) || bad(*end_angle),
            Shape::Point { position, .. } => bad_point(position),
            Shape::Ellipse(ellipse) => {
                bad_point(&ellipse.center)
                    || bad_point(&ellipse.major_axis)
                    || bad(ellipse.ratio)
                    || bad(ellipse.start_param)
                    || bad(ellipse.end_param)
            }
            Shape::Text(text) => {
                bad_point(&text.insert)
                    || bad(text.height)
                    || text.rotation.is_some_and(bad)
                    || text.width_factor.is_some_and(bad)
            }
            Shape::Insert(insert) => {
                bad_point(&insert.insert)
                    || insert.rotation.is_some_and(bad)
                    || match insert.scale {
                        Some(InsertScale::Uniform(scale)) => bad(scale),
                        Some(InsertScale::Axes(scale)) => bad_point(&scale),
                        None => false,
                    }
            }
            Shape::Hatch(hatch) => hatch.loops.iter().any(|hatch_loop| {
                hatch_loop.vertices.iter().any(bad_vertex)
                    || hatch_loop
                        .extra
                        .as_ref()
                        .is_some_and(|extra| map_has_absurd_number(extra, limit))
            }),
            Shape::Lwpolyline(polyline) => polyline.vertices.iter().any(bad_vertex),
            Shape::Other => false,
        };
        shape_is_bad
            || self
                .extra
                .as_ref()
                .is_some_and(|extra| map_has_absurd_number(extra, limit))
    }

    /// The IR ``kind``: of the shape, or the ``kind`` key of an untyped entity.
    pub fn kind(&self) -> Option<&str> {
        match self.shape.kind() {
            Some(kind) => Some(kind),
            None => self.extra_get("kind").and_then(Value::as_str),
        }
    }
}

fn value_has_absurd_number(value: &Value, limit: f64) -> bool {
    match value {
        Value::Float(number) => !(-limit <= *number && *number <= limit),
        Value::List(items) => items
            .iter()
            .any(|item| value_has_absurd_number(item, limit)),
        Value::Map(map) => map_has_absurd_number(map, limit),
        _ => false,
    }
}

fn map_has_absurd_number(map: &Map, limit: f64) -> bool {
    map.iter().any(|(key, value)| {
        key != "metadata" && key != "source" && value_has_absurd_number(value, limit)
    })
}

#[derive(Clone, Debug, PartialEq, Default)]
pub struct Block {
    pub base_point: Option<Point2>,
    pub entities: Vec<Entity>,
    /// ``false`` when the block ``dict`` had no ``entities`` key.
    pub has_entities: bool,
    pub extra: Map,
}

/// The ``tables`` of a document. The layer, linetype and text style tables are
/// small and loosely typed, so they stay dynamic.
#[derive(Clone, Debug, PartialEq, Default)]
pub struct Tables {
    pub layers: Option<Value>,
    pub linetypes: Option<Value>,
    pub text_styles: Option<Value>,
    pub blocks: Option<IndexMap<String, Block>>,
    pub extra: Map,
}

#[derive(Clone, Debug, PartialEq, Default)]
pub struct Document {
    pub header: Option<Map>,
    pub tables: Option<Tables>,
    pub entities: Vec<Entity>,
    /// ``false`` when the document ``dict`` had no ``entities`` key.
    pub has_entities: bool,
    /// ``format``, ``version``, ``source`` and every other top-level key.
    pub extra: Map,
}
