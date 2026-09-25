# Nod — Architecture

## 1. Shape of the system

```
browser mic ──PCM16 16k──┐
file replay ─────────────┤
                         ▼
                 ┌───────────────┐   audio frames (verbatim, never blocked)
                 │ SessionProxy  │ ─────────────────────────────────────────►  AssemblyAI
                 │  (nod_core)   │ ◄──────────── Turn / Begin / Termination ──  Universal-Streaming
                 └───────┬───────┘
                         │ fan-out (bounded queue, drop-oldest)
          ┌──────────────┼───────────────────────┐
          ▼              ▼                       ▼
   ┌────────────┐  ┌────────────┐        ┌──────────────┐
   │ Controller │  │ TraceSink  │        │ Telemetry hub│──► console WS
   │  profiler  │  │  (JSONL)   │        └──────────────┘
   │  policy    │  └────────────┘
   │  arbiter   │
   └─────┬──────┘
         │ ConfigPatch
         ▼
   UpdateConfiguration / ForceEndpoint  ──►  same live AssemblyAI socket

   AgentLoop (mode C only): Turn(final) → LLM → TTS → audio out, barge-in aware
```

Two rules the diagram encodes:

- The controller is on a **fan-out**, not in-line. Audio forwarding never awaits it.
- Config patches re-enter through the **same socket**. No reconnect, no session loss.

## 2. Modules and boundaries

### `nod_core`

| Module | Responsibility | Never does |
|---|---|---|
| `proxy.py` | Owns both sockets, byte-forwards audio, parses frames, fans out | Decide anything |
| `profiler.py` | Per-session rhythm statistics | Touch config |
| `policy.py` | Context axis: dialogue state → window hints | Look at the caller |
| `arbiter.py` | Merge axes, apply guards, emit `ConfigPatch` | Do I/O |
| `capabilities.py` | Probe which knobs and fields this model exposes | Assume |
| `trace.py` | Append-only JSONL with redaction and bounded flush buffer | Block the loop |
| `config.py` | `Settings` via pydantic-settings; all tunables | Hide constants |
| `types.py` | Frozen slotted dataclasses crossing boundaries | Import anything heavy |

All adapters sit behind `typing.Protocol` definitions in `nod_adapters/protocols.py`:
`SttSession`, `LlmClient`, `TtsEngine`. `nod_core` imports the protocols, never a
concrete adapter. This is what makes `FakeAssemblyAI` and offline tests possible.

### Event types consumed

From the stream: `Begin` (session id, expiry), `Turn` (both partial and final), and
`Termination`. A `Turn` carries `turn_order`, `end_of_turn`, `end_of_turn_confidence`,
`transcript`, and a `words` array of `{text, start, end, confidence, word_is_final}`
with millisecond timings. Partials matter as much as finals — they carry the word timings
the pause profile is built from. The `end_of_turn_confidence` trajectory is logged as a
feature but carries no control authority: P1 measured it near zero throughout an utterance
and spiking only on the boundary frame, so it reports turn completion rather than speaker
hesitation (CONTROL_SPEC.md §2.4, ADR-011).

## 3. Concurrency model

One asyncio event loop per worker process. Per session, exactly four tasks:

| Task | Priority | Backpressure policy |
|---|---|---|
| `pump_audio_up` | highest | never buffers beyond one frame; if the upstream socket is slow, drop the oldest frame and count it |
| `pump_events_down` | high | parses and pushes to fan-out queues without awaiting consumers |
| `run_controller` | normal | bounded queue size 256, drop-oldest, counter incremented |
| `drain_trace` | low | bounded queue 1024, drop-oldest with a `trace_dropped` counter |

Every queue is `asyncio.Queue(maxsize=N)` with an explicit overflow policy. Dropping
telemetry is always preferable to delaying audio. A dropped frame is a recorded metric,
never a silent loss.

Session concurrency is capped by `NOD_MAX_SESSIONS` (default 64 per worker). Beyond the
cap, new sessions are refused with a typed error rather than degrading every live call.

## 4. Time and speed complexity

Notation: `T` = turns in a session, `W` = words in a turn, `G` = gap ring capacity (256).

| Operation | Time | Space | Note |
|---|---|---|---|
| Ingest one partial `Turn` | `O(ΔW)` | `O(1)` | only new words are examined; `word_is_final` marks the boundary already processed |
| Update pause quantiles | `O(1)` | `O(1)` | P² streaming quantile estimator, five markers per quantile |
| Disfluency density update | `O(ΔW)` | `O(1)` | token comparison against a small ring of the last 4 tokens |
| Confidence jitter update | `O(1)` | `O(1)` | Welford running variance over the partial sequence. Weight 0 in the law; computed so the bench can evaluate it (ADR-011) |
| Cut detection | `O(1)` | `O(1)` | compares the new turn's first word start against the previous turn's last word end |
| `arbiter.decide()` | `O(1)` | `O(1)` | pure arithmetic over fixed scalars, no allocation beyond one frozen dataclass |
| Context policy lookup | `O(1)` | `O(P)` | policy compiled once into a dict at load |
| Trace append | `O(1)` amortised | `O(B)` | buffered writer, `B` = 64 KiB |
| Whole session | `O(ΣW)` | **`O(1)`** | resident memory independent of call length |

Hard budgets, enforced by a benchmark test in CI (`tests/property/test_budget.py`):

- `decide()` p99 < 5 ms, mean < 200 µs.
- Per-session resident footprint < 128 KiB excluding socket buffers.
- Added end-to-end latency attributable to Nod < 1 ms p99 (it is off the audio path, so
  the only cost is the fan-out put).

**Why P² and not a sorted window.** A sorted ring gives exact quantiles at
`O(G log G)` per recompute. P² gives a good estimate at `O(1)` per sample with five
floats of state. At `G = 256` a recompute is ~2 000 operations, which is fine once per
turn but wasteful per word. Implementation: P² as the primary estimator, with an exact
ring-buffer quantile available behind `NOD_EXACT_QUANTILES=1` for validating P² drift in
the bench. The bench asserts the two agree within 5 % on the corpora.

## 5. Caching

Five caches, each with a stated key, bound and invalidation rule. Nothing else caches.

| Cache | Key | Bound | Eviction | Why |
|---|---|---|---|---|
| **TTS audio** | `sha256(provider, voice_id, text, speed)` | 256 MiB on disk, 64 entries in memory | LRU | Prompt phrases repeat constantly; removes the dominant latency spike in the demo |
| **Policy compile** | `sha256(policy yaml)` | 8 entries | LRU | Avoids re-parsing YAML per session |
| **Capability probe** | `(model, api_version)` | 16 entries, TTL 1 h | TTL | A probe per session wastes a round trip |
| **Bench run** | — | **not built** | — | Designed, never implemented. `replay()` shipped without it and `code_version` exists nowhere in the tree. It stays unbuilt deliberately: caching a live run would serve five identical results for five repeats and collapse the IQR to zero, which is the precision-by-determinism defect ADR-019 exists to prevent (ADR-052) |
| **Console static** | Next.js build output | — | build | standard |

Rules:

- **Never cache a controller decision.** It is cheaper to recompute than to look up, and
  a stale decision is a correctness bug.
- **Never cache transcripts or caller audio** beyond the session, unless that session was
  explicitly marked for retention.
- The TTS cache is keyed on the full tuple including `speed`; a voice switch must not
  serve audio from the previous voice.
- Cache writes are atomic (write to `.tmp`, `os.replace`) so a crash cannot leave a
  truncated entry that later reads as valid.
- Every cache exposes hit/miss counters at `/metrics`.

## 6. Storage

- **Traces**: `data/traces/{session_id}.jsonl`, append-only, one JSON object per line,
  line schema versioned with a `v` field. Rotated by size (64 MiB) into `.1`, `.2`.
- **Index**: SQLite `data/nod.db`, tables `sessions`, `config_changes`, `bench_runs`.
  Holds metadata and file pointers only. Never the transcript text.
- **Why SQLite**: one container, no external dependency, transactional, and trivially
  backed up by copying a file. The write pattern is low-volume metadata. If concurrency
  ever exceeds one writer, revisit with an ADR; do not pre-emptively add Postgres.
- WAL mode on, `synchronous=NORMAL`, busy timeout 5 s.

## 7. API contracts

### `WS /v1/stream` — proxy mode

Query parameters mirror AssemblyAI's streaming endpoint so an existing client can switch
URL without changing code, plus:

| Param | Default | Meaning |
|---|---|---|
| `nod_preset` | `balanced` | starting configuration |
| `nod_mode` | `adapt` | `adapt` \| `observe` \| `off` |
| `nod_ceiling_ms` | `2600` | latency ceiling for the arbiter |

`observe` mode profiles and traces but sends no patches. This is the A arm of any A/B
and the safe first deployment step for a real user.

Client → server frames: binary audio (PCM16, 16 kHz, mono, 50 ms frames) forwarded
verbatim; JSON control frames passed through unchanged except `UpdateConfiguration`,
which is merged with Nod's own patch rather than fighting it (host wins on any field the
host set explicitly in the last 5 s).

Server → client frames: every upstream frame, unmodified, plus optional `NodEvent`
frames when `nod_events=1`.

### `WS /v1/console` — dashboard telemetry

Server → client only. Message types: `session.started`, `turn.partial`, `turn.final`,
`config.changed`, `cut.detected`, `agent.state`, `session.ended`. Each carries
`session_id` and `t_ms` (stream-relative). Fan-out is per-session-subscription; a console
client that falls behind is disconnected rather than allowed to buffer.

### `HTTP`

| Route | Purpose |
|---|---|
| `POST /v1/sessions` | create a session, returns id and websocket URL |
| `GET /v1/sessions/{id}` | metadata and config timeline |
| `GET /v1/sessions/{id}/trace` | JSONL download (redacted unless authorised) |
| `GET /v1/voices` | available TTS voices with provider, latency class, cost class |
| `POST /v1/sessions/{id}/voice` | switch voice mid-session |
| `GET /v1/presets` / `POST /v1/presets` | read and save presets |
| `POST /v1/bench/runs` | start a bench run, returns run id |
| `GET /v1/bench/runs/{id}` | status and results |
| `GET /healthz` `/readyz` `/metrics` | liveness, readiness, Prometheus |

All mutating routes require `Authorization: Bearer $NOD_API_TOKEN` when
`NOD_AUTH=required`. CORS defaults to deny; the console origin is allow-listed
explicitly.

## 8. Voice switching design

`TtsEngine` protocol:

```python
class TtsEngine(Protocol):
    id: str
    async def voices(self) -> Sequence[Voice]: ...
    async def synthesize(self, text: str, voice: Voice, speed: float) -> AudioChunkStream: ...
    def cancel(self) -> None: ...          # must be immediate, for barge-in
```

- Registry maps `provider:voice_id` to a resolved `Voice` carrying `latency_class`,
  `cost_class`, `sample_rate`, and `pacing_hint_ms`.
- A switch mid-session: cancel the in-flight synthesis, swap the engine reference,
  resynthesise only the **unspoken remainder** of the current utterance, and apply the
  new voice's `pacing_hint_ms` to the arbiter ceiling. The session never drops.
- `browser` provider (Web Speech API) is always registered and requires no key, so the
  demo works with zero cloud TTS spend and survives a dead vendor.
- Fallback chain is ordered and configurable; a synthesis failure falls to the next
  provider and emits `tts.fallback`, it never produces silence.

## 9. Observability

- `/metrics` exposes: `nod_turns_total`, `nod_cuts_total`, `nod_config_patches_total`,
  `nod_decide_seconds` (histogram), `nod_queue_dropped_total{queue}`,
  `nod_tts_cache_hits_total`, `nod_session_active`, `nod_upstream_reconnects_total`.
- Every log line carries `session_id` and `turn_order`.
- `nod_decide_seconds` is the canary for INV-2; alert above 5 ms p99.

## 10. Security

- Keys server-side only. The browser receives a short-lived session token scoped to one
  session id, not an API key.
- Trace redaction on by default: digit runs of 4+, date patterns, emails, and anything
  captured during a turn the policy marked as an entity turn.
- Rate limits per token on session creation and bench runs.
- No `eval`, no dynamic import of policy files; YAML is parsed with `yaml.safe_load` into
  a pydantic model with a closed schema.
- Dependencies pinned with hashes; `pip-audit` runs in CI.
