"""The committed probe trace is a usable, redacted `FakeAssemblyAI` seed.

ROADMAP Phase 1 bullet 5 replays this file instead of opening a socket
(INV-7, EC-45), so it has to stay parseable, stay on schema v1, and stay free of
PII. The digit assertions are the regression ADR-013 and ADR-015 exist for: a
redaction rule tuned to a finished utterance leaks every partial on the way to
it, and a committed fixture would carry that leak permanently.

The audio behind it was synthesized with macOS `say` and is not benchmark-grade
— see `tests/fixtures/traces/README.md`.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from pathlib import Path

import pytest

from nod_core.types import TRACE_SCHEMA_VERSION, JsonValue

FIXTURE = (
    Path(__file__).parent.parent
    / "fixtures"
    / "traces"
    / "seed-min-turn-silence-midstream.jsonl"
)

_MARK = re.compile(r"\[(?:NUM|PHONE|DATE|EMAIL)\]")


def _records() -> list[Mapping[str, JsonValue]]:
    """Every line of the fixture, parsed. `O(size)`."""
    with FIXTURE.open() as handle:
        loaded = [json.loads(line) for line in handle if line.strip()]
    records: list[Mapping[str, JsonValue]] = []
    for item in loaded:
        assert isinstance(item, dict), "every trace line is a JSON object"
        records.append(item)
    return records


def _payload(record: Mapping[str, JsonValue]) -> Mapping[str, JsonValue]:
    """Narrow one record's payload. `O(1)`."""
    payload = record["payload"]
    assert isinstance(payload, Mapping), "every record carries an object payload"
    return payload


def _turns(*, end_of_turn: bool) -> list[Mapping[str, JsonValue]]:
    """Downstream `Turn` frames, split by whether they closed a turn. `O(n)`."""
    found: list[Mapping[str, JsonValue]] = []
    for record in _records():
        if record["kind"] != "event":
            continue
        payload = _payload(record)
        if payload.get("type") != "Turn":
            continue
        if bool(payload.get("end_of_turn")) is end_of_turn:
            found.append(payload)
    return found


def _strings_under(value: JsonValue, key: str) -> list[str]:
    """Collect every string stored under `key`, at any depth. `O(size)`."""
    found: list[str] = []
    if isinstance(value, Mapping):
        for found_key, found_value in value.items():
            if found_key == key and isinstance(found_value, str):
                found.append(found_value)
            else:
                found.extend(_strings_under(found_value, key))
    elif isinstance(value, (list, tuple)):
        for item in value:
            found.extend(_strings_under(item, key))
    return found


def test_the_fixture_exists_and_parses() -> None:
    records = _records()
    assert records, "the replay seed must not be empty"
    assert all(r["v"] == TRACE_SCHEMA_VERSION for r in records)


def test_sequence_numbers_are_dense_and_monotonic() -> None:
    """A replay server reads this in order; a gap would silently drop a frame."""
    seqs = [r["seq"] for r in _records()]
    assert seqs == list(range(len(seqs)))


def test_the_fixture_carries_what_a_replay_server_needs() -> None:
    """One meta, a mid-stream reconfiguration, real boundaries, and a verdict."""
    kinds = [r["kind"] for r in _records()]
    assert kinds.count("meta") == 1
    assert kinds.count("verdict") == 1

    sent = [_payload(r).get("type") for r in _records() if r["kind"] == "sent"]
    assert "UpdateConfiguration" in sent, "replay needs a mid-stream config change"

    assert len(_turns(end_of_turn=True)) >= 2, "the Floor Meter needs >1 turn"
    assert _turns(end_of_turn=False), "the confidence trajectory needs partials"


def test_the_fixture_records_its_own_regime() -> None:
    """EC-50: which silence knob binds depends on the lead utterance.

    A future reader must be able to tell this is the complete-utterance regime
    without re-deriving it from the audio.
    """
    meta = _payload(next(r for r in _records() if r["kind"] == "meta"))
    assert meta["field"] == "min_turn_silence"
    assert meta["lead_segment"] == 1
    assert meta["trace_raw"] is False
    gap_start, gap_end = meta["gap_start_ms"], meta["gap_end_ms"]
    assert isinstance(gap_start, int) and isinstance(gap_end, int)
    assert gap_start < gap_end


@pytest.mark.parametrize("key", ["transcript", "text"])
def test_no_unmasked_digit_survives_in_the_fixture(key: str) -> None:
    """ADR-013 and ADR-015, EC-42.

    `transcript` is a sentence and `text` is one word token; they take different
    rules and both must come out clean. Redaction was applied at write time, so
    a trace captured before ADR-015 could not be cleaned afterwards and was
    rejected rather than scrubbed.
    """
    offenders: list[str] = []
    for record in _records():
        offenders.extend(
            found
            for found in _strings_under(_payload(record), key)
            if re.search(r"\d", _MARK.sub("", found))
        )
    assert not offenders, f"unmasked digits in {key}: {offenders[:5]}"


def test_the_fixture_actually_contains_redacted_pii() -> None:
    """Guards the test above from passing because there was nothing to redact."""
    text = FIXTURE.read_text()
    assert "[PHONE]" in text
    assert "[DATE]" in text
