# Schema Notes

## Canonical schema

The canonical schema source is:
- `ir_schema.json`

For package runtime use, a copy is bundled at:
- `src/cad2d_ir/data/ir_schema.json`

`load_schema()` resolves schema data in this order:
1. Explicit path argument
2. Packaged schema data
3. Repository-root fallback

## Versioning

IR document version follows semantic version format (`X.Y.Z`) in the `version` field.

When changing schema behavior:
- Add/adjust tests first
- Document compatibility impact in PR and changelog
- Keep unsupported mappings explicit via conversion warnings

## Current conversion limitations

- `constraints` are IR-only metadata today and are omitted on DXF export.
- `DIMENSION` mapping is staged and semantic, not full CAD-system-specific fidelity.
- `HATCH` mapping currently focuses on polyline-like loops.
