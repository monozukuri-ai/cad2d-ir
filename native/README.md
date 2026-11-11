# cad2d-ir-native (experimental)

A native (Rust) core for the IR of `cad2d-ir`, built as a vertical slice through
one conversion: **JWW → DXF R2010**.

`cad2d_ir` (Python) stays the reference implementation. What is here is checked
against it:

- a document converted to the typed IR and back is an equal `dict`;
- the DXF text written here is, byte for byte, the text `ir_to_dxf` writes, with
  the same export diagnostics;
- a JWW file imported here gives the document, diagnostics and statistics of
  `convert_jww_file_to_ir`.

What the native code has not learnt it does not guess: it raises `Unsupported`
and the caller uses the Python implementation.

## Install

```sh
pip install cad2d-ir-native
```

The package is released together with `cad2d-ir`, under the same version, and
installs exactly that `cad2d-ir` (with its `jww` extra). There are wheels for
Linux (x86_64, aarch64), Windows (x86_64) and macOS (universal2), one for all
Python versions from 3.10 (abi3); elsewhere `pip` builds the source
distribution, which needs a Rust toolchain. `cad2d-ir` itself stays pure Python
and does not depend on this package.

## Layout

| Path | What |
| --- | --- |
| `crates/cad2d-ir-core` | The typed IR (`Document`, `Entity`, `Shape`, …), the conversion to and from the dynamic form of a `dict`, and the DXF R2010 writer |
| `crates/cad2d-ir-jww` | JWW entities (`ezjww-core`) to the typed IR: the entity conversion of `cad2d_ir/importers/jww.py` |
| `crates/cad2d-ir-py` | The Python bindings as a library: `register(module)` adds the functions and classes to an extension module |
| `src/lib.rs` | The extension module `cad2d_ir_native._core`: `cad2d-ir-py` and nothing else |
| `python/cad2d_ir_native` | The Python package; `jww.py` assembles a JWW import from the header code of `cad2d_ir.importers.jww` and the native entities |
| `tests/test_native.py` | The checks against the Python reference |

The crates take `ezjww-core` from a released tag of the `ezjww` repository
(`[workspace.dependencies]` in `Cargo.toml`), so a build needs nothing next to
this repository. To build against a checkout of `ezjww` instead (a change of
the reader that is not released yet), name it in `native/.cargo/config.toml`,
which is not committed:

```toml
[patch."https://github.com/monozukuri-ai/ezjww"]
ezjww-core = { path = "../../ezjww/crates/ezjww-core" }
```

`--locked` fails with such a patch, since `Cargo.lock` records the tag.

## Build and test

```sh
cd native
cargo test -p cad2d-ir-core
maturin build --release -i python3 --out target/wheels   # or: maturin develop --release
python -m pytest tests                                   # with the wheel installed
```

The JWW tests read `../../ezjww/jww_samples` (`EZJWW_SAMPLES` names another
directory) and are skipped without it.

## Release

`cad2d-ir` and `cad2d-ir-native` are one release. The version is written in
four places, which `python scripts/check_release.py` compares (and
`tests/test_package_metadata.py` with it): `pyproject.toml`,
`src/cad2d_ir/__init__.py`, `native/pyproject.toml` (the version and the
`cad2d-ir[jww]==` pin) and `native/Cargo.toml` (then `cargo build` in `native/`
for `Cargo.lock`). When the tag of `ezjww-core` in `native/Cargo.toml` changes,
the floor of `ezjww` in `native/pyproject.toml` follows it.

Pushing the tag `v<version>` runs `.github/workflows/release.yml`: it checks
the versions and the tag, runs the tests, builds `cad2d-ir` (wheel and source
distribution) and `cad2d-ir-native` (a wheel on each platform and a source
distribution), installs each native wheel with the `cad2d-ir` of the release
and runs `native/tests` on its platform, and publishes all files to PyPI. Run
by hand (`workflow_dispatch`) it does everything but publish.

Publishing uses PyPI [trusted publishing](https://docs.pypi.org/trusted-publishers/):
the repository stores no token, and PyPI accepts the upload because it comes
from this workflow. Each of the two projects on PyPI names the same publisher
(for a project that does not exist yet, as a pending publisher of the account):

| Field | Value |
| --- | --- |
| Owner | `monozukuri-ai` |
| Repository name | `cad2d-ir` |
| Workflow name | `release.yml` |
| Environment name | `pypi` |

## Use

```python
from cad2d_ir.importers.base import ImportOptions
from cad2d_ir_native import Unsupported
from cad2d_ir_native.jww import convert_jww_file

try:
    result = convert_jww_file("plan.jww", options=ImportOptions(strict=False))
    dxf_bytes, export_diagnostics = result.document.to_dxf_bytes()
except Unsupported:
    ...  # the Python importer and writer
document = result.document.to_dict()  # the dict convert_jww_file_to_ir returns
```

`cad2d_ir_native.ir_to_dxf(document_dict)` writes any IR `dict` the writer
understands, and `NativeDocument.from_dict` holds one in native code.

### Native code of an application

An application with Rust code of its own that works on the typed IR (a renderer,
an exporter) links `cad2d-ir-py` into its extension module, calls
`cad2d_ir_py::register(module)` there and takes a `NativeDocument` as an
argument of its own functions (`NativeDocument::document()` is the typed
`Document`). The classes of two extension modules are different types to Python,
so the application uses the bindings of its own module throughout:

```python
result = convert_jww_file("plan.jww", options=..., core=my_app._core)
my_app._core.render(result.document)  # a function of the application
```

`core` is the extension module whose `parse_jww` and `Unsupported` are used; it
defaults to the one of this package.

The few entities a caller has to look at in Python can be read and changed
without the whole document becoming a `dict`:

```python
for block, index, entity in document.entities_of_kind("IMAGE", without=["data"]):
    pixels = document.entity_bytes(block, index, "data")  # base64 decoded, or None
    document.set_entity_value(block, index, "name", "plan_" + entity["name"])
```

`without` leaves keys out of the `dict`s, and `entity_bytes` gives the bytes of
a base64 value without the text becoming a Python `str`: the `data` of an
embedded image is as large as its file, which can be hundreds of megabytes.

## What is covered

- IR: `LINE`, `CIRCLE`, `ARC`, `POINT`, `ELLIPSE`, `TEXT`, `INSERT`, `HATCH` and
  `LWPOLYLINE` are structs. Every other entity, and every value that is not of
  the type the schema gives it, stays dynamic and converts back unchanged.
- DXF writer: R2010 for those entities (solid hatches only), layers, linetypes,
  text styles and blocks. A `DIMENSION` whose `dim_kind` is `GENERIC` (what the
  JWW importer makes) is written as the lines, texts and points it was made
  of, as the Python writer does. An `IMAGE` is written with its `IMAGEDEF` and
  `IMAGEDEF_REACTOR` objects, the image dictionary and the classes. Declined:
  every other `DIMENSION` (they need a geometry block), a `GENERIC` one that
  names a block, `MTEXT`, `SPLINE`, justified or oblique text, pattern hatches,
  paper space layouts, R12.
- JWW: every entity kind of a drawing, with and without entity provenance,
  the images of the archive of the drawing (decompressed and carried as base64,
  as the Python importer does) and what the parser reports (replaced
  characters, a list it could not read to its end). Declined: a file whose
  strings were read as UTF-8, an embedded image that does not decompress, an
  image placement the reader does not parse, an entity on a layer the header
  does not have, JWS symbols.

Two lookups are still asked from Python while writing, a few times per document:
the font of a text style (`cad2d_ir.fonts.style_font`) and the code page of the
header (`_select_r12_codepage`).

## What it costs to keep

The document level of `python/cad2d_ir_native/jww.py` follows
`_jw_document_to_ir` of the Python importer, and the Rust entity conversion and
DXF writer follow their Python originals. Until the Python package calls into
this one, a change on either side has to be made on the other; the tests above
are what notices a difference.

The numbers are those of Python 3.12 and later: `sum()` over floats is
compensated there (`num::py_sum`), and a centre or a length added one value
after the other can differ in its last bit.

## Measurements

JWW drawings of lines only (synthetic), one process per run, same DXF output for
the first two columns. `ezjww` is `ezjww.write_dxf_with_report`, whose output is
a different DXF.

| JWW | Python import + `ir_to_dxf` | native | `ezjww` |
| --- | --- | --- | --- |
| 0.7 MB | 0.58 s, 64 MB | 0.16 s, 46 MB | 0.05 s, 31 MB |
| 3.6 MB | 2.69 s, 195 MB | 0.33 s, 96 MB | 0.15 s, 70 MB |
| 9.4 MB | 7.08 s, 467 MB | 0.72 s, 199 MB | 0.47 s, 155 MB |
| 15 MB | 11.08 s, 709 MB | 1.04 s, 282 MB | 0.66 s, 218 MB |
