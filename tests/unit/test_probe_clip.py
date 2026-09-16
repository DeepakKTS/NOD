"""The probe stimulus is byte-reproducible and its gaps are exactly as claimed.

If the clip drifts between runs, every boundary measurement taken against it is
comparing two different stimuli, and the capability verdicts become noise. This
is the same determinism contract BENCH_SPEC.md §2 puts on the Track A generator.
"""

from __future__ import annotations

import math
import struct
from pathlib import Path

import pytest

from nod_bench.probe_clip import (
    SAMPLE_RATE,
    TONE_LOW,
    TONE_MID,
    ZEROS,
    ClipLayout,
    build_clip,
    clip_sha256,
    fill,
    read_wav,
    room_tone,
    silence,
    write_wav,
)

SEED_MS = 15_000
SEED_PCM = struct.pack(
    f"<{SEED_MS * SAMPLE_RATE // 1000}h",
    *[(i * 977) % 8000 - 4000 for i in range(SEED_MS * SAMPLE_RATE // 1000)],
)


def _rms_dbfs(pcm: bytes) -> float:
    values = struct.unpack(f"<{len(pcm) // 2}h", pcm)
    rms = math.sqrt(sum(v * v for v in values) / len(values))
    return 20 * math.log10(rms / 32767)


def test_same_seed_gives_byte_identical_audio() -> None:
    layout = ClipLayout(gap_ms=1500)
    a = build_clip(SEED_PCM, layout=layout, seed=7)
    b = build_clip(SEED_PCM, layout=layout, seed=7)
    assert a == b
    assert clip_sha256(a) == clip_sha256(b)


def test_a_different_seed_gives_different_fill() -> None:
    layout = ClipLayout(gap_ms=1500)
    assert build_clip(SEED_PCM, layout=layout, seed=7) != build_clip(
        SEED_PCM, layout=layout, seed=8
    )


def test_clip_length_matches_the_declared_layout() -> None:
    layout = ClipLayout(gap_ms=1500)
    pcm = build_clip(SEED_PCM, layout=layout)
    expected_samples = layout.total_ms * SAMPLE_RATE // 1000
    assert len(pcm) == expected_samples * 2


@pytest.mark.parametrize("gap_ms", [300, 1500, 2500])
def test_the_gap_sits_exactly_where_the_layout_says(gap_ms: int) -> None:
    """The inserted gap is where the sidecar claims, to the sample."""
    layout = ClipLayout(gap_ms=gap_ms, gap_fill=ZEROS)
    pcm = build_clip(SEED_PCM, layout=layout)

    start = layout.gap_start_ms * SAMPLE_RATE // 1000 * 2
    end = layout.gap_end_ms * SAMPLE_RATE // 1000 * 2
    assert pcm[start:end] == b"\x00" * (end - start)
    # And the sample immediately before the gap is speech, not fill.
    assert pcm[start - 2 : start] != b"\x00\x00"


def test_gap_start_and_end_are_derived_not_guessed() -> None:
    layout = ClipLayout(gap_ms=1500)
    assert layout.gap_start_ms == 2000 + 300 + 2000
    assert layout.gap_end_ms == layout.gap_start_ms + 1500


@pytest.mark.parametrize(("kind", "dbfs"), [(TONE_LOW, -50.0), (TONE_MID, -35.0)])
def test_room_tone_hits_its_target_level(kind: str, dbfs: float) -> None:
    """The vad_threshold arm depends on the fill level being what it claims."""
    tone = fill(kind, 1000, seed=3)
    assert abs(_rms_dbfs(tone) - dbfs) < 1.5


def test_room_tone_is_not_digital_silence() -> None:
    """A VAD may special-case an exactly-zero buffer, which would hide the knob."""
    assert set(fill(TONE_MID, 200, seed=3)) != {0}


def test_zeros_fill_is_exactly_silent() -> None:
    assert set(fill(ZEROS, 200, seed=3)) == {0}


def test_unknown_fill_is_rejected_loudly() -> None:
    with pytest.raises(ValueError, match="unknown gap fill"):
        fill("hiss", 100, seed=1)


def test_room_tone_clamps_to_int16_range() -> None:
    loud = room_tone(100, dbfs=0.0, seed=1)
    values = struct.unpack(f"<{len(loud) // 2}h", loud)
    assert max(values) <= 32767
    assert min(values) >= -32767


def test_silence_has_the_exact_requested_duration() -> None:
    assert len(silence(250)) == 250 * SAMPLE_RATE // 1000 * 2


def test_a_seed_recording_that_is_too_short_fails_loudly() -> None:
    with pytest.raises(ValueError, match="seed recording is"):
        build_clip(silence(1000), layout=ClipLayout(gap_ms=1500))


def test_wav_round_trip(tmp_path: Path) -> None:
    path = tmp_path / "clip.wav"
    pcm = build_clip(SEED_PCM, layout=ClipLayout(gap_ms=1500))
    write_wav(path, pcm)
    read_back, rate = read_wav(path)
    assert read_back == pcm
    assert rate == SAMPLE_RATE


def test_stereo_seed_is_rejected(tmp_path: Path) -> None:
    import wave

    path = tmp_path / "stereo.wav"
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(2)
        handle.setsampwidth(2)
        handle.setframerate(SAMPLE_RATE)
        handle.writeframes(b"\x00" * 400)
    with pytest.raises(ValueError, match="must be mono 16-bit"):
        read_wav(path)
