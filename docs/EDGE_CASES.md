# Nod — Edge cases and failure modes

Every row is a required test or a required guard. `EC-n` ids are referenced from test
names (`test_ec_014_upstream_expiry`).

## 1. Stream and session

| Id | Case | Required behaviour |
|---|---|---|
| EC-01 | Caller never speaks | Idle timer `IDLE_MS` (default 15 000) fires one prompt, then closes with `reason=idle`. Profiler stays `COLD`, no patches. |
| EC-02 | Caller speaks before the socket is ready | Buffer up to 500 ms of audio in a ring; drop older, count `prebuffer_dropped`. Never block. |
| EC-03 | Upstream session expiry (`Begin.expires_at` reached) | Reconnect proactively at `expires_at - 30 s`, carry the profiler state across by `session_id`, replay the current config on the new socket, emit `upstream_rotated`. Caller sees nothing. |
| EC-04 | Upstream closes abnormally (1006) | Exponential backoff 200 ms → 3.2 s, max 5 attempts, jittered. Audio is buffered up to 2 s then dropped oldest. On give-up, close the client socket with a typed error. |
| EC-05 | Client disconnects mid-turn | Flush trace, finalise session row, cancel all four tasks, release the upstream socket. No orphan tasks; asserted by a leak test. |
| EC-06 | Two `Begin` messages | Treat the second as a rotation, not a new session. |
| EC-07 | Out-of-order or duplicated `turn_order` | Ignore any turn with `turn_order <= last_seen`. Log once per session. |
| EC-08 | Clock: word timings vs wall clock | Controller uses stream-relative ms only. A test asserts `decide()` never reads the wall clock. |
| EC-09 | Very long call (4 h+) | Ring buffers keep memory flat; trace rotates at 64 MiB; P² needs no growth. Asserted by a soak test. |
| EC-10 | `MAX_SESSIONS` reached | Reject new sessions with 503 and `Retry-After`. Never degrade live sessions. |

## 2. Speech content

| Id | Case | Required behaviour |
|---|---|---|
| EC-11 | Single-word answer ("yes") | Context `boolean` clamps `min_ms ≤ 400`. Must not feel sluggish; covered by a latency assertion in the bench. |
| EC-12 | Caller spells a name letter by letter | `spelling` hint; many short words with large gaps must not be read as many turns. Fragmentation assertion. |
| EC-13 | Caller reads a long ID with grouped pauses | `entity_id` hint widens for that turn only; a test fails if the widening persists into the next turn. |
| EC-14 | Pathological 8-second silence mid-utterance | Gap clamped at 6 000 ms before ingestion; `max_ms` still bounded by `ceiling_ms`. One outlier must not shift `g_p90` more than a bounded amount — property test. |
| EC-15 | Extremely fast speaker | Floors (`min_ms ≥ 160`, `max_ms ≥ 400`) hold. Controller must not drive toward zero. |
| EC-16 | Empty or noise-only turn (`transcript == ""`) | Excluded from profiling entirely. Not counted as a turn for cut detection or rate caps. |
| EC-17 | Background TV or second speaker on the channel | Raise `vad_threshold` when short spurious turns exceed a rate threshold; if diarization is on and the speaker label changes, do not attribute those gaps to the primary profile. |
| EC-18 | Two genuine speakers share the handset | With diarization on, maintain a profile per `speaker_label`, capped at 4; beyond that, fall back to a shared profile. Without diarization, a speaker change looks like a rhythm change and the decay guard absorbs it. |
| EC-19 | Code-switching mid-sentence | Filler-word set is locale-configured; unknown-locale tokens simply do not fire the filler feature. Never treat a foreign token as a disfluency. |
| EC-20 | DTMF tones or hold music | Non-speech turns produce empty transcripts → EC-16. |
| EC-21 | Last word never finalises (`word_is_final=false` at boundary) | Skip that word for timing; do not compute a gap against an unfinalised end. |
| EC-22 | Caller laughs, coughs, sighs | Usually empty or low-confidence tokens; the duration-outlier feature must not treat a 900 ms `haha` as a prolongation — excluded by the filler/non-lexical list. |

## 3. Agent interaction

| Id | Case | Required behaviour |
|---|---|---|
| EC-23 | Barge-in: caller speaks over TTS | Cancel synthesis immediately, truncate the agent's recorded "spoken" text to what was actually emitted, and **do not** count the caller's speech as a cut (cut rule condition 3). |
| EC-24 | False barge-in: VAD fires, transcript empty | Resume the agent's utterance from where it stopped after `FALSE_BARGE_MS` (default 600). Do not restart from the beginning. |
| EC-25 | Backchannel ("mm-hm") while the agent talks | Not an interruption; agent continues. Backchannel token list is configurable. |
| EC-26 | LLM slow or times out | Emit a short filler after `FILLER_MS` (default 1200) from the fixed phrase set, then the real answer. Never silence, never a dropped turn. Filler turns are excluded from latency metrics and labelled in the trace. |
| EC-27 | TTS provider fails mid-utterance | Fall to the next provider in the chain, resynthesise only the unspoken remainder, emit `tts.fallback`. |
| EC-28 | Voice switched mid-utterance | Cancel, resynthesise the remainder in the new voice, apply the new `pacing_hint_ms` to the ceiling. Session must not drop — explicit integration test. |
| EC-29 | Agent and caller start simultaneously | Caller wins. Agent yields within one frame. |

## 4. Controller

| Id | Case | Required behaviour |
|---|---|---|
| EC-30 | Config oscillation | Freeze guard: 3 direction reversals in 5 turns → `FROZEN` for 10 turns. |
| EC-31 | Controller raises an exception | `SAFE` state, last known good config, `controller_error` counted, call continues. Dev re-raises post-call. |
| EC-32 | `UpdateConfiguration` rejected by upstream | One retry with the clamped value; then the session drops to `observe` and the console shows a banner. |
| EC-33 | Host app sends its own `UpdateConfiguration` | Host wins on fields it set, for 5 s. Nod merges rather than fights. |
| EC-34 | Model lacks confidence-based turn detection | Confidence axis disabled by the capability probe; silence axis carries the load; `capability_degraded` emitted once. |
| EC-35 | Patch budget exhausted (24) | Stop patching, keep profiling, mark the session `budget_exhausted`. This is a signal the constants are wrong, surfaced in the bench. |
| EC-36 | Profiler cold for the whole call (very short call) | Base + context only. Never adapt on fewer than 8 gaps. |
| EC-49 | Endpointing overhead on top of the configured gate | The boundary lands `ENDPOINT_OVERHEAD_MS` after whichever silence gate binds (P1 measured 155-290 ms). The ceiling subtracts it, so `ceiling_ms` is a promise about the boundary and not about the knob. The constant is derived by `make bench`, never hand-written (INV-9). |
| EC-50 | A knob measured in the wrong regime | Each silence knob binds in one regime only: `min_turn_silence` after a complete utterance, `max_turn_silence` after an incomplete one. A probe or bench arm that offers only one regime reads the other knob as inert. Stimuli name the regime they need and the trace records it. |

## 5. Bench

| Id | Case | Required behaviour |
|---|---|---|
| EC-37 | Feeder drift | Abort when 4 consecutive frames each lag more than 25 ms behind the absolute schedule; a drifted run is void, not reported. A single-frame stall is an OS scheduling artefact — a 5-minute soak saw p99 of 1.5 ms and one 16 ms outlier — and displaces one 50 ms frame, not the timeline (ADR-012). |
| EC-38 | Run-to-run variance exceeds arm difference | Report "inconclusive" in those words. Never present a difference smaller than the noise. |
| EC-39 | Corpus file changed since the manifest | Hash check fails the run loudly. |
| EC-40 | Partial run interrupted | Per-arm caching means resume re-runs only what is missing; the manifest records partial status. |
| EC-41 | Track A and Track C disagree in direction | Report both prominently; do not select the favourable one. |

## 6. Operations and data

| Id | Case | Required behaviour |
|---|---|---|
| EC-42 | PII in transcript | Redaction on by default, specified over **partials** rather than finished utterances, and **per key**. Sentence keys (`transcript`, `utterance`): digit runs of 3+, phone shapes separated by spaces, hyphens, en or em dashes, dates, emails, and everything captured during an entity turn. Word keys (`words[].text`): shape rules first, then any token still carrying a digit masks as `[NUM]` — a one-to-four character token is below every sentence threshold (ADR-015). A rule tuned to the completed number leaks every prefix of it, and the prefix of a phone number is an area code (ADR-013). Known gap: hyphen-separated single digits in a sentence (`6-1-1`) are still unmasked, see ADR-015. Raw retention requires `NOD_TRACE_RAW=1` plus an explicit per-session flag. |
| EC-43 | Disk full | Trace writer detects `ENOSPC`, disables tracing for the session, emits a metric, and the call continues. Tracing is never load-bearing for a call. |
| EC-44 | Cache corruption after a crash | Atomic write (`.tmp` + `os.replace`); a truncated file can never be read as valid. |
| EC-45 | API credits exhausted | Console switches to replay mode driven by committed traces so a demo still runs. |
| EC-46 | Clock skew between container and host | All metrics use `time.monotonic()` for durations; wall clock only for display. |
| EC-47 | Reduced-transparency or reduced-motion user setting | Glass falls back to solid surfaces; all motion becomes instant state changes. Not a degraded experience, an equivalent one. |
| EC-48 | Console opened on a 4 K display with six glass layers | Glass budget enforced (max 6 blurred layers, no animated blur); waveform drawn on canvas outside the blur stack. |
