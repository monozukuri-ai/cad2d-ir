# DRA-CAD test fixtures

Synthetic DRA-CAD drawings copied byte-for-byte from the `ezdracad` repository's
`tests/fixtures/` (`best-effort8.cfb` and `best-effort1.cfb` renamed to `.mpz`,
and `best-effort25u.mpuw`). They are assembled from scratch by `ezdracad`'s
`scripts/make_best_effort_fixture.py` and were **not** saved by DRA-CAD: they
contain no third-party drawing content. They stay under the preserved
[`ezdracad` license](LICENSE.ezdracad.txt) (PolyForm Noncommercial 1.0.0), not
under the MIT license of this repository.

```text
repository: https://github.com/monozukuri-ai/ezdracad
version:    0.1.0a4
paths:      tests/fixtures/best-effort8.cfb, tests/fixtures/best-effort25u.mpuw,
            tests/fixtures/best-effort1.cfb
regenerate: python scripts/make_best_effort_fixture.py
```

Two files hold the same drawing at 1:50 on an A3 sheet, once in the older
record layout (64-byte records, Shift_JIS strings, raw marker 8) and once in the
Unicode layout (80-byte records, UTF-16 strings, raw marker 25, in a compressed
envelope): a line, a circle filled with its own colour, an arc, an ellipse
filled without its outline, an elliptical arc, a point, two texts, a filled
polyline with a reversed member arc, a cubic spline, a region hatched with
lines, a filled region with a hole, a region hatched with a named pattern, a
linear dimension with the package that draws it, an angle dimension without
one, a leader with its package, a block reference and its definition, an image,
a reference to another drawing, a 3D line, a named area with a transparent
fill, a gradation hatch, a sheet with a window onto the model and a line of its
own, a face in space with a hole, a part placed from another file, a camera, a
light, two properties, an erased object, one object of a kind `ezdracad` does
not decode and one that does not fit its layout. The drawing has a comment,
states a scale of 1:100 and prints with the pen width of each colour.

`best-effort1.mpz` is a small drawing in the form of the first releases (raw
marker 1): shorter style tables, leftover bytes where nothing is stored and a
compound file without a class identifier.

| File | SHA-256 |
| --- | --- |
| `best-effort8.mpz` | `c0e7a82267b074882ef85a4f9c87efe3fd681da57f6744a0221107396a640169` |
| `best-effort25u.mpuw` | `6c8722272d64a509d7b97afd9457af16539e7bdcd3517283bc33f032c9602f2a` |
| `best-effort1.mpz` | `a8f6155b3f1e187f5ae051e2a1c673eea43defeea93cd86d1fb1ce40ed399d7a` |

Drawings saved by DRA-CAD are vendor or third-party material and are not
included; the opt-in gate `CAD2D_IR_DRACAD_FIXTURES` in
`tests/test_dracad_importer.py` runs them from a local directory.
