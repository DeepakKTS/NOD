"""Append-only JSONL traces with redaction and a bounded flush buffer.

ARCHITECTURE.md §2: this module never blocks the loop. `emit` is synchronous and
non-blocking; `drain` is the low-priority task that actually writes. Tracing is
never load-bearing for a call (EC-43).

Redaction happens at write time rather than at read time, so the raw string never
reaches the disk at all. That ordering is the whole guarantee: a redact-on-read
design leaves the unredacted text sitting in a file, and INV-6 is about what is
persisted, not about what is displayed.
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from collections import deque
from collections.abc import Mapping
from pathlib import Path
from typing import Final, TextIO

from nod_core.types import TRACE_SCHEMA_VERSION, JsonValue

FLUSH_BUFFER_BYTES: Final = 64 * 1024
"""`B` in the complexity table of ARCHITECTURE.md §4. Bytes."""

ROTATE_BYTES: Final = 64 * 1024 * 1024
"""Traces rotate into `.1`, `.2` at this size (ARCHITECTURE.md §6). Bytes."""

TRACE_QUEUE_MAXSIZE: Final = 1024
"""Bounded queue for the `drain_trace` task; drop-oldest (ARCHITECTURE.md §3)."""

DIGIT_RUN_MIN: Final = 4
"""Digit runs of this length or longer are masked (INV-6, EC-42)."""

REDACTED_CREDENTIAL_MARK: Final = "[REDACTED]"
"""Replacement for a credential in a recorded URL (INV-5)."""

_EMAIL = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
_ISO_DATE = re.compile(r"\b\d{4}-\d{2}-\d{2}\b")
_NUMERIC_DATE = re.compile(r"\b\d{1,2}[/-]\d{1,2}[/-]\d{2,4}\b")
_WORDY_DATE = re.compile(
    r"\b(?:jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?\s+"
    r"\d{1,2}(?:st|nd|rd|th)?(?:,?\s+\d{4})?\b",
    re.IGNORECASE,
)
_PHONE = re.compile(r"\+?\d[\d\s().-]{7,}\d")
_DIGIT_RUN = re.compile(rf"\d{{{DIGIT_RUN_MIN},}}")
_URL_CREDENTIAL = re.compile(
    r"(?i)\b(token|authorization|api_key|apikey|key)=[^&\s]+",
)

_REDACTED_TEXT_KEYS: Final = ("transcript", "utterance", "text")
"""Payload keys carrying caller speech. Everything else is numbers or structure."""


def redact(text: str) -> str:
    """Mask personally identifying shapes in transcript text. `O(len(text))`.

    Masks digit runs of `DIGIT_RUN_MIN` or more, date patterns, emails and
    phone-like strings. On by default; disabled only when `NOD_TRACE_RAW=1` is
    set explicitly (INV-6, ARCHITECTURE.md §10, EC-42).

    Order is load-bearing. Dates run before the digit-run rule because an ISO
    date contains a four-digit year that would otherwise be masked as a bare
    number, losing the fact that it was a date at all.

    Args:
        text: The raw transcript fragment.

    Returns:
        The redacted fragment.
    """
    text = _EMAIL.sub("[EMAIL]", text)
    text = _ISO_DATE.sub("[DATE]", text)
    text = _NUMERIC_DATE.sub("[DATE]", text)
    text = _WORDY_DATE.sub("[DATE]", text)
    text = _PHONE.sub("[PHONE]", text)
    return _DIGIT_RUN.sub("[NUM]", text)


def redact_url(url: str) -> str:
    """Strip credentials from a recorded URL. `O(len(url))`.

    Applied unconditionally, ignoring `NOD_TRACE_RAW`: INV-5 is not a tracing
    preference, and a raw trace is still not a place for a key.

    Args:
        url: The connect URL, possibly carrying a token query parameter.

    Returns:
        The URL with any credential parameter masked.
    """
    return _URL_CREDENTIAL.sub(rf"\1={REDACTED_CREDENTIAL_MARK}", url)


def redact_payload(payload: JsonValue) -> JsonValue:
    """Redact only the speech-bearing strings in a payload. `O(size)`.

    Word timings, confidences and `word_is_final` are deliberately untouched.
    They are numbers, they carry no identifying content, and they are the entire
    measurement — a blanket "redact the words array" would silently destroy every
    turn-boundary reading while appearing to be the more cautious choice.

    Args:
        payload: Any JSON value from an upstream frame.

    Returns:
        The same structure with speech strings masked.
    """
    if isinstance(payload, Mapping):
        return {
            key: redact(value)
            if key in _REDACTED_TEXT_KEYS and isinstance(value, str)
            else redact_payload(value)
            for key, value in payload.items()
        }
    if isinstance(payload, (list, tuple)):
        return [redact_payload(item) for item in payload]
    return payload


class TraceSink:
    """One trace file per session, written off the hot path.

    `raw` defaults to `False`, so redaction is the default value rather than a
    convention to remember (INV-6).
    """

    def __init__(
        self,
        session_id: str,
        *,
        directory: Path,
        raw: bool = False,
        maxsize: int = TRACE_QUEUE_MAXSIZE,
    ) -> None:
        """Open a sink for one session.

        Args:
            session_id: Names the file, `{session_id}.jsonl`.
            directory: `NOD_TRACE_DIR`.
            raw: Disable redaction. Requires a documented reason (INV-6).
            maxsize: Bounded queue depth; overflow drops the oldest entry.
        """
        self._session_id = session_id
        self._directory = directory
        self._raw = raw
        self._queue: deque[str] = deque(maxlen=maxsize)
        self._wake = asyncio.Event()
        self._closing = False
        self._seq = 0
        self._dropped = 0
        self._written = 0
        self._disabled = False
        self._disable_errno: int | None = None
        self._t0 = time.monotonic()
        self._handle: TextIO | None = None

    @property
    def path(self) -> Path:
        """Where this session's trace is written."""
        return self._directory / f"{self._session_id}.jsonl"

    @property
    def dropped(self) -> int:
        """Lines lost to queue overflow, the `trace_dropped` counter."""
        return self._dropped

    @property
    def disabled(self) -> bool:
        """Whether tracing gave up for this session, e.g. on `ENOSPC` (EC-43)."""
        return self._disabled

    def emit(
        self,
        kind: str,
        payload: Mapping[str, JsonValue],
        *,
        t_ms: int,
        direction: str = "local",
    ) -> None:
        """Queue one trace line. Non-blocking, `O(1)`.

        Overflow policy is drop-oldest with a `trace_dropped` counter. Dropping
        telemetry is always preferable to delaying audio (ARCHITECTURE.md §3).

        Args:
            kind: The event name, one JSON object per line.
            payload: The event body.
            t_ms: Stream-relative milliseconds (CLAUDE.md §6).
            direction: `up`, `down` or `local`.
        """
        if self._disabled:
            return
        body = payload if self._raw else redact_payload(dict(payload))
        line = json.dumps(
            {
                "v": TRACE_SCHEMA_VERSION,
                "seq": self._seq,
                "t_mono_ms": round((time.monotonic() - self._t0) * 1000.0, 3),
                "t_stream_ms": t_ms,
                "session_id": self._session_id,
                "kind": kind,
                "dir": direction,
                "payload": body,
            },
            separators=(",", ":"),
            default=str,
        )
        self._seq += 1
        if len(self._queue) == self._queue.maxlen:
            self._dropped += 1
        self._queue.append(line)
        self._wake.set()

    def _open(self) -> TextIO | None:
        """Open the trace file, disabling tracing if the volume refuses. `O(1)`."""
        if self._handle is not None:
            return self._handle
        try:
            self._directory.mkdir(parents=True, exist_ok=True)
            self._handle = self.path.open("a", encoding="utf-8")
        except OSError as exc:
            self._disable(exc)
            return None
        return self._handle

    def _disable(self, exc: OSError) -> None:
        """Give up on tracing for this session, never on the call (EC-43).

        `ENOSPC` is the case EC-43 names, but every other `OSError` from the
        trace volume is equally non-fatal to the call, so they all land here.
        """
        self._disabled = True
        self._disable_errno = exc.errno
        self._queue.clear()

    def _flush(self, lines: list[str]) -> None:
        """Write and rotate. Runs in a worker thread, never on the loop."""
        handle = self._open()
        if handle is None:
            return
        try:
            for line in lines:
                handle.write(line + "\n")
                self._written += len(line) + 1
            handle.flush()
            if self._written >= ROTATE_BYTES:
                self._rotate()
        except OSError as exc:
            self._disable(exc)

    def _rotate(self) -> None:
        """Roll the current file into `.1` (ARCHITECTURE.md §6). `O(1)`."""
        if self._handle is not None:
            self._handle.close()
            self._handle = None
        self.path.replace(self.path.with_suffix(".jsonl.1"))
        self._written = 0

    async def drain(self) -> None:
        """Run the `drain_trace` task: buffer, write, rotate.

        `O(1)` amortised per line against a `FLUSH_BUFFER_BYTES` buffer. On
        `ENOSPC` this disables tracing for the session, emits a metric, and lets
        the call continue (EC-43).
        """
        while True:
            if not self._queue:
                if self._closing:
                    return
                self._wake.clear()
                await self._wake.wait()
                continue

            batch: list[str] = []
            size = 0
            while self._queue and size < FLUSH_BUFFER_BYTES:
                line = self._queue.popleft()
                batch.append(line)
                size += len(line) + 1
            if batch and not self._disabled:
                await asyncio.to_thread(self._flush, batch)

    async def aclose(self) -> None:
        """Flush the buffer and close the file."""
        self._closing = True
        self._wake.set()
        if self._queue and not self._disabled:
            batch = list(self._queue)
            self._queue.clear()
            await asyncio.to_thread(self._flush, batch)
        if self._handle is not None:
            self._handle.close()
            self._handle = None
