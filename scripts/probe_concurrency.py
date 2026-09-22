"""Throwaway: measure the account's concurrent-stream limit empirically.

Gate 4a Step 0. Not part of the build, not imported by anything, not covered by
the suite. Ramps concurrent `AssemblyAISession` connections one at a time,
holding every earlier one open and fed, until the API refuses. Holding them open
is the whole point: opening and closing N sessions in sequence measures nothing
about concurrency.

    uv run --extra bench python scripts/probe_concurrency.py --max 80

Spends credits. One short clip, fed on a loop, ~1.5 s per rung.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from nod_adapters.assemblyai.session import (
    AssemblyAISession,
    UpstreamError,
)
from nod_bench.probe_clip import read_wav

FRAME_MS = 50
SAMPLE_RATE = 16000
FRAME_BYTES = int(SAMPLE_RATE * FRAME_MS / 1000) * 2


class Holder:
    """One live session, fed frames on a loop so the server does not idle it out."""

    def __init__(self, index: int, pcm: bytes, api_key: str, model: str) -> None:
        """Describe one session without opening it."""
        self.index = index
        self.pcm = pcm
        self.api_key = api_key
        self.model = model
        self.session: AssemblyAISession | None = None
        self.error: BaseException | None = None
        self.frames: list[dict[str, Any]] = []
        self._tasks: list[asyncio.Task[None]] = []

    async def open(self) -> None:
        """Open the socket and start pumping audio into it."""
        self.session = AssemblyAISession(
            api_key=self.api_key,
            model=self.model,
            sample_rate=SAMPLE_RATE,
            on_frame=lambda f: self.frames.append(dict(f)),
        )
        await self.session.__aenter__()
        self._tasks = [
            asyncio.create_task(self._pump()),
            asyncio.create_task(self._drain()),
        ]

    async def _pump(self) -> None:
        frames = [
            self.pcm[at : at + FRAME_BYTES]
            for at in range(0, len(self.pcm) - FRAME_BYTES + 1, FRAME_BYTES)
        ]
        assert self.session is not None
        deadline = time.monotonic()
        while True:
            for frame in frames:
                deadline += FRAME_MS / 1000
                await asyncio.sleep(max(0.0, deadline - time.monotonic()))
                await self.session.send_audio(frame)

    async def _drain(self) -> None:
        assert self.session is not None
        try:
            async for _ in self.session.events():
                pass
        except BaseException as exc:
            self.error = exc

    async def close(self) -> None:
        """Cancel the pumps and release the socket."""
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            with contextlib.suppress(BaseException):
                await task
        if self.session is not None:
            with contextlib.suppress(BaseException):
                await self.session.aclose()


def describe(exc: BaseException) -> dict[str, Any]:
    """Everything identifying about a refusal, for the record."""
    out: dict[str, Any] = {"type": type(exc).__name__, "message": str(exc)}
    if isinstance(exc, UpstreamError):
        out["error_code"] = getattr(exc, "error_code", None)
    for attr in ("status_code", "code", "reason", "rcvd", "sent"):
        value = getattr(exc, attr, None)
        if value is not None:
            out[attr] = str(value)
    if hasattr(exc, "response"):
        resp = exc.response
        out["http_status"] = str(getattr(resp, "status_code", ""))
        with contextlib.suppress(BaseException):
            out["http_body"] = str(getattr(resp, "body", ""))[:400]
    return out


async def main() -> int:
    """Ramp concurrent sessions until one is refused, and record the refusal.

    Returns:
        `0` once the ramp has completed or been refused, `2` on a bad setup.
    """
    parser = argparse.ArgumentParser()
    parser.add_argument("--max", type=int, default=80)
    parser.add_argument("--clip", type=Path, default=None)
    parser.add_argument("--hold-s", type=float, default=1.5)
    args = parser.parse_args()

    api_key = os.environ.get("ASSEMBLYAI_API_KEY")
    if not api_key:
        print("ASSEMBLYAI_API_KEY not set", file=sys.stderr)
        return 2
    model = os.environ.get("NOD_MODEL", "universal-streaming-english")

    clip = args.clip or Path("data/corpus/trackA/seed_seg0_burst_024.wav")
    pcm, rate = read_wav(clip)
    if rate != SAMPLE_RATE:
        print(f"clip is {rate} Hz, need {SAMPLE_RATE}", file=sys.stderr)
        return 2
    print(f"clip {clip.name}, {len(pcm) / 2 / SAMPLE_RATE:.1f}s, model {model}")

    holders: list[Holder] = []
    refusal: dict[str, Any] | None = None
    reached = 0
    try:
        for n in range(1, args.max + 1):
            holder = Holder(n, pcm, api_key, model)
            try:
                await holder.open()
            except BaseException as exc:
                refusal = {"at_n": n, "phase": "connect", **describe(exc)}
                print(f"n={n:3d}  REFUSED at connect: {refusal}")
                break
            holders.append(holder)
            await asyncio.sleep(args.hold_s)

            failed = [h for h in holders if h.error is not None]
            if failed:
                first = failed[0]
                assert first.error is not None
                refusal = {
                    "at_n": n,
                    "phase": "post-connect",
                    "failed_index": first.index,
                    **describe(first.error),
                }
                print(f"n={n:3d}  REFUSED post-connect: {refusal}")
                break

            reached = n
            began = sum(
                1 for h in holders for f in h.frames if f.get("type") == "Begin"
            )
            print(f"n={n:3d}  open, {began} Begin frames, all healthy")
    finally:
        await asyncio.gather(*(h.close() for h in holders))

    result = {
        "max_concurrent_reached": reached,
        "refusal": refusal,
        "ramp_cap": args.max,
        "model": model,
        "clip": str(clip),
    }
    print("\n" + json.dumps(result, indent=2))
    # Off the loop: every socket is closed by now, but ASYNC240 is right in
    # general and a one-line `to_thread` is cheaper than an exemption.
    await asyncio.to_thread(
        Path("scripts/concurrency-result.json").write_text,
        json.dumps(result, indent=2),
    )
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
