//! Number formatting of the DXF writer: that of Python (``%g``, ``repr``,
//! ``sum``), which the writer this code replaced was written in. The DXF text
//! is defined by it.

/// ``_num`` of the Python DXF writer: an integral value as an integer, any
/// other with twelve significant digits (``%.12g``), and never ``-0``.
pub fn num(value: f64) -> String {
    if value.is_nan() {
        return "nan".to_string();
    }
    if value.is_infinite() {
        return if value > 0.0 { "inf" } else { "-inf" }.to_string();
    }
    if value == value.trunc() {
        if value == 0.0 {
            return "0".to_string();
        }
        // ``str(int(x))``: every digit, no exponent
        return format!("{value:.0}");
    }
    let text = format_g12(value);
    if text == "-0" {
        "0".to_string()
    } else {
        text
    }
}

/// ``sum()`` of Python 3.12 and later over floats: Neumaier's compensated
/// summation, not one addition after the other. The last bit of a centre or a
/// length can differ between the two, and with it an order or a written digit.
pub fn py_sum(values: impl IntoIterator<Item = f64>) -> f64 {
    let mut result = 0.0_f64;
    let mut compensation = 0.0_f64;
    for value in values {
        let total = result + value;
        if result.abs() >= value.abs() {
            compensation += (result - total) + value;
        } else {
            compensation += (value - total) + result;
        }
        result = total;
    }
    // the compensation of an overflowed sum is no number
    if compensation != 0.0 && compensation.is_finite() {
        result += compensation;
    }
    result
}

/// C ``%.{precision}g`` (``f"{value:g}"`` of Python with that precision).
pub fn format_g(value: f64, precision: usize) -> String {
    if value.is_nan() {
        return "nan".to_string();
    }
    if value.is_infinite() {
        return if value > 0.0 { "inf" } else { "-inf" }.to_string();
    }
    if value == 0.0 {
        return if value.is_sign_negative() { "-0" } else { "0" }.to_string();
    }
    let precision = precision.max(1) as i32;
    // the significant digits, correctly rounded, with the exponent after rounding
    let scientific = format!("{:.*e}", (precision - 1) as usize, value);
    let (mantissa, exponent) = scientific
        .split_once('e')
        .expect("scientific notation has an exponent");
    let exponent: i32 = exponent.parse().expect("exponent is an integer");
    let negative = mantissa.starts_with('-');
    let digits: String = mantissa.chars().filter(|c| c.is_ascii_digit()).collect();
    let mut out = String::with_capacity(24);
    if negative {
        out.push('-');
    }
    if !(-4..precision).contains(&exponent) {
        let trimmed = digits.trim_end_matches('0');
        out.push_str(&trimmed[..1]);
        if trimmed.len() > 1 {
            out.push('.');
            out.push_str(&trimmed[1..]);
        }
        out.push('e');
        out.push(if exponent < 0 { '-' } else { '+' });
        out.push_str(&format!("{:02}", exponent.abs()));
        return out;
    }
    if exponent >= 0 {
        let split = (exponent + 1) as usize;
        out.push_str(&digits[..split]);
        let fraction = digits[split..].trim_end_matches('0');
        if !fraction.is_empty() {
            out.push('.');
            out.push_str(fraction);
        }
    } else {
        let fraction_digits = digits.trim_end_matches('0');
        out.push_str("0.");
        for _ in 0..(-exponent - 1) {
            out.push('0');
        }
        out.push_str(fraction_digits);
    }
    out
}

/// C ``%.12g`` for a finite, non-zero value.
fn format_g12(value: f64) -> String {
    format_g(value, 12)
}

/// ``math.hypot(a, b)`` of CPython 3.11 and later (its ``vector_norm``), which
/// can differ from the ``hypot`` of the C library in the last bit.
pub fn py_hypot(a: f64, b: f64) -> f64 {
    let a = a.abs();
    let b = b.abs();
    let mut max = 0.0_f64;
    if a > max {
        max = a;
    }
    if b > max {
        max = b;
    }
    vector_norm([a, b], max, a.is_nan() || b.is_nan())
}

fn vector_norm(mut vec: [f64; 2], max: f64, found_nan: bool) -> f64 {
    if max.is_infinite() {
        return max;
    }
    if found_nan {
        return f64::NAN;
    }
    if max == 0.0 {
        return max;
    }
    let max_e = frexp_exponent(max);
    if max_e < -1023 {
        for value in &mut vec {
            *value /= f64::MIN_POSITIVE;
        }
        return f64::MIN_POSITIVE * vector_norm(vec, max / f64::MIN_POSITIVE, found_nan);
    }
    let scale = power_of_two(-max_e);
    let mut csum = 1.0_f64;
    let mut frac1 = 0.0_f64;
    let mut frac2 = 0.0_f64;
    for value in vec {
        let x = value * scale;
        let (pr_hi, pr_lo) = dl_mul(x, x);
        let (sm_hi, sm_lo) = dl_fast_sum(csum, pr_hi);
        csum = sm_hi;
        frac1 += pr_lo;
        frac2 += sm_lo;
    }
    let mut h = (csum - 1.0 + (frac1 + frac2)).sqrt();
    let (pr_hi, pr_lo) = dl_mul(-h, h);
    let (sm_hi, sm_lo) = dl_fast_sum(csum, pr_hi);
    csum = sm_hi;
    frac1 += pr_lo;
    frac2 += sm_lo;
    let x = csum - 1.0 + (frac1 + frac2);
    h += x / (2.0 * h);
    h / scale
}

fn dl_fast_sum(a: f64, b: f64) -> (f64, f64) {
    let x = a + b;
    let y = (a - x) + b;
    (x, y)
}

fn dl_mul(x: f64, y: f64) -> (f64, f64) {
    let z = x * y;
    let zz = x.mul_add(y, -z);
    (z, zz)
}

/// The exponent of ``frexp(value)`` (value = m * 2^e with 0.5 <= m < 1) for a
/// positive finite value.
fn frexp_exponent(value: f64) -> i32 {
    let bits = value.to_bits();
    let biased = ((bits >> 52) & 0x7ff) as i32;
    if biased != 0 {
        return biased - 1022;
    }
    // subnormal: from the place of the first 1 of the fraction
    let fraction = bits & ((1u64 << 52) - 1);
    let top = 63 - fraction.leading_zeros() as i32;
    top - 1073
}

/// ``ldexp(1.0, exponent)``.
fn power_of_two(exponent: i32) -> f64 {
    if exponent >= -1022 {
        f64::from_bits(((exponent + 1023) as u64) << 52)
    } else {
        f64::from_bits(1u64 << (exponent + 1074))
    }
}

/// ``math.isclose(a, b, rel_tol=rel_tol, abs_tol=abs_tol)``.
pub fn py_isclose(a: f64, b: f64, rel_tol: f64, abs_tol: f64) -> bool {
    if a == b {
        return true;
    }
    if a.is_infinite() || b.is_infinite() {
        return false;
    }
    let diff = (b - a).abs();
    (diff <= (rel_tol * b).abs()) || (diff <= (rel_tol * a).abs()) || (diff <= abs_tol)
}

/// ``math.ceil(value)`` as an integer; ``None`` where Python raises (a NaN or
/// an infinity) and for a value no ``i64`` holds.
pub fn py_ceil_to_int(value: f64) -> Option<i64> {
    let ceiled = value.ceil();
    if !ceiled.is_finite() || ceiled.abs() >= 9.0e18 {
        return None;
    }
    Some(ceiled as i64)
}

/// ``int(round(x))`` of Python: to the nearest integer, a tie to the even one.
pub fn py_round_to_int(value: f64) -> Option<i64> {
    if !value.is_finite() {
        return None;
    }
    let rounded = value.round_ties_even();
    if rounded.abs() >= 9.0e18 {
        return None;
    }
    Some(rounded as i64)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn sums_are_compensated_like_python() {
        // python3.13 -c "print(repr(sum([0.1] * 10)), repr(sum([1e16, 1.0, -1e16])))"
        assert_eq!(py_sum([0.1; 10]), 1.0);
        assert_eq!(py_sum([1e16, 1.0, -1e16]), 1.0);
        assert_eq!(py_sum([]), 0.0);
        assert_eq!(py_sum([f64::INFINITY, 1.0]), f64::INFINITY);
        assert_eq!(py_sum([1.5, 2.25]), 3.75);
    }

    #[test]
    fn integral_values_have_no_fraction() {
        assert_eq!(num(5.0), "5");
        assert_eq!(num(-0.0), "0");
        assert_eq!(num(1e15), "1000000000000000");
        assert_eq!(num(-3.0), "-3");
    }

    #[test]
    fn twelve_significant_digits() {
        assert_eq!(num(0.1), "0.1");
        assert_eq!(num(1.0 / 3.0), "0.333333333333");
        assert_eq!(num(123456.789012345), "123456.789012");
        assert_eq!(num(0.00001234), "1.234e-05");
        assert_eq!(num(1.5e-7), "1.5e-07");
        assert_eq!(num(123456789012.5), "123456789012");
        assert_eq!(num(1234567890123.5), "1.23456789012e+12");
        assert_eq!(num(-2.5), "-2.5");
        assert_eq!(num(0.30000000000000004), "0.3");
        assert_eq!(num(99.99999999999999), "100");
    }

    #[test]
    fn general_format_with_six_digits() {
        // python3 -c "print(f'{2.0:g}', f'{0.5:g}', f'{1e-7:g}', f'{1234567.0:g}', f'{0.000123456789:g}')"
        assert_eq!(format_g(2.0, 6), "2");
        assert_eq!(format_g(0.5, 6), "0.5");
        assert_eq!(format_g(1e-7, 6), "1e-07");
        assert_eq!(format_g(1234567.0, 6), "1.23457e+06");
        assert_eq!(format_g(0.000123456789, 6), "0.000123457");
    }

    #[test]
    fn hypot_is_the_one_of_python() {
        // python3.13 -c "import math; print(repr(math.hypot(3.3, 4.7)), repr(math.hypot(1e-3, 1e-7)))"
        assert_eq!(py_hypot(3.3, 4.7), 5.742821606144492);
        assert_eq!(py_hypot(1e-3, 1e-7), 0.001000000005);
        assert_eq!(py_hypot(0.0, 0.0), 0.0);
        assert!(py_hypot(f64::NAN, 1.0).is_nan());
        assert_eq!(py_hypot(f64::INFINITY, f64::NAN), f64::INFINITY);
    }

    #[test]
    fn isclose_is_the_one_of_python() {
        assert!(py_isclose(1.0, 1.0 + 1e-13, 1e-12, 0.0));
        assert!(!py_isclose(1.0, 1.0 + 1e-11, 1e-12, 0.0));
        assert!(py_isclose(0.0, 1e-10, 0.0, 1e-9));
        assert!(!py_isclose(f64::INFINITY, 1.0, 1e-9, 1e-9));
        assert!(py_isclose(f64::INFINITY, f64::INFINITY, 0.0, 0.0));
    }

    #[test]
    fn rounding_ties_go_to_even() {
        assert_eq!(py_round_to_int(17.5), Some(18));
        assert_eq!(py_round_to_int(0.5), Some(0));
        assert_eq!(py_round_to_int(2.5), Some(2));
        assert_eq!(py_round_to_int(-0.5), Some(0));
    }
}
