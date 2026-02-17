# API Guide

## High-level API

Use `cad2d_ir.api` for a warning-aware interface.

```python
from cad2d_ir.api import convert_dxf_text_to_ir, convert_ir_to_dxf_text
```

### `convert_dxf_text_to_ir(dxf_text, *, ir_version="0.1.0", validate=True)`

Returns `DxfToIrResult`:
- `document`: parsed IR document (`dict`)
- `warnings`: conversion warnings (`list[str]`)

### `convert_ir_to_dxf_text(document, *, validate=True)`

Returns `IrToDxfResult`:
- `dxf_text`: output DXF text
- `warnings`: conversion warnings (`list[str]`)

### File-based helpers

- `convert_dxf_file_to_ir(path, ...)`
- `convert_ir_file_to_dxf(path, ...)`
- `load_ir_json(path, validate=True)`
- `dump_ir_json(document, path, pretty=True, validate=True)`

## Low-level API

`cad2d_ir.codecs.dxf` exposes lower-level conversion functions:
- `dxf_to_ir(...)`
- `ir_to_dxf(...)`

Use these when you need direct control over conversion options.
