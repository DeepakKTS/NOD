"""Synthetic seed generation, and the labelling that keeps it honest.

The seed is a stopgap. These tests care less about the audio than about the
caveat travelling with it: a reader who sees the manifest, the probe banner or
the report must not be able to mistake TTS for a human recording.
"""

from __future__ import annotations

import json
import shutil
import wave
from pathlib import Path

import pytest

from nod_bench.seed import (
    MANIFEST_SUFFIX,
    MIN_SEGMENT_MS,
    PROBE_SCRIPT,
    SCRIPT_SEGMENTS,
    VOICE_CANDIDATES,
    SayUnavailableError,
    SeedManifest,
    SeedSegment,
    choose_voice,
    manifest_for,
    synthesize_seed,
)

needs_say = pytest.mark.say
"""Marks a test that shells out to macOS `say`. **Deselected by default.**

Not a skipif on `shutil.which("say")`, which was the previous guard and did not
help: on a Mac `say` is present, so these ran in the default suite, and one of
them timed out after 120 s mid-`make check`. A 120-second subprocess in the
default gate is a non-deterministic gate, and a flaky gate is worse than a slow
one — it teaches you to re-run rather than to read, which is the habit that lets
a genuine failure through.

They still matter: they are the only coverage of seed synthesis and of the
caveat that keeps synthetic audio labelled. Run them deliberately with
`make seed-tests`, or `pytest -m say`.
"""


def test_the_seed_carries_both_regimes() -> None:
    """Each silence knob only binds in one regime, so the seed must offer both.

    After a complete utterance the semantic gate fires and `min_turn_silence`
    decides. After a fragment the gate keeps waiting and `max_turn_silence` is
    the only thing that ends the turn. A seed with only complete sentences
    measures `min` and reports `max` as inert; only fragments does the reverse.
    """
    complete = [t for t in SCRIPT_SEGMENTS if t.rstrip().endswith(".")]
    fragments = [t for t in SCRIPT_SEGMENTS if not t.rstrip().endswith(".")]
    assert len(complete) >= 3, "need preamble, complete lead, and trail"
    assert len(fragments) >= 1, "need a fragment lead for the acoustic regime"


def test_the_first_three_segments_are_complete_sentences() -> None:
    """Segments 0-2 are the complete-utterance regime."""
    for text in SCRIPT_SEGMENTS[:3]:
        assert text.rstrip().endswith("."), f"segment ends on a fragment: {text!r}"


def test_the_pii_sits_in_the_segments_that_reach_the_audio() -> None:
    """Segments 1 and 2 flank the gap; segment 0 is the preamble.

    In the first run both sat past the six-second mark the clip actually used
    and were never spoken, so INV-6 went untested on live output.
    """
    assert "617 555 0142" in SCRIPT_SEGMENTS[1]
    assert "March the fourth" in SCRIPT_SEGMENTS[2]
    assert "617 555 0142" in PROBE_SCRIPT


def test_voice_choice_prefers_the_candidates_then_falls_back() -> None:
    assert choose_voice(["Daniel", "Samantha"]) == "Samantha"
    assert choose_voice(["Daniel"]) == "Daniel"
    assert choose_voice(["Zarvox"]) == "system-default"
    assert choose_voice([]) == "system-default"
    assert VOICE_CANDIDATES[0] == "Samantha"


def test_missing_say_refuses_rather_than_falling_back_to_a_tone(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A tone is not speech; it would look like a successful run and mean nothing."""
    monkeypatch.setattr(shutil, "which", lambda _: None)
    with pytest.raises(SayUnavailableError) as caught:
        synthesize_seed(tmp_path / "seed.wav")

    message = str(caught.value)
    assert "macOS" in message
    assert "tone" in message
    assert "Record roughly 15 seconds" in message


@needs_say
def test_generated_seed_is_mono_16k_16bit(tmp_path: Path) -> None:
    path = tmp_path / "seed.wav"
    manifest = synthesize_seed(path)

    with wave.open(str(path), "rb") as handle:
        assert handle.getnchannels() == 1
        assert handle.getframerate() == 16000
        assert handle.getsampwidth() == 2
        assert handle.getcomptype() == "NONE"

    assert min(x.duration_ms for x in manifest.segments) >= MIN_SEGMENT_MS
    assert manifest.sample_rate == 16000
    assert manifest.channels == 1


@needs_say
def test_the_manifest_records_what_reproduces_it(tmp_path: Path) -> None:
    path = tmp_path / "seed.wav"
    manifest = synthesize_seed(path)

    sidecar = path.with_name(path.name + MANIFEST_SUFFIX)
    assert sidecar.is_file()
    written = json.loads(sidecar.read_text())

    assert written["synthesized"] is True
    assert written["engine"] == "macos-say"
    assert written["voice"]
    assert written["rate_wpm"] == manifest.rate_wpm
    assert written["script"] == PROBE_SCRIPT
    assert len(written["sha256"]) == 64
    assert len(written["segments"]) == len(SCRIPT_SEGMENTS)
    # Boundaries are exact and ordered, so the clip builder never guesses.
    spans = written["segments"]
    assert all(x["end_ms"] > x["start_ms"] for x in spans)
    assert all(
        spans[i]["end_ms"] <= spans[i + 1]["start_ms"] for i in range(len(spans) - 1)
    )


@needs_say
def test_the_caveat_is_unmissable_and_names_what_it_is_unfit_for(
    tmp_path: Path,
) -> None:
    manifest = synthesize_seed(tmp_path / "seed.wav")
    assert "SYNTHESIZED SPEECH" in manifest.caveat
    assert "NOT A HUMAN RECORDING" in manifest.caveat
    for unfit in ("Track C", "disfluency", "cut detection", "published number"):
        assert unfit in manifest.caveat


@needs_say
def test_the_same_voice_and_rate_reproduce_the_same_audio(tmp_path: Path) -> None:
    a = synthesize_seed(tmp_path / "a.wav", voice="Samantha", rate_wpm=160)
    b = synthesize_seed(tmp_path / "b.wav", voice="Samantha", rate_wpm=160)
    assert a.sha256 == b.sha256


def test_a_recording_with_no_sidecar_reads_as_unlabelled(tmp_path: Path) -> None:
    """Which is what a human recording looks like: no manifest, no caveat."""
    path = tmp_path / "human.wav"
    path.write_bytes(b"")
    assert manifest_for(path) is None


def test_a_corrupt_sidecar_is_ignored_rather_than_crashing(tmp_path: Path) -> None:
    path = tmp_path / "seed.wav"
    path.with_name(path.name + MANIFEST_SUFFIX).write_text("{not json")
    assert manifest_for(path) is None


def test_manifest_round_trips(tmp_path: Path) -> None:
    path = tmp_path / "seed.wav"
    original = SeedManifest(
        synthesized=True,
        caveat="c",
        engine="macos-say",
        voice="Samantha",
        rate_wpm=160,
        script="s",
        sha256="x" * 64,
        sample_rate=16000,
        channels=1,
        duration_ms=15000,
        generated_at="2026-09-16T00:00:00+00:00",
        conversion="",
        segments=(SeedSegment(index=0, text="s", start_ms=0, end_ms=1500),),
    )
    original.write(path)
    restored = manifest_for(path)
    assert restored == original
    assert restored is not None
    assert restored.segment_durations_ms == (1500,)
