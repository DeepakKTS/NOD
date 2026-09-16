"""The seed loader converts what it can and explains what it cannot.

A rejection that only says "wrong format" sends the reader back to a recorder UI
to guess which of three properties was wrong. These tests pin the message to the
file in front of the user.
"""

from __future__ import annotations

import struct
import wave
from array import array
from pathlib import Path

import pytest

from nod_bench.probe_clip import (
    SAMPLE_RATE,
    SeedFormatError,
    describe_wav,
    load_seed,
    read_wav,
)


def _write(path: Path, *, channels: int, width: int, rate: int, ms: int = 1000) -> Path:
    frames = rate * ms // 1000
    if width == 2:
        mono = array("h", (int(8000 * ((i % 100) / 50 - 1)) for i in range(frames)))
        raw = b"".join(mono[i : i + 1].tobytes() * channels for i in range(frames))
    elif width == 1:
        raw = bytes((128 + (i % 60) - 30) for i in range(frames * channels))
    elif width == 4:
        wide = array("i", (int(5e8 * ((i % 100) / 50 - 1)) for i in range(frames)))
        raw = b"".join(wide[i : i + 1].tobytes() * channels for i in range(frames))
    else:  # 24-bit
        raw = b"".join(
            int(2_000_000 * ((i % 100) / 50 - 1)).to_bytes(3, "little", signed=True)
            * channels
            for i in range(frames)
        )
    with wave.open(str(path), "wb") as h:
        h.setnchannels(channels)
        h.setsampwidth(width)
        h.setframerate(rate)
        h.writeframes(raw)
    return path


def test_an_already_correct_seed_is_passed_through(tmp_path: Path) -> None:
    path = _write(tmp_path / "ok.wav", channels=1, width=2, rate=SAMPLE_RATE)
    pcm, note = load_seed(path)
    assert pcm == read_wav(path)[0]
    assert "already" in note


def test_stereo_is_downmixed(tmp_path: Path) -> None:
    path = _write(tmp_path / "stereo.wav", channels=2, width=2, rate=SAMPLE_RATE)
    pcm, note = load_seed(path)
    assert "downmixed 2 channels to mono" in note
    assert len(pcm) == SAMPLE_RATE * 2  # 1 s of mono PCM16


@pytest.mark.parametrize("rate", [8000, 44100, 48000])
def test_any_rate_is_resampled_to_the_target(tmp_path: Path, rate: int) -> None:
    path = _write(tmp_path / f"r{rate}.wav", channels=1, width=2, rate=rate)
    pcm, note = load_seed(path)
    assert f"resampled {rate} Hz to {SAMPLE_RATE} Hz" in note
    assert abs(len(pcm) // 2 - SAMPLE_RATE) <= 2  # 1 s at the target rate


@pytest.mark.parametrize("width", [1, 3, 4])
def test_other_integer_widths_are_converted_to_16_bit(
    tmp_path: Path, width: int
) -> None:
    path = _write(tmp_path / f"w{width}.wav", channels=1, width=width, rate=SAMPLE_RATE)
    pcm, note = load_seed(path)
    assert f"converted {width * 8}-bit to 16-bit" in note
    assert len(pcm) == SAMPLE_RATE * 2
    # The signal survived: it is not all zeros.
    assert set(pcm) != {0}


def test_the_hard_case_converts_in_one_pass(tmp_path: Path) -> None:
    """What a phone or a Mac recorder actually produces."""
    path = _write(tmp_path / "real.wav", channels=2, width=3, rate=48000, ms=2000)
    pcm, note = load_seed(path)
    assert "downmixed" in note and "24-bit" in note and "48000 Hz" in note
    assert abs(len(pcm) // 2 - SAMPLE_RATE * 2) <= 4


def test_describe_wav_states_channels_rate_and_width(tmp_path: Path) -> None:
    path = _write(tmp_path / "d.wav", channels=2, width=3, rate=44100)
    assert describe_wav(path) == "2 channels, 44100 Hz, 24-bit PCM"
    mono = _write(tmp_path / "m.wav", channels=1, width=2, rate=16000)
    assert describe_wav(mono) == "1 channel, 16000 Hz, 16-bit PCM"


def test_a_file_wave_cannot_read_says_what_it_is_and_how_to_fix_it(
    tmp_path: Path,
) -> None:
    """Float wavs are common from Mac recorders and `wave` refuses them outright."""
    path = tmp_path / "float32.wav"
    # A WAVE_FORMAT_IEEE_FLOAT header, which the wave module rejects.
    data = struct.pack("<f", 0.0) * 100
    body = (
        b"WAVEfmt "
        + struct.pack("<IHHIIHH", 16, 3, 1, 48000, 48000 * 4, 4, 32)
        + b"data"
        + struct.pack("<I", len(data))
        + data
    )
    path.write_bytes(b"RIFF" + struct.pack("<I", len(body) + 4) + body)

    with pytest.raises(SeedFormatError) as caught:
        load_seed(path)

    message = str(caught.value)
    assert "float32.wav" in message
    assert f"mono {SAMPLE_RATE} Hz 16-bit PCM" in message
    assert "ffmpeg -i" in message
    assert f"-ar {SAMPLE_RATE}" in message
    assert "-ac 1" in message
    assert "-sample_fmt s16" in message


def test_a_non_wav_file_is_rejected_with_the_same_guidance(tmp_path: Path) -> None:
    path = tmp_path / "notaudio.wav"
    path.write_text("this is not a wav at all")
    with pytest.raises(SeedFormatError, match="ffmpeg"):
        load_seed(path)


def test_read_wav_still_refuses_to_convert(tmp_path: Path) -> None:
    """`read_wav` is the verbatim reader; conversion is `load_seed`'s job."""
    path = _write(tmp_path / "stereo.wav", channels=2, width=2, rate=SAMPLE_RATE)
    with pytest.raises(SeedFormatError, match="does not convert"):
        read_wav(path)
