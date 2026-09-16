"""Deterministic probe stimulus: speech segments spliced with exact-length fills.

The capability probe needs silences of *known* length, which no natural recording
provides. So the clip is assembled: real speech taken at fixed offsets from an
owner-recorded seed, separated by gaps this module generates to the millisecond.

The gaps are room tone rather than digital zeros by default. A voice activity
detector may special-case an exactly-zero buffer, and `vad_threshold` is only
measurable against a fill whose level sits between the two arms' decision points
— against pure silence both arms agree and the knob looks inert when it is not.

Everything here is seeded and byte-reproducible, which is the same contract
BENCH_SPEC.md §2 puts on the Track A generator at P2.
"""

from __future__ import annotations

import hashlib
import math
import random
import struct
import sys
import wave
from array import array
from dataclasses import dataclass
from pathlib import Path
from typing import Final

SAMPLE_RATE: Final = 16000
"""Samples per second. Matches the probe's connect parameter."""

BYTES_PER_SAMPLE: Final = 2
"""PCM16."""

FULL_SCALE: Final = 32767
"""Peak int16 amplitude."""

PREAMBLE_MS: Final = 2000
"""Speech before anything is probed, so the session is known healthy."""

WARM_GAP_MS: Final = 300
"""A gap short enough that no arm should ever end a turn on it."""

LEAD_MS: Final = 2000
"""Speech carrying the mid-stream update. The update lands inside this."""

TRAIL_MS: Final = 2000
"""Speech after the test gap, so a missed boundary is visibly a missed boundary."""

TAIL_MS: Final = 2500
"""Trailing fill, so the session is not cut off mid-decision."""

UPDATE_AT_MS: Final = 3000
"""Stream position of the `UpdateConfiguration`, inside the lead segment.

At least 1300 ms before the gap opens, so an update that took effect had ample
time to do so and a null result cannot be blamed on the frame arriving late.
"""

TONE_LOW_DBFS: Final = -50.0
"""Quiet room tone: audibly not silence, below any plausible VAD threshold."""

TONE_MID_DBFS: Final = -35.0
"""The `vad_threshold` stimulus: loud enough that the threshold decides."""

ZEROS: Final = "zeros"
TONE_LOW: Final = "tone_low"
TONE_MID: Final = "tone_mid"


@dataclass(frozen=True, slots=True)
class ClipLayout:
    """One probe clip's timing, in milliseconds."""

    gap_ms: int
    gap_fill: str = TONE_LOW
    preamble_ms: int = PREAMBLE_MS
    warm_gap_ms: int = WARM_GAP_MS
    lead_ms: int = LEAD_MS
    trail_ms: int = TRAIL_MS
    tail_ms: int = TAIL_MS

    @property
    def gap_start_ms(self) -> int:
        """Stream position at which the test gap opens. `O(1)`."""
        return self.preamble_ms + self.warm_gap_ms + self.lead_ms

    @property
    def gap_end_ms(self) -> int:
        """Stream position at which the test gap closes. `O(1)`."""
        return self.gap_start_ms + self.gap_ms

    @property
    def total_ms(self) -> int:
        """Whole clip duration. `O(1)`."""
        return self.gap_end_ms + self.trail_ms + self.tail_ms


def _samples(duration_ms: int, sample_rate: int) -> int:
    return duration_ms * sample_rate // 1000


def silence(duration_ms: int, *, sample_rate: int = SAMPLE_RATE) -> bytes:
    """Return exactly `duration_ms` of digital silence. `O(n)`."""
    return b"\x00" * (_samples(duration_ms, sample_rate) * BYTES_PER_SAMPLE)


def room_tone(
    duration_ms: int,
    *,
    dbfs: float,
    sample_rate: int = SAMPLE_RATE,
    seed: int,
) -> bytes:
    """Generate reproducible room tone at a known level. `O(n)`.

    Args:
        duration_ms: Exact length in milliseconds.
        dbfs: Target RMS relative to full scale, e.g. `-35.0`.
        sample_rate: Samples per second.
        seed: PRNG seed; the same seed always yields identical bytes.

    Returns:
        PCM16 little-endian mono bytes.
    """
    count = _samples(duration_ms, sample_rate)
    sigma = FULL_SCALE * math.pow(10.0, dbfs / 20.0)
    rng = random.Random(seed)  # noqa: S311 - stimulus generation, not cryptography
    values = [
        max(-FULL_SCALE, min(FULL_SCALE, int(rng.gauss(0.0, sigma))))
        for _ in range(count)
    ]
    return struct.pack(f"<{count}h", *values)


def fill(
    kind: str, duration_ms: int, *, sample_rate: int = SAMPLE_RATE, seed: int
) -> bytes:
    """Produce one gap fill by name. `O(n)`.

    Args:
        kind: `zeros`, `tone_low` or `tone_mid`.
        duration_ms: Exact length in milliseconds.
        sample_rate: Samples per second.
        seed: PRNG seed for the tone variants.

    Returns:
        PCM16 bytes.

    Raises:
        ValueError: `kind` is not a known fill.
    """
    if kind == ZEROS:
        return silence(duration_ms, sample_rate=sample_rate)
    if kind == TONE_LOW:
        return room_tone(
            duration_ms, dbfs=TONE_LOW_DBFS, sample_rate=sample_rate, seed=seed
        )
    if kind == TONE_MID:
        return room_tone(
            duration_ms, dbfs=TONE_MID_DBFS, sample_rate=sample_rate, seed=seed
        )
    msg = f"unknown gap fill {kind!r}; expected one of zeros, tone_low, tone_mid"
    raise ValueError(msg)


class SeedFormatError(ValueError):
    """The seed recording is not something we can read or convert.

    Carries what the file actually is alongside what it needs to be, plus the
    command that fixes it. "Rejected" on its own sends the reader back to a
    recorder UI to guess which of three properties was wrong.
    """

    def __init__(self, path: Path, actual: str, reason: str) -> None:
        """Explain the rejection in terms of the file in front of the user.

        Args:
            path: The offending file.
            actual: What the file is, in words.
            reason: Why that cannot be converted here.
        """
        self.path = path
        self.actual = actual
        super().__init__(
            f"{path} is {actual}.\n"
            f"The probe needs mono {SAMPLE_RATE} Hz 16-bit PCM WAV.\n"
            f"{reason}\n"
            f"Convert it with:\n"
            f"  ffmpeg -i {path} -ac 1 -ar {SAMPLE_RATE} -sample_fmt s16 "
            f"{path.with_name(path.stem + '-16k.wav')}"
        )


def describe_wav(path: Path) -> str:
    """Describe a wav's actual format in words. `O(1)`.

    Args:
        path: The file to inspect.

    Returns:
        A phrase such as `2 channels, 44100 Hz, 24-bit PCM`.
    """
    try:
        with wave.open(str(path), "rb") as handle:
            channels = handle.getnchannels()
            width = handle.getsampwidth()
            rate = handle.getframerate()
            comp = handle.getcomptype()
    except wave.Error as exc:
        return f"not a PCM wav Python can read ({exc})"
    plural = "channel" if channels == 1 else "channels"
    codec = "PCM" if comp == "NONE" else comp
    return f"{channels} {plural}, {rate} Hz, {width * 8}-bit {codec}"


def _to_int16(frames: bytes, width: int) -> array[int]:
    """Widen or narrow integer PCM to signed 16-bit. `O(n)`.

    Args:
        frames: Raw interleaved sample bytes.
        width: Bytes per sample in the source.

    Returns:
        Signed 16-bit samples, still interleaved.

    Raises:
        ValueError: `width` is not a supported integer PCM width.
    """
    if width == BYTES_PER_SAMPLE:
        out = array("h")
        out.frombytes(frames)
    elif width == 1:
        # 8-bit wav is unsigned with 128 as silence.
        out = array("h", ((b - 128) * 256 for b in frames))
    elif width == 3:
        out = array(
            "h",
            (
                int.from_bytes(frames[i : i + 3], "little", signed=True) >> 8
                for i in range(0, len(frames) - 2, 3)
            ),
        )
    elif width == 4:
        wide = array("i")
        wide.frombytes(frames)
        if sys.byteorder == "big":  # pragma: no cover - CI and dev are LE
            wide.byteswap()
        out = array("h", (v >> 16 for v in wide))
    else:
        msg = f"unsupported sample width {width}"
        raise ValueError(msg)
    if sys.byteorder == "big" and width == BYTES_PER_SAMPLE:  # pragma: no cover
        out.byteswap()
    return out


def _to_mono(samples: array[int], channels: int) -> array[int]:
    """Average interleaved channels down to one. `O(n)`."""
    if channels == 1:
        return samples
    return array(
        "h",
        (
            sum(samples[i : i + channels]) // channels
            for i in range(0, len(samples) - channels + 1, channels)
        ),
    )


def _resample(samples: array[int], source_rate: int, target_rate: int) -> array[int]:
    """Linearly resample mono PCM16. `O(n)`.

    Linear interpolation is enough here: the probe measures where turn
    boundaries land, and every silence it measures against is generated at the
    target rate rather than resampled.
    """
    if source_rate == target_rate or not samples:
        return samples
    count = len(samples) * target_rate // source_rate
    ratio = source_rate / target_rate
    out = array("h", bytes(count * BYTES_PER_SAMPLE))
    last = len(samples) - 1
    for i in range(count):
        position = i * ratio
        left = int(position)
        if left >= last:
            out[i] = samples[last]
            continue
        frac = position - left
        out[i] = int(samples[left] * (1 - frac) + samples[left + 1] * frac)
    return out


def load_seed(path: Path, *, sample_rate: int = SAMPLE_RATE) -> tuple[bytes, str]:
    """Read a seed recording, converting it to mono PCM16 if it is not already.

    `O(n)` in the recording length.

    Args:
        path: The seed recording.
        sample_rate: Target samples per second.

    Returns:
        Mono PCM16 bytes at `sample_rate`, and a note describing any conversion.

    Raises:
        SeedFormatError: The file cannot be read or converted.
    """
    try:
        with wave.open(str(path), "rb") as handle:
            channels = handle.getnchannels()
            width = handle.getsampwidth()
            rate = handle.getframerate()
            comptype = handle.getcomptype()
            frames = handle.readframes(handle.getnframes())
    except (wave.Error, EOFError) as exc:
        raise SeedFormatError(
            path,
            describe_wav(path),
            f"Python's wave module cannot read it ({exc}); float and compressed "
            f"wavs have to be converted first.",
        ) from exc

    if comptype != "NONE":
        raise SeedFormatError(
            path, describe_wav(path), f"{comptype} is compressed, not raw PCM."
        )

    if (channels, width, rate) == (1, BYTES_PER_SAMPLE, sample_rate):
        return frames, "already mono 16-bit PCM at the target rate"

    try:
        samples = _to_int16(frames, width)
    except ValueError as exc:
        raise SeedFormatError(path, describe_wav(path), str(exc)) from exc

    steps: list[str] = []
    if channels != 1:
        samples = _to_mono(samples, channels)
        steps.append(f"downmixed {channels} channels to mono")
    if width != BYTES_PER_SAMPLE:
        steps.append(f"converted {width * 8}-bit to 16-bit")
    if rate != sample_rate:
        samples = _resample(samples, rate, sample_rate)
        steps.append(f"resampled {rate} Hz to {sample_rate} Hz")

    return samples.tobytes(), "; ".join(steps)


def read_wav(path: Path) -> tuple[bytes, int]:
    """Read a mono PCM16 wav verbatim, with no conversion. `O(n)`.

    Stdlib `wave` rather than `soundfile`, because a 16-bit mono read needs no
    more than that and the probe should not depend on the bench audio extra.
    Use `load_seed` for anything that may need converting.

    Args:
        path: The wav to read.

    Returns:
        Raw PCM bytes and the sample rate.

    Raises:
        SeedFormatError: The file is not mono 16-bit.
    """
    with wave.open(str(path), "rb") as handle:
        if handle.getnchannels() != 1 or handle.getsampwidth() != BYTES_PER_SAMPLE:
            raise SeedFormatError(
                path, describe_wav(path), "read_wav does not convert."
            )
        return handle.readframes(handle.getnframes()), handle.getframerate()


def write_wav(path: Path, pcm: bytes, *, sample_rate: int = SAMPLE_RATE) -> None:
    """Write mono PCM16 to a wav file. `O(n)`."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as handle:
        handle.setnchannels(1)
        handle.setsampwidth(BYTES_PER_SAMPLE)
        handle.setframerate(sample_rate)
        handle.writeframes(pcm)


def _slice(pcm: bytes, start_ms: int, duration_ms: int, sample_rate: int) -> bytes:
    start = _samples(start_ms, sample_rate) * BYTES_PER_SAMPLE
    end = start + _samples(duration_ms, sample_rate) * BYTES_PER_SAMPLE
    chunk = pcm[start:end]
    return chunk.ljust(end - start, b"\x00")


def build_clip(
    seed_pcm: bytes,
    *,
    layout: ClipLayout,
    sample_rate: int = SAMPLE_RATE,
    seed: int = 7,
) -> bytes:
    """Assemble one probe clip. `O(n)`.

    Speech is taken from three fixed offsets in the seed recording, so every
    clip in a run shares the same words and differs only in the gap under test.

    Args:
        seed_pcm: The owner-recorded seed, mono PCM16.
        layout: Timing and gap fill.
        sample_rate: Samples per second.
        seed: PRNG seed for the generated fills.

    Returns:
        The clip as PCM16 bytes.

    Raises:
        ValueError: The seed recording is too short for the layout.
    """
    needed_ms = layout.preamble_ms + layout.lead_ms + layout.trail_ms
    have_ms = len(seed_pcm) // BYTES_PER_SAMPLE * 1000 // sample_rate
    if have_ms < needed_ms:
        msg = (
            f"seed recording is {have_ms} ms; the layout needs {needed_ms} ms "
            f"of speech (preamble + lead + trail)"
        )
        raise ValueError(msg)

    preamble = _slice(seed_pcm, 0, layout.preamble_ms, sample_rate)
    lead = _slice(seed_pcm, layout.preamble_ms, layout.lead_ms, sample_rate)
    trail = _slice(
        seed_pcm, layout.preamble_ms + layout.lead_ms, layout.trail_ms, sample_rate
    )

    return b"".join(
        (
            preamble,
            fill(TONE_LOW, layout.warm_gap_ms, sample_rate=sample_rate, seed=seed),
            lead,
            fill(
                layout.gap_fill, layout.gap_ms, sample_rate=sample_rate, seed=seed + 1
            ),
            trail,
            fill(TONE_LOW, layout.tail_ms, sample_rate=sample_rate, seed=seed + 2),
        )
    )


def clip_sha256(pcm: bytes) -> str:
    """Hash a clip so a run manifest can prove which audio it used. `O(n)`."""
    return hashlib.sha256(pcm).hexdigest()
