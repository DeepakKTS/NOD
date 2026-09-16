"""Traces are redacted at write time and keep the measurement intact.

INV-6, INV-5, EC-42, EC-43. The test that matters most here is the one asserting
word timings survive: a blanket "redact the words array" looks like the cautious
choice and would silently destroy every turn-boundary reading the probe exists to
take.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from nod_core.trace import (
    TraceSink,
    redact,
    redact_payload,
    redact_url,
    redact_word,
)
from nod_core.types import TRACE_SCHEMA_VERSION, JsonValue

WORDS: list[dict[str, JsonValue]] = [
    {
        "text": "call",
        "start": 100,
        "end": 240,
        "confidence": 0.91,
        "word_is_final": True,
    },
    {
        "text": "5551234567",
        "start": 240,
        "end": 900,
        "confidence": 0.77,
        "word_is_final": True,
    },
]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("my number is 5551234567", "[PHONE]"),
        ("account 987654", "[NUM]"),
        ("write to me@example.com", "[EMAIL]"),
        ("on 2026-09-15 please", "[DATE]"),
        ("on 09/15/2026 please", "[DATE]"),
        ("on September 15th 2026", "[DATE]"),
    ],
)
def test_redact_masks_each_shape(raw: str, expected: str) -> None:
    assert expected in redact(raw)


def test_dates_are_not_swallowed_by_the_digit_rule() -> None:
    """Order is load-bearing: an ISO year is also a four-digit run."""
    assert redact("2026-09-15") == "[DATE]"
    assert "[NUM]" not in redact("2026-09-15")


def test_short_digit_runs_survive() -> None:
    """EC-42 masks runs of three or more. "I have 3 cats" is not PII."""
    assert redact("I have 3 cats and 12 fish") == "I have 3 cats and 12 fish"


# The partial sequence a live probe session actually produced while the speaker
# read a phone number. Taken verbatim from
# probe-...-min_turn_silence-connect_low-r0.jsonl, where the completed number
# masked as [PHONE] and the two prefixes leaked in 74 trace records.
LEAKED_PARTIALS: list[str] = [
    "i am reading at a normal pace you can call me back on 617",
    "i am reading at a normal pace you can call me back on 617 555",
    "i am reading at a normal pace you can call me back on 617 555 0134",
]

_MARK = re.compile(r"\[(?:NUM|PHONE|DATE|EMAIL)\]")


@pytest.mark.parametrize("partial", LEAKED_PARTIALS)
def test_no_prefix_of_a_phone_number_survives_redaction(partial: str) -> None:
    """EC-42 over partials, not whole utterances (ADR-013).

    A streaming endpointer emits a number one group at a time. Masking only the
    completed shape leaks every prefix of it, and the prefix of a phone number
    is an area code.
    """
    out = redact(partial)
    assert "617" not in out
    assert "555" not in out
    assert not re.search(r"\d", _MARK.sub("", out)), out


def test_the_rest_of_a_leaking_partial_is_preserved() -> None:
    """Redaction removes the number, not the turn. The words carry the features."""
    out = redact(LEAKED_PARTIALS[1])
    assert out.startswith("i am reading at a normal pace you can call me back on ")
    assert _MARK.search(out) is not None


def test_word_timings_and_confidences_are_never_touched() -> None:
    """The measurement must survive redaction. This is the load-bearing case."""
    out = redact_payload({"transcript": "call 5551234567", "words": WORDS})
    assert isinstance(out, dict)
    words = out["words"]
    assert isinstance(words, list)

    assert words[0]["start"] == 100
    assert words[0]["end"] == 240
    assert words[0]["confidence"] == 0.91
    assert words[0]["word_is_final"] is True
    assert words[1]["start"] == 240
    assert words[1]["end"] == 900
    assert words[1]["confidence"] == 0.77

    # ...while the speech itself is masked, in both places it appears.
    assert "5551234567" not in json.dumps(out)
    assert "[PHONE]" in str(words[1]["text"])


# The `words[].text` tokens the P3 replay fixture actually carried while the
# speaker read a phone number. A streaming endpointer grows a word character by
# character, so the completed words masked and their prefixes did not.
LEAKED_WORD_TOKENS: list[str] = ["6", "61", "5", "55", "0", "01", "4"]


@pytest.mark.parametrize("token", LEAKED_WORD_TOKENS)
def test_no_digit_bearing_word_token_survives(token: str) -> None:
    """ADR-015. Every threshold in `redact` is too coarse for one word token."""
    assert redact_word(token) == "[NUM]"
    assert not re.search(r"\d", redact_word(token))


def test_a_word_token_keeps_its_mask_type() -> None:
    """Shape rules run first, so `redact_word` does not flatten [PHONE] to [NUM].

    This is the ordering ADR-015 chose. Masking digits first would lose the
    distinction between a recognised phone number and an unrecognisable
    fragment, and `test_word_timings_and_confidences_are_never_touched` asserts
    the [PHONE] case.
    """
    assert redact_word("5551234567") == "[PHONE]"
    assert redact_word("617") == "[NUM]"


def test_a_word_token_without_digits_is_untouched() -> None:
    """Disfluency features are token text. Masking real words would break them."""
    for token in ("um", "the", "appointment", "Nod"):
        assert redact_word(token) == token


def test_the_words_array_is_routed_through_the_word_rule() -> None:
    """The routing, not just `redact_word` in isolation (ADR-015).

    Calling `redact_word` directly does not prove `redact_payload` reaches it;
    putting `text` back on the sentence rule leaves such a test green while the
    leak returns. This drives the real fixture tokens through the walker.
    """
    words_in: list[dict[str, JsonValue]] = [
        {"text": token, "start": 0} for token in LEAKED_WORD_TOKENS
    ]
    out = redact_payload({"words": words_in})
    assert isinstance(out, dict)
    words = out["words"]
    assert isinstance(words, list)
    assert [w["text"] for w in words] == ["[NUM]"] * len(LEAKED_WORD_TOKENS)
    assert not re.search(r"\d", json.dumps([w["text"] for w in words]))


def test_sentence_keys_and_word_keys_take_different_rules() -> None:
    """ADR-015 split `text` out of the sentence keys.

    The same string must survive in a transcript and mask in a word token: "3"
    in a sentence is a quantity, "3" as a whole token being read aloud is a
    digit of something.
    """
    out = redact_payload(
        {"transcript": "I have 3 cats", "words": [{"text": "3", "confidence": 0.9}]}
    )
    assert isinstance(out, dict)
    assert out["transcript"] == "I have 3 cats"
    words = out["words"]
    assert isinstance(words, list)
    assert words[0]["text"] == "[NUM]"
    assert words[0]["confidence"] == 0.9


@pytest.mark.parametrize(
    "raw",
    [
        "call me back on 6\u20131\u20131\u20132",
        "call me back on 6\u20141\u20141\u20142",
    ],
)
def test_dash_separated_digit_groups_are_masked(raw: str) -> None:
    """universal-3-5-pro formats numbers with en and em dashes (ADR-015).

    A hyphen-only separator class does not see those numbers at all, so the
    whole sequence fell through `_PHONE` and, being single digits, through the
    digit-run rule too.
    """
    out = redact(raw)
    assert "[PHONE]" in out
    assert not re.search(r"\d", out)


def test_redact_url_strips_a_credential_and_keeps_the_rest() -> None:
    url = "wss://streaming.assemblyai.com/v3/ws?sample_rate=16000&token=sk-secret-123"
    out = redact_url(url)
    assert "sk-secret-123" not in out
    assert "sample_rate=16000" in out
    assert "[REDACTED]" in out


@pytest.mark.asyncio
async def test_sink_writes_versioned_lines(tmp_path: Path) -> None:
    sink = TraceSink("s1", directory=tmp_path)
    sink.emit(
        "event", {"type": "Turn", "transcript": "hello"}, t_ms=400, direction="down"
    )
    await sink.aclose()

    lines = sink.path.read_text().strip().splitlines()
    record = json.loads(lines[0])
    assert record["v"] == TRACE_SCHEMA_VERSION
    assert record["seq"] == 0
    assert record["t_stream_ms"] == 400
    assert record["dir"] == "down"
    assert record["session_id"] == "s1"
    assert record["kind"] == "event"


@pytest.mark.asyncio
async def test_sink_redacts_by_default_and_not_when_raw(tmp_path: Path) -> None:
    sink = TraceSink("redacted", directory=tmp_path)
    sink.emit("event", {"transcript": "call 5551234567"}, t_ms=0)
    await sink.aclose()
    assert "5551234567" not in sink.path.read_text()

    loud = TraceSink("raw", directory=tmp_path, raw=True)
    loud.emit("event", {"transcript": "call 5551234567"}, t_ms=0)
    await loud.aclose()
    assert "5551234567" in loud.path.read_text()


@pytest.mark.asyncio
async def test_sequence_numbers_are_monotonic(tmp_path: Path) -> None:
    """A truncated tail must be detectable rather than silent."""
    sink = TraceSink("seq", directory=tmp_path)
    for i in range(5):
        sink.emit("frame", {"n": i}, t_ms=i * 50)
    await sink.aclose()

    seqs = [
        json.loads(line)["seq"] for line in sink.path.read_text().strip().splitlines()
    ]
    assert seqs == [0, 1, 2, 3, 4]


@pytest.mark.asyncio
async def test_queue_overflow_drops_oldest_and_counts(tmp_path: Path) -> None:
    """ARCHITECTURE §3: dropping telemetry beats delaying audio."""
    sink = TraceSink("overflow", directory=tmp_path, maxsize=4)
    for i in range(10):
        sink.emit("frame", {"n": i}, t_ms=i)
    assert sink.dropped == 6

    await sink.aclose()
    kept = [
        json.loads(line)["payload"]["n"]
        for line in sink.path.read_text().strip().splitlines()
    ]
    assert kept == [6, 7, 8, 9]


@pytest.mark.asyncio
async def test_unwritable_directory_disables_tracing_not_the_call(
    tmp_path: Path,
) -> None:
    """EC-43: tracing is never load-bearing for a call."""
    blocker = tmp_path / "blocked"
    blocker.write_text("not a directory")

    sink = TraceSink("enospc", directory=blocker / "traces")
    sink.emit("event", {"transcript": "hello"}, t_ms=0)
    await sink.aclose()

    assert sink.disabled is True
    # And further emits are silently discarded rather than raising.
    sink.emit("event", {"transcript": "still here"}, t_ms=1)


@pytest.mark.asyncio
async def test_drain_writes_while_the_session_runs(tmp_path: Path) -> None:
    import asyncio

    sink = TraceSink("drained", directory=tmp_path)
    task = asyncio.ensure_future(sink.drain())
    for i in range(3):
        sink.emit("frame", {"n": i}, t_ms=i)
    await asyncio.sleep(0.05)
    await sink.aclose()
    await asyncio.wait_for(task, timeout=1.0)

    assert len(sink.path.read_text().strip().splitlines()) == 3


@pytest.mark.asyncio
async def test_trace_rotates_at_the_size_bound(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ARCHITECTURE §6: traces roll into `.1` rather than growing without bound."""
    import nod_core.trace as trace_module

    monkeypatch.setattr(trace_module, "ROTATE_BYTES", 200)
    sink = TraceSink("rotating", directory=tmp_path)
    for i in range(20):
        sink.emit("frame", {"n": i, "pad": "x" * 40}, t_ms=i)
    await sink.aclose()

    assert (tmp_path / "rotating.jsonl.1").exists()


@pytest.mark.asyncio
async def test_sink_reuses_one_handle_across_flushes(tmp_path: Path) -> None:
    import asyncio

    sink = TraceSink("reused", directory=tmp_path)
    task = asyncio.ensure_future(sink.drain())
    for i in range(3):
        sink.emit("frame", {"n": i}, t_ms=i)
        await asyncio.sleep(0.02)
    await sink.aclose()
    await asyncio.wait_for(task, timeout=1.0)

    lines = sink.path.read_text().strip().splitlines()
    assert [json.loads(line)["payload"]["n"] for line in lines] == [0, 1, 2]
