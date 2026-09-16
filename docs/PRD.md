# Nod — Product requirements

## 1. The bottleneck

Every deployed voice agent selects one end-of-turn threshold and applies it to every
human being who calls.

That single threshold is tuned for an average speaker, and it produces two failure modes
that pull in opposite directions:

- **Premature endpointing.** The agent treats a mid-sentence pause as the end of a turn
  and speaks over the caller. The caller loses their place, repeats themselves, and the
  turn costs three exchanges instead of one.
- **Sluggish endpointing.** The agent waits too long after a short answer and the
  conversation feels dead.

Teams resolve this by picking a point on that curve and living with it. The people at
the tails of the distribution pay for the compromise: older callers, non-native
speakers, people who stutter, anyone reading an ID or an address, anyone thinking.

Three things make it worse:

1. **It is invisible.** A cut-off caller does not file a ticket. They get frustrated and
   ask for a human. It surfaces as containment failure and handle time with no cause
   attached.
2. **It is unmeasured.** Teams can report word error rate. Almost none can report their
   premature cutoff rate, because nothing measures it.
3. **It is unfixable at the tails by tuning.** No static value is correct for both a
   brisk caller and a caller who blocks for two seconds on a word.

One sentence: *the industry can measure what the agent heard, and cannot measure whether
it let the person finish.*

## 2. What Nod does

Nod attaches to a live AssemblyAI Universal-Streaming session, profiles the caller's
rhythm from the transcript events already being sent, and rewrites the session's turn
detection configuration mid-call.

It moves on two axes at once:

- **Speaker axis.** Learned online from pause distribution, speech rate, disfluency
  density, and observed cut events. End-of-turn confidence jitter is logged but carries
  no control authority (ADR-011).
- **Context axis.** Declared by the agent's own dialogue state. When the expected answer
  is an ID, a date of birth, an address or a list, the listening window widens for that
  turn and snaps back afterwards.

The live configuration is therefore a function of **who is speaking × what they were
just asked**.

## 3. Users

| User | What they need | Surface |
|---|---|---|
| Voice agent developer | Stop their agent cutting callers off without hand-tuning per deployment | Proxy mode, SDK, presets |
| Contact centre operations lead | Evidence that the automated line serves everyone, and a number to put in a review | Console, benchmark report |
| Accessibility / compliance reviewer | Proof the deployment does not systematically fail atypical speakers | Trace export, benchmark report |
| The caller | To finish their sentence | Nothing visible, which is the point |

## 4. Deployment modes

Three, in increasing order of integration effort. All three ship.

**Mode A — Proxy (one URL change).**
Nod exposes a WebSocket endpoint with the same contract as AssemblyAI's streaming
endpoint. An existing application points at Nod instead, passes its own key, and gets
adaptation with no code change. Nod forwards audio verbatim, observes the transcript
stream, and injects `UpdateConfiguration` frames. This is the adoption path and the
single most important feature for real-world relevance.

**Mode B — SDK / sidecar.**
`from nod_core import Controller` — the host application owns the socket and feeds turn
events in, receives config patches out. Gives the host control over when patches apply,
and enables the context axis, which needs dialogue state the proxy cannot see.

**Mode C — Full reference agent.**
The bundled intake agent (STT → LLM → TTS) used for the demo and for end-to-end tests.
Proves the loop and is what the console visualises.

## 5. Feature list

### Core (must ship)
- F-1 Session proxy with byte-transparent audio forwarding.
- F-2 Speaker profiler: pause quantiles, speech rate, disfluency density, confidence
  jitter (logged, weight 0), cut detection.
- F-3 Context policy engine driven by a declarative YAML policy file.
- F-4 Arbiter with hysteresis, asymmetric decay, latency ceiling, update rate cap.
- F-5 Mid-stream `UpdateConfiguration` emission plus `ForceEndpoint` on confident early
  completion.
- F-6 Capability probe at session start; graceful degradation when a knob or a model
  feature is unavailable.
- F-7 Append-only JSONL trace with redaction, one file per session.
- F-8 Deterministic replay harness and metric suite (`make bench`).
- F-9 Console: live call view, A/B replay view, benchmark view, settings.
- F-10 `FakeAssemblyAI` fixture server so the whole system runs and tests offline.

### Product (differentiators)
- F-11 **Presets.** Named starting configurations per domain: `healthcare-intake`,
  `drive-thru`, `field-ops`, `elderly-outreach`, `id-capture`. Each is a base config plus
  a context policy map.
- F-12 **Auto-tune.** `nod tune --domain healthcare --corpus <dir>` sweeps the static
  parameter space against the bench corpus and emits a recommended preset with the
  supporting Pareto data. This is the "configure itself for the requirement" feature.
- F-13 **Voice switching.** TTS is a pluggable adapter with a registry. The console
  switches provider and voice live, mid-session, without dropping the call. Providers:
  `browser` (Web Speech, zero cost, always available), plus cloud adapters behind one
  interface. Latency, cost and availability are surfaced per voice.
- F-14 **Persona and pacing pairing.** A voice selection carries a pacing hint (a slower
  voice implies a slightly wider ceiling) so switching voice does not silently change
  perceived responsiveness.
- F-15 **Trace explorer.** Replay any past session in the console with the config strip
  animating exactly as it did live.
- F-16 **Report card.** One-page shareable HTML summary of a benchmark run.

### Explicit non-goals
- Not a transcription engine. Nod does not train or host ASR.
- Not an agent framework. It does not own dialogue state; it reads a declared one.
- No emotion, honesty, deception, stress or clinical inference from speech timing. Ever.
- No multi-tenant billing, SSO or org management in v1.
- No telephony. v1 is browser audio and file replay; SIP is a post-hackathon adapter.

## 6. Success criteria

The project is done when all of these are true:

1. `make bench` regenerates the README table from committed traces on a clean clone with
   no API key, using `FakeAssemblyAI`.
2. On the synthetic corpus, Nod shows a materially lower premature cutoff rate than the
   Balanced static config **at equal or better p90 turn latency**. The number is whatever
   it is; the requirement is that it is measured and reproducible, not that it is large.
3. The console demonstrates a live call with the config strip changing and a stated
   reason for each change.
4. Proxy mode works against a third-party sample app with a one-line URL change.
5. Voice can be switched mid-call without a dropped session.
6. Cold clone → `docker compose up` → working demo in under five minutes.

## 7. Risks

| Risk | Mitigation |
|---|---|
| A knob behaves differently from the docs | Capability probe on day one; degrade to the silence axis |
| Chosen model uses punctuation-based rather than confidence-based turn detection | Controller supports both; confidence axis disables itself |
| Real-time replay jitter makes metrics noisy | N repeats per condition, report variance, paced feeder with deadline scheduling |
| API credit exhaustion on demo day | `FakeAssemblyAI` replay mode runs the full console from recorded traces |
| Glass UI tanks frame rate next to a live waveform | Glass layer budget, no animated blur, canvas waveform outside the blur stack |
