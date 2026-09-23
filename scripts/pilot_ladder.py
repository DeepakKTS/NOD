"""The PILOT_REGIME ladder, as a committed and re-runnable procedure.

Two subcommands, and the order is enforced rather than advised:

    uv run --extra bench python scripts/pilot_ladder.py predict \
        --label say --prefix pre.wav --continuation con.wav
    uv run --extra bench python scripts/pilot_ladder.py run --label say

`run` refuses to start unless `predict` has already written its file. That is
the working method made structural: a prediction written after the observation
is not a prediction, and this is the one place in the repo where the *order* of
two steps is the evidence. The predictions file is committed before the run.

For a human take, one continuous recording of all four rows:

    uv run --extra bench python scripts/pilot_ladder.py predict \
        --label human --take take.wav
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
from pathlib import Path

from nod_adapters.assemblyai.session import AssemblyAISession
from nod_bench.corpus import GeneratedClip
from nod_bench.ladder import (
    GRID_MS,
    MEASURED_OVERHEAD_MS,
    PREDICTION_TOLERANCE_MS,
    TAIL_SILENCE_MS,
    LadderGeometry,
    LadderRow,
    ObservedBoundary,
    Prediction,
    build_from_halves,
    hold_invariance_ms,
    predict,
    read_mono,
    scores,
    segment_take,
    speech_runs,
    write_mono,
)
from nod_bench.perturb import Gap, TruthSpan
from nod_bench.replay import STATIC_ARMS, run_live_clip

DEFAULT_HOLDS: tuple[int, ...] = (500, 1000, 2000, 3500)
"""The four holds from docs/PILOT_REGIME.md. Milliseconds."""

CONFIDENCE_MS: float = 590.0
"""Silence at which the `confidence` hypothesis assumes the service commits.

**Derived from ADR-054's own published numbers, not fitted here.** `balanced`
fired at 3008 / 2980 / 3010 ms on a clip whose prefix ends at 2240 ms, so it
waited 740-770 ms; less `MEASURED_OVERHEAD_MS` that is ~590 ms of silence,
against a 400 ms min gate. One free parameter taken from one arm — which is why
it is written down as a constant here and tested against the *other* two arms,
where it makes a prediction it can fail: `aggressive` at 2990 rather than
`min_gate`'s 2560, a 430 ms separation.
"""

ARMS: tuple[str, ...] = ("aggressive", "balanced", "conservative")


def _paths(out: Path, label: str) -> tuple[Path, Path, Path]:
    """Clip dir, predictions file, observations file. `O(1)`."""
    return (
        out / f"ladder-{label}",
        out / f"pilot_ladder.{label}.live.predictions.json",
        out / f"pilot_ladder.{label}.live.json",
    )


def _clip(
    audio_dir: Path, label: str, geo: LadderGeometry, sample_rate: int
) -> GeneratedClip:
    """A `GeneratedClip` over one already-written ladder row. `O(n)`."""
    stem = f"ladder_{label}_{geo.hold_label_ms}"
    wav = audio_dir / f"{stem}.wav"
    truth_path = audio_dir / f"{stem}.truth.json"
    truth = TruthSpan(
        start_ms=0,
        end_ms=geo.continuation_end_ms,
        perturbation={"type": "ladder_hold", "len_ms": geo.hold_measured_ms},
        gaps=(
            Gap(
                start_ms=geo.prefix_end_ms,
                end_ms=geo.continuation_start_ms,
                origin="ladder_hold",
                preceding="fragment",
                certainty="certain",
                basis=(
                    "the prefix ends on a preposition, mid-digit-run or a "
                    "determiner; English cannot stop there, so the regime "
                    "label is syntactic and not a cut-point proxy "
                    "(docs/PILOT_REGIME.md)"
                ),
            ),
            Gap(
                start_ms=geo.continuation_end_ms,
                end_ms=geo.total_ms,
                origin="utterance_end",
                preceding="complete",
                certainty="certain",
                basis="the tail silence after the continuation finishes",
            ),
        ),
        text="",
    )
    truth_path.write_text(truth.model_dump_json(indent=1))
    return GeneratedClip(
        clip_id=stem,
        audio_path=wav,
        truth_path=truth_path,
        sha256=hashlib.sha256(wav.read_bytes()).hexdigest(),
        final_word_end_ms=geo.continuation_end_ms,
        total_ms=geo.total_ms,
        truth=truth,
        utterances=(),
    )


def _build(args: argparse.Namespace) -> tuple[list[LadderGeometry], int, Path]:
    """Write the ladder wavs and return their geometry. `O(n)`."""
    audio_dir, _, _ = _paths(args.out, args.label)
    audio_dir.mkdir(parents=True, exist_ok=True)
    holds = tuple(int(h) for h in args.holds.split(","))

    if args.take is not None:
        samples, rate = read_mono(args.take)
        runs = speech_runs(samples, rate)
        print(f"take: {len(samples) / rate * 1000:.0f} ms, {len(runs)} speech runs")
        for r in runs:
            print(f"  speech {r.start_ms:6d} - {r.end_ms:6d} ms")
        built = segment_take(samples, rate, holds)
    else:
        prefix, rate = read_mono(args.prefix)
        cont, rate_c = read_mono(args.continuation)
        if rate != rate_c:
            raise SystemExit(f"sample rates differ: {rate} vs {rate_c}")
        built = tuple(build_from_halves(prefix, cont, rate, h) for h in holds)

    for audio, geo in built:
        write_mono(
            audio_dir / f"ladder_{args.label}_{geo.hold_label_ms}.wav", audio, rate
        )
    return [geo for _, geo in built], rate, audio_dir


def cmd_predict(args: argparse.Namespace) -> int:
    """Build the clips and write every hypothesis's prediction. No network."""
    geometries, rate, _ = _build(args)
    _, pred_path, _ = _paths(args.out, args.label)

    rows: list[Prediction] = [
        predict(
            geo,
            arm,
            STATIC_ARMS[arm].min_turn_silence,
            STATIC_ARMS[arm].max_turn_silence,
            hypothesis,
            confidence_ms=CONFIDENCE_MS,
        )
        for geo in geometries
        for arm in ARMS
        for hypothesis in ("min_gate", "max_gate", "confidence")
    ]

    pred_path.write_text(
        json.dumps(
            {
                "label": args.label,
                "sample_rate": rate,
                "tail_silence_ms": TAIL_SILENCE_MS,
                "overhead_ms": MEASURED_OVERHEAD_MS,
                "confidence_ms": CONFIDENCE_MS,
                "tolerance_ms": PREDICTION_TOLERANCE_MS,
                "grid_ms": GRID_MS,
                "geometry": [g.model_dump() for g in geometries],
                "predictions": [p.model_dump() for p in rows],
            },
            indent=1,
        )
    )

    print(f"\nwrote {pred_path}")
    print(
        f"\n{'arm':14s} {'hold':>6s} {'min_gate':>18s} {'max_gate':>18s} {'confidence':>18s}"
    )
    print("-" * 80)
    for geo in geometries:
        for arm in ARMS:
            cells = []
            for h in ("min_gate", "max_gate", "confidence"):
                p = next(
                    r
                    for r in rows
                    if r.arm == arm
                    and r.hold_label_ms == geo.hold_label_ms
                    and r.hypothesis == h
                )
                mark = "in-hold" if p.fires_in_hold else "AFTER  "
                cells.append(f"{p.fired_at_ms:7.0f} {mark}")
            print(
                f"{arm:14s} {geo.hold_label_ms:6d} "
                + " ".join(f"{c:>18s}" for c in cells)
            )
    return 0


async def cmd_run(args: argparse.Namespace) -> int:
    """Feed every row to every static arm, against the real socket."""
    audio_dir, pred_path, obs_path = _paths(args.out, args.label)
    if not pred_path.exists():
        print(
            f"no predictions at {pred_path} — run `predict` first and commit it.\n"
            "A prediction written after the observation is not a prediction.",
            file=sys.stderr,
        )
        return 2
    api_key = os.environ.get("ASSEMBLYAI_API_KEY", "")
    if not api_key:
        print("ASSEMBLYAI_API_KEY not set", file=sys.stderr)
        return 2

    pred = json.loads(pred_path.read_text())
    geometries = [LadderGeometry.model_validate(g) for g in pred["geometry"]]
    rate = int(pred["sample_rate"])

    rows: list[LadderRow] = []
    print(
        f"{'arm':14s} {'hold':>6s} {'bounds':>6s} {'in-hold fire':>13s} {'silence':>8s} {'svc gap':>8s}  transcript"
    )
    print("-" * 110)
    for geo in geometries:
        clip = _clip(audio_dir, args.label, geo, rate)
        for arm in ARMS:
            run = await run_live_clip(
                clip,
                arm,  # type: ignore[arg-type]
                session_factory=AssemblyAISession,
                api_key=api_key,
                trace_dir=audio_dir / "traces" / f"{arm}_{geo.hold_label_ms}",
            )
            obs = run.observation
            bounds = tuple(
                ObservedBoundary(
                    fired_at_ms=f,
                    silence_started_ms=s,
                    turn_order=i,
                    word_count=0,
                    text="",
                )
                for i, (f, s) in enumerate(
                    zip(obs.emitted_end_ms, obs.emitted_silence_start_ms, strict=True)
                )
            )
            row = LadderRow(
                arm=arm,
                hold_label_ms=geo.hold_label_ms,
                hold_measured_ms=geo.hold_measured_ms,
                prefix_end_ms=geo.prefix_end_ms,
                boundaries=bounds,
                flush_turns=run.flush_turns,
            )
            rows.append(row)
            hit = row.in_hold
            sil = row.silence_at_fire_ms
            svc = (hit.fired_at_ms - hit.silence_started_ms) if hit else None
            print(
                f"{arm:14s} {geo.hold_label_ms:6d} {len(bounds):6d} "
                f"{(f'{hit.fired_at_ms:.0f}' if hit else '-- none --'):>13s} "
                f"{(f'{sil:.0f}' if sil is not None else '--'):>8s} "
                f"{(f'{svc:.0f}' if svc is not None else '--'):>8s}  "
                f"{[round(b.fired_at_ms) for b in bounds]}"
            )

    obs_path.write_text(
        json.dumps(
            {
                "label": args.label,
                "predictions_file": pred_path.name,
                "rows": [r.model_dump() for r in rows],
            },
            indent=1,
        )
    )
    print(f"\nwrote {obs_path}")

    print("\nhold-invariance of the in-hold firing silence (ADR-054's statistic):")
    for arm in ARMS:
        spread = hold_invariance_ms(tuple(r for r in rows if r.arm == arm))
        verdict = (
            "too few in-hold boundaries to say"
            if spread is None
            else (
                "INVARIANT — max gate did not bind"
                if spread <= PREDICTION_TOLERANCE_MS
                else "MOVES WITH THE HOLD — the regime is reachable"
            )
        )
        print(
            f"  {arm:14s} spread {('--' if spread is None else f'{spread:.0f} ms'):>9s}  {verdict}"
        )

    print(
        "\nhypothesis scoring (in-hold rows only, tolerance "
        f"{PREDICTION_TOLERANCE_MS} ms):"
    )
    for hypothesis in ("min_gate", "max_gate", "confidence"):
        hits = sum(
            scores(
                row,
                Prediction.model_validate(
                    next(
                        r
                        for r in pred["predictions"]
                        if r["arm"] == row.arm
                        and r["hold_label_ms"] == row.hold_label_ms
                        and r["hypothesis"] == hypothesis
                    )
                ),
            )
            for row in rows
        )
        print(f"  {hypothesis:12s} {hits:2d} / {len(rows):2d} rows")
    return 0


def main() -> int:
    """Parse and dispatch. Returns the subcommand's exit code."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("predict", "run"))
    parser.add_argument("--label", required=True, help="say | human | ...")
    parser.add_argument("--out", type=Path, default=Path("bench/runs"))
    parser.add_argument("--holds", default=",".join(str(h) for h in DEFAULT_HOLDS))
    parser.add_argument("--take", type=Path, help="one continuous recording")
    parser.add_argument("--prefix", type=Path, help="prefix half (synthesised holds)")
    parser.add_argument("--continuation", type=Path, help="continuation half")
    args = parser.parse_args()

    if args.command == "predict":
        if args.take is None and (args.prefix is None or args.continuation is None):
            parser.error("give --take, or both --prefix and --continuation")
        return cmd_predict(args)
    return asyncio.run(cmd_run(args))


if __name__ == "__main__":
    sys.exit(main())
