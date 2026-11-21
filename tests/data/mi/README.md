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
| `dimension-text.mi` | `phase5.mi` as of `ezmi2d` v0.2.3: two `DTA` text attributes, the pointers to them from the dimension and its tolerance, and a stored text box (text height 0.2, tolerance at 0.6 of it). An `ezmi2d` before 0.2.3 does not read its `DTV`; the test that uses it is skipped there | `02a59c96ceed96184b17224a27507bd538078514769f3785fb0f9f02c11fad0b` |
| `dimension-graphics.mi` | `phase5.mi` as of `ezmi2d` v0.2.4: the angular dimension stores its drawn form after the formatted text (two terminators, one of them not drawn, four extension line pieces, two arc pieces, a tolerance with an upper and a lower text, a prefix and a text in a row below) with the `DAF` and `DLA` records it names. The tests that use it need `ezmi2d` 0.2.4 and are skipped before | `d05df1c3f75a607ea2bfa4c4e9a128ea0b49cba9d86d2e75b38f3972d0f91f79` |
