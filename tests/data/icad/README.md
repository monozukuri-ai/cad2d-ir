# iCAD SX test fixtures

Synthetic iCAD SX documents written with `icadkit`'s `DrawingWriter` on top of
the empty two-view sheet of its test helpers (`tests/drawing_fixtures.py`).
They were **not** created by iCAD SX and contain no vendor drawing content; the
byte layout follows the V8L3 (little-endian) and V7L7 records icadkit
qualified from its corpus. The generator lives in the zumen-gate repository
(`docs/research/icad-sx-prototype/make_2d_fixtures.py`).

| File | Content | SHA-256 |
| --- | --- | --- |
| `drawing-v8l3.icd` | 2D global view with a frame (lines, 3-line title text at 1/1) and the view `XY` (scale 1/2, placed at (80, 60)): outline lines, centre lines (style 3), hidden lines (style 2), a circle, an arc, a point, a horizontal text, a vertical text, a length dimension, a diameter dimension and a 45° hatch | `12cad5c7e0beaaaf3e1a08c913c142f38d22e23af2c6a48ea496f5af41954a7a` |
| `drawing-mixed-v8l3.icd` | the same plus the view `DET` (scale 2/1 at (220, 150)) with a circle and a line, so the view scales differ | `d271cb575504de8f3db4d98ffd8cadba05d7d20d4efa74f8e13321dedea3538b` |
| `drawing-symbols-v8l3.icd` | the same frame and the view `XY` (scale 1/2 at (80, 60)) with two lines, an ellipse (semi-axes 12 and 6 rotated 30°), a half elliptical arc, a free curve through four knots, a two-line leader note, an angle dimension between two legs and a symbol record (a point marker with the label `Ｍ１`); the record types icadkit 0.3.9 decodes | `fac8c5e45306408b23b29686d06048e1c1d38dea4a7883af63422368d1c42960` |
| `model-only-v7l7.icd` | a 3D-only document (root part plus one child part with a saved body, no 2D view); copied from `cad3d-ir-icad`'s authored fixtures | `5492af0ed997d425de0c283039771e271cb83ff8b1abd5c781fcb93dd2ee59aa` |

iCAD SX's own sample drawings (the trial installation's tutorial and
operation-guide data) are vendor material and are not included; the opt-in gate
`CAD2D_IR_ICAD_FIXTURES` in `tests/test_icad_importer.py` runs them from a local
directory.
