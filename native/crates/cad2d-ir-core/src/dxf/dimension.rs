//! GENERIC dimensions: a dimension whose kind the importer could not tell is
//! written as the lines, texts and points it was made of
//! (``_generic_dimension_components`` and the GENERIC branch of
//! ``_render_entity``).

use std::sync::Arc;

use super::pairs::py_strip;
use super::{Context, WriteError};
use crate::model::{Entity, PolylineData, Shape, TextData, Vertex};
use crate::value::{Map, Value};

/// Whether ``entity`` is a ``DIMENSION`` with ``dim_kind == "GENERIC"``.
pub(crate) fn is_generic_dimension(entity: &Entity) -> bool {
    matches!(entity.shape, Shape::Other)
        && entity.kind() == Some("DIMENSION")
        && matches!(entity.extra_get("dim_kind"), Some(Value::Str(kind)) if kind == "GENERIC")
}

/// The ``definition`` of a dimension when it is a ``dict``.
fn definition_of(entity: &Entity) -> Option<&Map> {
    entity.extra_get("definition").and_then(Value::as_map)
}

/// The text styles a dimension names: its ``style`` and the font of the text it
/// was made of (``_normalized_tables``).
pub(crate) fn referenced_styles(entity: &Entity) -> Result<Vec<String>, WriteError> {
    let mut styles = Vec::new();
    if let Some(Value::Str(style)) = entity.extra_get("style") {
        styles.push(style.clone());
    }
    let font_name = definition_of(entity)
        .and_then(|definition| definition.get("source_geometry"))
        .and_then(Value::as_map)
        .and_then(|source| source.get("text"))
        .and_then(Value::as_map)
        .and_then(|text| text.get("font_name"));
    match font_name {
        None => {}
        Some(Value::Str(font)) => {
            let font = py_strip(font);
            if !font.is_empty() {
                styles.push(font.to_string());
            }
        }
        Some(_) => {
            return Err(WriteError::unsupported(
                "dimension font name is not a string",
            ))
        }
    }
    Ok(styles)
}

/// ``_point_like`` of the writer: a list of two or more whose first two items
/// are numbers (a ``bool`` is one).
fn point_like(value: Option<&Value>) -> Option<[f64; 2]> {
    let items = value?.as_list()?;
    if items.len() < 2 {
        return None;
    }
    Some([items[0].as_python_number()?, items[1].as_python_number()?])
}

/// ``float(mapping.get(key, default))`` for a value that is a number or absent.
fn float_or(mapping: &Map, key: &str, default: f64) -> Result<f64, WriteError> {
    match mapping.get(key) {
        None => Ok(default),
        Some(value) => value.as_python_number().ok_or_else(|| {
            WriteError::unsupported(format!("dimension field {key} is not a number"))
        }),
    }
}

/// ``str(mapping.get(key, default))`` for a value that is a string or absent.
fn str_or<'a>(
    mapping: &'a Map,
    key: &str,
    default: Option<&'a Value>,
) -> Result<&'a str, WriteError> {
    match mapping.get(key).or(default) {
        None => Ok(""),
        Some(Value::Str(text)) => Ok(text),
        Some(_) => Err(WriteError::unsupported(format!(
            "dimension field {key} is not a string"
        ))),
    }
}

struct Builder<'a> {
    entity: &'a Entity,
    /// ``linetype_scale`` and ``visible`` of the dimension, which every
    /// component takes with the typed common fields
    extra: Option<Box<Map>>,
    components: Vec<Entity>,
    line_keys: Vec<[f64; 4]>,
}

impl Builder<'_> {
    /// ``{**common, "kind": ...}``.
    fn component(&mut self, shape: Shape) {
        self.components.push(Entity {
            id: self.entity.id.clone(),
            layer: self.entity.layer.clone(),
            linetype: self.entity.linetype.clone(),
            color: self.entity.color.clone(),
            lineweight_mm: self.entity.lineweight_mm,
            shape,
            extra: self.extra.clone(),
        });
    }

    fn add_line(&mut self, start: Option<[f64; 2]>, end: Option<[f64; 2]>) {
        let (Some(start), Some(end)) = (start, end) else {
            return;
        };
        let key = [start[0], start[1], end[0], end[1]];
        let reverse = [end[0], end[1], start[0], start[1]];
        // a line drawn twice, in either direction, is written once
        if self
            .line_keys
            .iter()
            .any(|known| *known == key || *known == reverse)
        {
            return;
        }
        self.line_keys.push(key);
        self.component(Shape::Line { p1: start, p2: end });
    }

    fn add_source_line(&mut self, value: Option<&Value>) {
        let Some(Value::Map(value)) = value else {
            return;
        };
        let coordinate = |key: &str| value.get(key).and_then(Value::as_python_number);
        match (
            coordinate("start_x"),
            coordinate("start_y"),
            coordinate("end_x"),
            coordinate("end_y"),
        ) {
            (Some(start_x), Some(start_y), Some(end_x), Some(end_y)) => {
                self.add_line(Some([start_x, start_y]), Some([end_x, end_y]));
            }
            _ => self.add_line(point_like(value.get("p1")), point_like(value.get("p2"))),
        }
    }
}

/// ``_generic_dimension_components``: the entities a GENERIC dimension is
/// written as.
fn components(entity: &Entity, context: &mut Context<'_>) -> Result<Vec<Entity>, WriteError> {
    if entity.extra_get("id").is_some() {
        return Err(WriteError::unsupported("dimension id is not a string"));
    }
    let definition = definition_of(entity);
    let field = |key: &str| definition.and_then(|definition| definition.get(key));
    if field("block").is_some_and(Value::truthy) {
        // the drawn geometry of the dimension is in a block
        // (``_generic_dimension_block_components``)
        return Err(WriteError::unsupported(
            "GENERIC dimension with a geometry block",
        ));
    }
    let extra = {
        let mut extra = Map::new();
        for key in ["linetype_scale", "visible"] {
            if let Some(value) = entity.extra_get(key) {
                extra.insert(key.to_string(), value.clone());
            }
        }
        (!extra.is_empty()).then(|| Box::new(extra))
    };
    let mut builder = Builder {
        entity,
        extra,
        components: Vec::new(),
        line_keys: Vec::new(),
    };

    if let Some(Value::Map(source)) = field("source_geometry") {
        // The auxiliary slots of a JWW dimension only mean something in SXF
        // mode. In mode 0 they are unused storage.
        let unused_auxiliaries = match source.get("sxf_mode") {
            Some(Value::Int(mode)) => *mode == 0,
            Some(Value::Float(mode)) => *mode == 0.0,
            Some(Value::Bool(mode)) => !*mode,
            _ => false,
        };
        builder.add_source_line(source.get("line"));
        if !unused_auxiliaries {
            if let Some(Value::List(aux_lines)) = source.get("aux_lines") {
                for value in aux_lines {
                    builder.add_source_line(Some(value));
                }
            }
        }

        if let Some(Value::Map(text)) = source.get("text") {
            let content = str_or(text, "content", field("text"))?;
            let insert = [
                float_or(text, "start_x", 0.0)?,
                float_or(text, "start_y", 0.0)?,
            ];
            let mut height = float_or(text, "size_y", 2.5)?;
            if height <= 0.0 {
                height = 2.5;
            }
            // JWW text angles are in degrees
            let rotation = text
                .get("angle")
                .and_then(Value::as_python_number)
                .filter(|angle| *angle != 0.0);
            let font = py_strip(str_or(text, "font_name", None)?);
            let width_factor = text
                .get("size_x")
                .and_then(Value::as_python_number)
                .filter(|size_x| *size_x > 0.0)
                .map(|size_x| size_x / height);
            builder.component(Shape::Text(Box::new(TextData {
                insert,
                height,
                text: content.to_string(),
                rotation,
                style: (!font.is_empty()).then(|| Arc::from(font)),
                width_factor,
            })));
        }

        if !unused_auxiliaries {
            if let Some(Value::List(aux_points)) = source.get("aux_points") {
                for point in aux_points {
                    let Value::Map(point) = point else { continue };
                    let x = point.get("x").and_then(Value::as_python_number);
                    let y = point.get("y").and_then(Value::as_python_number);
                    if let (Some(x), Some(y)) = (x, y) {
                        builder.component(Shape::Point {
                            position: [x, y],
                            temporary: None,
                            marker_code: None,
                        });
                    }
                }
            }
        }
    }

    if let Some(Value::List(paths)) = field("rendered_paths") {
        for path in paths {
            let Value::Map(path) = path else { continue };
            let Some(Value::List(points)) = path.get("points") else {
                continue;
            };
            if points.len() < 2 {
                continue;
            }
            if points.len() == 2 {
                builder.add_line(point_like(Some(&points[0])), point_like(Some(&points[1])));
                continue;
            }
            let vertices: Option<Vec<Vertex>> = points
                .iter()
                .map(|point| point_like(Some(point)).map(|[x, y]| Vertex { x, y, bulge: None }))
                .collect();
            if let Some(vertices) = vertices {
                builder.component(Shape::Lwpolyline(Box::new(PolylineData {
                    vertices,
                    closed: Some(path.get("closed").is_some_and(Value::truthy)),
                })));
            }
        }
    }

    if let Some(Value::List(texts)) = field("rendered_texts") {
        for rendered in texts {
            let Value::Map(rendered) = rendered else {
                continue;
            };
            let anchor = point_like(rendered.get("anchor"));
            let height = rendered.get("height").and_then(Value::as_python_number);
            let (Some(anchor), Some(height)) = (anchor, height) else {
                continue;
            };
            let content = str_or(rendered, "text", None)?;
            let rotation = rendered
                .get("angle_deg")
                .and_then(Value::as_python_number)
                .filter(|angle| *angle != 0.0);
            builder.component(Shape::Text(Box::new(TextData {
                insert: anchor,
                // ``max(float(height), 1e-9)``
                height: if 1e-9 > height { 1e-9 } else { height },
                text: content.to_string(),
                rotation,
                style: None,
                width_factor: None,
            })));
        }
    }

    let points = field("points").and_then(Value::as_map);
    let point = |key: &str| points.and_then(|points| points.get(key));
    if builder.line_keys.is_empty() {
        builder.add_line(point_like(point("p1")), point_like(point("p2")));
    }

    let has_text = builder
        .components
        .iter()
        .any(|component| matches!(component.shape, Shape::Text(_)));
    match field("text") {
        None | Some(Value::Null) => {}
        Some(_) if has_text => {}
        Some(Value::Str(content)) => {
            let placed = point("text_midpoint").or_else(|| point("location"));
            let insert = match point_like(placed).or_else(|| point_like(field("location"))) {
                Some(insert) => insert,
                None => {
                    let entity_id = entity.id.as_deref().filter(|id| !id.is_empty());
                    context.diagnose(
                        "DXF_GENERIC_DIMENSION_DEFAULTED",
                        "warning",
                        format!(
                            "[DIMENSION:{}] GENERIC dimension missing text placement was \
                             defaulted to [0, 0].",
                            entity_id.unwrap_or("?")
                        ),
                        entity_id.map(str::to_string),
                        Some("normalized"),
                    );
                    [0.0, 0.0]
                }
            };
            let height = match field("text_height") {
                None => 2.5,
                Some(value) => match value.as_python_number() {
                    // ``float(height) <= 0.0`` is the default; a NaN is not below zero
                    Some(height) if height > 0.0 || height.is_nan() => height,
                    _ => 2.5,
                },
            };
            builder.component(Shape::Text(Box::new(TextData {
                insert,
                height,
                text: content.clone(),
                rotation: None,
                style: None,
                width_factor: None,
            })));
        }
        Some(_) => return Err(WriteError::unsupported("dimension text is not a string")),
    }
    Ok(builder.components)
}

/// The GENERIC branch of ``_render_entity``: the components of the dimension,
/// with the diagnostic that says so, or nothing when it has none.
pub(crate) fn exploded(
    entity: &Entity,
    context: &mut Context<'_>,
) -> Result<Vec<Entity>, WriteError> {
    let components = components(entity, context)?;
    let entity_id = entity.id.as_deref().filter(|id| !id.is_empty());
    if components.is_empty() {
        context.diagnose(
            "DXF_GENERIC_DIMENSION_GEOMETRY_MISSING",
            "warning",
            format!(
                "[DIMENSION:{}] GENERIC dimension had no renderable primitive geometry and \
                 was omitted.",
                entity_id.unwrap_or("?")
            ),
            entity_id.map(str::to_string),
            Some("skipped"),
        );
        return Ok(components);
    }
    context.diagnose(
        "DXF_GENERIC_DIMENSION_EXPLODED",
        "info",
        format!(
            "[DIMENSION:{}] GENERIC dimension was expanded into {} primitive(s).",
            entity_id.unwrap_or("?"),
            components.len()
        ),
        entity_id.map(str::to_string),
        Some("exploded"),
    );
    Ok(components)
}
