# Contributing

Thank you for contributing to `cad2d-ir`.

## Development setup

```bash
uv sync
```

`uv sync` builds the Rust code under `native/` (the package
`cad2d-ir-native`, which `cad2d-ir` depends on), so a Rust toolchain is needed.
It is built again when a file under `native/` changes.

## Local checks

```bash
uv run pytest -q
uv run ruff check src tests
uv run cad2d-ir --help
uv run --extra dwg cad2d-ir import path/to/sample.dwg -o /tmp/sample.json
uv run --extra dgn cad2d-ir import path/to/sample.dgn -o /tmp/sample.json
uv run --extra dwf cad2d-ir import path/to/sample.dwfx -o /tmp/sample.json
uv run --extra jww cad2d-ir import path/to/sample.jww -o /tmp/sample.json
uv run --extra sxf cad2d-ir import path/to/sample.sfc -o /tmp/sample.json
uv run python scripts/sync_packaged_schema.py
```

## Native package

The DXF writer and the entity conversion of the JWW importer are the Rust code
under `native/` ([native/README.md](native/README.md)). There is no Python
implementation of either to compare with: a change of the writer is checked
by the tests and by the DXF files under `native/tests/golden`.

```bash
cd native
cargo fmt --all --check
cargo clippy --workspace --all-targets --locked -- -D warnings
cargo test -p cad2d-ir-core -p cad2d-ir-jww --locked
cd ..
uv run pytest -q                 # builds native/ again, then tests/ and native/tests
```

When the output of the writer changes on purpose, write the golden files again
and review the difference:

```bash
CAD2D_IR_UPDATE_GOLDEN=1 uv run pytest native/tests/test_dxf_golden.py
git diff native/tests/golden
```

CI builds the native code from source on Linux, macOS and Windows and runs
`native/tests` there.

## Releasing

```bash
uv run python scripts/check_release.py   # the versions of both packages agree
git tag v<version> && git push origin v<version>
```

The tag runs `.github/workflows/release.yml`, which builds `cad2d-ir` and the
wheels of `cad2d-ir-native` for every platform and publishes them together
(`cad2d-ir-native` first, since `cad2d-ir` pins it) through PyPI trusted
publishing (see [native/README.md](native/README.md#release)).

## Branch and PR guidelines

- Keep each PR focused on one logical change.
- Include tests for behavior changes.
- For schema updates, include compatibility notes in the PR description.
- For DXF mapping changes, include at least one round-trip test case.
- For importer changes, include synthetic mapping tests and record the real-file corpus used for validation.

## Commit style

Use short imperative commit messages. Examples:
- `schema: add spline weight validation`
- `dxf: map mtext attachment code`
- `docs: expand api examples`

## Project structure notes

- `ir_schema.json` is the canonical schema source in the repository root.
- `src/cad2d_ir/data/ir_schema.json` is the packaged copy used at runtime after installation.
- The DXF reader lives in `src/cad2d_ir/codecs/dxf.py`, the DXF writer in
  `native/crates/cad2d-ir-core/src/dxf/`.
- Importer contracts and adapters live in `src/cad2d_ir/importers/`.
