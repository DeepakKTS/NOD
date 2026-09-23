# Pilot gate — where does the incomplete-utterance regime begin?

**Thirty seconds of audio, recorded before any Track C session is booked.**

This is a second pilot, additional to `TRACK_C_SCRIPT.md` §9. That one asks whether the
*seams* register at all — a room-tone question, about whether silence is silent enough.
This one asks whether the *regime* registers: whether the service ever judges a human's
mid-sentence pause as incomplete, and at what pause length it starts to.

## Why it exists

Gate 4c established (ADR-051) that on Track A **`max_turn_silence` never binds**. Pauses
up to 2200 ms — 2.75× `conservative`'s min gate — were all judged as following *complete*
utterances, so the min gate fired every time. The max gate is the knob ADR-011 makes Nod's
primary lever and the only one that tolerates a mid-sentence pause.

The complement shows the service is discriminating, not indifferent: the end-of-clip
silence, which follows "…the next thing I wanted to mention is", **does** bind at max on
all three arms. So there is a boundary. Track A simply sits on the wrong side of it
everywhere.

**A Track C script whose pauses all follow completable prefixes would reproduce that
result at ten times the cost**, and the gap budget is not recoverable from the audio
afterwards (ADR-031). Hence: probe first.

## What to record

One take, about thirty seconds, same mic and room as the real session. Six short
utterances, each **cut off mid-structure** and then held silent for a measured count.

Read each line, pause for the stated hold, then continue with the continuation. Do not
drop pitch at the pause — a falling contour is itself a completion cue, and the point is
to test the text, not the prosody.

| # | say this, then hold | hold | then continue |
|---|---|---|---|
| 1 | "I need to reschedule my appointment to" | **0.5 s** | "the following Tuesday" |
| 2 | "I need to reschedule my appointment to" | **1.0 s** | "the following Tuesday" |
| 3 | "I need to reschedule my appointment to" | **2.0 s** | "the following Tuesday" |
| 4 | "I need to reschedule my appointment to" | **3.5 s** | "the following Tuesday" |
| 5 | "My member number is four seven" | **2.0 s** | "two one nine" |
| 6 | "My appointment was on March the" | **2.0 s** | "fourteenth" |

Rows 1–4 are a **ladder on one prefix**: same words, four hold lengths, so the only thing
varying is duration. Rows 5–6 vary the *prefix kind* at a fixed 2.0 s — a truncated digit
run and a truncated date, the two shapes an intake call actually produces.

Every prefix ends where English cannot stop: on a preposition ("to"), mid-digit-run, or on
a determiner ("the"). That is the deliberate contrast with Track A, whose insertion points
left grammatically complete prefixes such as "…my appointment was on March".

## How to run it

Record as mono 16 kHz PCM16 WAV, then feed it to each arm and read where the boundaries
fell. No new code is required: `scripts/live_smoke.py` already drives one clip through all
six arms and prints per-arm boundary times.

    uv run --extra bench python scripts/live_smoke.py --clip <pilot-clip-id>

Six arms x one clip = 6 sessions. At the measured 16 s start interval that is under two
minutes and a few cents.

## Pass / fail

Read the boundary that falls inside each hold, and compare it to that arm's two gates.

- **Fired at ≈ `min_turn_silence` + ~160 ms** → the service judged the prefix *complete*.
  The regime is not reached at that hold length.
- **Fired at ≈ `max_turn_silence` + ~160 ms**, or **no boundary at all** where the hold is
  shorter than the max gate → the service judged it *incomplete*. **The regime is reached.**

The ~160 ms is the endpoint overhead measured across three arms at Gate 4b (159–164 ms).

**PASS: at least one row reaches the incomplete regime on `balanced`.** Record the
shortest hold that does, and the prefix kind. That number is what the Track C pause budget
must exceed, and it is the single most useful figure this pilot produces.

**FAIL: every row fires at the min gate on every arm.** Then the service does not treat
human mid-sentence pauses as incomplete under our configuration either, and Track A was
not the problem. **Do not book the session on a FAIL.** Two things to check first, in this
order, because both are cheaper than a recording day:

1. `end_of_turn_confidence_threshold` was measured **INERT** at P1 (ADR-001). If semantic
   gating is genuinely inactive on `universal-streaming-english`, `max_turn_silence` may be
   unreachable by construction rather than by corpus — which would be a finding about the
   product's premise, not about the script, and belongs in the honest-scope section
   immediately.
2. Try the ladder with a longer hold (5 s, 8 s). If the regime appears only past
   `conservative`'s 3600 ms gate, the mechanism exists but no arm in the published set can
   express it.

**Either outcome is worth the thirty seconds.** A PASS sizes the recording script. A FAIL
stops a three-hour session that would have produced ten calls of the same null result, and
redirects the claim before the deadline rather than after it.
