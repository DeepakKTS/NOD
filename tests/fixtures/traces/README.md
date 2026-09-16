# Committed probe traces

The `FakeAssemblyAI` replay seed for ROADMAP Phase 1 bullet 5, "`FakeAssemblyAI`
replay server built from Phase 0 traces", so the console, the bench and the
integration tests can run with no API key and no credits (INV-7, EC-45).

## `seed-min-turn-silence-midstream.jsonl`

> **The audio behind this trace was synthesized with macOS `say`. It is not
> benchmark-grade.**
>
> Voice Samantha at 160 wpm, seed sha256 `528e279a49e6`. Adequate for
> capability probing, which asks only whether a turn boundary moves. **Not**
> adequate for Track C, disfluency features, cut detection, or any published
> number. Any figure derived from this file inherits that caveat, and INV-9
> still applies: benchmark numbers come from `make bench` over real audio.

Byte-identical to what `nod_bench.probe` wrote on 2026-09-16 — not hand-edited,
so the `clip_sha256` in its `meta` record still verifies and it replays as a
faithful session. JSONL has no comment syntax and injecting a header record
would have made it something other than a real trace, so the provenance lives
here instead.

Written **under ADR-015**. Redaction is applied at write time, so an earlier
trace of the same cell could not be cleaned after the fact — it carried the
phone number's digits in `words[].text` and was rejected rather than scrubbed.
This file is a fresh capture of the identical clip; `clip_sha256` is unchanged
from the 69-session matrix, which is what proves the audio is the same and only
the masking differs.

| | |
|---|---|
| source | `probe-20260916T202324031290Z-min_turn_silence-mid_low-r0.jsonl` |
| model | `universal-streaming-english` |
| cell | `min_turn_silence` / `mid_low`, arm 100 ms |
| pinned | `end_of_turn_confidence_threshold` 0.2, `max_turn_silence` 3000 |
| records | 461 (1 `meta`, 1 `UpdateConfiguration`, 4 `end_of_turn` turns, 1 `verdict`) |
| gap under test | 10263–14263 ms, zero-filled |
| lead segment | 1 — a **complete** utterance, so the semantic gate fires and `min_turn_silence` is what decides (ADR-011, EC-50) |

Chosen for P3 because it is the smallest trace carrying all three things a
replay server has to reproduce: a mid-stream `UpdateConfiguration` round-trip,
several real turn boundaries with partials, and redacted PII. Its transcripts
contain `[PHONE]` and `[DATE]` masks and **zero** unmasked digits, which is the
regression ADR-013 exists for — `test_fixture_trace.py` asserts that, so a
redaction regression fails here too.

Note it is a *complete*-utterance trace. The fragment regime that
`max_turn_silence` governs is not represented; a fragment clip is built from
seed segments 0, 3 and 2 and skips segment 1, so it carries no phone number and
could not serve the redaction assertion. Add a second fixture if P3 needs that
regime.
