"""Unrecognised upstream frame types surface instead of being swallowed.

`AssemblyAISession.events` ignores what it cannot translate, which is correct
for INV-8 — an unknown message must never drop a call — and is also how a new
frame type stays invisible. `unknown_frame_types` is the counterweight: run it
over a trace and anything new shows up as a name.

`SpeechStarted` is the concrete case. The 69-session P1 matrix saw it 22 times,
only ever from `universal-3-5-pro`, and it appears in no documentation.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path

from nod_adapters.assemblyai.session import KNOWN_FRAME_TYPES, unknown_frame_types
from nod_core.types import JsonValue

FIXTURE = (
    Path(__file__).parent.parent
    / "fixtures"
    / "traces"
    / "seed-min-turn-silence-midstream.jsonl"
)


def _frames_of(path: Path) -> list[Mapping[str, JsonValue]]:
    """Downstream frames recorded in a trace. `O(size)`."""
    frames: list[Mapping[str, JsonValue]] = []
    with path.open() as handle:
        for line in handle:
            record = json.loads(line)
            if record.get("kind") != "event":
                continue
            payload = record["payload"]
            assert isinstance(payload, Mapping)
            frames.append(payload)
    return frames


def test_the_documented_four_are_known() -> None:
    assert {"Begin", "Turn", "Termination", "Error"} <= KNOWN_FRAME_TYPES


def test_speech_started_is_tolerated_and_written_down() -> None:
    """Undocumented, pro-only, 22 occurrences in the matrix — known, not parsed."""
    assert "SpeechStarted" in KNOWN_FRAME_TYPES
    assert unknown_frame_types([{"type": "SpeechStarted"}]) == frozenset()


def test_a_genuinely_new_type_is_still_flagged() -> None:
    """Tolerating one undocumented type must not tolerate the next one."""
    stream: list[Mapping[str, JsonValue]] = [
        {"type": "Begin"},
        {"type": "Turn"},
        {"type": "SpeechStarted"},
        {"type": "SentimentDetected"},
        {"type": "Termination"},
    ]
    assert unknown_frame_types(stream) == frozenset({"SentimentDetected"})


def test_several_new_types_are_all_reported() -> None:
    stream: list[Mapping[str, JsonValue]] = [
        {"type": "Alpha"},
        {"type": "Beta"},
        {"type": "Alpha"},
    ]
    assert unknown_frame_types(stream) == frozenset({"Alpha", "Beta"})


def test_a_frame_without_a_type_is_not_a_new_type() -> None:
    assert unknown_frame_types([{"transcript": "hello"}]) == frozenset()


def test_the_committed_fixture_carries_no_unknown_type() -> None:
    """If a future re-capture introduces one, this is where it is noticed."""
    assert unknown_frame_types(_frames_of(FIXTURE)) == frozenset()
