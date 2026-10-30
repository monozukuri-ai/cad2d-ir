# eDrawings test fixtures

Self-authored eDrawings drawings from the `edrawings-kit` repository
(`tests/fixtures/native-m4`, Apache-2.0): project-authored DXFs with a 100 × 50
rectangle, a circle, a text and a dimension in model space and two paper layouts
(`M4_SHEET_A`, 210 × 148 mm with a viewport at 1.375; `M4_SHEET_B`, 297 × 210 mm
at 1.625), exported by eDrawings 2026 SP4.1 (its DXF/DWG Reader). They contain
no third-party CAD design, fonts or vendor software. Do not modify the binaries
to update expectations; add a separately documented input instead.

| File | Content | SHA-256 |
| --- | --- | --- |
| `two-layouts-mm.edrw` | the two layouts plus the `Model` sheet; every text stroked into `textasgeom` polylines, white (`c16777215t1`) segments | `d601dbac0130f4084800ab3cdbeb5e15d4ce8f7992a3c360b0b8d6936fc70632` |
| `two-layouts-japanese-mm.edrw` | the same drawing with the label `寸法確認 100 mm` kept as a text (TakaoPGothic, 3 mm) on layer `M4_JAPANESE`, red (`c255t1`) segments | `3de42bf9aa6424678e3e919562573dfe89593f1ce878efde733ca4bc89516fc1` |

eDrawings-authored drawings (SOLIDWORKS writers, the 2008-era HSF 8.05 and
14.50 streams) are not redistributable and are run from a local directory
through the opt-in gate `CAD2D_IR_EDRW_FIXTURES` in
`tests/test_edrawings_importer.py`.
