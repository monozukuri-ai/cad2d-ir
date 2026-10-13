# JWS test fixtures

Jw_cad registered symbols (図形登録) copied byte-for-byte from the `ezjww`
repository's `jws_samples/native/`. They were made for the `ezjww` JWS reader
from original generated geometry and text (`scripts/jws/make_inputs.py`) and
saved with Jw_cad 10.02.1 under Wine on Linux on 2026-10-13. They contain no
third-party drawing content and are redistributed under the preserved
[`ezjww` MIT license](LICENSE.ezjww.txt). No Jw_cad executable or installed
symbol library is included.

```text
repository: https://github.com/monozukuri-ai/ezjww
commit:     0965e059e9ece2c341d1a845815ae68fb9fbd703 (0.7.0)
paths:      jws_samples/native/*.jws
```

Every symbol was registered with its origin snapped to the shared line
endpoint `(10, 20)`. `.gitattributes` marks `*.jws` as binary.

| File | Version | Main records | Purpose | SHA-256 |
| --- | --- | --- | --- | --- |
| `basic_v600.jws` | 600 (class schema 700) | 2 lines, 1 circle | old-format save, nonzero origin | `e0fe69555566167dcd525ceb4513f9156305f5f78ceed174bc8bc8a3de511c95` |
| `basic_v700.jws` | 700 | 2 lines, 1 circle | current save | `2519743d0b6755f0483863860a38cc4007726f0a524078cd4b5040ac60ca8df1` |
| `scales_v700.jws` | 700 | 3 lines | group 0 scale 50, group 1 scale 100 | `82d569925b95f20476aefa6b6a63bbc7cb4e2b5413bb2f4cba02bc7fd5a70855` |
| `geometry_v700.jws` | 700 | 9 | arc, ellipse, point, solids, dimension | `a1a19098c2c797d27dbf48f2ec00603d721a4224d04d4fe2ffa070616f11a43d` |
| `unicode_v700.jws` | 700 | 2 lines, 2 texts | Japanese, surrogate pair, half-width kana, rotation | `20d238a531ebdabf0d8ab58420c7d3ed10cafa7004755f84cf60ca4f9f802f05` |
| `blocks_v700.jws` | 700 | 2 lines, 2 inserts | two definitions, nested rotation, nonuniform scale | `abdf1fb42228dd10cab11de54e8bdfc9e594cb33d339ccee9f6899f5d49d7238` |
| `image_v700.jws` | 700 | 2 lines, image text | image-reference-only symbol (no bitmap archive) | `23f2d4719bf1fb15cb62317528a65a632af2868adde2d7e8aca5704de47d5056` |
