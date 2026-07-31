# DGN test data

The binaries in this directory were taken unmodified from OSGeo/GDAL at the
pinned revision below. The GDAL distribution license is MIT style; the full
text at the time of retrieval is preserved in
[`LICENSE.GDAL.txt`](LICENSE.GDAL.txt).

```text
repository: https://github.com/OSGeo/gdal
commit:     18e7cceb43a0dd58be474c9fdd5384baa3cde7c9
```

| Local file | Upstream path | SHA-256 | Purpose |
| --- | --- | --- | --- |
| `smalltest.dgn` | `autotest/ogr/data/dgn/smalltest.dgn` | `9d9faddb67216f9d56fc9a1027adc1a927c8c13529dfd3416b771b4dc4e9a284` | V7 routing and mapping regression |
| `test_dgnv8.dgn` | `autotest/ogr/data/dgnv8/test_dgnv8.dgn` | `8f32f87ce4b16881aa64f5cb9f75c98851833f96fef37ca0ad31aa6bb18d1df0` | Native V8 import regression (authored by GDAL's `createdgnv8testfile.cpp` through the ODA SDK) |
| `LICENSE.GDAL.txt` | `LICENSE.TXT` | `1dae3468e81d00da56e2936f74d33b8b3ad09d726437f19ce209a5dabea41f77` | Upstream license |

The same file (byte-identical) is used by `ezdgn` as its V8 reader
regression input, so importer results can be cross-checked against the
upstream `autotest/ogr/data/dgnv8/test_dgnv8_ref.csv` expectations that
GDAL/ODA produced for it.
