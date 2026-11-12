# cad2d-ir-native

The native code (Rust) of [`cad2d-ir`](../README.md): a typed form of the IR,
the **DXF writer** and the **entity conversion of the JWW / JWS importer**.

`cad2d-ir` depends on this package and uses it for both. There is no Python
implementation of either next to it (there was one up to 0.17.22; the two were
compared byte for byte before the Python one was removed in 0.18.0).

## Install

```sh
pip install cad2d-ir
```

installs this package with it: `cad2d-ir` pins the `cad2d-ir-native` of its own
version, and the two are released together. There are wheels for Linux (x86_64
and aarch64, glibc), Windows (x86_64) and macOS (universal2), one for all
Python versions from 3.10 (abi3); elsewhere `pip` builds the source
distribution, which needs a Rust toolchain.

The package has no dependencies of its own and is not meant to be installed
alone: the DXF writer calls back into `cad2d_ir` (see "What stays in Python").

## Layout

| Path | What |
| --- | --- |
| `crates/cad2d-ir-core` | The typed IR (`Document`, `Entity`, `Shape`, …), the conversion to and from the dynamic form of a `dict`, and the DXF writer (`src/dxf/`: R12 and R2010) |
| `crates/cad2d-ir-jww` | JWW and JWS entities (`ezjww-core`) to the typed IR |
| `crates/cad2d-ir-py` | The Python bindings as a library: `register(module)` adds the functions and classes to an extension module |
| `src/lib.rs` | The extension module `cad2d_ir_native._core`: `cad2d-ir-py` and nothing else |
| `python/cad2d_ir_native` | The Python package: the names of the extension module |
| `tests/` | `test_native.py` (the typed document, and that a `dict` and the typed IR, a file and the `dict` of `ezjww` give the same result), `test_dxf_golden.py` with `golden/` (the DXF of a set of documents, as files), `test_text_cells.py` (a table of the native code against the codec of Python) |

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

## What stays in Python

Each piece of work is done in one place. What needs the codecs and the tables
of Python is done there, and the native code asks for it:

- DXF writer: the font of a text style (`cad2d_ir.fonts.style_font`), the code
  page of an R12 file and what a character it cannot store is written as
  (`_select_r12_codepage`, `_r12_text_plan` in `cad2d_ir/codecs/dxf.py`), the
  inline codes of `MTEXT` and its lines for R12 (`cad2d_ir.mtext`), and the
  view of a viewport (`cad2d_ir.layouts.viewport_to_dxf_view`). These are a
  few calls per document, not per entity.
- JWW importer: what comes from the header of a file - layers, line types, the
  pen table, the metadata and the diagnostics about the document - is made in
  `cad2d_ir/importers/jww.py`, which reads the header with `ezjww`. The native
  code reads the file itself for the entities. A `dict` of
  `ezjww.read_document` (`jww_document_to_ir`, JWS symbols, a drawing whose
  strings are UTF-8) is read into the records of `ezjww-core` first
  (`crates/cad2d-ir-py/src/jww_dict.rs`) and converted by the same code.

`cad2d-ir-core/src/num.rs` and `pystr.rs` write numbers and compare strings
the way Python does (`repr` of a float, `%g`, `sum`, `str.casefold`): the DXF
text is defined by the Python writer that came first.

## Build and test

In a checkout, `uv sync` at the top of the repository builds this directory
and installs it (`[tool.uv.sources]` in `../pyproject.toml`), and builds it
again when a file here changes. Then:

```sh
uv run pytest -q                  # tests/ and native/tests
cd native
cargo fmt --all --check
cargo clippy --workspace --all-targets --locked -- -D warnings
cargo test -p cad2d-ir-core -p cad2d-ir-jww --locked
```

Without `uv`: `pip install ./native` and then `pip install -e ".[jww]"` (in
this order: the version `cad2d-ir` pins is the one just built).

The tests with real drawings read `../../ezjww/jww_samples` (`EZJWW_SAMPLES`
names another directory) and are skipped without it.

### Golden files

`tests/golden/<name>.json` is an IR document; `<name>.ac1024.dxf` and
`<name>.ac1009.dxf` are what the writer makes of it, and `<name>.report.json`
the export diagnostics and the entity map. They are what notices a change of
the writer's output. When the change is meant:

```sh
CAD2D_IR_UPDATE_GOLDEN=1 uv run pytest native/tests/test_dxf_golden.py
git diff native/tests/golden      # review what changed
```

A number in a DXF may differ in its last digits between platforms (it comes
from the sine and cosine of the platform), and the test allows for that;
everything else is compared to the byte.

## Release

`cad2d-ir` and `cad2d-ir-native` are one release. The version is written in
four places, which `python scripts/check_release.py` compares (and
`tests/test_package_metadata.py` with it): `pyproject.toml` (the version and
the `cad2d-ir-native==` pin), `src/cad2d_ir/__init__.py`,
`native/pyproject.toml` and `native/Cargo.toml` (then `cargo build` in
`native/` for `Cargo.lock`, and `uv lock`). When the tag of `ezjww-core` in
`native/Cargo.toml` changes, the floor of `ezjww` in the `jww` and `all` extras
of `pyproject.toml` follows it.

Pushing the tag `v<version>` runs `.github/workflows/release.yml`: it checks
the versions and the tag, runs the tests, builds `cad2d-ir` (wheel and source
distribution) and `cad2d-ir-native` (a wheel on each platform and a source
distribution), installs each native wheel with the `cad2d-ir` of the release
and runs `native/tests` on its platform, and publishes all files to PyPI,
`cad2d-ir-native` first. Run by hand (`workflow_dispatch`) it does everything
but publish.

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

The writer and the importer are used through `cad2d_ir` (`ir_to_dxf`,
`convert_jww_file_to_ir`, …). What this package adds is a document that stays
in native code:

```python
from cad2d_ir.importers.base import ImportOptions
from cad2d_ir.importers.jww import import_jww_file

result = import_jww_file("plan.jww", options=ImportOptions(strict=False))
dxf_bytes, export_diagnostics = result.document.to_dxf_bytes()
document = result.document.to_dict()  # the dict convert_jww_file_to_ir returns
```

`result.document` is a `NativeDocument`. Its entities do not become Python
objects unless `to_dict()` is asked for: the entities of a large drawing take
a fraction of the memory of their `dict`s. `NativeDocument.from_dict` holds any
IR `dict` that way, and `to_dxf(target_version=..., curve_segments=...,
generic_dimensions=..., entity_map=...)` writes it with all the options of
`ir_to_dxf`.

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

### Native code of an application

An application with Rust code of its own that works on the typed IR (a renderer,
an exporter) links `cad2d-ir-py` into its extension module, calls
`cad2d_ir_py::register(module)` there and takes a `NativeDocument` as an
argument of its own functions (`NativeDocument::document()` is the typed
`Document`). The classes of two extension modules are different types to Python,
so the application uses the bindings of its own module throughout:

```python
result = import_jww_file("plan.jww", options=..., core=my_app._core)
my_app._core.render(result.document)  # a function of the application
```

`core` is the extension module whose bindings do the work; it defaults to the
one of this package. `Unsupported` is an exception the bindings define and
never raise: it is for the native code of such an application, when it
declines a document its Python side can handle in another way.

## The typed IR

`LINE`, `CIRCLE`, `ARC`, `POINT`, `ELLIPSE`, `TEXT`, `INSERT`, `HATCH` and
`LWPOLYLINE` are structs. Every other entity, and every value that is not of
the type the schema gives it, stays dynamic and converts back unchanged, so a
document is the same `dict` after `NativeDocument.from_dict(...).to_dict()`.
Two things are not kept as they came: a `tuple` is a `list` afterwards, and
the halves of a surrogate pair held as two code points are the character they
stand for (a half alone is U+FFFD) - Rust has no such string.

`NativeDocument.from_dict` raises `TypeError` for a value JSON does not have
(`bytes`, a `set`, a key that is no `str`), which it could not give back. The
writer, which only reads, passes over such a value where it is not what is
written.

A `dict` that `ir_to_dxf` hands over is converted for the writer alone: one
entity at a time (the dynamic form of an entity, every key and value an object
of its own, is several kilobytes and exists for one entity only), and without
what the writer never reads - the `source` of an entity, and the `metadata` of
the kinds that are written from their fields (`dxf::reads_entity_key`). An
imported drawing carries both on every entity, and without them an entity is
about a hundred bytes. `NativeDocument` keeps everything; the two are written
the same (`tests/test_native.py`).

## Measurements

Taken with 0.17.22, when both implementations existed: JWW drawings of lines
only (synthetic), one process per run, JWW import and DXF R2010 export, the
same DXF for the first two columns. `ezjww` is `ezjww.write_dxf_with_report`,
whose output is a different DXF.

| JWW | Python (removed in 0.18.0) | native | `ezjww` |
| --- | --- | --- | --- |
| 0.7 MB | 0.58 s, 64 MB | 0.16 s, 46 MB | 0.05 s, 31 MB |
| 3.6 MB | 2.69 s, 195 MB | 0.33 s, 96 MB | 0.15 s, 70 MB |
| 9.4 MB | 7.08 s, 467 MB | 0.72 s, 199 MB | 0.47 s, 155 MB |
| 15 MB | 11.08 s, 709 MB | 1.04 s, 282 MB | 0.66 s, 218 MB |
