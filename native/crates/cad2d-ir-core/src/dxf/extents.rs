//! The extents of model space: ``$EXTMIN`` and ``$EXTMAX`` of the header.
//!
//! A program that opens a DXF takes its first view from them. Jw_cad goes
//! further and chooses the scale of the drawing from them, for the paper it has
//! at that moment; without them it keeps the scale it had, and a drawing in
//! real size lies far outside the paper (with the settings Jw_cad is installed
//! with, a paper of A1 at 1:1, nothing of the drawing is on the screen).
//!
//! The extents are those of the geometry the entities state: the points of
//! lines, polylines and hatch loops with the arcs between them, circles, arcs
//! and ellipses, the boxes of images, the blocks of block references and of
//! dimensions, and for a text the box its height and the number of its
//! characters give (a text has no width without its font).

use std::collections::HashMap;

use indexmap::IndexMap;

use super::common::point_like;
use super::dimension::GeometryBlocks;
use crate::model::{
    Block, Document, EllipseData, Entity, InsertData, InsertScale, Shape, TextData, Vertex,
};
use crate::value::{Map, Value};

/// Block references nest this deep at most; a deeper one is left out.
const MAX_DEPTH: usize = 16;

/// The width of a character in text heights: a wide one (kanji, kana) and
/// another one. A text is at least this wide in most fonts.
const WIDE_CHARACTER: f64 = 1.0;
const NARROW_CHARACTER: f64 = 0.6;

/// The distance between the lines of an MTEXT in text heights.
const LINE_SPACING: f64 = 1.6;

/// ``[min_x, min_y, max_x, max_y]`` as it grows.
#[derive(Default)]
struct Extents {
    bounds: Option<[f64; 4]>,
}

impl Extents {
    fn add(&mut self, [x, y]: [f64; 2]) {
        if !(x.is_finite() && y.is_finite()) {
            return;
        }
        match self.bounds.as_mut() {
            None => self.bounds = Some([x, y, x, y]),
            Some(bounds) => {
                bounds[0] = bounds[0].min(x);
                bounds[1] = bounds[1].min(y);
                bounds[2] = bounds[2].max(x);
                bounds[3] = bounds[3].max(y);
            }
        }
    }

    /// The part of a circle from ``start`` over ``sweep`` degrees, counter-
    /// clockwise: its two ends and the points where it is farthest in x and y.
    fn add_arc(&mut self, center: [f64; 2], radius: f64, start: f64, sweep: f64) {
        let radius = radius.abs();
        if !(radius.is_finite() && start.is_finite() && sweep.is_finite()) {
            return;
        }
        let at = |degrees: f64| {
            let angle = degrees.to_radians();
            [
                center[0] + radius * angle.cos(),
                center[1] + radius * angle.sin(),
            ]
        };
        if sweep >= 360.0 {
            self.add([center[0] - radius, center[1] - radius]);
            self.add([center[0] + radius, center[1] + radius]);
            return;
        }
        self.add(at(start));
        self.add(at(start + sweep));
        for quadrant in [0.0, 90.0, 180.0, 270.0] {
            if (quadrant - start).rem_euclid(360.0) <= sweep {
                self.add(at(quadrant));
            }
        }
    }

    /// The vertices of a polyline or a hatch loop, with the arcs of the bulges
    /// between them.
    fn add_vertices(&mut self, vertices: &[Vertex], closed: bool) {
        for (index, vertex) in vertices.iter().enumerate() {
            self.add([vertex.x, vertex.y]);
            let bulge = vertex.bulge.unwrap_or(0.0);
            if bulge == 0.0 || !bulge.is_finite() {
                continue;
            }
            let next = match vertices.get(index + 1) {
                Some(next) => next,
                None if closed => &vertices[0],
                None => continue,
            };
            let (dx, dy) = (next.x - vertex.x, next.y - vertex.y);
            let chord = dx.hypot(dy);
            if chord <= 0.0 {
                continue;
            }
            // The centre is to the left of the chord for a positive bulge, at
            // ``chord / 2 * (1 - b²) / (2b)`` from its middle.
            let offset = (1.0 - bulge * bulge) / (4.0 * bulge);
            let center = [
                (vertex.x + next.x) / 2.0 - dy * offset,
                (vertex.y + next.y) / 2.0 + dx * offset,
            ];
            let radius = (vertex.x - center[0]).hypot(vertex.y - center[1]);
            let sweep = 4.0 * bulge.abs().atan().to_degrees();
            // a negative bulge runs clockwise: counter-clockwise from its end
            let from = if bulge > 0.0 { vertex } else { next };
            let start = (from.y - center[1]).atan2(from.x - center[0]).to_degrees();
            self.add_arc(center, radius, start, sweep);
        }
    }

    /// The corners of a box ``[x0, x1] x [y0, y1]`` beside ``origin``, turned
    /// by ``rotation`` degrees around it.
    fn add_box(&mut self, origin: [f64; 2], x: [f64; 2], y: [f64; 2], rotation: f64) {
        let (sin, cos) = if rotation.is_finite() {
            rotation.to_radians().sin_cos()
        } else {
            (0.0, 1.0)
        };
        for local_x in x {
            for local_y in y {
                self.add([
                    origin[0] + local_x * cos - local_y * sin,
                    origin[1] + local_x * sin + local_y * cos,
                ]);
            }
        }
    }
}

fn number(map: Option<&Map>, key: &str) -> Option<f64> {
    map?.get(key)?.as_python_number()
}

fn text_of<'a>(map: Option<&'a Map>, key: &str) -> Option<&'a str> {
    map?.get(key)?.as_str()
}

/// The width of one line of text of height 1.
fn line_width(line: &str) -> f64 {
    line.chars()
        .map(|character| {
            if character.is_ascii() {
                NARROW_CHARACTER
            } else {
                WIDE_CHARACTER
            }
        })
        .sum()
}

/// Where a box of ``size`` lies beside its insertion point for an alignment:
/// ``0`` at its start, ``1`` in its middle, ``2`` at its end.
fn span(size: f64, alignment: u8) -> [f64; 2] {
    match alignment {
        1 => [-size / 2.0, size / 2.0],
        2 => [-size, 0.0],
        _ => [0.0, size],
    }
}

struct Walker<'a> {
    blocks: Option<&'a IndexMap<String, Block>>,
    generated: &'a GeometryBlocks,
    /// The extents of the blocks seen so far, in their own coordinates.
    block_bounds: HashMap<&'a str, Option<[f64; 4]>>,
    /// The blocks being measured, outermost first.
    open: Vec<&'a str>,
}

impl<'a> Walker<'a> {
    fn block(&self, name: &str) -> Option<(&'a str, &'a Block)> {
        let stated = self.blocks.and_then(|blocks| blocks.get_key_value(name));
        stated
            .or_else(|| self.generated.blocks.get_key_value(name))
            .map(|(name, block)| (name.as_str(), block))
    }

    /// The extents of a block in its own coordinates. A block inside itself
    /// and one nested too deep count as empty.
    fn bounds_of(&mut self, name: &str) -> Option<[f64; 4]> {
        let (name, block) = self.block(name)?;
        if let Some(known) = self.block_bounds.get(name) {
            return *known;
        }
        if self.open.len() >= MAX_DEPTH || self.open.contains(&name) {
            return None;
        }
        self.open.push(name);
        let mut extents = Extents::default();
        for entity in &block.entities {
            self.add_entity(&mut extents, entity);
        }
        self.open.pop();
        self.block_bounds.insert(name, extents.bounds);
        extents.bounds
    }

    fn add_entity(&mut self, extents: &mut Extents, entity: &'a Entity) {
        let extra = entity.extra.as_deref();
        match &entity.shape {
            Shape::Line { p1, p2 } => {
                extents.add(*p1);
                extents.add(*p2);
            }
            Shape::Circle { center, radius } => extents.add_arc(*center, *radius, 0.0, 360.0),
            Shape::Arc {
                center,
                radius,
                start_angle,
                end_angle,
                ccw,
            } => {
                let (start, end) = if ccw.unwrap_or(true) {
                    (*start_angle, *end_angle)
                } else {
                    (*end_angle, *start_angle)
                };
                let sweep = (end - start).rem_euclid(360.0);
                // equal angles are a whole circle
                let sweep = if sweep == 0.0 { 360.0 } else { sweep };
                extents.add_arc(*center, *radius, start, sweep);
            }
            Shape::Point { position, .. } => extents.add(*position),
            Shape::Ellipse(ellipse) => add_ellipse(extents, ellipse),
            Shape::Text(text) => add_text(extents, text, extra),
            Shape::Insert(insert) => self.add_insert(extents, insert, extra),
            Shape::Hatch(hatch) => {
                for hatch_loop in &hatch.loops {
                    extents.add_vertices(&hatch_loop.vertices, true);
                }
            }
            Shape::Lwpolyline(polyline) => {
                extents.add_vertices(&polyline.vertices, polyline.closed.unwrap_or(false));
            }
            Shape::Other => match entity.kind() {
                Some("MTEXT") => add_mtext(extents, extra),
                Some("IMAGE") => add_image(extents, extra),
                Some("DIMENSION") => self.add_dimension(extents, entity),
                _ => add_stated_points(extents, extra),
            },
        }
    }

    /// The box of the block, placed as the reference places it.
    fn add_insert(&mut self, extents: &mut Extents, insert: &InsertData, extra: Option<&Map>) {
        let Some(bounds) = self.bounds_of(&insert.block) else {
            // a reference to a block without geometry is at its insertion point
            extents.add(insert.insert);
            return;
        };
        let corners = [
            [bounds[0], bounds[1]],
            [bounds[2], bounds[1]],
            [bounds[2], bounds[3]],
            [bounds[0], bounds[3]],
        ];
        // ``transform`` maps the block straight into its parent
        let transform: Option<Vec<f64>> = extra
            .and_then(|extra| extra.get("transform"))
            .and_then(Value::as_list)
            .filter(|items| items.len() == 6)
            .and_then(|items| items.iter().map(Value::as_python_number).collect());
        if let Some(matrix) = transform {
            for [x, y] in corners {
                extents.add([
                    matrix[0] * x + matrix[2] * y + matrix[4],
                    matrix[1] * x + matrix[3] * y + matrix[5],
                ]);
            }
            return;
        }
        let base = self
            .block(&insert.block)
            .and_then(|(_, block)| {
                block
                    .base_point
                    .or_else(|| point_like(block.extra.get("base_point")))
            })
            .unwrap_or([0.0, 0.0]);
        let [scale_x, scale_y] = match insert.scale {
            Some(InsertScale::Uniform(scale)) => [scale, scale],
            Some(InsertScale::Axes(scale)) => scale,
            None => match extra.and_then(|extra| extra.get("scale")) {
                Some(Value::List(items)) if items.len() == 2 => [
                    items[0].as_python_number().unwrap_or(1.0),
                    items[1].as_python_number().unwrap_or(1.0),
                ],
                Some(value) => {
                    let scale = value.as_python_number().unwrap_or(1.0);
                    [scale, scale]
                }
                None => [1.0, 1.0],
            },
        };
        let rotation = insert
            .rotation
            .or_else(|| number(extra, "rotation"))
            .unwrap_or(0.0);
        let (sin, cos) = rotation.to_radians().sin_cos();
        for [x, y] in corners {
            let (x, y) = ((x - base[0]) * scale_x, (y - base[1]) * scale_y);
            extents.add([
                insert.insert[0] + x * cos - y * sin,
                insert.insert[1] + x * sin + y * cos,
            ]);
        }
    }

    /// The points a dimension measures between and its block: the one it
    /// names, or the one made for a dimension that has only its geometry.
    fn add_dimension(&mut self, extents: &mut Extents, entity: &'a Entity) {
        let definition = entity.extra_get("definition").and_then(Value::as_map);
        let points = definition
            .and_then(|definition| definition.get("points"))
            .and_then(Value::as_map);
        // ``insert`` is where the block is placed, not a point of the dimension
        let offset = points
            .and_then(|points| point_like(points.get("insert")))
            .unwrap_or([0.0, 0.0]);
        for (key, point) in points.into_iter().flatten() {
            if key != "insert" {
                if let Some(point) = point_like(Some(point)) {
                    extents.add(point);
                }
            }
        }
        let named = definition
            .and_then(|definition| definition.get("block"))
            .and_then(Value::as_str);
        let generated = self.generated.name_of(entity).map(|name| name.as_ref());
        if let Some(bounds) = named.or(generated).and_then(|name| self.bounds_of(name)) {
            extents.add([bounds[0] + offset[0], bounds[1] + offset[1]]);
            extents.add([bounds[2] + offset[0], bounds[3] + offset[1]]);
        }
    }
}

/// A whole ellipse: half of its width is ``sqrt(mx² + (r my)²)``, half of its
/// height ``sqrt(my² + (r mx)²)`` for the major axis ``(mx, my)`` and the
/// ratio ``r`` of the axes. An elliptical arc is measured as its ellipse.
fn add_ellipse(extents: &mut Extents, ellipse: &EllipseData) {
    let [major_x, major_y] = ellipse.major_axis;
    let ratio = ellipse.ratio;
    let half_x = major_x.hypot(ratio * major_y);
    let half_y = major_y.hypot(ratio * major_x);
    extents.add([ellipse.center[0] - half_x, ellipse.center[1] - half_y]);
    extents.add([ellipse.center[0] + half_x, ellipse.center[1] + half_y]);
}

fn add_text(extents: &mut Extents, text: &TextData, extra: Option<&Map>) {
    let height = text.height.abs();
    let width = line_width(&text.text) * height * text.width_factor.unwrap_or(1.0).abs();
    let horizontal = match text_of(extra, "halign") {
        Some("center" | "middle") => 1,
        Some("right") => 2,
        _ => 0,
    };
    let vertical = match text_of(extra, "valign") {
        Some("middle") => 1,
        Some("top") => 2,
        _ => 0,
    };
    extents.add_box(
        text.insert,
        span(width, horizontal),
        span(height, vertical),
        text.rotation
            .or_else(|| number(extra, "rotation"))
            .unwrap_or(0.0),
    );
}

/// An MTEXT: its lines below (``attach`` ``top_*``, the default), around or
/// above its insertion point, as wide as it states or as its longest line.
fn add_mtext(extents: &mut Extents, extra: Option<&Map>) {
    let Some(insert) = point_like(extra.and_then(|extra| extra.get("insert"))) else {
        return;
    };
    let height = number(extra, "height").unwrap_or(0.0).abs();
    let text = text_of(extra, "text").unwrap_or("");
    let lines: Vec<&str> = text
        .split("\\P")
        .flat_map(|part| part.split('\n'))
        .collect();
    let stated = number(extra, "width").unwrap_or(0.0);
    let width = if stated > 0.0 {
        stated
    } else {
        lines.iter().copied().map(line_width).fold(0.0, f64::max) * height
    };
    let tall = height * (1.0 + LINE_SPACING * (lines.len().max(1) - 1) as f64);
    let attach = text_of(extra, "attach").unwrap_or("top_left");
    let horizontal = if attach.ends_with("center") {
        1
    } else if attach.ends_with("right") {
        2
    } else {
        0
    };
    // the lines run downwards from the top
    let vertical = if attach.starts_with("middle") {
        [-tall / 2.0, tall / 2.0]
    } else if attach.starts_with("bottom") {
        [0.0, tall]
    } else {
        [-tall, 0.0]
    };
    extents.add_box(
        insert,
        span(width, horizontal),
        vertical,
        number(extra, "rotation").unwrap_or(0.0),
    );
}

fn add_image(extents: &mut Extents, extra: Option<&Map>) {
    let Some(insert) = point_like(extra.and_then(|extra| extra.get("insert"))) else {
        return;
    };
    extents.add_box(
        insert,
        [0.0, number(extra, "width").unwrap_or(0.0)],
        [0.0, number(extra, "height").unwrap_or(0.0)],
        number(extra, "rotation").unwrap_or(0.0),
    );
}

/// The points an entity without a typed shape states (a spline, a leader, an
/// entity whose fields are not as the schema has them).
fn add_stated_points(extents: &mut Extents, extra: Option<&Map>) {
    let Some(extra) = extra else { return };
    for key in ["p1", "p2", "insert", "position"] {
        if let Some(point) = point_like(extra.get(key)) {
            extents.add(point);
        }
    }
    if let Some(center) = point_like(extra.get("center")) {
        let radius = number(Some(extra), "radius").unwrap_or(0.0);
        extents.add_arc(center, radius, 0.0, 360.0);
    }
    for key in ["vertices", "control_points", "fit_points"] {
        for point in extra
            .get(key)
            .and_then(Value::as_list)
            .into_iter()
            .flatten()
        {
            if let Some(point) = point_like(Some(point)) {
                extents.add(point);
            }
        }
    }
}

/// The extents of the entities of model space, ``[min_x, min_y, max_x, max_y]``;
/// ``None`` for a model space without geometry.
pub(crate) fn model_extents(document: &Document, generated: &GeometryBlocks) -> Option<[f64; 4]> {
    let mut walker = Walker {
        blocks: document
            .tables
            .as_ref()
            .and_then(|tables| tables.blocks.as_ref()),
        generated,
        block_bounds: HashMap::new(),
        open: Vec::new(),
    };
    let mut extents = Extents::default();
    for entity in &document.entities {
        walker.add_entity(&mut extents, entity);
    }
    extents.bounds
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::model::{HatchData, HatchLoop, PolylineData};

    fn extents_of(entities: Vec<Entity>, blocks: Vec<(&str, Block)>) -> Option<[f64; 4]> {
        let mut document = Document {
            entities,
            has_entities: true,
            ..Document::default()
        };
        if !blocks.is_empty() {
            document.tables = Some(crate::model::Tables {
                blocks: Some(
                    blocks
                        .into_iter()
                        .map(|(name, block)| (name.to_string(), block))
                        .collect(),
                ),
                ..crate::model::Tables::default()
            });
        }
        model_extents(&document, &GeometryBlocks::default())
    }

    fn close(found: Option<[f64; 4]>, wanted: [f64; 4]) {
        let found = found.expect("extents");
        for (found, wanted) in found.iter().zip(wanted) {
            assert!((found - wanted).abs() < 1e-9, "{found} != {wanted}");
        }
    }

    fn line(p1: [f64; 2], p2: [f64; 2]) -> Entity {
        Entity::new(Shape::Line { p1, p2 })
    }

    fn vertex(x: f64, y: f64, bulge: f64) -> Vertex {
        Vertex {
            x,
            y,
            bulge: (bulge != 0.0).then_some(bulge),
        }
    }

    fn block(entities: Vec<Entity>, base_point: Option<[f64; 2]>) -> Block {
        Block {
            base_point,
            entities,
            has_entities: true,
            ..Block::default()
        }
    }

    fn insert(name: &str, at: [f64; 2], rotation: f64, scale: f64) -> Entity {
        Entity::new(Shape::Insert(Box::new(InsertData {
            block: name.into(),
            insert: at,
            rotation: Some(rotation),
            scale: Some(InsertScale::Uniform(scale)),
        })))
    }

    #[test]
    fn a_model_space_without_geometry_has_no_extents() {
        assert_eq!(extents_of(Vec::new(), Vec::new()), None);
    }

    #[test]
    fn lines_and_circles() {
        let circle = Entity::new(Shape::Circle {
            center: [10.0, 0.0],
            radius: 3.0,
        });
        close(
            extents_of(vec![line([0.0, 0.0], [5.0, -2.0]), circle], Vec::new()),
            [0.0, -3.0, 13.0, 3.0],
        );
    }

    #[test]
    fn an_arc_reaches_as_far_as_its_part_of_the_circle() {
        let arc = |start_angle: f64, end_angle: f64, ccw: Option<bool>| {
            Entity::new(Shape::Arc {
                center: [0.0, 0.0],
                radius: 10.0,
                start_angle,
                end_angle,
                ccw,
            })
        };
        // a quarter in the first quadrant
        close(
            extents_of(vec![arc(0.0, 90.0, None)], Vec::new()),
            [0.0, 0.0, 10.0, 10.0],
        );
        // the same angles clockwise are the other three quarters
        close(
            extents_of(vec![arc(0.0, 90.0, Some(false))], Vec::new()),
            [-10.0, -10.0, 10.0, 10.0],
        );
        // through 180 degrees, across the wrap at 360
        let wrapped = extents_of(vec![arc(315.0, 45.0, None)], Vec::new());
        let half = 10.0 * std::f64::consts::FRAC_1_SQRT_2;
        close(wrapped, [half, -half, 10.0, half]);
    }

    #[test]
    fn a_bulge_is_the_arc_between_two_vertices() {
        // a half circle below the chord from (0, 0) to (10, 0)
        let polyline = Entity::new(Shape::Lwpolyline(Box::new(PolylineData {
            vertices: vec![vertex(0.0, 0.0, 1.0), vertex(10.0, 0.0, 0.0)],
            closed: None,
        })));
        close(
            extents_of(vec![polyline], Vec::new()),
            [0.0, -5.0, 10.0, 0.0],
        );
        // a hatch loop closes: the last bulge runs back to the first vertex
        let hatch = Entity::new(Shape::Hatch(Box::new(HatchData {
            loops: vec![HatchLoop {
                vertices: vec![vertex(0.0, 0.0, 0.0), vertex(10.0, 0.0, -1.0)],
                extra: None,
            }],
            solid: Some(true),
        })));
        close(extents_of(vec![hatch], Vec::new()), [0.0, -5.0, 10.0, 0.0]);
    }

    #[test]
    fn a_block_reference_places_the_box_of_its_block() {
        let door = block(vec![line([1.0, 0.0], [3.0, 1.0])], Some([1.0, 0.0]));
        // turned a quarter and doubled: (0..2, 0..1) becomes (-2..0, 0..4)
        close(
            extents_of(
                vec![insert("DOOR", [100.0, 50.0], 90.0, 2.0)],
                vec![("DOOR", door)],
            ),
            [98.0, 50.0, 100.0, 54.0],
        );
    }

    #[test]
    fn blocks_inside_blocks_and_a_block_inside_itself() {
        let inner = block(vec![line([0.0, 0.0], [1.0, 1.0])], None);
        let outer = block(
            vec![
                insert("INNER", [10.0, 0.0], 0.0, 1.0),
                insert("OUTER", [500.0, 500.0], 0.0, 1.0),
            ],
            None,
        );
        // the reference of OUTER to itself is at its insertion point only
        close(
            extents_of(
                vec![insert("OUTER", [0.0, 0.0], 0.0, 1.0)],
                vec![("INNER", inner), ("OUTER", outer)],
            ),
            [10.0, 0.0, 500.0, 500.0],
        );
    }

    #[test]
    fn a_text_is_as_wide_as_its_characters() {
        let text = |halign: Option<&str>| {
            let mut entity = Entity::new(Shape::Text(Box::new(TextData {
                insert: [100.0, 100.0],
                height: 10.0,
                text: "AB図".to_string(),
                rotation: None,
                style: None,
                width_factor: None,
            })));
            if let Some(halign) = halign {
                entity.extra_insert("halign", Value::Str(halign.to_string()));
            }
            entity
        };
        // two narrow characters and a wide one: 22 wide, 10 high
        close(
            extents_of(vec![text(None)], Vec::new()),
            [100.0, 100.0, 122.0, 110.0],
        );
        close(
            extents_of(vec![text(Some("center"))], Vec::new()),
            [89.0, 100.0, 111.0, 110.0],
        );
        close(
            extents_of(vec![text(Some("right"))], Vec::new()),
            [78.0, 100.0, 100.0, 110.0],
        );
    }

    #[test]
    fn a_dimension_is_where_it_measures_not_where_its_block_is_inserted() {
        let mut points = Map::new();
        for (key, x, y) in [
            ("defpoint2", 10.0, 20.0),
            ("defpoint3", 30.0, 20.0),
            ("text_midpoint", 20.0, 25.0),
            ("insert", 0.0, 0.0),
        ] {
            points.insert(
                key.to_string(),
                Value::List(vec![Value::Float(x), Value::Float(y)]),
            );
        }
        let mut definition = Map::new();
        definition.insert("points".to_string(), Value::Map(points));
        let mut dimension = Entity::new(Shape::Other);
        dimension.extra_insert("kind", Value::Str("DIMENSION".to_string()));
        dimension.extra_insert("definition", Value::Map(definition));
        close(
            extents_of(vec![dimension], Vec::new()),
            [10.0, 20.0, 30.0, 25.0],
        );
    }
}
