r"""Drive one full call against a deployed Nod instance and report what came back.

Every other client in this repository talks to the application in-process
through ASGI. That answers "does the code work" and says nothing about "does
the deployed URL work", which is a different question with a TLS terminator, a
WebSocket upgrade at the edge and a container image in between. ADR-064 exists
because a platform served every plain-HTTP route correctly and refused every
upgrade; no in-process test could have found it.

    uv run --extra bench python scripts/deployed_smoke.py \\
        --url https://nod-turn-timing.fly.dev

**What this probe can and cannot observe, stated because the last two could
not answer the question they were pointed at (CLAUDE.md §5).**

It CAN see: that both sockets upgrade over TLS, that audio reaches the upstream,
that turns come back, what the transcript actually says, and which mode the
session was created in.

It CANNOT see whether a *browser* can read the console frames. The console is
sent with `send_bytes`, and `json.loads` accepts `bytes` without complaint while
`JSON.parse` throws on a `Blob`. That is a real defect this repository has had,
and a Python client is structurally incapable of failing on it. The browser half
is handled by `binaryType = "arraybuffer"` in `static/index.html`; read that
line, do not infer it from this script passing.

It also prints the first transcript, deliberately. A live session whose input
was never checked is not a measurement however clean its internals: the first
human-voice run here spent five minutes adapting to a video playing on the
machine, and the only reason anyone noticed was that a probe happened to print
the words.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from pathlib import Path
from typing import Any

import httpx
import soundfile as sf
import websockets

FRAME_MS = 50
"""Audio is paced in 50 ms frames, the size the demo screen sends."""

SAMPLE_RATE_HZ = 16000
"""Caller audio is mono 16 kHz PCM16 (ARCHITECTURE.md §7)."""


async def _read_console(
    url: str, frames: list[dict[str, Any]], ready: asyncio.Event
) -> None:
    """Subscribe to the console fan-out and collect every frame.

    Args:
        url: The `wss://.../v1/console?session_id=...` address.
        frames: Accumulator, appended to in arrival order.
        ready: Set once the socket is open, so audio does not start early.
    """
    async with websockets.connect(url) as ws:
        ready.set()
        try:
            async for message in ws:
                raw = message if isinstance(message, bytes) else message.encode()
                frames.extend(
                    json.loads(line) for line in raw.splitlines() if line.strip()
                )
        except websockets.exceptions.ConnectionClosed:
            pass


async def _send_audio(url: str, wav: Path) -> float:
    """Stream one clip in soft real time and close the socket.

    Args:
        url: The `wss://.../v1/stream?session_id=...` address.
        wav: A mono 16 kHz PCM16 clip.

    Returns:
        Wall-clock seconds spent streaming.
    """
    audio, rate = sf.read(wav, dtype="int16")
    if rate != SAMPLE_RATE_HZ:
        raise SystemExit(f"{wav}: expected {SAMPLE_RATE_HZ} Hz, got {rate}")
    samples_per_frame = SAMPLE_RATE_HZ * FRAME_MS // 1000
    async with websockets.connect(url) as ws:
        # **Started after the connect, not before.** The first version timed the
        # TLS handshake into the stream and reported 21.3 s for a 9.3 s clip.
        # Worse than a wrong label: the pacing target is computed from this
        # instant, so a slow connect would have made the loop blast every frame
        # it was "behind" by, and the upstream would have read a real-time clip
        # as a fast one.
        started = time.monotonic()
        for offset in range(0, len(audio), samples_per_frame):
            await ws.send(audio[offset : offset + samples_per_frame].tobytes())
            # Pace against the start, not the previous frame: sleeping a fixed
            # interval per frame accumulates every scheduling delay into a drift
            # the upstream reads as silence (EC-37).
            target = started + (offset + samples_per_frame) / SAMPLE_RATE_HZ
            await asyncio.sleep(max(0.0, target - time.monotonic()))
        streamed = time.monotonic() - started
        # Let the endpointer finish the last turn before the socket drops.
        await asyncio.sleep(2.0)
    return streamed


async def main() -> int:
    """Create a session, run one clip through it, and print what came back.

    Returns:
        0 if turns came back, 2 if the call produced nothing.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", default="https://nod-turn-timing.fly.dev")
    parser.add_argument(
        "--clip", type=Path, default=Path("data/corpus/trackA/seed_seg0_pause_000.wav")
    )
    args = parser.parse_args()

    base = args.url.rstrip("/")
    wss = base.replace("https://", "wss://").replace("http://", "ws://")

    async with httpx.AsyncClient(timeout=30) as client:
        created = await client.post(f"{base}/v1/sessions", json={})
    created.raise_for_status()
    session = created.json()
    print(
        f"session {session['session_id']}  mode={session['mode']}  "
        f"preset={session['preset']}  ceiling={session['ceiling_ms']}ms"
    )

    frames: list[dict[str, Any]] = []
    ready = asyncio.Event()
    console = asyncio.create_task(
        _read_console(f"{wss}{session['console_url']}", frames, ready)
    )
    await asyncio.wait_for(ready.wait(), timeout=20)
    elapsed = await _send_audio(f"{wss}{session['ws_url']}", args.clip)
    await asyncio.sleep(1.5)
    console.cancel()

    kinds: dict[str, int] = {}
    for frame in frames:
        key = str(frame.get("kind") or frame.get("type") or "?")
        kinds[key] = kinds.get(key, 0) + 1
    turns = [f for f in frames if "turn" in str(f.get("kind") or f.get("type") or "")]
    patches = [
        f for f in frames if "config" in str(f.get("kind") or f.get("type") or "")
    ]

    print(f"streamed {args.clip.name} in {elapsed:.1f}s, {len(frames)} console frames")
    print(f"  kinds: {kinds}")
    print(f"  turns: {len(turns)}   config frames: {len(patches)}")
    # **The patches are the point once the instance runs `adapt`.** A count alone
    # cannot distinguish a controller that moved the window from one that emitted
    # six no-ops, and INV-4 requires every change to carry its reason, so print
    # the reason and the values rather than the tally.
    for frame in patches:
        payload = frame.get("payload", frame)
        print(
            f"  patch: min={payload.get('min_turn_silence')} "
            f"max={payload.get('max_turn_silence')} "
            f"state={payload.get('state')} rule={payload.get('rule_id')} "
            f"trigger={payload.get('trigger')}"
        )
    if not patches:
        print(
            "  no patches. Expected when the input is short: the profiler needs "
            "24 inter-word gaps to warm, and in `observe` it never patches at all."
        )
    for frame in turns[:4]:
        payload = frame.get("payload", frame)
        text = str(payload.get("transcript") or payload.get("text") or "")
        if text:
            print(f"  transcript: {text[:110]!r}")
    if not frames:
        print("NO CONSOLE FRAMES: the upgrade succeeded and nothing arrived")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
