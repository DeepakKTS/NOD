| arm | PCR | IQR | TTL p90 (ms) | IQR | FRAG |
|---|---|---|---|---|---|
| `aggressive` | 0.833 | [0.833, 0.833] | 559 | [549, 574] | 1.833 |
| `balanced` | 0.500 | [0.500, 0.500] | 1444 | [1443, 1453] | 1.500 |
| `conservative` | 0.250 | [0.250, 0.250] | 3762 | [3760, 3763] | 1.250 |
| `nod` | 0.500 | [0.500, 0.500] | 1462 | [1460, 1465] | 1.500 |
| `nod-nocontext` | 0.500 | [0.500, 0.500] | 1471 | [1454, 1471] | 1.500 |
| `nod-nospeaker` | 0.500 | [0.500, 0.500] | 1471 | [1471, 1475] | 1.500 |

bars: interquartile range over N live repeats (BENCH_SPEC 4) - NOT a bootstrap over clips.
Percentiles are nearest-rank, inclusive. n=12 clips x 5 repeats from one synthetic voice; the interval is a lower bound on uncertainty (ADR-018, ADR-019).