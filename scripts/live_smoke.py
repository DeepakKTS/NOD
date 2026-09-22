"""One clip, six arms, N=1, against the real API. Gate 4b Step 0.4.

The first observation of the live path that can distinguish a working controller
from a plumbed one. `FakeProbeSession` emits one word per turn, so the profiler
can never warm offline and every controlled arm is cold against it by
construction — the offline suite cannot tell the two apart.

    uv run --extra bench python scripts/live_smoke.py --clip seed_seg0_pause_000
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time
from pathlib import Path

from nod_adapters.assemblyai.session import AssemblyAISession
from nod_bench.corpus import BuiltCorpus, GeneratedClip
from nod_bench.metrics import frag, pcr, ttl
from nod_bench.replay import Arm, LiveRun, run_live_clip
from nod_core.policy import CompiledPolicy, load_policy

ARMS: tuple[Arm, ...] = (
    "aggressive",
    "balanced",
    "conservative",
    "nod",
    "nod-nocontext",
    "nod-nospeaker",
)


async def main() -> int:
    """Run the smoke and print a row per arm.

    Returns:
        `0` once every arm has run, `2` on a bad setup.
    """
    parser = argparse.ArgumentParser()
    parser.add_argument("--corpus", type=Path, default=Path("data/corpus/trackA"))
    parser.add_argument("--clip", default="seed_seg0_pause_000")
    parser.add_argument("--out", type=Path, default=Path("bench/smoke"))
    args = parser.parse_args()

    api_key = os.environ.get("ASSEMBLYAI_API_KEY", "")
    if not api_key:
        print("ASSEMBLYAI_API_KEY not set", file=sys.stderr)
        return 2

    corpus = BuiltCorpus.model_validate_json((args.corpus / "corpus.json").read_text())
    clip = next((c for c in corpus.clips if c.clip_id == args.clip), None)
    if clip is None:
        print(f"no clip {args.clip}", file=sys.stderr)
        return 2
    policy = load_policy(Path("config/policy.yaml"))

    print(
        f"clip {clip.clip_id}  {clip.total_ms} ms  speech ends {clip.final_word_end_ms} ms"
    )
    print(f"sidecar: {len(clip.truth.words)} words, {len(clip.truth.gaps)} gaps\n")
    print(
        f"{'arm':16s} {'bounds':>6s} {'patch':>5s} {'decide':>6s} {'flush':>5s} "
        f"{'PCR':>5s} {'TTLp90':>7s} {'FRAG':>5s}  first boundary"
    )
    print("-" * 86)

    rows: dict[str, dict[str, object]] = {}
    started = time.monotonic()
    for arm in ARMS:
        run = await _one(clip, arm, api_key, policy, args.out)

        obs = [run.observation]
        first = (
            run.observation.emitted_end_ms[0]
            if run.observation.emitted_end_ms
            else None
        )
        rows[arm] = {
            "boundaries": len(run.observation.emitted_end_ms),
            "patches": run.patches,
            "decides": len(run.decide_ms),
            "pcr": round(pcr(obs), 4),
            "ttl_p90": round(ttl(obs).p90, 1),
            "frag": round(frag(obs), 4),
            "emitted_end_ms": [round(x, 1) for x in run.observation.emitted_end_ms],
            "dec_p99_ms": round(max(run.decide_ms), 4) if run.decide_ms else None,
        }
        print(
            f"{arm:16s} {len(run.observation.emitted_end_ms):6d} {run.patches:5d} "
            f"{len(run.decide_ms):6d} {run.flush_turns:5d} {pcr(obs):5.3f} {ttl(obs).p90:7.0f} "
            f"{frag(obs):5.3f}  {first}"
        )

    print(f"\nwall clock {time.monotonic() - started:.1f} s")
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "smoke.json").write_text(json.dumps(rows, indent=2))

    nod_arms = ["nod", "nod-nocontext", "nod-nospeaker"]
    same = {
        json.dumps({k: v for k, v in rows[a].items() if k != "decides"})
        for a in nod_arms
    }
    print(f"\nthree nod arms identical (ignoring decide counts): {len(same) == 1}")
    base = json.dumps(
        {k: v for k, v in rows["balanced"].items() if k not in {"decides", "patches"}}
    )
    nod = json.dumps(
        {k: v for k, v in rows["nod"].items() if k not in {"decides", "patches"}}
    )
    print(f"nod identical to balanced: {base == nod}")
    print(f"nod decide count (>0 means the controller ran): {rows['nod']['decides']}")
    return 0


async def _one(
    clip: GeneratedClip,
    arm: Arm,
    api_key: str,
    policy: CompiledPolicy,
    out: Path,
) -> LiveRun:
    """One arm over one clip, against the real socket."""
    return await run_live_clip(
        clip,
        arm,
        session_factory=AssemblyAISession,
        api_key=api_key,
        policy=policy,
        trace_dir=out / arm,
    )


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
