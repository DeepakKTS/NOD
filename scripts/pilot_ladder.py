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
    Hypothesis,
    LadderGeometry,
    LadderRow,
    ObservedBoundary,
    Prediction,
    build_from_halves,
    envelope_of,
    hold_invariance_ms,
    predict,
    read_mono,
    scores,
    segment_take,
    speech_runs,
    timeline_svg,
    write_mono,
)
from nod_bench.perturb import Gap, TruthSpan
from nod_bench.replay import (
    STATIC_ARMS,
    ArmSettings,
    StartRateGate,
    run_live_clip,
)

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

HYPOTHESES: tuple[Hypothesis, ...] = (
    "min_gate",
    "max_gate",
    "confidence",
    "clamped",
)

MIN_SWEEP_MS: tuple[int, ...] = (400, 900, 1600, 2400)
"""`min_turn_silence` values for the sweep. Milliseconds.

400 is `balanced`'s and sits *below* the service's commit point, so it is the
control: the clamp model says it cannot move the boundary. 900 is
`arbiter.MIN_MS_CEIL`, the highest the controller can currently ask for. 1600
and 2400 are past it, because ADR-001 measured `min_turn_silence` LIVE and
continuous out to 2175 ms — so if the boundary tracks those, the ceiling is
leaving usable range unused and that is a finding about our own constant.
"""

SWEEP_MAX_MS: int = 3600
"""`max_turn_silence` held at `conservative`'s value for every swept arm.

Held constant on purpose: the sweep asks what `min_turn_silence` does, and a
max gate that moved with it would leave the two indistinguishable.
"""


def arms_for(sweep: bool) -> dict[str, ArmSettings]:
    """The arms this run measures, label to gates. Pure. `O(1)`.

    Without `sweep`, the three published presets, untouched. With it, one arm
    per `MIN_SWEEP_MS` value, each labelled by the number it carries so a swept
    row can never be read as a preset row.
    """
    if not sweep:
        return {arm: STATIC_ARMS[arm] for arm in ARMS}
    return {
        f"min{ms}": ArmSettings(
            end_of_turn_confidence_threshold=0.4,
            min_turn_silence=ms,
            max_turn_silence=SWEEP_MAX_MS,
        )
        for ms in MIN_SWEEP_MS
    }


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

    arms = arms_for(args.sweep)
    rows: list[Prediction] = [
        predict(
            geo,
            arm,
            arms[arm].min_turn_silence,
            arms[arm].max_turn_silence,
            hypothesis,
            confidence_ms=CONFIDENCE_MS,
        )
        for geo in geometries
        for arm in arms
        for hypothesis in HYPOTHESES
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
                "sweep": args.sweep,
                "arms": {k: v.model_dump() for k, v in arms.items()},
                "geometry": [g.model_dump() for g in geometries],
                "predictions": [p.model_dump() for p in rows],
            },
            indent=1,
        )
    )

    print(f"\nwrote {pred_path}")
    header = " ".join(f"{h:>18s}" for h in HYPOTHESES)
    print(f"\n{'arm':14s} {'hold':>6s} {header}")
    print("-" * (21 + 19 * len(HYPOTHESES)))
    for geo in geometries:
        for arm in arms:
            cells = []
            for h in HYPOTHESES:
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
    arms = arms_for(bool(pred.get("sweep")))
    geometries = [LadderGeometry.model_validate(g) for g in pred["geometry"]]
    rate = int(pred["sample_rate"])

    # ADR-048: the account sustains about one new session per 16 s for a
    # churning workload, and a single 1008 voids the run. Twelve sessions is
    # under four minutes at that rate, so the gate costs nothing here and
    # removes the only failure mode that would need the whole ladder re-run.
    starts = StartRateGate()

    rows: list[LadderRow] = []
    print(
        f"{'arm':14s} {'hold':>6s} {'bounds':>6s} {'in-hold fire':>13s} {'silence':>8s} {'svc gap':>8s}  transcript"
    )
    print("-" * 110)
    for geo in geometries:
        clip = _clip(audio_dir, args.label, geo, rate)
        for arm in arms:
            await starts.wait()
            run = await run_live_clip(
                clip,
                arm,  # type: ignore[arg-type]
                session_factory=AssemblyAISession,
                api_key=api_key,
                trace_dir=audio_dir / "traces" / f"{arm}_{geo.hold_label_ms}",
                override=None if arm in STATIC_ARMS else arms[arm],
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
    for arm in arms:
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
    for hypothesis in HYPOTHESES:
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


def cmd_svg(args: argparse.Namespace) -> int:
    """Render one timeline per hold from a completed run. No network.

    Reads the committed observations rather than re-running anything: the
    picture in the video has to be the picture of the run that is in the
    repository, not of a fresh one that happens to agree.
    """
    audio_dir, _, obs_path = _paths(args.out, args.label)
    if not obs_path.exists():
        print(f"no observations at {obs_path}; run first", file=sys.stderr)
        return 2
    obs = json.loads(obs_path.read_text())
    rows = [LadderRow.model_validate(r) for r in obs["rows"]]
    by_hold: dict[int, list[LadderRow]] = {}
    for row in rows:
        by_hold.setdefault(row.hold_label_ms, []).append(row)

    written = []
    for hold, group in by_hold.items():
        wav = audio_dir / f"ladder_{args.label}_{hold}.wav"
        samples, rate = read_mono(wav)
        total = len(samples) / rate * 1000.0
        caption = (
            f"{wav.name} - hold {group[0].hold_measured_ms} ms after "
            f'"...my appointment TO" - red = turn ended inside the pause - '
            f"{obs_path.name}"
        )
        svg = timeline_svg(group, envelope_of(samples), total, caption=caption)
        dest = args.out / f"ladder_{args.label}_{hold}.timeline.live.svg"
        dest.write_text(svg)
        written.append(dest)
        print(f"wrote {dest}")
    return 0 if written else 2


def main() -> int:
    """Parse and dispatch. Returns the subcommand's exit code."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("predict", "run", "svg"))
    parser.add_argument("--label", required=True, help="say | human | ...")
    parser.add_argument("--out", type=Path, default=Path("bench/runs"))
    parser.add_argument("--holds", default=",".join(str(h) for h in DEFAULT_HOLDS))
    parser.add_argument("--take", type=Path, help="one continuous recording")
    parser.add_argument("--prefix", type=Path, help="prefix half (synthesised holds)")
    parser.add_argument("--continuation", type=Path, help="continuation half")
    parser.add_argument(
        "--sweep",
        action="store_true",
        help="walk min_turn_silence instead of running the three presets",
    )
    args = parser.parse_args()

    if args.command == "svg":
        return cmd_svg(args)
    if args.command == "predict":
        if args.take is None and (args.prefix is None or args.continuation is None):
            parser.error("give --take, or both --prefix and --continuation")
        return cmd_predict(args)
    return asyncio.run(cmd_run(args))


if __name__ == "__main__":
    sys.exit(main())
