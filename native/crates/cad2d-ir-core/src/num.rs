//! Number formatting that matches the Python writer byte for byte.

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

/// C ``%.12g`` for a finite, non-zero value.
fn format_g12(value: f64) -> String {
    const PRECISION: i32 = 12;
    // twelve significant digits, correctly rounded, with the exponent after rounding
    let scientific = format!("{:.*e}", (PRECISION - 1) as usize, value);
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
    if !(-4..PRECISION).contains(&exponent) {
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
    fn rounding_ties_go_to_even() {
        assert_eq!(py_round_to_int(17.5), Some(18));
        assert_eq!(py_round_to_int(0.5), Some(0));
        assert_eq!(py_round_to_int(2.5), Some(2));
        assert_eq!(py_round_to_int(-0.5), Some(0));
    }
}
