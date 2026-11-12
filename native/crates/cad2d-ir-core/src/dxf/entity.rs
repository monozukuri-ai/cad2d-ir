//! The records of the entities: ``_render_entity``, ``_append_entity_output``
//! and the ``_*_to_pairs`` functions of the Python writer.
//!
//! A record is written once, in the form of its target: for DXF R2010 with its
//! handle, its owner (a placeholder the structure pass replaces) and the
//! subclass markers where ``_r2010_record_body`` puts them, for DXF R12 as the
//! bare groups.

use std::borrow::Cow;
use std::sync::Arc;

use super::common::{
    entity_id, float_or, kind_text, label_id, point_like, py_str, required_float, required_point,
    Common,
};
use super::pairs::{Pair, Val, MODEL_SPACE_OWNER, PAPER_SPACE_OWNER};
use super::text::justified_text_start;
use super::{
    curves, dimension, image, Context, EntityMapEntry, Env, GenericDimensions, WriteError,
};
use crate::model::{
    EllipseData, Entity, HatchData, InsertData, InsertScale, PolylineData, Shape, TextData, Vertex,
};
use crate::num::{num, py_hypot, py_isclose};
use crate::pystr::repr;
use crate::value::{Map, Value};

/// How deep the geometry blocks of GENERIC dimensions may nest.
const GENERIC_DEPTH_LIMIT: u32 = 64;

/// Whose entities a record belongs to (group 330 of DXF R2010).
#[derive(Clone, Copy, PartialEq, Eq)]
pub(crate) enum Owner {
    /// A block: the structure pass fills in the handle of its block record.
    Block,
    Model,
    Paper,
}

/// The form records are written in.
#[derive(Clone, Copy)]
pub(crate) struct Mode {
    pub(crate) r2010: bool,
    /// Paper space: group 67 = 1 behind every record head.
    pub(crate) paper: bool,
    pub(crate) owner: Owner,
}

/// Where entities are written: the form of their records and what the entity
/// map says about them.
pub(crate) struct Scope<'s> {
    pub(crate) mode: Mode,
    /// ``modelspace``, ``block:<name>`` or ``layout:<name>``.
    pub(crate) name: &'s Arc<str>,
    /// The entity a generated block stands for (the id the entity map names).
    pub(crate) id_override: Option<&'s Arc<str>>,
}

fn n(code: i32, value: f64) -> Pair {
    (code, Val::owned(num(value)))
}

/// The head of a record: its kind, for R2010 its handle, its owner and the
/// ``AcDbEntity`` marker, and the paper space flag.
pub(crate) fn begin(
    out: &mut Vec<Pair>,
    kind: &'static str,
    mode: Mode,
    context: &mut Context<'_>,
) -> Option<Val> {
    out.push((0, Val::S(kind)));
    let handle = if mode.r2010 {
        let handle = context.entity_handle();
        out.push((5, handle.clone()));
        match mode.owner {
            Owner::Model => out.push((330, Val::S(MODEL_SPACE_OWNER))),
            Owner::Paper => out.push((330, Val::S(PAPER_SPACE_OWNER))),
            Owner::Block => {}
        }
        out.push((100, Val::S("AcDbEntity")));
        Some(handle)
    } else {
        None
    };
    if mode.paper {
        out.push((67, Val::S("1")));
    }
    handle
}

/// The head of a record that belongs to the entity before it (an ATTRIB or the
/// SEQEND of an INSERT, a VERTEX or the SEQEND of a POLYLINE).
fn begin_sub(
    out: &mut Vec<Pair>,
    kind: &'static str,
    mode: Mode,
    parent: Option<&Val>,
    context: &mut Context<'_>,
) {
    out.push((0, Val::S(kind)));
    if mode.r2010 {
        out.push((5, context.entity_handle()));
        if let Some(parent) = parent {
            out.push((330, parent.clone()));
        }
        out.push((100, Val::S("AcDbEntity")));
    }
    if mode.paper {
        out.push((67, Val::S("1")));
    }
}

/// A subclass marker (R2010 only).
fn sub(out: &mut Vec<Pair>, mode: Mode, name: &'static str) {
    if mode.r2010 {
        out.push((100, Val::S(name)));
    }
}

fn invalid_geometry(entity: &Entity, kind: &str) -> WriteError {
    let id = entity_id(entity).map(Cow::into_owned).unwrap_or_default();
    WriteError::invalid(format!(
        "[{kind}:{}] the geometry of the entity is not what the IR has for a {kind}",
        label_id(&id)
    ))
}

/// ``_append_entity_output``: the records of one entity and its entries in the
/// entity map.
pub(crate) fn append_entity_output(
    out: &mut Vec<Pair>,
    entity: &Entity,
    scope: &Scope<'_>,
    env: &Env<'_>,
    context: &mut Context<'_>,
) -> Result<(), WriteError> {
    let before = context.output_index;
    let ir_id: Option<Arc<str>> = if context.options.entity_map {
        Some(match scope.id_override {
            Some(id) => Arc::clone(id),
            None => Arc::from(entity_id(entity)?.as_ref()),
        })
    } else {
        None
    };
    let reason = render(out, entity, scope, ir_id.as_ref(), env, context)?;
    if context.output_index == before {
        if let Some(ir_id) = ir_id {
            context.entity_map.push(EntityMapEntry {
                ir_id,
                handle: None,
                dxf_type: None,
                index: before,
                scope: Arc::clone(scope.name),
                reason_code: reason,
            });
        }
    }
    Ok(())
}

/// One written item: its entry in the entity map.
fn rendered(
    context: &mut Context<'_>,
    scope: &Scope<'_>,
    ir_id: Option<&Arc<str>>,
    handle: Option<Val>,
    dxf_type: &'static str,
) {
    if let Some(ir_id) = ir_id {
        context.entity_map.push(EntityMapEntry {
            ir_id: Arc::clone(ir_id),
            handle: handle.map(|handle| handle.as_str().to_string()),
            dxf_type: Some(dxf_type),
            index: context.output_index,
            scope: Arc::clone(scope.name),
            reason_code: None,
        });
    }
    context.output_index += 1;
}

fn diagnostic_id(id: &str) -> Option<String> {
    (!id.is_empty()).then(|| id.to_string())
}

/// ``_render_entity``, written straight into ``out``. Returns the reason code
/// when the entity was left out on purpose.
fn render(
    out: &mut Vec<Pair>,
    entity: &Entity,
    scope: &Scope<'_>,
    ir_id: Option<&Arc<str>>,
    env: &Env<'_>,
    context: &mut Context<'_>,
) -> Result<Option<&'static str>, WriteError> {
    let id = entity_id(entity)?;
    let kind = kind_text(entity, "")?;
    let mode = scope.mode;

    if kind == "IMAGE" {
        if !mode.r2010 {
            context.diagnose(
                "DXF_IMAGE_SKIPPED",
                "warning",
                format!(
                    "[IMAGE:{}] raster image placements need DXF R14 or later (IMAGE with \
                     IMAGEDEF objects); the placement was skipped for AC1009.",
                    label_id(&id)
                ),
                diagnostic_id(&id),
                Some("skipped"),
            );
            return Ok(Some("DXF_IMAGE_SKIPPED"));
        }
        let Some(prepared) = image::prepare(entity, context)? else {
            return Ok(Some("DXF_IMAGE_SKIPPED"));
        };
        let common = Common::plain(entity)?;
        let handle = begin(out, "IMAGE", mode, context).expect("an R2010 record has a handle");
        context.images.bind(&prepared, handle.clone());
        image::append_record(out, entity, &prepared, &common)?;
        rendered(context, scope, ir_id, Some(handle), "IMAGE");
        return Ok(None);
    }

    if kind == "DIMENSION" && dimension::is_generic(entity) {
        if context.options.generic_dimensions == GenericDimensions::Skip {
            context.diagnose(
                "DXF_GENERIC_DIMENSION_SKIPPED",
                "warning",
                format!(
                    "[DIMENSION:{}] GENERIC dimension was omitted by generic_dimensions='skip'.",
                    label_id(&id)
                ),
                diagnostic_id(&id),
                Some("skipped"),
            );
            return Ok(Some("DXF_GENERIC_DIMENSION_SKIPPED"));
        }
        let components = match dimension::block_components(entity, env)? {
            Some(components) => components,
            None => dimension::components(entity, context)?,
        };
        if components.is_empty() {
            context.diagnose(
                "DXF_GENERIC_DIMENSION_GEOMETRY_MISSING",
                "warning",
                format!(
                    "[DIMENSION:{}] GENERIC dimension had no renderable primitive geometry and \
                     was omitted.",
                    label_id(&id)
                ),
                diagnostic_id(&id),
                Some("skipped"),
            );
            return Ok(Some("DXF_GENERIC_DIMENSION_GEOMETRY_MISSING"));
        }
        context.diagnose(
            "DXF_GENERIC_DIMENSION_EXPLODED",
            "info",
            format!(
                "[DIMENSION:{}] GENERIC dimension was expanded into {} primitive(s).",
                label_id(&id),
                components.len()
            ),
            diagnostic_id(&id),
            Some("exploded"),
        );
        // A block of a dimension can hold a dimension that names the block
        // again: such a drawing has no end (Python stops at its recursion limit).
        if context.generic_depth >= GENERIC_DEPTH_LIMIT {
            return Err(WriteError::invalid(format!(
                "[DIMENSION:{}] the geometry blocks of GENERIC dimensions are nested more than \
                 {GENERIC_DEPTH_LIMIT} deep",
                label_id(&id)
            )));
        }
        let (start, map_start) = (out.len(), context.entity_map.len());
        context.generic_depth += 1;
        for component in &components {
            render(out, component, scope, ir_id, env, context)?;
        }
        context.generic_depth -= 1;
        if context.generic_depth == 0 {
            context.resolve_handles(out, start, map_start);
        }
        return Ok(None);
    }

    if kind == "POINT" {
        let temporary = match &entity.shape {
            Shape::Point { temporary, .. } => *temporary == Some(true),
            _ => matches!(entity.extra_get("temporary"), Some(Value::Bool(true))),
        };
        if temporary {
            // A construction point of the source (a Jw_cad temporary point): it is
            // shown on screen and left out of a plot. DXF has no such point, and
            // written as a POINT it would become part of the drawing.
            context.diagnose(
                "DXF_TEMPORARY_POINT_SKIPPED",
                "info",
                format!(
                    "[POINT:{}] temporary (construction) point was not written; DXF has no \
                     points that are left out of a plot.",
                    label_id(&id)
                ),
                diagnostic_id(&id),
                Some("skipped"),
            );
            return Ok(Some("DXF_TEMPORARY_POINT_SKIPPED"));
        }
    }

    if kind == "HATCH" && mode.r2010 {
        let Shape::Hatch(hatch) = &entity.shape else {
            return Err(hatch_error(entity));
        };
        if !hatch_is_solid(entity, hatch) && !hatch_has_pattern_lines(entity) {
            // A pattern hatch needs the lines of its pattern; AutoCAD and ODA drop a
            // HATCH without them. Its boundary is what a reader can draw.
            let common = Common::plain(entity)?;
            let mut written = 0;
            for hatch_loop in &hatch.loops {
                if hatch_loop.vertices.is_empty() {
                    continue;
                }
                let handle = lwpolyline(out, &hatch_loop.vertices, true, &common, mode, context);
                rendered(context, scope, ir_id, handle, "LWPOLYLINE");
                written += 1;
            }
            let pattern = match entity.extra_get("pattern") {
                None | Some(Value::Null) => "None".to_string(),
                Some(Value::Str(pattern)) => repr(pattern),
                Some(other) => py_str(other, "HATCH pattern")?.into_owned(),
            };
            context.diagnose(
                "DXF_HATCH_PATTERN_EXPLODED",
                "warning",
                format!(
                    "[HATCH:{}] pattern {pattern} has no definition lines; its {written} \
                     boundary loop(s) were written as polylines.",
                    label_id(&id)
                ),
                diagnostic_id(&id),
                Some("exploded"),
            );
            return Ok(None);
        }
    }

    // an INSERT with an affine transform, as the insertion point, rotation and
    // scale DXF has for it
    let placement = match (&entity.shape, kind.as_ref()) {
        (Shape::Insert(insert), _) => Some(insert_placement(entity, insert, &id, env, context)?),
        (Shape::Other, "INSERT") => return Err(invalid_geometry(entity, "INSERT")),
        _ => None,
    };

    let common = Common::of(entity, context)?;

    if !mode.r2010 {
        match &entity.shape {
            Shape::Lwpolyline(polyline) => {
                context.diagnose(
                    "DXF_R12_LWPOLYLINE_EXPLODED",
                    "info",
                    format!("[LWPOLYLINE:{}] emitted as POLYLINE/VERTEX.", label_id(&id)),
                    diagnostic_id(&id),
                    Some("exploded"),
                );
                polyline_r12(
                    out,
                    &polyline.vertices,
                    polyline_is_closed(entity, polyline),
                    &common,
                    mode,
                    context,
                );
                rendered(context, scope, ir_id, None, "POLYLINE");
                return Ok(None);
            }
            Shape::Ellipse(ellipse) => {
                let (start, end) = ellipse_parameters(entity, ellipse);
                let vertices: Vec<Vertex> = curves::sample_ellipse(
                    ellipse.center,
                    ellipse.major_axis,
                    ellipse.ratio,
                    start,
                    end,
                    context.options.curve_segments,
                )?
                .into_iter()
                .map(|[x, y]| Vertex { x, y, bulge: None })
                .collect();
                context.diagnose(
                    "DXF_R12_ELLIPSE_APPROXIMATED",
                    "warning",
                    format!(
                        "[ELLIPSE:{}] approximated as a {}-vertex POLYLINE for DXF R12.",
                        label_id(&id),
                        vertices.len()
                    ),
                    diagnostic_id(&id),
                    Some("approximated"),
                );
                let closed = curves::ellipse_is_closed(ellipse.start_param, ellipse.end_param);
                polyline_r12(out, &vertices, closed, &common, mode, context);
                rendered(context, scope, ir_id, None, "POLYLINE");
                return Ok(None);
            }
            Shape::Hatch(hatch) => {
                for hatch_loop in &hatch.loops {
                    polyline_r12(out, &hatch_loop.vertices, true, &common, mode, context);
                    rendered(context, scope, ir_id, None, "POLYLINE");
                }
                context.diagnose(
                    "DXF_R12_HATCH_EXPLODED",
                    "warning",
                    format!(
                        "[HATCH:{}] fill was replaced by {} boundary POLYLINE(s) for DXF R12.",
                        label_id(&id),
                        hatch.loops.len()
                    ),
                    diagnostic_id(&id),
                    Some("exploded"),
                );
                return Ok(None);
            }
            Shape::Other if kind == "HATCH" => return Err(hatch_error(entity)),
            Shape::Other if kind == "MTEXT" => {
                let prepared = r12_prepared_entity(entity, &common);
                let (texts, formatting_changed) =
                    context.host.explode_mtext(&prepared, env.text_styles)?;
                context.diagnose(
                    "DXF_R12_MTEXT_EXPLODED",
                    "warning",
                    format!(
                        "[MTEXT:{}] expanded into {} TEXT entity/entities for DXF R12.",
                        label_id(&id),
                        texts.len()
                    ),
                    diagnostic_id(&id),
                    Some("exploded"),
                );
                if formatting_changed {
                    context.diagnose(
                        "DXF_R12_MTEXT_FORMATTING_NORMALIZED",
                        "warning",
                        format!(
                            "[MTEXT:{}] formatting codes were removed for DXF R12.",
                            label_id(&id)
                        ),
                        diagnostic_id(&id),
                        Some("normalized"),
                    );
                }
                for text_entity in &texts {
                    let Shape::Text(data) = &text_entity.shape else {
                        return Err(WriteError::invalid("an exploded MTEXT line is no TEXT"));
                    };
                    let line_common = Common::plain(text_entity)?;
                    text(out, text_entity, data, &line_common, mode, context)?;
                    rendered(context, scope, ir_id, None, "TEXT");
                }
                return Ok(None);
            }
            Shape::Other if kind == "SPLINE" => {
                let spline = SplineFields::of(entity)?;
                let vertices: Vec<Vertex> = curves::sample_spline(
                    &spline.control_points,
                    spline.degree,
                    spline.sampling_knots(entity)?.as_deref(),
                    spline.sampling_weights(entity)?.as_deref(),
                    context.options.curve_segments,
                )?
                .into_iter()
                .map(|[x, y]| Vertex { x, y, bulge: None })
                .collect();
                context.diagnose(
                    "DXF_R12_SPLINE_APPROXIMATED",
                    "warning",
                    format!(
                        "[SPLINE:{}] approximated as a {}-vertex POLYLINE for DXF R12.",
                        label_id(&id),
                        vertices.len()
                    ),
                    diagnostic_id(&id),
                    Some("approximated"),
                );
                polyline_r12(out, &vertices, spline.closed, &common, mode, context);
                rendered(context, scope, ir_id, None, "POLYLINE");
                return Ok(None);
            }
            _ => {}
        }
    }

    match &entity.shape {
        Shape::Line { p1, p2 } => {
            let handle = begin(out, "LINE", mode, context);
            common.write(out);
            sub(out, mode, "AcDbLine");
            out.extend([n(10, p1[0]), n(20, p1[1]), n(11, p2[0]), n(21, p2[1])]);
            rendered(context, scope, ir_id, handle, "LINE");
        }
        Shape::Circle { center, radius } => {
            let handle = begin(out, "CIRCLE", mode, context);
            common.write(out);
            sub(out, mode, "AcDbCircle");
            out.extend([n(10, center[0]), n(20, center[1]), n(40, *radius)]);
            rendered(context, scope, ir_id, handle, "CIRCLE");
        }
        Shape::Arc {
            center,
            radius,
            start_angle,
            end_angle,
            ..
        } => {
            let handle = begin(out, "ARC", mode, context);
            common.write(out);
            sub(out, mode, "AcDbCircle");
            out.extend([n(10, center[0]), n(20, center[1]), n(40, *radius)]);
            sub(out, mode, "AcDbArc");
            out.extend([n(50, *start_angle), n(51, *end_angle)]);
            rendered(context, scope, ir_id, handle, "ARC");
        }
        Shape::Point { position, .. } => {
            let handle = begin(out, "POINT", mode, context);
            common.write(out);
            sub(out, mode, "AcDbPoint");
            out.extend([n(10, position[0]), n(20, position[1])]);
            rendered(context, scope, ir_id, handle, "POINT");
        }
        Shape::Ellipse(ellipse) => {
            let (start, end) = ellipse_parameters(entity, ellipse);
            let handle = begin(out, "ELLIPSE", mode, context);
            common.write(out);
            sub(out, mode, "AcDbEllipse");
            out.extend([
                n(10, ellipse.center[0]),
                n(20, ellipse.center[1]),
                n(11, ellipse.major_axis[0]),
                n(21, ellipse.major_axis[1]),
                n(40, ellipse.ratio),
                n(41, start),
                n(42, end),
            ]);
            rendered(context, scope, ir_id, handle, "ELLIPSE");
        }
        Shape::Lwpolyline(polyline) => {
            let closed = polyline_is_closed(entity, polyline);
            let handle = lwpolyline(out, &polyline.vertices, closed, &common, mode, context);
            rendered(context, scope, ir_id, handle, "LWPOLYLINE");
        }
        Shape::Text(data) => {
            let handle = text(out, entity, data, &common, mode, context)?;
            rendered(context, scope, ir_id, handle, "TEXT");
        }
        Shape::Insert(insert) => {
            let placement = placement.expect("an INSERT has a placement");
            let handle = insert_record(out, entity, insert, &placement, &common, mode, context)?;
            rendered(context, scope, ir_id, handle, "INSERT");
        }
        Shape::Hatch(hatch) => {
            let handle = hatch_record(out, entity, hatch, &common, mode, context)?;
            rendered(context, scope, ir_id, handle, "HATCH");
        }
        Shape::Other => match kind.as_ref() {
            "MTEXT" => {
                let handle = mtext(out, entity, &common, mode, context)?;
                rendered(context, scope, ir_id, handle, "MTEXT");
            }
            "SPLINE" => {
                let handle = spline(out, entity, &common, mode, context)?;
                rendered(context, scope, ir_id, handle, "SPLINE");
            }
            "DIMENSION" => {
                let handle = dimension::record(out, entity, &common, mode, env, context)?;
                rendered(context, scope, ir_id, handle, "DIMENSION");
            }
            "LINE" | "CIRCLE" | "ARC" | "POINT" | "ELLIPSE" | "LWPOLYLINE" | "TEXT" => {
                return Err(invalid_geometry(entity, &kind));
            }
            "HATCH" => return Err(hatch_error(entity)),
            _ => {
                context.diagnose(
                    "DXF_UNKNOWN_ENTITY_SKIPPED",
                    "warning",
                    format!(
                        "[{}:{}] unsupported IR entity kind was skipped during DXF export.",
                        if kind.is_empty() { "UNKNOWN" } else { &kind },
                        label_id(&id)
                    ),
                    diagnostic_id(&id),
                    Some("skipped"),
                );
                return Ok(Some("DXF_UNKNOWN_ENTITY_SKIPPED"));
            }
        },
    }
    Ok(None)
}

/// The MTEXT entity as ``_prepare_r12_entity`` leaves it: the colour as the
/// index it was approximated with, no line weight and no linetype scale. It is
/// what the lines of the text take their common values from.
fn r12_prepared_entity(entity: &Entity, common: &Common) -> Entity {
    let mut prepared = entity.clone();
    if let super::common::ColorOut::Index(index) = &common.color {
        if let Some(crate::model::Color::Text(_)) = &prepared.color {
            if let Ok(index) = index.as_str().parse::<i64>() {
                prepared.color = Some(crate::model::Color::Index(index));
            }
        }
    }
    prepared.lineweight_mm = None;
    if let Some(extra) = prepared.extra.as_mut() {
        extra.shift_remove("lineweight_mm");
        extra.shift_remove("linetype_scale");
    }
    prepared
}

fn ellipse_parameters(entity: &Entity, ellipse: &EllipseData) -> (f64, f64) {
    // ``entity.get("ccw") is False``
    let clockwise = match ellipse.ccw {
        Some(ccw) => !ccw,
        None => matches!(entity.extra_get("ccw"), Some(Value::Bool(false))),
    };
    if clockwise {
        (ellipse.end_param, ellipse.start_param)
    } else {
        (ellipse.start_param, ellipse.end_param)
    }
}

/// ``entity.get("closed")`` as a truth value.
fn polyline_is_closed(entity: &Entity, polyline: &PolylineData) -> bool {
    match polyline.closed {
        Some(closed) => closed,
        None => entity.extra_get("closed").is_some_and(Value::truthy),
    }
}

/// ``_lwpolyline_to_pairs``.
fn lwpolyline(
    out: &mut Vec<Pair>,
    vertices: &[Vertex],
    closed: bool,
    common: &Common,
    mode: Mode,
    context: &mut Context<'_>,
) -> Option<Val> {
    let handle = begin(out, "LWPOLYLINE", mode, context);
    common.write(out);
    sub(out, mode, "AcDbPolyline");
    out.push((90, Val::owned(vertices.len().to_string())));
    out.push((70, Val::S(if closed { "1" } else { "0" })));
    for vertex in vertices {
        out.push(n(10, vertex.x));
        out.push(n(20, vertex.y));
        if let Some(bulge) = vertex.bulge {
            out.push(n(42, bulge));
        }
    }
    handle
}

/// ``_polyline_r12_to_pairs``: a POLYLINE with its VERTEX records and SEQEND.
fn polyline_r12(
    out: &mut Vec<Pair>,
    vertices: &[Vertex],
    closed: bool,
    common: &Common,
    mode: Mode,
    context: &mut Context<'_>,
) {
    begin(out, "POLYLINE", mode, context);
    common.write(out);
    out.push((66, Val::S("1")));
    out.push((10, Val::S("0")));
    out.push((20, Val::S("0")));
    out.push((30, Val::S("0")));
    out.push((70, Val::S(if closed { "1" } else { "0" })));
    for vertex in vertices {
        begin_sub(out, "VERTEX", mode, None, context);
        out.push((8, common.layer.clone()));
        out.push(n(10, vertex.x));
        out.push(n(20, vertex.y));
        out.push((30, Val::S("0")));
        if let Some(bulge) = vertex.bulge {
            out.push(n(42, bulge));
        }
    }
    begin_sub(out, "SEQEND", mode, None, context);
    out.push((8, common.layer.clone()));
}

fn halign_code(halign: &str) -> i64 {
    match halign {
        "center" => 1,
        "right" => 2,
        _ => 0,
    }
}

fn valign_code(valign: &str) -> i64 {
    match valign {
        "bottom" => 1,
        "middle" => 2,
        "top" => 3,
        _ => 0,
    }
}

/// ``str(mapping.get(key, default))`` for an alignment: a string names one,
/// anything else none.
fn alignment<'a>(value: Option<&'a Value>, default: &'static str) -> &'a str {
    match value {
        None => default,
        Some(Value::Str(text)) => text,
        Some(_) => "",
    }
}

/// A number that is written when it is there (``if value is not None``).
fn optional_number(value: Option<&Value>, what: &str) -> Result<Option<f64>, WriteError> {
    match value {
        None | Some(Value::Null) => Ok(None),
        Some(value) => value
            .as_python_number()
            .map(Some)
            .ok_or_else(|| WriteError::invalid(format!("{what} is not a number"))),
    }
}

/// A name that is written when it is not empty (``if value: str(value)``).
fn optional_name(value: Option<&Value>, what: &str) -> Result<Option<Val>, WriteError> {
    match value {
        None => Ok(None),
        Some(value) if !value.truthy() => Ok(None),
        Some(value) => Ok(Some(Val::owned(py_str(value, what)?.into_owned()))),
    }
}

/// ``_text_to_pairs``.
fn text(
    out: &mut Vec<Pair>,
    entity: &Entity,
    data: &TextData,
    common: &Common,
    mode: Mode,
    context: &mut Context<'_>,
) -> Result<Option<Val>, WriteError> {
    let rotation = match data.rotation {
        Some(rotation) => rotation,
        None => float_or(entity.extra_get("rotation"), 0.0, "TEXT rotation")?,
    };
    let halign = alignment(entity.extra_get("halign"), "left");
    let valign = alignment(entity.extra_get("valign"), "baseline");
    let (halign_code, valign_code) = (halign_code(halign), valign_code(valign));
    // the factor a text is laid out with is a number that is no ``bool``; the
    // one that is written is whatever ``float`` reads
    let width_factor = match data.width_factor {
        Some(factor) => Some(factor),
        None => optional_number(entity.extra_get("width_factor"), "TEXT width_factor")?,
    };
    let justified = halign_code != 0 || valign_code != 0;
    let start = if justified {
        justified_text_start(
            data.insert,
            &data.text,
            data.height,
            rotation,
            data.width_factor,
            halign,
            valign,
        )
    } else {
        data.insert
    };
    let style = match &data.style {
        Some(style) => (!style.is_empty()).then(|| Val::A(Arc::clone(style))),
        None => optional_name(entity.extra_get("style"), "TEXT style")?,
    };
    let oblique = optional_number(entity.extra_get("oblique_deg"), "TEXT oblique_deg")?;

    let handle = begin(out, "TEXT", mode, context);
    common.write(out);
    sub(out, mode, "AcDbText");
    out.push(n(10, start[0]));
    out.push(n(20, start[1]));
    out.push(n(40, data.height));
    out.push((1, Val::owned(data.text.clone())));
    if rotation != 0.0 {
        out.push(n(50, rotation));
    }
    if let Some(style) = style {
        out.push((7, style));
    }
    if halign_code != 0 {
        out.push((72, Val::integer(halign_code)));
    }
    if valign_code != 0 && !mode.r2010 {
        out.push((73, Val::integer(valign_code)));
    }
    if justified {
        out.push(n(11, data.insert[0]));
        out.push(n(21, data.insert[1]));
    }
    if let Some(width_factor) = width_factor {
        out.push(n(41, width_factor));
    }
    if let Some(oblique) = oblique {
        out.push(n(51, oblique));
    }
    if mode.r2010 {
        // The horizontal justification (72) and the second point (11, 21)
        // belong to the first AcDbText group, the vertical one (73) to the
        // second; AutoCAD and ODA read them only there.
        out.push((100, Val::S("AcDbText")));
        if valign_code != 0 {
            out.push((73, Val::integer(valign_code)));
        }
    }
    Ok(handle)
}

/// The text of an MTEXT as DXF holds it. A text without inline codes, line
/// breaks, a width factor or a stored DWG text is written as it is; any other
/// is read by ``cad2d_ir.mtext`` (through the host).
fn mtext_text(entity: &Entity, context: &Context<'_>) -> Result<String, WriteError> {
    let plain = match entity.extra_get("text") {
        Some(Value::Str(text)) if !text.contains(['\\', '\r', '\n']) => Some(text),
        _ => None,
    };
    let stored = entity
        .extra_get("metadata")
        .and_then(Value::as_map)
        .and_then(|metadata| metadata.get("dwg"))
        .and_then(Value::as_map)
        .and_then(|section| section.get("raw_text"));
    match plain {
        Some(text)
            if entity.extra_get("width_factor").is_none()
                && !matches!(stored, Some(Value::Str(_))) =>
        {
            Ok(text.clone())
        }
        _ => context.host.mtext_text(entity),
    }
}

/// A DXF MTEXT keeps at most this many characters in a group; a longer text is
/// written in groups 3, with the rest in group 1.
const MTEXT_CHUNK: usize = 250;

/// ``_mtext_to_pairs``.
fn mtext(
    out: &mut Vec<Pair>,
    entity: &Entity,
    common: &Common,
    mode: Mode,
    context: &mut Context<'_>,
) -> Result<Option<Val>, WriteError> {
    let insert = required_point(entity.extra_get("insert"), "MTEXT insert")?;
    let height = required_float(entity.extra_get("height"), "MTEXT height")?;
    let text = mtext_text(entity, context)?;
    let rotation = float_or(entity.extra_get("rotation"), 0.0, "MTEXT rotation")?;
    let width = optional_number(entity.extra_get("width"), "MTEXT width")?;
    let style = optional_name(entity.extra_get("style"), "MTEXT style")?;
    let attach = match entity.extra_get("attach") {
        Some(Value::Str(attach)) => match attach.as_str() {
            "top_center" => Some(2),
            "top_right" => Some(3),
            "middle_left" => Some(4),
            "middle_center" => Some(5),
            "middle_right" => Some(6),
            "bottom_left" => Some(7),
            "bottom_center" => Some(8),
            "bottom_right" => Some(9),
            // ``top_left`` is the attachment of a record without the group
            _ => None,
        },
        _ => None,
    };

    let handle = begin(out, "MTEXT", mode, context);
    common.write(out);
    sub(out, mode, "AcDbMText");
    out.push(n(10, insert[0]));
    out.push(n(20, insert[1]));
    out.push(n(40, height));
    // chunks of ``MTEXT_CHUNK`` characters (not bytes)
    let mut rest: &str = &text;
    loop {
        let mut indices = rest.char_indices();
        match indices.nth(MTEXT_CHUNK) {
            Some((split, _)) => {
                out.push((3, Val::owned(rest[..split].to_string())));
                rest = &rest[split..];
            }
            None => break,
        }
    }
    out.push((1, Val::owned(rest.to_string())));
    if rotation != 0.0 {
        out.push(n(50, rotation));
    }
    if let Some(width) = width {
        out.push(n(41, width));
    }
    if let Some(style) = style {
        out.push((7, style));
    }
    if let Some(attach) = attach {
        out.push((71, Val::integer(attach)));
    }
    Ok(handle)
}

/// Where an INSERT is placed: its insertion point, rotation and scale as
/// ``_insert_to_pairs`` reads them, after ``_insert_from_transform``.
struct Placement {
    insert: [f64; 2],
    rotation: f64,
    /// Groups 41 and 42 when they are written.
    scale: Option<[f64; 2]>,
}

/// ``entity.get("scale", 1)`` as ``_insert_to_pairs`` writes it.
fn written_scale(entity: &Entity, insert: &InsertData) -> Result<Option<[f64; 2]>, WriteError> {
    let uniform = |scale: f64| ((scale - 1.0).abs() > 1e-12).then_some([scale, scale]);
    match insert.scale {
        Some(InsertScale::Uniform(scale)) => Ok(uniform(scale)),
        Some(InsertScale::Axes(scale)) => Ok(Some(scale)),
        None => match entity.extra_get("scale") {
            None => Ok(None),
            Some(Value::Bool(flag)) => Ok(uniform(if *flag { 1.0 } else { 0.0 })),
            Some(Value::List(items)) if items.len() == 2 => {
                match (items[0].as_python_number(), items[1].as_python_number()) {
                    (Some(x), Some(y)) => Ok(Some([x, y])),
                    _ => Err(WriteError::invalid("INSERT scale is not a number")),
                }
            }
            Some(_) => Ok(None),
        },
    }
}

fn insert_placement(
    entity: &Entity,
    insert: &InsertData,
    id: &str,
    env: &Env<'_>,
    context: &mut Context<'_>,
) -> Result<Placement, WriteError> {
    let stated = |entity: &Entity| -> Result<Placement, WriteError> {
        Ok(Placement {
            insert: insert.insert,
            rotation: match insert.rotation {
                Some(rotation) => rotation,
                None => float_or(entity.extra_get("rotation"), 0.0, "INSERT rotation")?,
            },
            scale: written_scale(entity, insert)?,
        })
    };
    let Some(transform) = entity.extra_get("transform") else {
        return stated(entity);
    };
    // ``_insert_from_transform``: DXF places a block by its insertion point,
    // one rotation and two signed scale factors:
    // ``p = insert + R(rotation) * S(sx, sy) * (q - base_point)``. ``transform``
    // maps the block straight into the parent, so the two agree when its
    // columns are perpendicular (a rotation, scaling and mirroring); the
    // insertion point then is where the base point of the block lands. A
    // transform that also shears is written without the shear.
    let values: Option<Vec<f64>> = transform
        .as_list()
        .filter(|items| items.len() == 6)
        .and_then(|items| items.iter().map(Value::as_python_number).collect());
    let Some(values) = values else {
        return Err(WriteError::invalid("INSERT transform is not six numbers"));
    };
    let (a, b, c, d, e, f) = (
        values[0], values[1], values[2], values[3], values[4], values[5],
    );
    let (base_x, base_y) = match env.block(&insert.block) {
        Some(block) => match (block.base_point, block.extra.get("base_point")) {
            (Some(base), _) => (base[0], base[1]),
            (None, other) => point_like(other).map_or((0.0, 0.0), |base| (base[0], base[1])),
        },
        None => (0.0, 0.0),
    };
    let scale_x = py_hypot(a, b);
    let determinant = a * d - b * c;
    if !(scale_x.is_finite() && scale_x > 1e-12 && determinant.abs() > 1e-24) {
        context.diagnose(
            "DXF_INSERT_TRANSFORM_OMITTED",
            "warning",
            format!(
                "[INSERT:{}] affine transform collapses the block and was omitted.",
                label_id(id)
            ),
            diagnostic_id(id),
            Some("skipped"),
        );
        return stated(entity);
    }
    let angle = b.atan2(a);
    let scale_y = determinant / scale_x;
    let tolerance = 1e-9 * scale_x;
    let sheared = !(py_isclose(c, -angle.sin() * scale_y, 1e-9, tolerance)
        && py_isclose(d, angle.cos() * scale_y, 1e-9, tolerance));
    let rotation = angle.to_degrees();
    let scale = if py_isclose(scale_x, scale_y, 1e-12, 0.0) {
        if py_isclose(scale_x, 1.0, 1e-12, 0.0) {
            None
        } else {
            ((scale_x - 1.0).abs() > 1e-12).then_some([scale_x, scale_x])
        }
    } else {
        Some([scale_x, scale_y])
    };
    if sheared {
        context.diagnose(
            "DXF_INSERT_TRANSFORM_OMITTED",
            "warning",
            format!(
                "[INSERT:{}] affine transform shears the block, which a DXF INSERT cannot; it \
                 was written with its rotation and scale only.",
                label_id(id)
            ),
            diagnostic_id(id),
            Some("approximated"),
        );
    }
    Ok(Placement {
        insert: [e + a * base_x + c * base_y, f + b * base_x + d * base_y],
        rotation: if rotation.abs() > 1e-9 { rotation } else { 0.0 },
        scale,
    })
}

/// The colour of an ATTRIB: an index or a true colour, written as it is in
/// both targets.
fn attribute_color(out: &mut Vec<Pair>, attribute: &Map) {
    match attribute.get("color") {
        Some(Value::Int(index)) => out.push((62, Val::integer(*index))),
        Some(Value::Bool(flag)) => out.push((62, Val::S(if *flag { "True" } else { "False" }))),
        Some(Value::Str(text)) => {
            if let Some(rgb) = super::common::hex_color(text) {
                out.push((420, Val::owned(rgb.to_string())));
            }
        }
        _ => {}
    }
}

/// ``_attribute_to_pairs``: an attribute with its own text geometry.
fn attribute(
    out: &mut Vec<Pair>,
    attribute: &Map,
    default_layer: &Val,
    parent: Option<&Val>,
    mode: Mode,
    context: &mut Context<'_>,
) -> Result<(), WriteError> {
    let insert = required_point(attribute.get("insert"), "ATTRIB insert")?;
    let layer = match attribute.get("layer") {
        None => default_layer.clone(),
        Some(value) => Val::owned(py_str(value, "ATTRIB layer")?.into_owned()),
    };
    let rotation = float_or(attribute.get("rotation"), 0.0, "ATTRIB rotation")?;
    let halign = alignment(attribute.get("halign"), "left");
    let valign = alignment(attribute.get("valign"), "baseline");
    let (halign_code, valign_code) = (halign_code(halign), valign_code(valign));
    let justified = halign_code != 0 || valign_code != 0;
    let content = match attribute.get("text") {
        None => Cow::Borrowed(""),
        Some(value) => py_str(value, "ATTRIB text")?,
    };
    let height = required_float(attribute.get("height"), "ATTRIB height")?;
    let start = if justified {
        justified_text_start(
            insert,
            &content,
            height,
            rotation,
            attribute.get("width_factor").and_then(Value::as_number),
            halign,
            valign,
        )
    } else {
        insert
    };
    let width_factor = optional_number(attribute.get("width_factor"), "ATTRIB width_factor")?;
    let oblique = optional_number(attribute.get("oblique_deg"), "ATTRIB oblique_deg")?;
    let style = optional_name(attribute.get("style"), "ATTRIB style")?;
    let tag = match attribute.get("tag") {
        Some(value) => py_str(value, "ATTRIB tag")?.into_owned(),
        None => return Err(WriteError::invalid("an attribute text has no tag")),
    };
    let invisible = matches!(attribute.get("visible"), Some(Value::Bool(false)));

    begin_sub(out, "ATTRIB", mode, parent, context);
    out.push((8, layer));
    attribute_color(out, attribute);
    sub(out, mode, "AcDbText");
    out.push(n(10, start[0]));
    out.push(n(20, start[1]));
    out.push(n(40, height));
    out.push((1, Val::owned(content.into_owned())));
    if rotation != 0.0 {
        out.push(n(50, rotation));
    }
    if let Some(width_factor) = width_factor {
        out.push(n(41, width_factor));
    }
    if let Some(oblique) = oblique {
        out.push(n(51, oblique));
    }
    if let Some(style) = style {
        out.push((7, style));
    }
    if halign_code != 0 {
        out.push((72, Val::integer(halign_code)));
    }
    if justified {
        out.push(n(11, insert[0]));
        out.push(n(21, insert[1]));
    }
    sub(out, mode, "AcDbAttribute");
    out.push((2, Val::owned(tag)));
    out.push((70, Val::S(if invisible { "1" } else { "0" })));
    if valign_code != 0 {
        out.push((74, Val::integer(valign_code)));
    }
    Ok(())
}

/// ``_insert_to_pairs``: the INSERT, and its attributes with their SEQEND.
fn insert_record(
    out: &mut Vec<Pair>,
    entity: &Entity,
    insert: &InsertData,
    placement: &Placement,
    common: &Common,
    mode: Mode,
    context: &mut Context<'_>,
) -> Result<Option<Val>, WriteError> {
    let attribute_texts: Vec<&Map> = match entity.extra_get("attribute_texts") {
        Some(Value::List(items)) => items.iter().filter_map(Value::as_map).collect(),
        _ => Vec::new(),
    };
    // a value without geometry is written at the insertion point
    let mut unplaced: Vec<(&str, Cow<'_, str>)> = Vec::new();
    if let Some(Value::Map(attributes)) = entity.extra_get("attributes") {
        let mut placed: Vec<Cow<'_, str>> = Vec::with_capacity(attribute_texts.len());
        for attribute in &attribute_texts {
            placed.push(match attribute.get("tag") {
                None => Cow::Borrowed("None"),
                Some(value) => py_str(value, "ATTRIB tag")?,
            });
        }
        for (tag, value) in attributes {
            if !placed.iter().any(|known| known == tag) {
                unplaced.push((tag, py_str(value, "INSERT attribute")?));
            }
        }
    }

    let handle = begin(out, "INSERT", mode, context);
    common.write(out);
    sub(out, mode, "AcDbBlockReference");
    out.push((2, Val::A(Arc::clone(&insert.block))));
    out.push(n(10, placement.insert[0]));
    out.push(n(20, placement.insert[1]));
    if placement.rotation != 0.0 {
        out.push(n(50, placement.rotation));
    }
    if let Some(scale) = placement.scale {
        out.push(n(41, scale[0]));
        out.push(n(42, scale[1]));
    }
    if attribute_texts.is_empty() && unplaced.is_empty() {
        return Ok(handle);
    }
    out.push((66, Val::S("1")));
    for attribute_text in attribute_texts {
        attribute(
            out,
            attribute_text,
            &common.layer,
            handle.as_ref(),
            mode,
            context,
        )?;
    }
    for (tag, value) in unplaced {
        begin_sub(out, "ATTRIB", mode, handle.as_ref(), context);
        out.push((8, common.layer.clone()));
        sub(out, mode, "AcDbText");
        out.push(n(10, placement.insert[0]));
        out.push(n(20, placement.insert[1]));
        out.push((40, Val::S("1")));
        if mode.r2010 {
            out.push((1, Val::owned(value.into_owned())));
            out.push((100, Val::S("AcDbAttribute")));
            out.push((2, Val::owned(tag.to_string())));
        } else {
            out.push((2, Val::owned(tag.to_string())));
            out.push((1, Val::owned(value.into_owned())));
        }
        out.push((70, Val::S("0")));
    }
    begin_sub(out, "SEQEND", mode, handle.as_ref(), context);
    out.push((8, common.layer.clone()));
    sub(out, mode, "AcDbSequenceEnd");
    Ok(handle)
}

/// ``bool(entity.get("solid", True))``.
fn hatch_is_solid(entity: &Entity, hatch: &HatchData) -> bool {
    match hatch.solid {
        Some(solid) => solid,
        None => entity.extra_get("solid").is_none_or(Value::truthy),
    }
}

/// ``_hatch_has_pattern_lines``.
fn hatch_has_pattern_lines(entity: &Entity) -> bool {
    matches!(
        entity.extra_get("pattern_lines"),
        Some(Value::List(lines)) if lines.iter().any(|line| matches!(line, Value::Map(_)))
    )
}

/// What ``_hatch_to_pairs`` raises for a HATCH whose loops are no loops.
fn hatch_error(entity: &Entity) -> WriteError {
    match entity.extra_get("loops") {
        Some(Value::List(loops)) if !loops.is_empty() => invalid_geometry(entity, "HATCH"),
        _ => WriteError::invalid("HATCH requires non-empty loops"),
    }
}

/// ``_hatch_to_pairs``.
fn hatch_record(
    out: &mut Vec<Pair>,
    entity: &Entity,
    hatch: &HatchData,
    common: &Common,
    mode: Mode,
    context: &mut Context<'_>,
) -> Result<Option<Val>, WriteError> {
    if hatch.loops.is_empty() {
        return Err(WriteError::invalid("HATCH requires non-empty loops"));
    }
    let solid = hatch_is_solid(entity, hatch);
    let pattern = if solid {
        Val::S("SOLID")
    } else {
        match entity.extra_get("pattern") {
            None => Val::S("SOLID"),
            Some(value) => Val::owned(py_str(value, "HATCH pattern")?.into_owned()),
        }
    };
    // the lines of the pattern, read before anything is written
    let mut pattern_pairs: Vec<Pair> = Vec::new();
    if !solid {
        let lines: &[Value] = match entity.extra_get("pattern_lines") {
            Some(Value::List(lines)) => lines,
            _ => &[],
        };
        pattern_pairs.push(n(
            52,
            float_or(
                entity.extra_get("pattern_angle"),
                0.0,
                "HATCH pattern_angle",
            )?,
        ));
        pattern_pairs.push(n(
            41,
            float_or(
                entity.extra_get("pattern_scale"),
                1.0,
                "HATCH pattern_scale",
            )?,
        ));
        pattern_pairs.push((77, Val::S("0")));
        pattern_pairs.push((78, Val::owned(lines.len().to_string())));
        for line in lines {
            let Value::Map(line) = line else {
                return Err(WriteError::invalid("a HATCH pattern line is no object"));
            };
            let dashes: &[Value] = match line.get("dashes") {
                Some(Value::List(dashes)) => dashes,
                Some(other) if other.truthy() => {
                    return Err(WriteError::invalid("HATCH pattern dashes are no list"));
                }
                _ => &[],
            };
            let base = required_point(line.get("base"), "HATCH pattern line base")?;
            let offset = required_point(line.get("offset"), "HATCH pattern line offset")?;
            pattern_pairs.push(n(
                53,
                required_float(line.get("angle"), "HATCH pattern line angle")?,
            ));
            pattern_pairs.push(n(43, base[0]));
            pattern_pairs.push(n(44, base[1]));
            pattern_pairs.push(n(45, offset[0]));
            pattern_pairs.push(n(46, offset[1]));
            pattern_pairs.push((79, Val::owned(dashes.len().to_string())));
            for dash in dashes {
                pattern_pairs.push(n(49, required_float(Some(dash), "HATCH pattern dash")?));
            }
        }
    }

    let handle = begin(out, "HATCH", mode, context);
    common.write(out);
    sub(out, mode, "AcDbHatch");
    for (code, value) in [
        (10, "0"),
        (20, "0"),
        (30, "0"),
        (210, "0"),
        (220, "0"),
        (230, "1"),
    ] {
        out.push((code, Val::S(value)));
    }
    out.push((2, pattern));
    out.push((70, Val::S(if solid { "1" } else { "0" })));
    out.push((71, Val::S("0")));
    out.push((91, Val::owned(hatch.loops.len().to_string())));
    for hatch_loop in &hatch.loops {
        let has_bulge = hatch_loop
            .vertices
            .iter()
            .any(|vertex| vertex.bulge.is_some());
        out.push((92, Val::S("2")));
        out.push((72, Val::S(if has_bulge { "1" } else { "0" })));
        out.push((73, Val::S("1")));
        out.push((93, Val::owned(hatch_loop.vertices.len().to_string())));
        for Vertex { x, y, bulge } in &hatch_loop.vertices {
            out.push(n(10, *x));
            out.push(n(20, *y));
            if has_bulge {
                out.push(n(42, bulge.unwrap_or(0.0)));
            }
        }
        // no source boundary objects (the boundary is not associative)
        out.push((97, Val::S("0")));
    }
    out.push((75, Val::S("0")));
    out.push((76, Val::S("1")));
    out.extend(pattern_pairs);
    out.push((98, Val::S("0")));
    Ok(handle)
}

/// What the writer reads of a SPLINE.
struct SplineFields {
    control_points: Vec<[f64; 2]>,
    degree: i64,
    closed: bool,
}

/// A list of numbers, each read with ``float``.
fn numbers(values: &[Value], what: &str) -> Result<Vec<f64>, WriteError> {
    values
        .iter()
        .map(|value| required_float(Some(value), what))
        .collect()
}

impl SplineFields {
    fn of(entity: &Entity) -> Result<SplineFields, WriteError> {
        let Some(Value::List(raw_points)) = entity.extra_get("control_points") else {
            return Err(invalid_geometry(entity, "SPLINE"));
        };
        let control_points = raw_points
            .iter()
            .map(|point| required_point(Some(point), "SPLINE control point"))
            .collect::<Result<Vec<_>, _>>()?;
        // ``int(entity["degree"])``
        let degree = match entity.extra_get("degree") {
            Some(Value::Int(degree)) => *degree,
            Some(Value::Bool(flag)) => i64::from(*flag),
            Some(Value::Float(degree)) if degree.is_finite() => degree.trunc() as i64,
            _ => return Err(WriteError::invalid("SPLINE degree is not an integer")),
        };
        Ok(SplineFields {
            control_points,
            degree,
            closed: entity.extra_get("closed").is_some_and(Value::truthy),
        })
    }

    /// The knots ``_sample_spline`` works with: the ones of the entity when
    /// there are as many as the control points and the degree ask for.
    fn sampling_knots(&self, entity: &Entity) -> Result<Option<Vec<f64>>, WriteError> {
        let count = self.control_points.len() as i64;
        let degree = self.degree.min(count - 1);
        match entity.extra_get("knots") {
            Some(Value::List(knots)) if knots.len() as i64 == count + degree + 1 => {
                Ok(Some(numbers(knots, "SPLINE knot")?))
            }
            _ => Ok(None),
        }
    }

    fn sampling_weights(&self, entity: &Entity) -> Result<Option<Vec<f64>>, WriteError> {
        match entity.extra_get("weights") {
            Some(Value::List(weights)) if weights.len() == self.control_points.len() => {
                Ok(Some(numbers(weights, "SPLINE weight")?))
            }
            _ => Ok(None),
        }
    }
}

/// ``_spline_to_pairs``.
fn spline(
    out: &mut Vec<Pair>,
    entity: &Entity,
    common: &Common,
    mode: Mode,
    context: &mut Context<'_>,
) -> Result<Option<Val>, WriteError> {
    let fields = SplineFields::of(entity)?;
    let knots = match entity.extra_get("knots") {
        Some(Value::List(knots)) if !knots.is_empty() => numbers(knots, "SPLINE knot")?,
        _ => curves::default_spline_knots(fields.control_points.len(), fields.degree)?,
    };
    let weights = match entity.extra_get("weights") {
        Some(Value::List(weights)) => numbers(weights, "SPLINE weight")?,
        _ => Vec::new(),
    };

    let handle = begin(out, "SPLINE", mode, context);
    common.write(out);
    sub(out, mode, "AcDbSpline");
    out.push((70, Val::S(if fields.closed { "1" } else { "0" })));
    out.push((71, Val::owned(fields.degree.to_string())));
    out.push((72, Val::owned(knots.len().to_string())));
    out.push((73, Val::owned(fields.control_points.len().to_string())));
    out.push((74, Val::S("0")));
    for knot in knots {
        out.push(n(40, knot));
    }
    for point in &fields.control_points {
        out.push(n(10, point[0]));
        out.push(n(20, point[1]));
    }
    for weight in weights {
        out.push(n(41, weight));
    }
    Ok(handle)
}
