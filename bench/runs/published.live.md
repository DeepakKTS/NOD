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

**Live run** against the streaming API, `trackA-stratified-12`, 12 clips x 5 repeats x 6 arms.
**Subsample: 12 of 120 clips.** The account permits roughly one new session every 15 s once a closed session's slot is counted, which puts the specified sweep at about 16 hours (ADR-048). **n is a tenth of the design**, so the clip axis — which clips happened to be drawn — dominates the uncertainty here.
Corpus is synthetic speech from one macOS `say` voice, whose own manifest states it is not adequate for a published number; see the README's honest-scope section.
TCT and RES are **not reported**: both need a caller who reacts to being cut off, and recorded audio does not (ADR-046).
Endpoint overhead 217 ms; percentiles nearest-rank, inclusive.

> **These intervals understate the uncertainty.** They are the interquartile range over live repeats, which measures run-to-run variation only. At n=12 the dominant term is *which clips were drawn*, and this estimator does not carry it — which is why several are zero-width, reading as precision that is not there. ADR-049 replaced it with a cluster bootstrap over clips for exactly this reason. **Re-rendering this table under it is not possible from the committed artifacts** — a bootstrap resamples clips and only per-arm aggregates were persisted, so it needs the sweep re-run (ADR-052). Later runs write `observations.live.json` and are re-analysable.
