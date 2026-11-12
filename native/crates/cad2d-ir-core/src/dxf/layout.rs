//! Paper space: the layout of a document that is written, and its viewports
//! (``_append_layout_output``, ``_layout_extents`` and ``_viewport_to_pairs``).

use std::borrow::Cow;
use std::collections::HashMap;
use std::sync::Arc;

use super::common::{float_or, point_like, py_str, required_float};
use super::entity::{append_entity_output, begin, Mode, Owner, Scope};
use super::pairs::{Pair, Val};
use super::{Context, Env, WriteError};
use crate::convert::entity_from_map;
use crate::model::{Document, Entity, Interner, Shape};
use crate::num::num;
use crate::pystr::repr;
use crate::value::{Map, Value};

/// A paper-space layout of the document: its ``dict``, and its entities as the
/// typed IR when ``entities`` is a list.
pub(crate) struct LayoutScope<'a> {
    pub(crate) raw: &'a Map,
    pub(crate) entities: Option<Vec<Entity>>,
}

/// The layouts of a document (``document.get("layouts")`` when it is a list;
/// an item that is no ``dict`` is no layout).
pub(crate) fn layout_scopes(document: &Document) -> Result<Vec<LayoutScope<'_>>, WriteError> {
    let Some(Value::List(layouts)) = document.extra.get("layouts") else {
        return Ok(Vec::new());
    };
    let mut interner = Interner::new();
    let mut scopes = Vec::with_capacity(layouts.len());
    for layout in layouts {
        let Value::Map(raw) = layout else { continue };
        let entities = match raw.get("entities") {
            Some(Value::List(items)) => {
                let mut entities = Vec::with_capacity(items.len());
                for item in items {
                    let Value::Map(map) = item else {
                        return Err(WriteError::invalid(
                            "a layout entity is not an entity object",
                        ));
                    };
                    entities.push(entity_from_map(map.clone(), &mut interner));
                }
                Some(entities)
            }
            Some(other) if other.truthy() => {
                return Err(WriteError::invalid("layout entities are not a list"));
            }
            _ => None,
        };
        scopes.push(LayoutScope { raw, entities });
    }
    Ok(scopes)
}

/// ``repr(mapping.get("name"))``.
fn name_repr(layout: &Map) -> Result<String, WriteError> {
    Ok(match layout.get("name") {
        None | Some(Value::Null) => "None".to_string(),
        Some(Value::Str(name)) => repr(name),
        Some(other) => py_str(other, "layout name")?.into_owned(),
    })
}

/// A point an entity has under ``key``.
fn key_point(entity: &Entity, key: &str) -> Option<[f64; 2]> {
    match (&entity.shape, key) {
        (Shape::Line { p1, .. }, "p1") => Some(*p1),
        (Shape::Line { p2, .. }, "p2") => Some(*p2),
        (Shape::Text(text), "insert") => Some(text.insert),
        (Shape::Insert(insert), "insert") => Some(insert.insert),
        (Shape::Point { position, .. }, "position") => Some(*position),
        (Shape::Circle { center, .. } | Shape::Arc { center, .. }, "center") => Some(*center),
        (Shape::Ellipse(ellipse), "center") => Some(ellipse.center),
        _ => point_like(entity.extra_get(key)),
    }
}

/// The extents of a sheet as they grow: ``min`` and ``max`` of Python over the
/// values in the order they were added.
#[derive(Default)]
struct Extents {
    bounds: Option<[f64; 4]>,
}

impl Extents {
    fn add(&mut self, point: Option<[f64; 2]>, radius: f64) {
        let Some([x, y]) = point else { return };
        if !(x.is_finite() && y.is_finite()) {
            return;
        }
        for (px, py) in [(x - radius, y - radius), (x + radius, y + radius)] {
            match self.bounds.as_mut() {
                None => self.bounds = Some([px, py, px, py]),
                Some(bounds) => {
                    if px < bounds[0] {
                        bounds[0] = px;
                    }
                    if py < bounds[1] {
                        bounds[1] = py;
                    }
                    if px > bounds[2] {
                        bounds[2] = px;
                    }
                    if py > bounds[3] {
                        bounds[3] = py;
                    }
                }
            }
        }
    }
}

/// ``_layout_extents``: rough extents of a sheet, from the points of its
/// entities and its windows.
fn layout_extents(layout: &LayoutScope<'_>) -> Result<Option<[f64; 4]>, WriteError> {
    let mut extents = Extents::default();
    for entity in layout.entities.iter().flatten() {
        for key in ["p1", "p2", "insert", "position"] {
            extents.add(key_point(entity, key), 0.0);
        }
        let radius = match &entity.shape {
            Shape::Circle { radius, .. } | Shape::Arc { radius, .. } => *radius,
            _ => entity
                .extra_get("radius")
                .and_then(Value::as_python_number)
                .unwrap_or(0.0),
        };
        extents.add(key_point(entity, "center"), radius);
        match &entity.shape {
            Shape::Lwpolyline(polyline) => {
                for vertex in &polyline.vertices {
                    extents.add(Some([vertex.x, vertex.y]), 0.0);
                }
            }
            _ => {
                if let Some(Value::List(points)) = entity.extra_get("vertices") {
                    for point in points {
                        extents.add(point_like(Some(point)), 0.0);
                    }
                }
            }
        }
        if let Some(Value::List(points)) = entity.extra_get("control_points") {
            for point in points {
                extents.add(point_like(Some(point)), 0.0);
            }
        }
        match &entity.shape {
            Shape::Hatch(hatch) => {
                for hatch_loop in &hatch.loops {
                    for vertex in &hatch_loop.vertices {
                        extents.add(Some([vertex.x, vertex.y]), 0.0);
                    }
                }
            }
            _ => {
                if let Some(Value::List(loops)) = entity.extra_get("loops") {
                    for hatch_loop in loops {
                        let Value::Map(hatch_loop) = hatch_loop else {
                            continue;
                        };
                        if let Some(Value::List(points)) = hatch_loop.get("vertices") {
                            for point in points {
                                extents.add(point_like(Some(point)), 0.0);
                            }
                        }
                    }
                }
            }
        }
    }
    if let Some(Value::List(viewports)) = layout.raw.get("viewports") {
        for viewport in viewports {
            let Value::Map(viewport) = viewport else {
                continue;
            };
            let Some(center) = point_like(viewport.get("center")) else {
                continue;
            };
            let half_width = float_or(viewport.get("width"), 0.0, "viewport width")? / 2.0;
            let half_height = float_or(viewport.get("height"), 0.0, "viewport height")? / 2.0;
            extents.add(Some([center[0] - half_width, center[1] - half_height]), 0.0);
            extents.add(Some([center[0] + half_width, center[1] + half_height]), 0.0);
        }
    }
    Ok(extents.bounds)
}

/// The third coordinate of a view vector: ``[*value, default][:3][2]``.
fn third(value: &Value, default: f64) -> Result<f64, WriteError> {
    match value.as_list() {
        Some(items) if items.len() >= 3 => required_float(Some(&items[2]), "viewport view vector"),
        _ => Ok(default),
    }
}

/// ``_viewport_to_pairs``: the VIEWPORT record of a window onto model space.
fn viewport_record(
    out: &mut Vec<Pair>,
    viewport: &Map,
    viewport_id: usize,
    layer_handles: &HashMap<String, Val>,
    mode: Mode,
    context: &mut Context<'_>,
) -> Result<(), WriteError> {
    let center = point_like(viewport.get("center"))
        .ok_or_else(|| WriteError::invalid("viewport center is not a point"))?;
    let width = required_float(viewport.get("width"), "viewport width")?;
    let height = required_float(viewport.get("height"), "viewport height")?;
    let view = context.host.viewport_view(viewport)?;
    // what the source kept of the viewport: the first ``dict`` under ``metadata``
    let stored: Option<&Map> = viewport
        .get("metadata")
        .and_then(Value::as_map)
        .and_then(|metadata| metadata.values().find_map(Value::as_map));
    let mut flags: i64 = match stored.and_then(|stored| stored.get("status_flags")) {
        Some(Value::Int(flags)) => *flags,
        _ => 0x8060,
    };
    flags &= !0x10000; // the clip boundary entity is not written
    let visible = !matches!(viewport.get("visible"), Some(Value::Bool(false)));
    flags = if visible {
        flags & !0x20000
    } else {
        flags | 0x20000
    };

    let mut direction = [0.0, 0.0, 1.0];
    let mut target = [0.0, 0.0, 0.0];
    let mut view_center: Option<[f64; 2]> = None;
    let mut view_height: Option<f64> = None;
    let mut twist = 0.0;
    if let Some(view) = view {
        view_center = Some(view.center);
        view_height = Some(view.height);
        twist = view.twist;
    } else if let Some(Value::Map(source_view)) = stored.and_then(|stored| stored.get("view")) {
        // A view the IR does not express (3D or perspective), as the source had it.
        if let Some(value) = source_view.get("direction") {
            if let Some(point) = point_like(Some(value)) {
                direction = [point[0], point[1], third(value, 1.0)?];
            }
        }
        if let Some(value) = source_view.get("target") {
            if let Some(point) = point_like(Some(value)) {
                target = [point[0], point[1], third(value, 0.0)?];
            }
        }
        if let Some(point) = point_like(source_view.get("center")) {
            view_center = Some(point);
        }
        if let Some(height) = source_view.get("height").and_then(Value::as_python_number) {
            view_height = Some(height);
        }
        if let Some(angle) = source_view.get("twist").and_then(Value::as_python_number) {
            twist = angle;
        }
    }
    let view_center = view_center.unwrap_or(center);
    // ``not view_height > 0``: a NaN is no height either
    let view_height = match view_height {
        Some(view_height) if view_height > 0.0 => view_height,
        _ => height,
    };
    let layer = match viewport.get("layer") {
        Some(value) if value.truthy() => Val::owned(py_str(value, "viewport layer")?.into_owned()),
        _ => Val::S("0"),
    };
    let id = Val::owned(viewport_id.to_string());

    begin(out, "VIEWPORT", mode, context);
    out.push((8, layer));
    if mode.r2010 {
        out.push((100, Val::S("AcDbViewport")));
    }
    let number = |code: i32, value: f64| -> Pair { (code, Val::owned(num(value))) };
    out.push(number(10, center[0]));
    out.push(number(20, center[1]));
    out.push((30, Val::S("0")));
    out.push(number(40, width));
    out.push(number(41, height));
    out.push((68, if visible { id.clone() } else { Val::S("0") }));
    out.push((69, id));
    out.push(number(12, view_center[0]));
    out.push(number(22, view_center[1]));
    for (code, value) in [
        (13, "0"),
        (23, "0"),
        (14, "10"),
        (24, "10"),
        (15, "10"),
        (25, "10"),
    ] {
        out.push((code, Val::S(value)));
    }
    out.push(number(16, direction[0]));
    out.push(number(26, direction[1]));
    out.push(number(36, direction[2]));
    out.push(number(17, target[0]));
    out.push(number(27, target[1]));
    out.push(number(37, target[2]));
    out.push((42, Val::S("50")));
    out.push((43, Val::S("0")));
    out.push((44, Val::S("0")));
    out.push(number(45, view_height));
    out.push((50, Val::S("0")));
    out.push(number(51, twist));
    out.push((72, Val::S("1000")));
    match viewport.get("frozen_layers") {
        Some(Value::List(names)) => {
            for name in names {
                let name = py_str(name, "viewport frozen layer")?;
                if let Some(handle) = layer_handles.get(name.as_ref()) {
                    if !handle.as_str().is_empty() {
                        out.push((331, handle.clone()));
                    }
                }
            }
        }
        Some(Value::Str(names)) => {
            // a string is walked a character at a time, as Python walks it
            for name in names.chars() {
                if let Some(handle) = layer_handles.get(name.to_string().as_str()) {
                    out.push((331, handle.clone()));
                }
            }
        }
        _ => {}
    }
    out.push((90, Val::owned(flags.to_string())));
    Ok(())
}

/// ``_append_layout_output``: one paper-space layout in the ENTITIES section.
///
/// The writer has one paper space: the entities carry group 67 = 1 and a
/// reader puts them on its first layout. The active sheet is written (the
/// first sheet with content when none is marked); the others are reported and
/// left out. R12 keeps the view of a viewport in extended data, which is not
/// written: R12 output has no viewports.
pub(crate) fn append_layout_output(
    out: &mut Vec<Pair>,
    layouts: &[LayoutScope<'_>],
    layer_handles: &HashMap<String, Val>,
    env: &Env<'_>,
    context: &mut Context<'_>,
) -> Result<(), WriteError> {
    let has = |layout: &LayoutScope<'_>, key: &str| layout.raw.get(key).is_some_and(Value::truthy);
    let with_content: Vec<&LayoutScope<'_>> = layouts
        .iter()
        .filter(|layout| has(layout, "entities") || has(layout, "viewports"))
        .collect();
    let Some(first) = with_content.first() else {
        return Ok(());
    };
    let chosen: &LayoutScope<'_> = with_content
        .iter()
        .find(|layout| has(layout, "active"))
        .unwrap_or(first);
    let chosen_name = name_repr(chosen.raw)?;
    for layout in &with_content {
        if std::ptr::eq(*layout, chosen) {
            continue;
        }
        // ``len(layout.get("entities") or [])``
        let count = match layout.raw.get("entities") {
            Some(Value::List(items)) => items.len(),
            Some(Value::Map(map)) => map.len(),
            Some(Value::Str(text)) => text.chars().count(),
            Some(other) if other.truthy() => {
                return Err(WriteError::invalid("layout entities are not a list"));
            }
            _ => 0,
        };
        context.diagnose(
            "DXF_LAYOUT_OMITTED",
            "warning",
            format!(
                "Layout {} with {count} entities was omitted: the DXF output holds one \
                 paper-space layout ({chosen_name}).",
                name_repr(layout.raw)?
            ),
            None,
            Some("skipped"),
        );
    }

    let name: Cow<'_, str> = match chosen.raw.get("name") {
        None => Cow::Borrowed(""),
        Some(value) => py_str(value, "layout name")?,
    };
    let r2010 = context.r2010();
    let mode = Mode {
        r2010,
        paper: true,
        owner: Owner::Paper,
    };
    let viewports: Vec<&Map> = match chosen.raw.get("viewports") {
        Some(Value::List(viewports)) => viewports
            .iter()
            .filter_map(Value::as_map)
            .filter(|viewport| {
                point_like(viewport.get("center")).is_some()
                    && viewport
                        .get("width")
                        .and_then(Value::as_python_number)
                        .is_some()
                    && viewport
                        .get("height")
                        .and_then(Value::as_python_number)
                        .is_some()
            })
            .collect(),
        _ => Vec::new(),
    };
    if !viewports.is_empty() && !r2010 {
        context.diagnose(
            "DXF_R12_VIEWPORT_OMITTED",
            "warning",
            format!(
                "{} viewport(s) of layout {} were omitted from DXF R12.",
                viewports.len(),
                repr(&name)
            ),
            None,
            Some("skipped"),
        );
    } else if !viewports.is_empty() {
        let extents = layout_extents(chosen)?.unwrap_or([0.0, 0.0, 420.0, 297.0]);
        // ``max(value, 1e-6)``
        let at_least = |value: f64| if 1e-6 > value { 1e-6 } else { value };
        let width = at_least(extents[2] - extents[0]);
        let height = at_least(extents[3] - extents[1]);
        // Viewport 1 stands for the sheet itself.
        let center = Value::List(vec![
            Value::Float((extents[0] + extents[2]) / 2.0),
            Value::Float((extents[1] + extents[3]) / 2.0),
        ]);
        let mut sheet = Map::new();
        sheet.insert("center".to_string(), center.clone());
        sheet.insert("width".to_string(), Value::Float(width * 1.1));
        sheet.insert("height".to_string(), Value::Float(height * 1.1));
        sheet.insert("view_center".to_string(), center);
        sheet.insert("view_height".to_string(), Value::Float(height * 1.1));
        for (index, viewport) in std::iter::once(&sheet).chain(viewports).enumerate() {
            viewport_record(out, viewport, index + 1, layer_handles, mode, context)?;
        }
    }

    let scope_name: Arc<str> = Arc::from(format!("layout:{name}").as_str());
    let scope = Scope {
        mode,
        name: &scope_name,
        id_override: None,
    };
    for entity in chosen.entities.iter().flatten() {
        append_entity_output(out, entity, &scope, env, context)?;
    }
    Ok(())
}
