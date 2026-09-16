# Nod — Benchmark specification

The benchmark is the reason this project is credible. Everything in the README that is a
number comes from here.

## 1. What is measured

Whether the agent let the person finish, and what it cost in responsiveness.

## 2. Corpora

### Track A — synthetic, redistributable, primary
Built from CC-licensed read speech. A generator applies controlled perturbations with
**known** utterance boundaries, which is the whole point: because the harness inserted
the pause, ground truth is exact rather than judged.

Perturbations:

| Id | Transformation | Sweep |
|---|---|---|
| `pause` | insert silence at a word boundary mid-utterance | 200→3000 ms, 200 ms steps |
| `repeat` | duplicate a word with a natural short gap | 1–3 repetitions |
| `prolong` | time-stretch a vowel segment without pitch shift | 1.5×, 2.5×, 4× |
| `correct` | splice a self-correction ("change my, no, cancel my") | 3 templates |
| `burst` | split one utterance into 2–4 bursts with uneven gaps | 2–4 |
| `noise` | add stationary background at a set SNR | 30, 20, 12 dB |

Each generated clip ships with a sidecar `.truth.json`:

```json
{"clip_id":"a_pause_1400_003","utterances":[{"start_ms":0,"end_ms":4820,
 "text":"...","perturbation":{"type":"pause","at_ms":2100,"len_ms":1400}}],
 "source":{"corpus":"...","license":"...","file":"..."},"generator_version":"1.2.0"}
```

Determinism: the generator takes a seed; the same seed and version produce byte-identical
audio. The seed is recorded in the run manifest.

### Track B — real atypical speech, secondary, stretch
SEP-28k ships annotations only; the audio is downloaded from podcast URLs and copyright
remains with the podcast owners, and the released clips are three seconds long, which is
useless for turn-boundary work without reassembly from the source episodes. Therefore:

- ship a download script and a manifest, **never audio**;
- reassemble continuous segments from episode-level timestamps;
- boundaries here are hand-labelled, so report Track B separately and never pool it with
  Track A.

Track B is a stretch goal. Nothing in the success criteria depends on it.

### Track C — owner-recorded, always available
Ten to twenty calls recorded by the project owner, MIT licensed, committed to the repo.
Includes a deliberately hesitant read of an intake script. This is the insurance policy
that guarantees a table exists on any clone.

## 3. Conditions (arms)

| Arm | Config |
|---|---|
| `aggressive` | the vendor's aggressive quick-start values |
| `balanced` | the vendor's balanced quick-start values — **the headline baseline** |
| `conservative` | the vendor's conservative quick-start values |
| `nod` | Nod in `adapt` mode, default preset |
| `nod-nocontext` | speaker axis only, ablation |
| `nod-nospeaker` | context axis only, ablation |
| `oracle` | ground-truth boundaries, upper bound on achievable latency |

Baselines are the vendor's own published quick-start values, never a strawman picked to
lose. The `oracle` arm exists so the reader can see how much headroom remains.

## 4. Replay: the part that is easy to get wrong

**Audio must be fed in real time.** Dumping a wav into the socket at once destroys every
silence in it and makes the measurement meaningless. The feeder:

- emits 50 ms PCM16 frames on a monotonic deadline schedule
  (`next_deadline += 0.05`, sleep to deadline, never `sleep(0.05)` in a loop, so jitter
  does not accumulate);
- records actual send timestamps in the run trace;
- aborts the run if cumulative drift exceeds 25 ms, because a drifting feeder invalidates
  latency numbers.

Because the upstream service is real time and non-deterministic, every (clip, arm) pair
runs `N = 5` times by default. Report median and interquartile range, not a single value.
A result with IQR larger than the arm-to-arm difference is reported as inconclusive, in
those words.

`FakeAssemblyAI` mode replays recorded event streams with the original inter-event
timing, giving exact determinism for CI and for anyone without credits. CI runs the fake;
the published table is generated from live runs and the traces are committed.

## 5. Metrics

Defined here once. Definitions borrow from published turn-taking work — Full-Duplex-Bench
v3, EVA-Bench, IHBench and τ-Voice — and the borrowings are cited in the report.

| Metric | Definition | Direction |
|---|---|---|
| **PCR** premature cutoff rate | fraction of ground-truth utterances where `end_of_turn` fired before the utterance's final word ended | lower better |
| **TTL** turn latency | ms from true utterance end to `end_of_turn`; report p50 / p90 / p99 | lower better |
| **FRAG** fragmentation | mean number of emitted turns per ground-truth utterance; 1.0 is perfect | → 1.0 |
| **TCT** task completion time | wall-clock seconds to complete the scripted intake, including repeats caused by cuts | lower better |
| **RES** resume rate | fraction of cuts the caller had to recover from by repeating | lower better |
| **PATCH** patches per session | controller activity, a cost measure | context |
| **DEC** decide latency | p99 of `decide()`, guards INV-2 | < 5 ms |

Negative latencies are physically possible when a turn ends early; they are recorded as
negative and never clipped to zero, because clipping hides exactly the failure being
measured.

## 6. The headline chart

PCR on the y axis, TTL p90 on the x axis. The three static arms trace a tradeoff curve.
Nod is plotted as a point with error bars.

The claim to make in the README, and the only claim supported by this design:

> A single static configuration traces a fixed tradeoff curve between cutting people off
> and feeling slow. A per-caller controller is not on that curve.

Do not write "faster". Do not write "more accurate".

## 7. Reproducibility

- `make bench` runs the full suite against `FakeAssemblyAI` and regenerates
  `docs/RESULTS.md` and the README table.
- `make bench-live` runs against the real API and requires `ASSEMBLYAI_API_KEY`.
- Every run writes `bench/runs/{run_id}/manifest.json` recording: git sha, generator
  version and seed, corpus hashes, arm configs, model name, API region, host, and wall
  clock.
- Raw event traces are committed under `bench/traces/` so any reader can recompute every
  metric with `make metrics` and zero API spend. This is the single most persuasive
  artefact in the repository.
- Result caching is keyed on `sha256(audio, config, code_version)`; changing one arm
  re-runs only that arm.

## 8. Report card

`make report` renders a single self-contained HTML file: the Pareto chart, the arm table
with IQRs, ablation deltas, the corpus manifest, and the honest-scope section. No
external assets, no network on open.

## 9. Statistical hygiene

- Paired comparison: the same clips through every arm, so use a paired test
  (Wilcoxon signed-rank) rather than an unpaired one.
- Report effect size alongside any p-value, and the n.
- Never report a percentage without the denominator.
- If Track A and Track C disagree in direction, say so prominently rather than reporting
  the favourable one.
