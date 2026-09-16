"""Synthesize a probe seed recording with the macOS `say` command.

A stopgap, and labelled as one everywhere it surfaces. Synthetic speech is
adequate for the question P1 asks — does the turn boundary move when a knob
moves — because that is categorical and does not depend on the speaker being
human. It is **not** adequate for anything downstream that measures behaviour on
real speech: Track C, the disfluency features of CONTROL_SPEC.md §2.3, cut
detection, or any published number. TTS has none of the hesitation, repair or
uneven pacing the controller exists to accommodate.

Every artefact this module writes says so: the manifest carries
`synthesized: true`, and the probe report prints a warning built from it.

There is deliberately no fallback to a generated tone when `say` is missing. A
tone is not speech; it would produce turn boundaries that mean nothing while
looking exactly like a successful run.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import tempfile
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Final

from nod_bench.probe_clip import BYTES_PER_SAMPLE, SAMPLE_RATE, load_seed, write_wav

SCRIPT_SEGMENTS: Final = (
    "This is a test recording for the Nod capability probe. "
    "I am reading at a normal pace.",
    "You can call me back on 617 555 0142.",
    "My appointment was on March the fourth. Thank you very much.",
    "The session should produce several clean turn boundaries "
    "and the next thing I wanted to mention is",
)
"""The three speech segments, synthesized separately so their edges are exact.

Two properties are load-bearing and were learned the expensive way.

**Segments 0 to 2 end on a complete sentence; segment 3 is a deliberate
fragment.** The two are different regimes and each silence knob only binds in
one of them. After a complete utterance the semantic gate fires and
`min_turn_silence` decides; after a fragment the gate keeps waiting and
`max_turn_silence` is the only thing that ends the turn. A probe that offers
only one of the two can measure only one of the knobs, and will report the other
as inert.

**A complete pre-gap segment is what gives the confidence field a reason to
rise.** The segment before the test gap
is what a semantic endpointer is judging, and it is *designed* to keep waiting
after a fragment. The first live run ended that segment on "the session should",
so `end_of_turn_confidence` never rose above 0.182 and the confidence arm
returned a null that looked like an unusable field rather than a stimulus which
never gave the field a reason to act.

**The phone number and the date sit in segments 1 and 2**, the ones that reach
the audio either side of the gap, so the INV-6 redactor is exercised on live
transcript output rather than on a fixture. In the first run they sat past the
six-second mark the clip actually used, and were never spoken.
"""

PROBE_SCRIPT: Final = " ".join(SCRIPT_SEGMENTS)
"""The whole script, for the manifest."""

SEGMENT_GAP_MS: Final = 200
"""Silence between segments in the archived seed. Milliseconds.

Only separates them in the seed file; the clip builder uses the segments
individually and inserts its own gaps.
"""

VOICE_CANDIDATES: Final = ("Samantha", "Alex", "Karen", "Daniel", "Fred")
"""Preferred voices, in order. Falls back to the system default if none exist."""

RATE_WPM: Final = 160
"""Words per minute. The script is ~45 words, so this lands near 15 seconds."""

MIN_SEGMENT_MS: Final = 1200
"""Shortest usable segment. Milliseconds.

Below roughly this the upstream has too little speech to establish a turn at all,
and a boundary measured against it says more about the stimulus than the knob.
"""

SAY_DATA_FORMAT: Final = f"LEI16@{SAMPLE_RATE}"
"""Little-endian signed 16-bit at the probe's rate, so no conversion is needed."""

MANIFEST_SUFFIX: Final = ".manifest.json"


class SayUnavailableError(RuntimeError):
    """The `say` command is not available, so no seed can be synthesized."""

    def __init__(self) -> None:
        """Explain the two ways forward, neither of which is a tone."""
        super().__init__(
            "macOS `say` was not found, so a synthetic seed cannot be generated.\n"
            "This is only available on macOS.\n"
            "Record roughly 15 seconds of speech instead and save it as a wav:\n"
            "  a few short complete sentences, plus one line containing a phone "
            "number and a date.\n"
            "No tone fallback is offered on purpose: a tone is not speech and "
            "would produce turn boundaries that mean nothing."
        )


class SynthesisError(RuntimeError):
    """`say` ran but did not produce usable audio."""


@dataclass(frozen=True, slots=True)
class SeedSegment:
    """One speech segment and where it sits in the seed file."""

    index: int
    text: str
    start_ms: int
    end_ms: int

    @property
    def duration_ms(self) -> int:
        """How long this segment speaks for. `O(1)`."""
        return self.end_ms - self.start_ms


@dataclass(frozen=True, slots=True)
class SeedManifest:
    """How a seed recording was produced, so it can be reproduced or distrusted.

    `synthesized` is first and is never omitted. A reader who sees only the hash
    should still be unable to mistake this for a human recording.
    """

    synthesized: bool
    caveat: str
    engine: str
    voice: str
    rate_wpm: int
    script: str
    sha256: str
    sample_rate: int
    channels: int
    duration_ms: int
    generated_at: str
    conversion: str
    segments: tuple[SeedSegment, ...] = ()
    """Exact segment boundaries, so the clip builder never guesses at offsets."""

    def write(self, seed_path: Path) -> Path:
        """Write the manifest beside its wav. `O(1)`.

        Args:
            seed_path: The wav this manifest describes.

        Returns:
            The manifest path.
        """
        path = seed_path.with_name(seed_path.name + MANIFEST_SUFFIX)
        path.write_text(json.dumps(asdict(self), indent=2) + "\n", encoding="utf-8")
        return path

    @property
    def segment_durations_ms(self) -> tuple[int, ...]:
        """Each segment's speaking length. `O(n)`."""
        return tuple(seg.duration_ms for seg in self.segments)


SYNTHETIC_CAVEAT: Final = (
    "SYNTHESIZED SPEECH, NOT A HUMAN RECORDING. Adequate for capability probing, "
    "which asks only whether a turn boundary moves. Not adequate for Track C, "
    "disfluency features, cut detection, or any published number."
)


def manifest_for(seed_path: Path) -> SeedManifest | None:
    """Load the manifest beside a seed, if one exists. `O(1)`.

    Args:
        seed_path: The wav to look up.

    Returns:
        The manifest, or `None` for a recording with no sidecar — which is what
        a human recording looks like.
    """
    path = seed_path.with_name(seed_path.name + MANIFEST_SUFFIX)
    if not path.is_file():
        return None
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    segments = raw.pop("segments", [])
    try:
        return SeedManifest(
            **raw,
            segments=tuple(SeedSegment(**seg) for seg in segments),
        )
    except TypeError:
        return None


def available_voices(say_path: str) -> tuple[str, ...]:
    """List installed `say` voices. `O(n)` in the voice count.

    Args:
        say_path: Absolute path to the `say` binary.

    Returns:
        Voice names, or an empty tuple if they could not be listed.
    """
    try:
        listed = subprocess.run(  # noqa: S603 - absolute path, fixed argv, no shell
            [say_path, "-v", "?"],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return ()
    names: list[str] = []
    for line in listed.stdout.splitlines():
        # Voice names may carry a curly apostrophe, spelled by codepoint so the
        # source stays unambiguous ASCII.
        match = re.match("^([A-Za-z][\\w'\u2019 -]*?)\\s{2,}[a-z]{2}_", line)
        if match:
            names.append(match.group(1).strip())
    return tuple(names)


def choose_voice(available: Sequence[str]) -> str:
    """Pick the first preferred voice that is installed. `O(n)`.

    Args:
        available: Installed voice names.

    Returns:
        A voice name, or `"system-default"` when none of the candidates exist.
    """
    for candidate in VOICE_CANDIDATES:
        if candidate in available:
            return candidate
    return "system-default"


def _render(
    say_path: str, text: str, voice: str, rate_wpm: int, workdir: Path
) -> bytes:
    """Render one segment with `say` and return mono PCM16. `O(n)`.

    Raises:
        SynthesisError: `say` produced no usable audio.
    """
    target = workdir / f"seg{abs(hash(text))}.wav"
    argv = [say_path, "-r", str(rate_wpm)]
    if voice != "system-default":
        argv += ["-v", voice]
    argv += [
        "--file-format=WAVE",
        f"--data-format={SAY_DATA_FORMAT}",
        "-o",
        str(target),
        text,
    ]
    try:
        result = subprocess.run(  # noqa: S603 - absolute path, fixed argv, no shell
            argv, capture_output=True, text=True, timeout=120, check=False
        )
    except (OSError, subprocess.SubprocessError) as exc:
        msg = f"`say` could not be run: {exc}"
        raise SynthesisError(msg) from exc
    if result.returncode != 0 or not target.is_file():
        msg = (
            f"`say` exited {result.returncode} without usable audio. "
            f"stderr: {result.stderr.strip() or '(empty)'}"
        )
        raise SynthesisError(msg)
    pcm, _ = load_seed(target)
    return pcm


def synthesize_seed(
    path: Path,
    *,
    voice: str | None = None,
    rate_wpm: int = RATE_WPM,
    segments: Sequence[str] = SCRIPT_SEGMENTS,
) -> SeedManifest:
    """Generate a seed recording with `say`, one segment at a time.

    Rendering each segment separately is what makes the boundaries exact. Slicing
    one long recording at fixed millisecond offsets cuts mid-word, which is how
    the first live run ended its pre-gap segment on a fragment.

    `O(n)` in the recording length.

    Args:
        path: Where to write the wav.
        voice: Voice name, or `None` to choose one.
        rate_wpm: Speaking rate in words per minute.
        segments: The sentences to speak, each complete on its own.

    Returns:
        The manifest, already written beside the wav.

    Raises:
        SayUnavailableError: `say` is not installed.
        SynthesisError: `say` ran but produced nothing usable.
    """
    say_path = shutil.which("say")
    if say_path is None:
        raise SayUnavailableError

    chosen = voice or choose_voice(available_voices(say_path))
    spacer = b"\x00" * (SEGMENT_GAP_MS * SAMPLE_RATE // 1000 * BYTES_PER_SAMPLE)

    with tempfile.TemporaryDirectory() as workdir:
        rendered = [
            _render(say_path, text, chosen, rate_wpm, Path(workdir))
            for text in segments
        ]

    pieces: list[bytes] = []
    boundaries: list[SeedSegment] = []
    cursor_ms = 0
    for index, (text, audio) in enumerate(zip(segments, rendered, strict=True)):
        length_ms = len(audio) // BYTES_PER_SAMPLE * 1000 // SAMPLE_RATE
        boundaries.append(
            SeedSegment(
                index=index, text=text, start_ms=cursor_ms, end_ms=cursor_ms + length_ms
            )
        )
        pieces.append(audio)
        cursor_ms += length_ms
        if index < len(segments) - 1:
            pieces.append(spacer)
            cursor_ms += SEGMENT_GAP_MS
    pcm = b"".join(pieces)
    conversion = "rendered per segment"

    duration_ms = len(pcm) // BYTES_PER_SAMPLE * 1000 // SAMPLE_RATE
    shortest = min(seg.duration_ms for seg in boundaries)
    if shortest < MIN_SEGMENT_MS:
        msg = (
            f"shortest segment is {shortest} ms; each needs at least "
            f"{MIN_SEGMENT_MS} ms of speech. Lower --seed-rate or lengthen it."
        )
        raise SynthesisError(msg)

    write_wav(path, pcm)
    manifest = SeedManifest(
        synthesized=True,
        caveat=SYNTHETIC_CAVEAT,
        engine="macos-say",
        voice=chosen,
        rate_wpm=rate_wpm,
        script=" ".join(segments),
        sha256=hashlib.sha256(pcm).hexdigest(),
        sample_rate=SAMPLE_RATE,
        channels=1,
        duration_ms=duration_ms,
        generated_at=datetime.now(tz=UTC).isoformat(timespec="seconds"),
        conversion=conversion,
        segments=tuple(boundaries),
    )
    manifest.write(path)
    return manifest
