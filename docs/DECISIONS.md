# Decision log

Long-term memory. `CLAUDE.md` is the standing contract; this file records why the code
looks the way it does, so a future session does not re-litigate a settled question.

Format, five lines maximum per entry:

```
## ADR-NNN — <title>
Date · Status: accepted | superseded by ADR-NNN
Context: what forced a choice
Decision: what was chosen
Consequence: what this now costs or rules out
```

---

## ADR-000 — Stack
2026-09-15 · Status: accepted
Context: fifteen days, one owner, a controller plus a harness plus a console.
Decision: Python 3.12 / FastAPI for core and bench, Next.js 15 for the console, SQLite and
JSONL for storage, no Redis or Postgres in v1.
Consequence: one language across the two parts that share logic; horizontal scaling needs
sticky routing by session id, which is accepted until measurements say otherwise.

## ADR-001 — Model and capability baseline
Status: **pending Phase 0**
Context: the controller needs `end_of_turn_confidence` and mid-stream updates; Universal-3
Pro Streaming uses punctuation-based turn detection rather than a confidence score.
Decision: record here after `python -m nod_core.probe` — which knobs accepted updates,
whether confidence appears on Turn events, whether ForceEndpoint was accepted.
Consequence: determines whether the confidence axis is live or the controller runs on the
silence axis alone.

## ADR-002 — Streaming quantiles
2026-09-15 · Status: accepted
Context: pause quantiles update per word and the decision path is budgeted at 5 ms p99.
Decision: P² estimator as primary, exact ring-buffer behind `NOD_EXACT_QUANTILES` for
validation; the bench asserts they agree within 5 %.
Consequence: `O(1)` per sample and constant memory, at the cost of an approximation that
must be re-validated whenever the corpora change.

## ADR-003 — Realtime STT path, not the Voice Agent API
2026-09-15 · Status: accepted
Context: the Voice Agent API ships its own closed adaptive pacing and exposes no knobs.
Decision: Nod runs on the Realtime STT path; the Voice Agent API may appear only as a
clearly labelled comparison arm in the benchmark.
Consequence: Nod owns orchestration, LLM and TTS, which is more work but is the only way
the control loop can exist at all.

## ADR-004 — Packaging and lockfile
2026-09-15 · Status: accepted
Context: ARCHITECTURE §10 requires dependencies pinned with hashes, which version pins in
`pyproject.toml` alone cannot express, and CLAUDE §3 names no packaging tool.
Decision: `uv` with a committed `uv.lock`; `make install` and CI both run `uv sync
--frozen`, and `uv python install 3.12` supplies the interpreter the repo pins.
Consequence: lockfile drift fails the build instead of silently resolving something new;
contributors need `uv` on PATH, and `requires-python = ">=3.12,<3.13"` rejects a 3.13 box.
