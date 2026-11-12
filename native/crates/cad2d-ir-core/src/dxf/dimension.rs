//! Dimensions.
//!
//! A dimension of a known kind is a ``DIMENSION`` record that names the block
//! its geometry is drawn in (``_dimension_to_pairs``); a block is made for one
//! that has none (``_ensure_dimension_geometry_blocks``). A dimension whose kind
//! the importer could not tell (``GENERIC``) is written as the lines, texts and
//! points it was made of (``_generic_dimension_components`` and the GENERIC
//! branch of ``_render_entity``).

use std::collections::{HashMap, HashSet};
use std::sync::Arc;

use indexmap::IndexMap;

use super::common::{entity_id, label_id, point_like, py_str, Common};
use super::entity::{begin, Mode};
use super::layout::LayoutScope;
use super::pairs::{Pair, Val};
use super::{Context, Env, ExportDiagnostic, WriteError};
use crate::model::{Block, Document, Entity, PolylineData, Shape, TextData, Vertex};
use crate::num::num;
use crate::pystr::{repr, strip, upper};
use crate::value::{Map, Value};

/// Whether ``entity`` is a ``DIMENSION``.
fn is_dimension(entity: &Entity) -> bool {
    matches!(entity.shape, Shape::Other) && entity.kind() == Some("DIMENSION")
}

/// Whether ``entity`` is a ``DIMENSION`` with ``dim_kind == "GENERIC"``.
pub(crate) fn is_generic(entity: &Entity) -> bool {
    is_dimension(entity)
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
    if let Some(font) = font_name {
        // ``str(text_geometry.get("font_name", "")).strip()``
        let font = py_str(font, "dimension font name")?;
        let font = strip(&font);
        if !font.is_empty() {
            styles.push(font.to_string());
        }
    }
    Ok(styles)
}

/// ``float(mapping.get(key, default))`` for a value that is a number or absent.
fn float_or(mapping: &Map, key: &str, default: f64) -> Result<f64, WriteError> {
    super::common::float_or(mapping.get(key), default, "a dimension field")
}

/// ``str(mapping.get(key, default))``.
fn str_or<'a>(
    mapping: &'a Map,
    key: &str,
    default: Option<&'a Value>,
) -> Result<std::borrow::Cow<'a, str>, WriteError> {
    match mapping.get(key).or(default) {
        None => Ok(std::borrow::Cow::Borrowed("")),
        Some(value) => py_str(value, "a dimension text"),
    }
}

// ---------------------------------------------------------------------------
// Geometry blocks of dimensions that have none
// ---------------------------------------------------------------------------

/// The blocks made for the dimensions of a document that name no block, or one
/// the document does not have. The document is left as it is: the name a
/// dimension gets is kept beside it, by the address of the entity.
#[derive(Default)]
pub(crate) struct GeometryBlocks {
    names: HashMap<usize, Arc<str>>,
    /// The generated blocks in the order they were made.
    pub(crate) blocks: IndexMap<String, Block>,
}

impl GeometryBlocks {
    /// The block made for ``entity``.
    pub(crate) fn name_of(&self, entity: &Entity) -> Option<&Arc<str>> {
        self.names.get(&(entity as *const Entity as usize))
    }
}

/// The keys ``_entity_common_values`` copies, without the id.
fn common_copy(dimension: &Entity) -> Entity {
    let mut entity = Entity::new(Shape::Other);
    entity.layer = dimension.layer.clone();
    entity.linetype = dimension.linetype.clone();
    entity.color = dimension.color.clone();
    entity.lineweight_mm = dimension.lineweight_mm;
    for key in [
        "layer",
        "linetype",
        "linetype_scale",
        "color",
        "lineweight_mm",
        "visible",
    ] {
        if let Some(value) = dimension.extra_get(key) {
            entity.extra_insert(key, value.clone());
        }
    }
    entity
}

/// ``_dimension_geometry_entities``: what stands for a dimension in its block.
fn geometry_entities(dimension: &Entity, serial: i64) -> Result<Vec<Entity>, WriteError> {
    let definition = definition_of(dimension);
    let field = |key: &str| definition.and_then(|definition| definition.get(key));
    let points = field("points").and_then(Value::as_map);
    let point = |key: &str| points.and_then(|points| points.get(key));
    let make = |suffix: &str, shape: Shape| {
        let mut entity = common_copy(dimension);
        entity.id = Some(format!("DGEOM_{serial}_{suffix}").into_boxed_str());
        entity.shape = shape;
        entity
    };

    let mut entities = Vec::new();
    let p1 = point_like(point("p1"));
    let p2 = point_like(point("p2"));
    if let (Some(p1), Some(p2)) = (p1, p2) {
        entities.push(make("L", Shape::Line { p1, p2 }));
    }

    if let Some(text_value) = field("text").filter(|value| !matches!(value, Value::Null)) {
        let placed = point("text_midpoint").or_else(|| point("location"));
        let insert = match (point_like(placed), p1, p2) {
            (Some(insert), _, _) => insert,
            (None, Some(p1), Some(p2)) => [(p1[0] + p2[0]) / 2.0, (p1[1] + p2[1]) / 2.0],
            _ => [0.0, 0.0],
        };
        let height = match field("text_height") {
            None => 2.5,
            Some(value) => match value.as_python_number() {
                // ``float(height) <= 0.0`` is the default; a NaN is not below zero
                Some(height) if height > 0.0 || height.is_nan() => height,
                _ => 2.5,
            },
        };
        let style = match dimension.extra_get("style") {
            Some(Value::Str(style)) => Some(Arc::from(style.as_str())),
            _ => None,
        };
        entities.push(make(
            "T",
            Shape::Text(Box::new(TextData {
                insert,
                height,
                text: py_str(text_value, "dimension text")?.into_owned(),
                rotation: None,
                style,
                width_factor: None,
            })),
        ));
    }

    if entities.is_empty() {
        let location = point_like(points.map_or_else(
            || field("location"),
            |points| points.get("location").or_else(|| field("location")),
        ))
        .unwrap_or([0.0, 0.0]);
        entities.push(make(
            "P",
            Shape::Point {
                position: location,
                temporary: None,
                marker_code: None,
            },
        ));
    }
    Ok(entities)
}

/// ``_dimension_geometry_blocks_are_missing`` and
/// ``_ensure_dimension_geometry_blocks``: a block for every dimension of a known
/// kind that names none the document has.
pub(crate) fn geometry_blocks(
    document: &Document,
    layouts: &[LayoutScope<'_>],
    diagnostics: &mut Vec<ExportDiagnostic>,
) -> Result<GeometryBlocks, WriteError> {
    let blocks = document
        .tables
        .as_ref()
        .and_then(|tables| tables.blocks.as_ref());
    let mut used_names: HashSet<String> = blocks
        .into_iter()
        .flat_map(|blocks| blocks.keys())
        .map(|name| upper(name))
        .collect();

    // the entity lists in the order the Python writer walks them
    let mut scopes: Vec<&[Entity]> = vec![&document.entities];
    scopes.extend(
        blocks
            .into_iter()
            .flat_map(|blocks| blocks.values())
            .map(|block| block.entities.as_slice()),
    );
    scopes.extend(
        layouts
            .iter()
            .filter_map(|layout| layout.entities.as_deref()),
    );
    let dimensions = || {
        scopes
            .iter()
            .flat_map(|entities| entities.iter())
            .filter(|entity| is_dimension(entity) && !is_generic(entity))
    };

    let mut missing = false;
    for entity in dimensions() {
        let block_name = match definition_of(entity).and_then(|definition| definition.get("block"))
        {
            None => std::borrow::Cow::Borrowed(""),
            Some(value) => py_str(value, "dimension block")?,
        };
        if block_name.is_empty() || !used_names.contains(&upper(&block_name)) {
            missing = true;
            break;
        }
    }
    let mut generated = GeometryBlocks::default();
    if !missing {
        return Ok(generated);
    }

    let mut next_number: i64 = 0;
    for entity in dimensions() {
        let raw_name = definition_of(entity).and_then(|definition| definition.get("block"));
        let mut block_name = match raw_name {
            Some(value) if value.truthy() => py_str(value, "dimension block")?.into_owned(),
            _ => String::new(),
        };
        if !block_name.is_empty() && used_names.contains(&upper(&block_name)) {
            continue;
        }
        if block_name.is_empty() {
            while used_names.contains(&upper(&format!("*D{next_number}"))) {
                next_number += 1;
            }
            block_name = format!("*D{next_number}");
            next_number += 1;
        }
        let id = entity_id(entity)?;
        let generated_for = if id.is_empty() {
            Value::Null
        } else {
            Value::Str(id.to_string())
        };
        let mut cad2d_ir = Map::new();
        cad2d_ir.insert("generated_for_entity_id".to_string(), generated_for);
        let mut metadata = Map::new();
        metadata.insert("cad2d_ir".to_string(), Value::Map(cad2d_ir));
        let mut extra = Map::new();
        extra.insert("metadata".to_string(), Value::Map(metadata));
        generated.blocks.insert(
            block_name.clone(),
            Block {
                base_point: Some([0.0, 0.0]),
                entities: geometry_entities(entity, next_number)?,
                has_entities: true,
                extra,
            },
        );
        used_names.insert(upper(&block_name));
        diagnostics.push(ExportDiagnostic {
            code: "DXF_DIMENSION_BLOCK_GENERATED",
            severity: "info",
            message: format!(
                "[DIMENSION:{}] geometry block {} was generated for DXF export.",
                label_id(&id),
                repr(&block_name)
            ),
            entity_id: (!id.is_empty()).then(|| id.to_string()),
            action: Some("normalized"),
        });
        generated.names.insert(
            entity as *const Entity as usize,
            Arc::from(block_name.as_str()),
        );
    }
    Ok(generated)
}

// ---------------------------------------------------------------------------
// The DIMENSION record
// ---------------------------------------------------------------------------

/// ``_dimension_point``: a point of a dimension by its name or the name ezdwg
/// gives it (after its DXF group).
fn dimension_point(
    points: Option<&Map>,
    definition: Option<&Map>,
    key: &str,
) -> Result<Option<[f64; 2]>, WriteError> {
    let alias = match key {
        "location" => Some("defpoint"),
        "p1" => Some("defpoint2"),
        "p2" => Some("defpoint3"),
        "p3" => Some("defpoint4"),
        "p4" => Some("defpoint5"),
        _ => None,
    };
    for name in std::iter::once(key).chain(alias) {
        // ``points.get(name, definition.get(name))``
        let candidate = match points.and_then(|points| points.get(name)) {
            Some(value) => Some(value),
            None => definition.and_then(|definition| definition.get(name)),
        };
        if let Some(Value::List(items)) = candidate {
            if items.len() >= 2 {
                return match (items[0].as_python_number(), items[1].as_python_number()) {
                    (Some(x), Some(y)) => Ok(Some([x, y])),
                    _ => Err(WriteError::invalid("a dimension point is not a number")),
                };
            }
        }
    }
    Ok(None)
}

/// ``isinstance(value, (int, float))``.
fn python_number(value: Option<&Value>) -> Option<f64> {
    value.and_then(Value::as_python_number)
}

/// ``_dimension_to_pairs`` with the groups where ``_r2010_record_body`` puts
/// them for DXF R2010.
pub(crate) fn record(
    out: &mut Vec<Pair>,
    entity: &Entity,
    common: &Common,
    mode: Mode,
    env: &Env<'_>,
    context: &mut Context<'_>,
) -> Result<Option<Val>, WriteError> {
    let definition = definition_of(entity);
    let field = |key: &str| definition.and_then(|definition| definition.get(key));
    let dim_kind = match entity.extra_get("dim_kind") {
        None => std::borrow::Cow::Borrowed("LINEAR"),
        Some(value) => py_str(value, "dimension dim_kind")?,
    };
    // the block of the dimension: the one made for it, else the one it names
    let block_name: Option<Val> = match env.generated.name_of(entity) {
        Some(name) => Some(Val::A(Arc::clone(name))),
        None => match field("block") {
            Some(value) if value.truthy() => {
                Some(Val::owned(py_str(value, "dimension block")?.into_owned()))
            }
            _ => None,
        },
    };
    let style = match entity.extra_get("style") {
        None => Val::S("Standard"),
        Some(value) => Val::owned(py_str(value, "dimension style")?.into_owned()),
    };
    let dim_code: i64 = match dim_kind.as_ref() {
        "ALIGNED" => 1,
        "ANGULAR" => 2,
        "DIAMETER" => 3,
        "RADIAL" => 4,
        "ORDINATE" => 6,
        _ => 0,
    };
    let points = field("points").and_then(Value::as_map);
    let location = dimension_point(points, definition, "location")?.unwrap_or([0.0, 0.0]);
    let text_midpoint = dimension_point(points, definition, "text_midpoint")?;

    // the groups of ``AcDbDimension`` before the points of the kind
    let mut head: Vec<Pair> = Vec::with_capacity(12);
    if let Some(block_name) = block_name {
        head.push((2, block_name));
    }
    head.push((3, style));
    head.push((70, Val::integer(dim_code)));
    head.push((10, Val::owned(num(location[0]))));
    head.push((20, Val::owned(num(location[1]))));
    head.push((30, Val::S("0")));
    if let Some(text_midpoint) = text_midpoint {
        head.push((11, Val::owned(num(text_midpoint[0]))));
        head.push((21, Val::owned(num(text_midpoint[1]))));
        head.push((31, Val::S("0")));
    }
    let mut kind_points: Vec<Pair> = Vec::with_capacity(8);
    for (key, x_code, y_code) in [
        ("p1", 13, 23),
        ("p2", 14, 24),
        ("p3", 15, 25),
        ("p4", 16, 26),
    ] {
        if let Some(point) = dimension_point(points, definition, key)? {
            kind_points.push((x_code, Val::owned(num(point[0]))));
            kind_points.push((y_code, Val::owned(num(point[1]))));
        }
    }
    let text: Option<Pair> = match field("text") {
        None | Some(Value::Null) => None,
        Some(value) => Some((1, Val::owned(py_str(value, "dimension text")?.into_owned()))),
    };
    // A dimension read from DWG keeps its rotation with the source fields.
    let mut rotation = field("rotation");
    if python_number(rotation).is_none() && dim_kind == "LINEAR" {
        if let Some(Value::Map(source_geometry)) = field("source_geometry") {
            rotation = source_geometry.get("angle");
        }
    }
    let rotation: Option<Pair> = rotation
        .and_then(Value::as_number)
        .map(|rotation| (50, Val::owned(num(rotation))));
    let text_rotation: Option<Pair> =
        python_number(field("text_rotation")).map(|rotation| (53, Val::owned(num(rotation))));
    let measurement: Option<Pair> =
        python_number(field("measurement")).map(|measurement| (42, Val::owned(num(measurement))));

    let handle = begin(out, "DIMENSION", mode, context);
    common.write(out);
    if !mode.r2010 {
        out.extend(head);
        out.extend(kind_points);
        out.extend(text);
        out.extend(rotation);
        out.extend(text_rotation);
        out.extend(measurement);
        return Ok(handle);
    }
    out.push((100, Val::S("AcDbDimension")));
    out.extend(head);
    out.extend(text);
    out.extend(text_rotation);
    out.extend(measurement);
    if dim_code == 0 {
        // A rotated (linear) dimension is an aligned one with a rotation: its
        // points and angle are in AcDbAlignedDimension.
        out.push((100, Val::S("AcDbAlignedDimension")));
        out.extend(kind_points);
        out.extend(rotation);
        out.push((100, Val::S("AcDbRotatedDimension")));
    } else {
        out.push((
            100,
            Val::S(match dim_code {
                1 => "AcDbAlignedDimension",
                2 => "AcDb2LineAngularDimension",
                3 => "AcDbDiametricDimension",
                4 => "AcDbRadialDimension",
                6 => "AcDbOrdinateDimension",
                _ => "AcDbRotatedDimension",
            }),
        ));
        out.extend(kind_points);
        out.extend(rotation);
    }
    Ok(handle)
}

// ---------------------------------------------------------------------------
// GENERIC dimensions
// ---------------------------------------------------------------------------

/// ``_generic_dimension_block_components``: the drawn geometry of a GENERIC
/// dimension, when it has a block of it.
///
/// A dimension whose kind the importer could not tell still carries the lines,
/// arrows and text the source drew it with (``definition.block``, in drawing
/// coordinates). They are what the dimension looks like, so they are written
/// in place of the bare line and text ``components`` builds. ``None`` when
/// there is no such block.
pub(crate) fn block_components(
    entity: &Entity,
    env: &Env<'_>,
) -> Result<Option<Vec<Entity>>, WriteError> {
    let name = match definition_of(entity).and_then(|definition| definition.get("block")) {
        Some(value) if value.truthy() => py_str(value, "dimension block")?,
        _ => return Ok(None),
    };
    let Some(block) = env.block(&name) else {
        return Ok(None);
    };
    if block.entities.is_empty() {
        return Ok(None);
    }
    // ``block.get("base_point") or [0.0, 0.0]``: at the origin
    let at_origin = match (block.base_point, block.extra.get("base_point")) {
        (Some(base), _) => base[0] == 0.0 && base[1] == 0.0,
        (None, None) => true,
        (None, Some(value)) if !value.truthy() => true,
        (None, Some(value)) => {
            point_like(Some(value)).is_some_and(|base| base[0] == 0.0 && base[1] == 0.0)
        }
    };
    if !at_origin {
        return Ok(None);
    }
    if !matches!(entity.extra_get("layer"), None | Some(Value::Null)) {
        return Err(WriteError::invalid("dimension layer is not a text"));
    }
    let mut components: Vec<Entity> = Vec::with_capacity(block.entities.len());
    for member in &block.entities {
        if matches!(member.extra_get("visible"), Some(Value::Bool(false))) {
            continue;
        }
        let mut component = member.clone();
        // The geometry of a dimension is drawn on layer 0 and follows the layer
        // of the dimension.
        if let Some(layer) = &entity.layer {
            let on_zero = match (&component.layer, component.extra_get("layer")) {
                (Some(own), _) => own.as_ref() == "0",
                (None, None) => true,
                (None, Some(value)) => py_str(value, "entity layer")? == "0",
            };
            if on_zero {
                component.layer = Some(Arc::clone(layer));
                if let Some(extra) = component.extra.as_mut() {
                    extra.shift_remove("layer");
                }
            }
        }
        // a dimension of the block that got a geometry block of its own
        if let Some(generated) = env.generated.name_of(member) {
            let mut definition = definition_of(&component).cloned().unwrap_or_default();
            definition.insert("block".to_string(), Value::Str(generated.to_string()));
            component.extra_insert("definition", Value::Map(definition));
        }
        components.push(component);
    }
    Ok((!components.is_empty()).then_some(components))
}

struct Builder<'a> {
    entity: &'a Entity,
    /// ``linetype_scale`` and ``visible`` of the dimension (and a common field
    /// of another type than the IR has for it), which every component takes
    /// with the typed common fields
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
pub(crate) fn components(
    entity: &Entity,
    context: &mut Context<'_>,
) -> Result<Vec<Entity>, WriteError> {
    let id = entity_id(entity)?;
    let definition = definition_of(entity);
    let field = |key: &str| definition.and_then(|definition| definition.get(key));
    let extra = {
        let mut extra = Map::new();
        for key in [
            "id",
            "layer",
            "linetype",
            "linetype_scale",
            "color",
            "lineweight_mm",
            "visible",
        ] {
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
            let font = str_or(text, "font_name", None)?;
            let font = strip(&font);
            let width_factor = text
                .get("size_x")
                .and_then(Value::as_python_number)
                .filter(|size_x| *size_x > 0.0)
                .map(|size_x| size_x / height);
            builder.component(Shape::Text(Box::new(TextData {
                insert,
                height,
                text: content.into_owned(),
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
                text: content.into_owned(),
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
        Some(content) => {
            let content = py_str(content, "dimension text")?.into_owned();
            let placed = point("text_midpoint").or_else(|| point("location"));
            let insert = match point_like(placed).or_else(|| point_like(field("location"))) {
                Some(insert) => insert,
                None => {
                    context.diagnose(
                        "DXF_GENERIC_DIMENSION_DEFAULTED",
                        "warning",
                        format!(
                            "[DIMENSION:{}] GENERIC dimension missing text placement was \
                             defaulted to [0, 0].",
                            label_id(&id)
                        ),
                        (!id.is_empty()).then(|| id.to_string()),
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
                text: content,
                rotation: None,
                style: None,
                width_factor: None,
            })));
        }
    }
    Ok(builder.components)
}
