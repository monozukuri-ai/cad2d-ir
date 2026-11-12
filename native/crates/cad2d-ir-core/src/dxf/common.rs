//! What every entity carries (layer, linetype, colour, line weight, visibility)
//! and how the loosely typed parts of an entity are read.
//!
//! The IR is a ``dict`` in Python, and a writer there reads it with ``get``,
//! ``str`` and ``float``. A value of the type the IR gives a field is read here
//! as it is there. A value of another type is an error of the document
//! ([`WriteError::Invalid`]), except where reading it is as simple as it is in
//! Python (``None`` for a layer is the layer ``None``, a ``bool`` is a number).

use std::borrow::Cow;
use std::sync::Arc;

use super::pairs::{Pair, Val};
use super::{Context, WriteError};
use crate::model::{Color, Entity};
use crate::num::{format_g, num};
use crate::value::Value;

/// ``_point_like``: a list of two or more whose first two items are numbers
/// (a ``bool`` is one).
pub(crate) fn point_like(value: Option<&Value>) -> Option<[f64; 2]> {
    let items = value?.as_list()?;
    if items.len() < 2 {
        return None;
    }
    Some([items[0].as_python_number()?, items[1].as_python_number()?])
}

/// ``float(mapping.get(key, default))`` for a value that is a number or absent.
pub(crate) fn float_or(value: Option<&Value>, default: f64, what: &str) -> Result<f64, WriteError> {
    match value {
        None => Ok(default),
        Some(value) => value
            .as_python_number()
            .ok_or_else(|| WriteError::invalid(format!("{what} is not a number"))),
    }
}

/// ``float(value)`` for a value that has to be there.
pub(crate) fn required_float(value: Option<&Value>, what: &str) -> Result<f64, WriteError> {
    value
        .and_then(Value::as_python_number)
        .ok_or_else(|| WriteError::invalid(format!("{what} is not a number")))
}

/// A point that has to be there: ``value[0]`` and ``value[1]`` as numbers.
pub(crate) fn required_point(value: Option<&Value>, what: &str) -> Result<[f64; 2], WriteError> {
    point_like(value).ok_or_else(|| WriteError::invalid(format!("{what} is not a point")))
}

/// ``str(value)`` for the values a document gives where a text is read: a
/// string, a number, a ``bool`` or ``None``.
pub(crate) fn py_str<'a>(value: &'a Value, what: &str) -> Result<Cow<'a, str>, WriteError> {
    Ok(match value {
        Value::Str(text) => Cow::Borrowed(text.as_str()),
        Value::Null => Cow::Borrowed("None"),
        Value::Bool(true) => Cow::Borrowed("True"),
        Value::Bool(false) => Cow::Borrowed("False"),
        Value::Int(number) => Cow::Owned(number.to_string()),
        Value::BigInt(digits) => Cow::Borrowed(digits.as_ref()),
        Value::Float(number) => Cow::Owned(crate::pystr::float_repr(*number)),
        Value::List(_) | Value::Map(_) => {
            return Err(WriteError::invalid(format!("{what} is not a text")));
        }
    })
}

/// ``str(entity.get("id", ""))``.
pub(crate) fn entity_id(entity: &Entity) -> Result<Cow<'_, str>, WriteError> {
    match (&entity.id, entity.extra_get("id")) {
        (Some(id), _) => Ok(Cow::Borrowed(id)),
        (None, None) => Ok(Cow::Borrowed("")),
        (None, Some(value)) => py_str(value, "entity id"),
    }
}

/// ``str(entity.get("kind", default))``.
pub(crate) fn kind_text<'a>(
    entity: &'a Entity,
    default: &'static str,
) -> Result<Cow<'a, str>, WriteError> {
    if let Some(kind) = entity.shape.kind() {
        return Ok(Cow::Borrowed(kind));
    }
    match entity.extra_get("kind") {
        None => Ok(Cow::Borrowed(default)),
        Some(value) => py_str(value, "entity kind"),
    }
}

/// The id of an entity as a message names it: the id, or ``?`` without one.
pub(crate) fn label_id(id: &str) -> &str {
    if id.is_empty() {
        "?"
    } else {
        id
    }
}

/// ``str(entity.get("layer", "0"))``.
pub(crate) fn layer_val(entity: &Entity) -> Result<Val, WriteError> {
    match (&entity.layer, entity.extra_get("layer")) {
        (Some(layer), _) => Ok(Val::A(Arc::clone(layer))),
        (None, None) => Ok(Val::S("0")),
        (None, Some(value)) => Ok(Val::owned(py_str(value, "entity layer")?.into_owned())),
    }
}

/// The linetype an entity names: ``entity.get("linetype")`` when it is not empty.
fn linetype_val(entity: &Entity) -> Result<Option<Val>, WriteError> {
    match (&entity.linetype, entity.extra_get("linetype")) {
        (Some(linetype), _) => Ok((!linetype.is_empty()).then(|| Val::A(Arc::clone(linetype)))),
        (None, None) => Ok(None),
        (None, Some(value)) if !value.truthy() => Ok(None),
        (None, Some(value)) => Ok(Some(Val::owned(
            py_str(value, "entity linetype")?.into_owned(),
        ))),
    }
}

/// ``_linetype_scale_value``: a positive number other than 1 (a ``bool`` is none).
pub(crate) fn linetype_scale_value(value: Option<&Value>) -> Option<f64> {
    let scale = value?.as_number()?;
    if !scale.is_finite() || scale <= 0.0 || (scale - 1.0).abs() < 1e-12 {
        return None;
    }
    Some(scale)
}

/// The RGB number of ``#RRGGBB`` or ``#RRGGBBAA``.
pub(crate) fn hex_color(text: &str) -> Option<u32> {
    let digits = text.strip_prefix('#')?;
    if !(digits.len() == 6 || digits.len() == 8) || !digits.bytes().all(|b| b.is_ascii_hexdigit()) {
        return None;
    }
    u32::from_str_radix(&digits[..6], 16).ok()
}

/// ``_nearest_aci``: the first of the seven basic colours that is nearest.
pub(crate) fn nearest_aci(rgb: u32) -> i64 {
    const PALETTE: [(i64, [i64; 3]); 7] = [
        (1, [255, 0, 0]),
        (2, [255, 255, 0]),
        (3, [0, 255, 0]),
        (4, [0, 255, 255]),
        (5, [0, 0, 255]),
        (6, [255, 0, 255]),
        (7, [255, 255, 255]),
    ];
    let channels = [
        ((rgb >> 16) & 0xFF) as i64,
        ((rgb >> 8) & 0xFF) as i64,
        (rgb & 0xFF) as i64,
    ];
    let distance = |colour: &[i64; 3]| -> i64 {
        (0..3)
            .map(|channel| (channels[channel] - colour[channel]).pow(2))
            .sum()
    };
    let mut nearest = PALETTE[0].0;
    let mut best = distance(&PALETTE[0].1);
    for (index, colour) in &PALETTE[1..] {
        let candidate = distance(colour);
        if candidate < best {
            nearest = *index;
            best = candidate;
        }
    }
    nearest
}

/// The line weights DXF has (group 370, in hundredths of a millimetre). AutoCAD
/// and ODA take any other value for "by layer", so a width in between is written
/// as the nearest of these.
const DXF_LINEWEIGHTS: [i64; 24] = [
    0, 5, 9, 13, 15, 18, 20, 25, 30, 35, 40, 50, 53, 60, 70, 80, 90, 100, 106, 120, 140, 158, 200,
    211,
];

/// ``_dxf_lineweight``: group 370 for a width in millimetres. Like ``min`` in
/// Python, the first of the line weights stays unless a later one is nearer
/// (a width that is not a number is written as 0).
pub(crate) fn dxf_lineweight(lineweight_mm: f64) -> i64 {
    let hundredths = lineweight_mm * 100.0;
    let mut nearest = DXF_LINEWEIGHTS[0];
    let mut distance = (nearest as f64 - hundredths).abs();
    for &weight in &DXF_LINEWEIGHTS[1..] {
        let candidate = (weight as f64 - hundredths).abs();
        if candidate < distance {
            nearest = weight;
            distance = candidate;
        }
    }
    nearest
}

/// ``_lineweight_is_normalized``: whether a width is written as another line
/// weight than it is (by half a hundredth of a millimetre or more).
pub(crate) fn lineweight_is_normalized(lineweight_mm: f64) -> bool {
    (dxf_lineweight(lineweight_mm) as f64 - lineweight_mm * 100.0).abs() >= 0.5
}

/// The line weight of an entity: ``lineweight_mm`` when it is a number
/// (``isinstance(value, (int, float))``, so a ``bool`` is one).
pub(crate) fn lineweight_of(entity: &Entity) -> Option<f64> {
    entity
        .lineweight_mm
        .or_else(|| match entity.extra_get("lineweight_mm") {
            Some(Value::Bool(flag)) => Some(if *flag { 1.0 } else { 0.0 }),
            _ => None,
        })
}

/// The colour of an entity as it is written.
#[derive(Clone, Debug, PartialEq)]
pub(crate) enum ColorOut {
    None,
    /// Group 62.
    Index(Val),
    /// Group 420.
    True(u32),
}

/// The common groups of an entity (``_common_to_pairs``).
#[derive(Clone, Debug)]
pub(crate) struct Common {
    pub(crate) layer: Val,
    pub(crate) linetype: Option<Val>,
    pub(crate) linetype_scale: Option<f64>,
    pub(crate) color: ColorOut,
    /// Group 370.
    pub(crate) lineweight: Option<i64>,
    pub(crate) invisible: bool,
}

fn color_of(entity: &Entity) -> ColorOut {
    match &entity.color {
        Some(Color::Index(index)) => ColorOut::Index(Val::integer(*index)),
        Some(Color::Text(text)) => match hex_color(text) {
            Some(rgb) => ColorOut::True(rgb),
            None => ColorOut::None,
        },
        None => match entity.extra_get("color") {
            // ``isinstance(True, int)``: written as ``str`` writes it
            Some(Value::Bool(true)) => ColorOut::Index(Val::S("True")),
            Some(Value::Bool(false)) => ColorOut::Index(Val::S("False")),
            _ => ColorOut::None,
        },
    }
}

impl Common {
    /// The common groups as the entity states them.
    pub(crate) fn plain(entity: &Entity) -> Result<Common, WriteError> {
        Ok(Common {
            layer: layer_val(entity)?,
            linetype: linetype_val(entity)?,
            linetype_scale: linetype_scale_value(entity.extra_get("linetype_scale")),
            color: color_of(entity),
            lineweight: lineweight_of(entity).map(dxf_lineweight),
            invisible: matches!(entity.extra_get("visible"), Some(Value::Bool(false))),
        })
    }

    /// ``_prepare_r12_entity``: the common groups DXF R12 can hold, with a
    /// diagnostic for each one it cannot (a true colour, a line weight, a
    /// linetype scale).
    pub(crate) fn prepared_r12(
        entity: &Entity,
        context: &mut Context<'_>,
    ) -> Result<Common, WriteError> {
        let mut common = Common::plain(entity)?;
        let id = entity_id(entity)?;
        let diagnostic_id = (!id.is_empty()).then(|| id.to_string());
        let kind = kind_text(entity, "ENTITY")?;
        if let (ColorOut::True(rgb), Some(Color::Text(text))) = (&common.color, &entity.color) {
            let aci = nearest_aci(*rgb);
            context.diagnose(
                "DXF_R12_TRUE_COLOR_APPROXIMATED",
                "warning",
                format!(
                    "[{kind}:{}] true color {text} was approximated as ACI {aci} for DXF R12.",
                    label_id(&id)
                ),
                diagnostic_id.clone(),
                Some("approximated"),
            );
            common.color = ColorOut::Index(Val::integer(aci));
        }
        if entity.lineweight_mm.is_some() || entity.extra_get("lineweight_mm").is_some() {
            common.lineweight = None;
            context.diagnose(
                "DXF_R12_LINEWEIGHT_OMITTED",
                "warning",
                format!(
                    "[{kind}:{}] lineweight was omitted from DXF R12.",
                    label_id(&id)
                ),
                diagnostic_id.clone(),
                Some("skipped"),
            );
        }
        if let Some(scale) = common.linetype_scale.take() {
            context.diagnose(
                "DXF_R12_LINETYPE_SCALE_OMITTED",
                "warning",
                format!(
                    "[{kind}:{}] linetype scale {} was omitted from DXF R12.",
                    label_id(&id),
                    format_g(scale, 6)
                ),
                diagnostic_id,
                Some("skipped"),
            );
        }
        Ok(common)
    }

    /// The common groups of ``entity`` for ``target``: as stated for R2010,
    /// prepared (with diagnostics) for R12.
    pub(crate) fn of(entity: &Entity, context: &mut Context<'_>) -> Result<Common, WriteError> {
        if context.r2010() {
            Common::plain(entity)
        } else {
            Common::prepared_r12(entity, context)
        }
    }

    pub(crate) fn write(&self, pairs: &mut Vec<Pair>) {
        pairs.push((8, self.layer.clone()));
        if let Some(linetype) = &self.linetype {
            pairs.push((6, linetype.clone()));
        }
        if let Some(scale) = self.linetype_scale {
            pairs.push((48, Val::owned(num(scale))));
        }
        match &self.color {
            ColorOut::None => {}
            ColorOut::Index(index) => pairs.push((62, index.clone())),
            ColorOut::True(rgb) => pairs.push((420, Val::owned(rgb.to_string()))),
        }
        if let Some(lineweight) = self.lineweight {
            pairs.push((370, Val::integer(lineweight)));
        }
        if self.invisible {
            pairs.push((60, Val::S("1")));
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_nearest_basic_colour_is_the_first_one_that_is_nearest() {
        assert_eq!(nearest_aci(0xFF0000), 1);
        assert_eq!(nearest_aci(0x00FFFF), 4);
        assert_eq!(nearest_aci(0xFFFFFF), 7);
        // black is as far from red as from green and blue: the first one stays
        assert_eq!(nearest_aci(0x000000), 1);
        assert_eq!(nearest_aci(0x808080), 7);
    }

    #[test]
    fn line_weights_are_the_nearest_dxf_has() {
        assert_eq!(dxf_lineweight(0.17), 18);
        assert_eq!(dxf_lineweight(0.08), 9);
        assert_eq!(dxf_lineweight(0.42), 40);
        assert_eq!(dxf_lineweight(0.07), 9);
        assert_eq!(dxf_lineweight(0.14), 15);
        assert_eq!(dxf_lineweight(5.0), 211);
        assert!(lineweight_is_normalized(0.17));
        assert!(!lineweight_is_normalized(0.25));
    }
}
