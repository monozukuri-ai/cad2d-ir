//! The extension module ``cad2d_ir_native._core``: the bindings of ``cad2d-ir-py``.

use pyo3::prelude::*;

#[pymodule]
fn _core(module: &Bound<'_, PyModule>) -> PyResult<()> {
    cad2d_ir_py::register(module)
}
