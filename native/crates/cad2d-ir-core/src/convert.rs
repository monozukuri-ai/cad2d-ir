//! Between the dynamic form of a document (what a Python ``dict`` holds) and the
//! typed one. A value goes into a typed field when it has the type of that
//! field; everything else is kept as it came, so the way back gives an equal
//! document.

use indexmap::IndexMap;

use crate::model::{
    Block, Color, Document, EllipseData, Entity, HatchData, HatchLoop, InsertData, InsertScale,
    Interner, Point2, PolylineData, Shape, Tables, TextData, Vertex,
};
use crate::value::{Map, Value};

fn point_of(value: Option<&Value>) -> Option<Point2> {
    let Value::List(items) = value? else {
        return None;
    };
    if items.len() != 2 {
        return None;
    }
    Some([items[0].as_number()?, items[1].as_number()?])
}

fn vertex_of(value: &Value) -> Option<Vertex> {
    let Value::List(items) = value else {
        return None;
    };
    match items.len() {
        2 => Some(Vertex {
            x: items[0].as_number()?,
            y: items[1].as_number()?,
            bulge: None,
        }),
        3 => Some(Vertex {
            x: items[0].as_number()?,
            y: items[1].as_number()?,
            bulge: Some(items[2].as_number()?),
        }),
        _ => None,
    }
}

fn vertices_of(value: Option<&Value>) -> Option<Vec<Vertex>> {
    let Value::List(items) = value? else {
        return None;
    };
    items.iter().map(vertex_of).collect()
}

fn number_of(value: Option<&Value>) -> Option<f64> {
    value?.as_number()
}

fn take_number(map: &mut Map, key: &str) -> Option<f64> {
    let number = number_of(map.get(key))?;
    map.shift_remove(key);
    Some(number)
}

fn take_bool(map: &mut Map, key: &str) -> Option<bool> {
    let Some(Value::Bool(flag)) = map.get(key) else {
        return None;
    };
    let flag = *flag;
    map.shift_remove(key);
    Some(flag)
}

fn take_str(map: &mut Map, key: &str) -> Option<String> {
    if !matches!(map.get(key), Some(Value::Str(_))) {
        return None;
    }
    match map.shift_remove(key) {
        Some(Value::Str(text)) => Some(text),
        _ => None,
    }
}

fn remove_all(map: &mut Map, keys: &[&str]) {
    for key in keys {
        map.shift_remove(*key);
    }
}

fn shape_from_map(kind: &str, map: &mut Map, interner: &mut Interner) -> Shape {
    match kind {
        "LINE" => {
            let (Some(p1), Some(p2)) = (point_of(map.get("p1")), point_of(map.get("p2"))) else {
                return Shape::Other;
            };
            remove_all(map, &["kind", "p1", "p2"]);
            Shape::Line { p1, p2 }
        }
        "CIRCLE" => {
            let (Some(center), Some(radius)) =
                (point_of(map.get("center")), number_of(map.get("radius")))
            else {
                return Shape::Other;
            };
            remove_all(map, &["kind", "center", "radius"]);
            Shape::Circle { center, radius }
        }
        "ARC" => {
            let (Some(center), Some(radius), Some(start_angle), Some(end_angle)) = (
                point_of(map.get("center")),
                number_of(map.get("radius")),
                number_of(map.get("start_angle")),
                number_of(map.get("end_angle")),
            ) else {
                return Shape::Other;
            };
            remove_all(
                map,
                &["kind", "center", "radius", "start_angle", "end_angle"],
            );
            Shape::Arc {
                center,
                radius,
                start_angle,
                end_angle,
                ccw: take_bool(map, "ccw"),
            }
        }
        "POINT" => {
            let Some(position) = point_of(map.get("position")) else {
                return Shape::Other;
            };
            remove_all(map, &["kind", "position"]);
            let marker_code = match map.get("marker_code") {
                Some(Value::Int(code)) => {
                    let code = *code;
                    map.shift_remove("marker_code");
                    Some(code)
                }
                _ => None,
            };
            Shape::Point {
                position,
                temporary: take_bool(map, "temporary"),
                marker_code,
            }
        }
        "ELLIPSE" => {
            let (Some(center), Some(major_axis), Some(ratio), Some(start_param), Some(end_param)) = (
                point_of(map.get("center")),
                point_of(map.get("major_axis")),
                number_of(map.get("ratio")),
                number_of(map.get("start_param")),
                number_of(map.get("end_param")),
            ) else {
                return Shape::Other;
            };
            remove_all(
                map,
                &[
                    "kind",
                    "center",
                    "major_axis",
                    "ratio",
                    "start_param",
                    "end_param",
                ],
            );
            Shape::Ellipse(Box::new(EllipseData {
                center,
                major_axis,
                ratio,
                start_param,
                end_param,
                ccw: take_bool(map, "ccw"),
            }))
        }
        "TEXT" => {
            let (Some(insert), Some(height), Some(Value::Str(_))) = (
                point_of(map.get("insert")),
                number_of(map.get("height")),
                map.get("text"),
            ) else {
                return Shape::Other;
            };
            let text = take_str(map, "text").expect("checked above");
            remove_all(map, &["kind", "insert", "height"]);
            Shape::Text(Box::new(TextData {
                insert,
                height,
                text,
                rotation: take_number(map, "rotation"),
                style: take_str(map, "style").map(|style| interner.intern(&style)),
                width_factor: take_number(map, "width_factor"),
            }))
        }
        "INSERT" => {
            let (Some(insert), Some(Value::Str(_))) =
                (point_of(map.get("insert")), map.get("block"))
            else {
                return Shape::Other;
            };
            let block = take_str(map, "block").expect("checked above");
            remove_all(map, &["kind", "insert"]);
            let scale = match map.get("scale") {
                Some(Value::List(_)) => point_of(map.get("scale")).map(InsertScale::Axes),
                other => number_of(other).map(InsertScale::Uniform),
            };
            if scale.is_some() {
                map.shift_remove("scale");
            }
            Shape::Insert(Box::new(InsertData {
                block: interner.intern(&block),
                insert,
                rotation: take_number(map, "rotation"),
                scale,
            }))
        }
        "HATCH" => {
            let Some(Value::List(raw_loops)) = map.get("loops") else {
                return Shape::Other;
            };
            let mut loops: Vec<HatchLoop> = Vec::with_capacity(raw_loops.len());
            for raw in raw_loops {
                let Value::Map(raw) = raw else {
                    return Shape::Other;
                };
                let Some(vertices) = vertices_of(raw.get("vertices")) else {
                    return Shape::Other;
                };
                let mut rest = raw.clone();
                rest.shift_remove("vertices");
                loops.push(HatchLoop {
                    vertices,
                    extra: if rest.is_empty() {
                        None
                    } else {
                        Some(Box::new(rest))
                    },
                });
            }
            remove_all(map, &["kind", "loops"]);
            Shape::Hatch(Box::new(HatchData {
                loops,
                solid: take_bool(map, "solid"),
            }))
        }
        "LWPOLYLINE" => {
            let Some(vertices) = vertices_of(map.get("vertices")) else {
                return Shape::Other;
            };
            remove_all(map, &["kind", "vertices"]);
            Shape::Lwpolyline(Box::new(PolylineData {
                vertices,
                closed: take_bool(map, "closed"),
            }))
        }
        _ => Shape::Other,
    }
}

/// The typed entity of an entity ``dict``.
pub fn entity_from_map(mut map: Map, interner: &mut Interner) -> Entity {
    let id = take_str(&mut map, "id").map(String::into_boxed_str);
    let layer = take_str(&mut map, "layer").map(|layer| interner.intern(&layer));
    let linetype = take_str(&mut map, "linetype").map(|linetype| interner.intern(&linetype));
    let color = match map.get("color") {
        Some(Value::Int(index)) => {
            let index = *index;
            map.shift_remove("color");
            Some(Color::Index(index))
        }
        Some(Value::Str(_)) => {
            take_str(&mut map, "color").map(|text| Color::Text(interner.intern(&text)))
        }
        _ => None,
    };
    let lineweight_mm = take_number(&mut map, "lineweight_mm");
    let kind = match map.get("kind") {
        Some(Value::Str(kind)) => Some(kind.clone()),
        _ => None,
    };
    let shape = match kind {
        Some(kind) => shape_from_map(&kind, &mut map, interner),
        None => Shape::Other,
    };
    Entity {
        id,
        layer,
        linetype,
        color,
        lineweight_mm,
        shape,
        extra: if map.is_empty() {
            None
        } else {
            Some(Box::new(map))
        },
    }
}

fn point_value(point: Point2) -> Value {
    Value::List(vec![Value::Float(point[0]), Value::Float(point[1])])
}

fn vertex_value(vertex: &Vertex) -> Value {
    let mut items = vec![Value::Float(vertex.x), Value::Float(vertex.y)];
    if let Some(bulge) = vertex.bulge {
        items.push(Value::Float(bulge));
    }
    Value::List(items)
}

fn put(map: &mut Map, key: &str, value: Value) {
    map.insert(key.to_string(), value);
}

/// The entity ``dict`` of a typed entity.
pub fn entity_to_map(entity: &Entity) -> Map {
    let mut map = entity_typed_map(entity);
    if let Some(extra) = &entity.extra {
        for (key, value) in extra.iter() {
            map.insert(key.clone(), value.clone());
        }
    }
    map
}

/// The keys of an entity ``dict`` that have a typed field, without the keys of
/// ``Entity::extra``. A caller that makes the ``dict`` in another form adds
/// those itself, without a copy of each value in between (the text of an
/// embedded image is a value of ``extra``, and can be very large).
pub fn entity_typed_map(entity: &Entity) -> Map {
    let mut map = Map::new();
    if let Some(id) = &entity.id {
        put(&mut map, "id", Value::Str(id.to_string()));
    }
    if let Some(kind) = entity.shape.kind() {
        put(&mut map, "kind", Value::Str(kind.to_string()));
    }
    if let Some(layer) = &entity.layer {
        put(&mut map, "layer", Value::Str(layer.to_string()));
    }
    if let Some(linetype) = &entity.linetype {
        put(&mut map, "linetype", Value::Str(linetype.to_string()));
    }
    match &entity.color {
        Some(Color::Index(index)) => put(&mut map, "color", Value::Int(*index)),
        Some(Color::Text(text)) => put(&mut map, "color", Value::Str(text.to_string())),
        None => {}
    }
    if let Some(lineweight) = entity.lineweight_mm {
        put(&mut map, "lineweight_mm", Value::Float(lineweight));
    }
    match &entity.shape {
        Shape::Line { p1, p2 } => {
            put(&mut map, "p1", point_value(*p1));
            put(&mut map, "p2", point_value(*p2));
        }
        Shape::Circle { center, radius } => {
            put(&mut map, "center", point_value(*center));
            put(&mut map, "radius", Value::Float(*radius));
        }
        Shape::Arc {
            center,
            radius,
            start_angle,
            end_angle,
            ccw,
        } => {
            put(&mut map, "center", point_value(*center));
            put(&mut map, "radius", Value::Float(*radius));
            put(&mut map, "start_angle", Value::Float(*start_angle));
            put(&mut map, "end_angle", Value::Float(*end_angle));
            if let Some(ccw) = ccw {
                put(&mut map, "ccw", Value::Bool(*ccw));
            }
        }
        Shape::Point {
            position,
            temporary,
            marker_code,
        } => {
            put(&mut map, "position", point_value(*position));
            if let Some(temporary) = temporary {
                put(&mut map, "temporary", Value::Bool(*temporary));
            }
            if let Some(code) = marker_code {
                put(&mut map, "marker_code", Value::Int(*code));
            }
        }
        Shape::Ellipse(ellipse) => {
            put(&mut map, "center", point_value(ellipse.center));
            put(&mut map, "major_axis", point_value(ellipse.major_axis));
            put(&mut map, "ratio", Value::Float(ellipse.ratio));
            put(&mut map, "start_param", Value::Float(ellipse.start_param));
            put(&mut map, "end_param", Value::Float(ellipse.end_param));
            if let Some(ccw) = ellipse.ccw {
                put(&mut map, "ccw", Value::Bool(ccw));
            }
        }
        Shape::Text(text) => {
            put(&mut map, "insert", point_value(text.insert));
            put(&mut map, "height", Value::Float(text.height));
            put(&mut map, "text", Value::Str(text.text.clone()));
            if let Some(rotation) = text.rotation {
                put(&mut map, "rotation", Value::Float(rotation));
            }
            if let Some(style) = &text.style {
                put(&mut map, "style", Value::Str(style.to_string()));
            }
            if let Some(width_factor) = text.width_factor {
                put(&mut map, "width_factor", Value::Float(width_factor));
            }
        }
        Shape::Insert(insert) => {
            put(&mut map, "block", Value::Str(insert.block.to_string()));
            put(&mut map, "insert", point_value(insert.insert));
            if let Some(rotation) = insert.rotation {
                put(&mut map, "rotation", Value::Float(rotation));
            }
            match insert.scale {
                Some(InsertScale::Uniform(scale)) => put(&mut map, "scale", Value::Float(scale)),
                Some(InsertScale::Axes(scale)) => put(&mut map, "scale", point_value(scale)),
                None => {}
            }
        }
        Shape::Hatch(hatch) => {
            let loops = hatch
                .loops
                .iter()
                .map(|hatch_loop| {
                    let mut raw = Map::new();
                    put(
                        &mut raw,
                        "vertices",
                        Value::List(hatch_loop.vertices.iter().map(vertex_value).collect()),
                    );
                    if let Some(extra) = &hatch_loop.extra {
                        for (key, value) in extra.iter() {
                            raw.insert(key.clone(), value.clone());
                        }
                    }
                    Value::Map(raw)
                })
                .collect();
            put(&mut map, "loops", Value::List(loops));
            if let Some(solid) = hatch.solid {
                put(&mut map, "solid", Value::Bool(solid));
            }
        }
        Shape::Lwpolyline(polyline) => {
            put(
                &mut map,
                "vertices",
                Value::List(polyline.vertices.iter().map(vertex_value).collect()),
            );
            if let Some(closed) = polyline.closed {
                put(&mut map, "closed", Value::Bool(closed));
            }
        }
        Shape::Other => {}
    }
    map
}

/// The entities of a list of entity ``dict``s, or ``None`` when an item is no ``dict``.
fn entities_from_value(value: Value, interner: &mut Interner) -> Result<Vec<Entity>, Value> {
    let Value::List(items) = value else {
        return Err(value);
    };
    if !items.iter().all(|item| matches!(item, Value::Map(_))) {
        return Err(Value::List(items));
    }
    Ok(items
        .into_iter()
        .map(|item| match item {
            Value::Map(map) => entity_from_map(map, interner),
            _ => unreachable!("checked above"),
        })
        .collect())
}

fn block_from_map(mut map: Map, interner: &mut Interner) -> Block {
    let base_point = point_of(map.get("base_point"));
    if base_point.is_some() {
        map.shift_remove("base_point");
    }
    let mut block = Block {
        base_point,
        ..Block::default()
    };
    if let Some(raw) = map.shift_remove("entities") {
        match entities_from_value(raw, interner) {
            Ok(entities) => {
                block.entities = entities;
                block.has_entities = true;
            }
            Err(raw) => {
                map.insert("entities".to_string(), raw);
            }
        }
    }
    block.extra = map;
    block
}

fn tables_from_map(mut map: Map, interner: &mut Interner) -> Tables {
    let mut tables = Tables {
        layers: map.shift_remove("layers"),
        linetypes: map.shift_remove("linetypes"),
        text_styles: map.shift_remove("text_styles"),
        ..Tables::default()
    };
    match map.shift_remove("blocks") {
        Some(Value::Map(raw)) if raw.values().all(|value| matches!(value, Value::Map(_))) => {
            let mut blocks = IndexMap::with_capacity(raw.len());
            for (name, value) in raw {
                if let Value::Map(block) = value {
                    blocks.insert(name, block_from_map(block, interner));
                }
            }
            tables.blocks = Some(blocks);
        }
        Some(other) => {
            map.insert("blocks".to_string(), other);
        }
        None => {}
    }
    tables.extra = map;
    tables
}

/// The typed document of a document ``dict``.
pub fn document_from_map(mut map: Map) -> Document {
    let mut interner = Interner::new();
    let mut document = Document::default();
    match map.shift_remove("header") {
        Some(Value::Map(header)) => document.header = Some(header),
        Some(other) => {
            map.insert("header".to_string(), other);
        }
        None => {}
    }
    match map.shift_remove("tables") {
        Some(Value::Map(tables)) => document.tables = Some(tables_from_map(tables, &mut interner)),
        Some(other) => {
            map.insert("tables".to_string(), other);
        }
        None => {}
    }
    if let Some(raw) = map.shift_remove("entities") {
        match entities_from_value(raw, &mut interner) {
            Ok(entities) => {
                document.entities = entities;
                document.has_entities = true;
            }
            Err(raw) => {
                map.insert("entities".to_string(), raw);
            }
        }
    }
    document.extra = map;
    document
}

fn entities_value(entities: &[Entity]) -> Value {
    Value::List(
        entities
            .iter()
            .map(|entity| Value::Map(entity_to_map(entity)))
            .collect(),
    )
}

/// The document ``dict`` of a typed document.
pub fn document_to_map(document: &Document) -> Map {
    let mut map = Map::new();
    for key in ["format", "version"] {
        if let Some(value) = document.extra.get(key) {
            map.insert(key.to_string(), value.clone());
        }
    }
    if let Some(header) = &document.header {
        put(&mut map, "header", Value::Map(header.clone()));
    }
    if let Some(value) = document.extra.get("source") {
        put(&mut map, "source", value.clone());
    }
    if let Some(tables) = &document.tables {
        let mut raw = Map::new();
        if let Some(layers) = &tables.layers {
            put(&mut raw, "layers", layers.clone());
        }
        if let Some(linetypes) = &tables.linetypes {
            put(&mut raw, "linetypes", linetypes.clone());
        }
        if let Some(text_styles) = &tables.text_styles {
            put(&mut raw, "text_styles", text_styles.clone());
        }
        if let Some(blocks) = &tables.blocks {
            let mut raw_blocks = Map::new();
            for (name, block) in blocks {
                let mut raw_block = Map::new();
                if let Some(base_point) = block.base_point {
                    put(&mut raw_block, "base_point", point_value(base_point));
                }
                if block.has_entities {
                    put(&mut raw_block, "entities", entities_value(&block.entities));
                }
                for (key, value) in &block.extra {
                    raw_block.insert(key.clone(), value.clone());
                }
                raw_blocks.insert(name.clone(), Value::Map(raw_block));
            }
            put(&mut raw, "blocks", Value::Map(raw_blocks));
        }
        for (key, value) in &tables.extra {
            raw.insert(key.clone(), value.clone());
        }
        put(&mut map, "tables", Value::Map(raw));
    }
    if document.has_entities {
        put(&mut map, "entities", entities_value(&document.entities));
    }
    for (key, value) in &document.extra {
        if !map.contains_key(key) {
            map.insert(key.clone(), value.clone());
        }
    }
    map
}
