# MI test fixtures

These are small synthetic drawings copied byte-for-byte from the `ezmi2d`
v0.2.0 test suite. They were generated specifically for parser tests, contain
no third-party drawing content, and are redistributed under the preserved
[`ezmi2d` MIT license](LICENSE.ezmi2d.txt).

```text
repository: https://github.com/monozukuri-ai/ezmi2d
tag:        v0.2.0
commit:     486ac155f1d25c5ab380c7b778e2b488f03a425f
paths:      tests/data/geometry.mi
            tests/data/phase5.mi
            tests/data/text-utf8.mi
```

The byte streams are fixtures: `.gitattributes` disables newline conversion
for MI/BI files so byte offsets, hashes, and parser source spans remain stable.

| File | Coverage | SHA-256 |
|---|---|---|
| `geometry.mi` | line, arc, circle, layer, raw unsupported fallback | `d100addad10b18c293d74d55a9bd2d0bd969544f75f06af35d1b9f4b3cec0241` |
| `phase5.mi` | fillet, B-spline, dimension, leader, hatch, symbol, nested/shared parts, sheets | `4530ce28c5f0718f7cdc291a090540dcce680a287cefa3d83e7d4d10d6060b82` |
| `text-utf8.mi` | UTF-8 text, font, alignment, placement | `b0103fb2dbbf468459627379b3a4a6cafc7eb4fe73555364b9c93ff44ad161d4` |
| `LICENSE.ezmi2d.txt` | upstream license | `c23c02ff94202de884636aee3b4a411761255ebb68fee9f317947ce5142a8806` |
