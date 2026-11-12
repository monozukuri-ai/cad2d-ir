//! Curves as polylines, for DXF R12 (which has no ELLIPSE and no SPLINE), and
//! the knots a SPLINE is written with.

use super::WriteError;
use crate::num::{py_ceil_to_int, py_isclose, py_sum};

const TAU: f64 = 2.0 * std::f64::consts::PI;

/// Whether the parameters span the whole ellipse.
pub(crate) fn ellipse_is_closed(start_param: f64, end_param: f64) -> bool {
    let span = (end_param - start_param).abs();
    py_isclose(span, TAU, 0.0, 1e-9)
}

/// Points on an ellipse from its start to its end parameter (in radians),
/// ``curve_segments`` of them for a whole turn: the centre, the end of the
/// major axis from the centre and the ratio of the minor axis to it. The end
/// point is among them unless the ellipse is whole.
pub fn sample_ellipse(
    center: [f64; 2],
    major: [f64; 2],
    ratio: f64,
    start: f64,
    end: f64,
    curve_segments: u32,
) -> Result<Vec<[f64; 2]>, WriteError> {
    let mut span = end - start;
    if span.is_infinite() {
        return Err(WriteError::invalid("ELLIPSE parameters are not finite"));
    }
    while span <= 0.0 {
        span += TAU;
    }
    let closed = py_isclose(span, TAU, 0.0, 1e-9);
    let turns = f64::from(curve_segments) * span / TAU;
    let count = py_ceil_to_int(turns)
        .ok_or_else(|| WriteError::invalid("ELLIPSE parameters are not finite"))?
        .max(2);
    let sample_count = if closed { count } else { count + 1 };
    let minor = [-major[1] * ratio, major[0] * ratio];
    let mut vertices = Vec::with_capacity(sample_count as usize);
    for index in 0..sample_count {
        let parameter = start + span * index as f64 / count as f64;
        vertices.push([
            center[0] + major[0] * parameter.cos() + minor[0] * parameter.sin(),
            center[1] + major[1] * parameter.cos() + minor[1] * parameter.sin(),
        ]);
    }
    Ok(vertices)
}

/// Clamped uniform knots for a spline without knots of its own.
pub(crate) fn default_spline_knots(
    control_point_count: usize,
    degree: i64,
) -> Result<Vec<f64>, WriteError> {
    let total = control_point_count as i64 + degree + 1;
    let edge = degree + 1;
    let interior = total - 2 * edge;
    if interior < 0 {
        return Err(WriteError::invalid(
            "Invalid SPLINE definition: too few control points for the degree",
        ));
    }
    let edge = edge.max(0) as usize;
    let mut knots = vec![0.0; edge];
    if interior > 0 {
        let step = 1.0 / (interior + 1) as f64;
        knots.extend((0..interior).map(|index| step * (index + 1) as f64));
    }
    knots.extend(std::iter::repeat_n(1.0, edge));
    Ok(knots)
}

/// The basis functions of every control point at ``parameter``.
fn bspline_basis(
    parameter: f64,
    degree: usize,
    knots: &[f64],
    control_point_count: usize,
    domain_end: f64,
) -> Vec<f64> {
    let mut basis = vec![0.0; control_point_count];
    for index in 0..control_point_count {
        if (knots[index] <= parameter && parameter < knots[index + 1])
            || (parameter == domain_end && index == control_point_count - 1)
        {
            basis[index] = 1.0;
        }
    }
    for order in 1..=degree {
        let mut next_basis = vec![0.0; control_point_count];
        for index in 0..control_point_count {
            let left_denominator = knots[index + order] - knots[index];
            if left_denominator != 0.0 {
                next_basis[index] += (parameter - knots[index]) / left_denominator * basis[index];
            }
            if index + 1 < control_point_count {
                let right_denominator = knots[index + order + 1] - knots[index + 1];
                if right_denominator != 0.0 {
                    next_basis[index] += (knots[index + order + 1] - parameter) / right_denominator
                        * basis[index + 1];
                }
            }
        }
        basis = next_basis;
    }
    basis
}

/// ``curve_segments + 1`` points on a (rational) B-spline.
///
/// Knots of another length than the control points and the degree ask for, or
/// that go down, are replaced by clamped uniform ones, and weights of another
/// length than the control points by ones. The degree is at most the number of
/// control points less one.
pub fn sample_spline(
    control_points: &[[f64; 2]],
    degree: i64,
    knots: Option<&[f64]>,
    weights: Option<&[f64]>,
    curve_segments: u32,
) -> Result<Vec<[f64; 2]>, WriteError> {
    let count = control_points.len();
    let degree = degree.min(count as i64 - 1);
    if degree < 0 {
        return Err(WriteError::invalid("SPLINE degree is negative"));
    }
    let expected = count as i64 + degree + 1;
    let default_knots;
    let knots: &[f64] = match knots {
        Some(knots)
            if knots.len() as i64 == expected
                && !knots.windows(2).any(|pair| pair[0] > pair[1]) =>
        {
            knots
        }
        _ => {
            default_knots = default_spline_knots(count, degree)?;
            &default_knots
        }
    };
    let default_weights;
    let weights: &[f64] = match weights {
        Some(weights) if weights.len() == count => weights,
        _ => {
            default_weights = vec![1.0; count];
            &default_weights
        }
    };
    let degree = degree as usize;
    let start = knots[degree];
    let end = knots[count];
    // a NaN is no domain either (``end <= start`` is false for one in Python,
    // and every basis function is zero then)
    if end <= start {
        return Err(WriteError::invalid("Invalid SPLINE knot domain"));
    }

    let mut vertices = Vec::with_capacity(curve_segments as usize + 1);
    for sample in 0..=curve_segments {
        let parameter = if sample == curve_segments {
            end
        } else {
            start + (end - start) * f64::from(sample) / f64::from(curve_segments)
        };
        let basis = bspline_basis(parameter, degree, knots, count, end);
        let denominator = py_sum((0..count).map(|index| basis[index] * weights[index]));
        if denominator.abs() < 1e-15 {
            continue;
        }
        let coordinate = |axis: usize| {
            py_sum(
                (0..count).map(|index| basis[index] * weights[index] * control_points[index][axis]),
            ) / denominator
        };
        vertices.push([coordinate(0), coordinate(1)]);
    }
    if vertices.len() < 2 {
        return Err(WriteError::invalid(
            "SPLINE approximation produced fewer than two vertices",
        ));
    }
    Ok(vertices)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn default_knots_are_clamped_and_uniform() {
        assert_eq!(
            default_spline_knots(4, 3).unwrap(),
            vec![0.0, 0.0, 0.0, 0.0, 1.0, 1.0, 1.0, 1.0]
        );
        assert_eq!(
            default_spline_knots(5, 2).unwrap(),
            vec![0.0, 0.0, 0.0, 1.0 / 3.0, 2.0 / 3.0, 1.0, 1.0, 1.0]
        );
        assert!(default_spline_knots(2, 3).is_err());
    }

    #[test]
    fn a_whole_ellipse_is_closed() {
        assert!(ellipse_is_closed(0.0, TAU));
        assert!(!ellipse_is_closed(0.0, 3.0));
        let points = sample_ellipse([0.0, 0.0], [2.0, 0.0], 0.5, 0.0, TAU, 8).unwrap();
        assert_eq!(points.len(), 8);
        assert_eq!(points[0], [2.0, 0.0]);
        let points = sample_ellipse([0.0, 0.0], [2.0, 0.0], 0.5, 0.0, 1.0, 96).unwrap();
        assert_eq!(points.len(), 17);
    }

    #[test]
    fn a_spline_starts_and_ends_at_its_end_points() {
        let points = sample_spline(
            &[[0.0, 0.0], [1.0, 2.0], [3.0, 2.0], [4.0, 0.0]],
            3,
            None,
            None,
            8,
        )
        .unwrap();
        assert_eq!(points.len(), 9);
        assert_eq!(points[0], [0.0, 0.0]);
        assert_eq!(points[8], [4.0, 0.0]);
    }
}
