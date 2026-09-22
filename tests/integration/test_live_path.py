"""The `--live` driver, exercised against the in-memory upstream (INV-7).

No socket, no key, no credit: `FakeProbeSession` satisfies the same `ProbeSession`
protocol `AssemblyAISession` does, and it endpoints on the audio it is actually
given rather than replaying a script. So these tests drive the real orchestration
— the paced feeder, `SessionProxy`, the controller, the live scorer — and the only
thing swapped is the socket.

What they are **not**: evidence about the service. Nothing here measures
AssemblyAI. The live run at Phase 4 does that, and every figure it produces is
labelled `live` (ADR-016).
"""

from __future__ import annotations

import argparse
import asyncio
import io
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Final, override

import numpy as np
import pytest
import soundfile as sf

from nod_adapters.assemblyai.session import UpstreamError
from nod_bench.corpus import BuiltCorpus, GeneratedClip
from nod_bench.fake_session import FakeProbeSession
from nod_bench.perturb import Gap, TruthSpan, UtteranceSpan
from nod_bench.replay import (
    CONCURRENCY_REFUSED_CODE,
    TERMINATION_TIMEOUT_S,
    ClipContext,
    ConcurrencyRefusedError,
    LiveBoundary,
    LiveRunAbortedError,
    RunResult,
    TimedArbiter,
    replay,
    run_live_clip,
    score_live_clip,
)
from nod_core.capabilities import MEASURED_CAPABILITIES
from nod_core.policy import load_policy
from nod_core.types import SessionBegin, Termination, Turn

SAMPLE_RATE: Final = 16000
POLICY: Final = Path(__file__).resolve().parents[2] / "config" / "policy.yaml"


def _clip(
    tmp_path: Path,
    *,
    speech_ms: int = 900,
    gap_ms: int = 2000,
    turns: int = 3,
) -> GeneratedClip:
    """A multi-turn clip: speech, a long digital silence, speech, ...

    The silences are wide enough that every arm endpoints inside them, so a
    boundary count of zero means the driver is broken rather than the arms being
    conservative.
    """
    rng = np.random.default_rng(7)
    blocks: list[np.ndarray] = []
    spans: list[UtteranceSpan] = []
    at = 0
    for _ in range(turns):
        speech = rng.normal(0, 0.2, int(SAMPLE_RATE * speech_ms / 1000))
        blocks.append(speech.astype("float32"))
        spans.append(
            UtteranceSpan(
                start_ms=at,
                final_word_end_ms=at + speech_ms,
                expected_answer="spelling",
            )
        )
        at += speech_ms
        blocks.append(np.zeros(int(SAMPLE_RATE * gap_ms / 1000), dtype="float32"))
        at += gap_ms

    audio = np.concatenate(blocks)
    path = tmp_path / "live.wav"
    sf.write(path, audio, SAMPLE_RATE, subtype="PCM_16")
    total_ms = int(len(audio) / SAMPLE_RATE * 1000)
    truth = TruthSpan(
        start_ms=0,
        end_ms=total_ms,
        gaps=tuple(
            Gap(
                start_ms=s.final_word_end_ms,
                end_ms=s.final_word_end_ms + gap_ms,
                preceding="complete",
                certainty="certain",
                origin="utterance_end",
                basis="generated utterance-end silence, fixture",
            )
            for s in spans
        ),
    )
    truth_path = tmp_path / "live.truth.json"
    truth_path.write_text(truth.model_dump_json())
    return GeneratedClip(
        clip_id="live-1",
        audio_path=path,
        truth_path=truth_path,
        sha256="0" * 64,
        final_word_end_ms=spans[-1].final_word_end_ms,
        total_ms=total_ms,
        truth=truth,
        utterances=tuple(spans),
    )


# --- the third scorer -------------------------------------------------------


def test_the_live_scorer_pairs_every_boundary_with_a_silence_start(
    tmp_path: Path,
) -> None:
    """`emitted_silence_start_ms` must align 1:1 or `certain_only` cannot scope."""
    clip = _clip(tmp_path)
    boundaries = [
        LiveBoundary(
            fired_at_ms=1000.0, silence_started_ms=900.0, turn_order=1, words=()
        ),
        LiveBoundary(
            fired_at_ms=4000.0, silence_started_ms=3800.0, turn_order=2, words=()
        ),
    ]
    observation = score_live_clip(clip, "balanced", boundaries)
    assert observation.emitted_end_ms == (1000.0, 4000.0)
    assert observation.emitted_silence_start_ms == (900.0, 3800.0)
    assert len(observation.utterances) == 3


def test_the_live_scorer_keeps_ground_truth_generator_owned(tmp_path: Path) -> None:
    """ADR-031's line: the service says *when*, never *what counts as premature*.

    The boundaries below claim absurd times; the utterances and their gap labels
    must be untouched by that, because they come from the sidecar.
    """
    clip = _clip(tmp_path)
    observation = score_live_clip(
        clip,
        "nod",
        [
            LiveBoundary(
                fired_at_ms=-5.0, silence_started_ms=-5.0, turn_order=1, words=()
            )
        ],
    )
    assert [u.final_word_end_ms for u in observation.utterances] == [
        u.final_word_end_ms for u in clip.utterances
    ]
    assert all(g.certainty == "certain" for u in observation.utterances for g in u.gaps)


# --- the context axis, which the proxy did not have (ADR-044) ---------------


def test_clip_context_answers_per_turn_from_the_sidecar(tmp_path: Path) -> None:
    """`SessionProxy` knows only a turn order, so that is what it asks with."""
    clip = _clip(tmp_path)
    context = ClipContext(clip, policy=load_policy(POLICY), enabled=True)
    assert context(1) == "spelling"
    assert context(3) == "spelling"
    assert context.hint_for("spelling").max_mult > 1.0


def test_the_nocontext_ablation_neutralises_the_input_not_the_call(
    tmp_path: Path,
) -> None:
    """BENCH_SPEC §3's ablations neutralise an input; they do not branch."""
    clip = _clip(tmp_path)
    off = ClipContext(clip, policy=load_policy(POLICY), enabled=False)
    assert off(1) is None
    assert off.hint_for("spelling") == off.hint_for(None)
    assert off.hint_for("spelling").max_mult == 1.0


# --- the driver, end to end -------------------------------------------------


@pytest.mark.asyncio
async def test_a_static_arm_endpoints_and_is_scored(tmp_path: Path) -> None:
    """The whole static path: paced feed, boundaries collected, clip scored."""
    clip = _clip(tmp_path)
    run = await run_live_clip(
        clip,
        "aggressive",
        session_factory=FakeProbeSession,
        api_key="",
        trace_dir=tmp_path / "traces",
    )
    assert run.turns > 0, "no boundary fired on 2 s silences; the driver is broken"
    assert run.observation.arm == "aggressive"
    assert len(run.observation.emitted_end_ms) == run.turns
    assert run.patches == 0, "a static arm must not patch"
    assert run.decide_ms == (), "a static arm never calls decide"


@pytest.mark.asyncio
async def test_a_controlled_arm_runs_through_the_production_proxy(
    tmp_path: Path,
) -> None:
    """The arm has to be the production controller, or the number is about us.

    Asserts `decide` was actually called, which is what distinguishes a
    controlled arm that ran from one that silently behaved like `balanced` —
    CLAUDE.md §5's named failure for this module.
    """
    clip = _clip(tmp_path)
    run = await run_live_clip(
        clip,
        "nod",
        session_factory=FakeProbeSession,
        api_key="",
        policy=load_policy(POLICY),
        trace_dir=tmp_path / "traces",
    )
    assert run.turns > 0
    assert run.decide_ms, "decide() was never called, so the controller never ran"
    assert all(ms >= 0.0 for ms in run.decide_ms)


@pytest.mark.asyncio
async def test_the_controlled_arm_writes_a_trace_carrying_its_decisions(
    tmp_path: Path,
) -> None:
    """INV-4: no `UpdateConfiguration` without a `ConfigDecision` explaining it.

    This is also the artifact ADR-035 clause 4 is blocked on. 118 committed
    traces carry **zero** `config_decision` lines, because the controller had
    never emitted a patch outside a synthetic driver, and replay mode has nothing
    to render until one does. So the assertion is not "a trace exists" — it is
    that the pairing INV-4 requires actually holds, one decision per applied
    patch, each carrying its reason.
    """
    clip = _clip(tmp_path, turns=6, speech_ms=1400)
    traces = tmp_path / "traces"
    run = await run_live_clip(
        clip,
        "nod",
        session_factory=FakeProbeSession,
        api_key="",
        policy=load_policy(POLICY),
        trace_dir=traces,
    )
    lines = [
        json.loads(line)
        for path in sorted(traces.glob("*.jsonl"))
        for line in path.read_text().splitlines()
        if line.strip()
    ]
    decisions = [line for line in lines if line["kind"] == "config_decision"]
    applied = [line for line in lines if line["kind"] == "config_applied"]

    assert run.patches > 0, (
        "the controller applied no patch, so this asserts nothing about INV-4; "
        "the fixture must be long enough to warm the profiler (24 gaps, ADR-022)"
    )
    assert len(applied) == run.patches, (
        f"{len(applied)} config_applied lines against {run.patches} patches the "
        "proxy counted at the socket"
    )
    assert len(decisions) == len(applied), (
        f"INV-4: {len(applied)} patches applied with {len(decisions)} decisions "
        "explaining them"
    )
    for decision in decisions:
        payload = decision["payload"]
        for field in ("rule_id", "trigger", "changed", "state"):
            assert payload.get(field), f"config_decision is missing {field} (INV-4)"
        assert payload["min_turn_silence"] > 0
        assert payload["max_turn_silence"] > payload["min_turn_silence"]


@pytest.mark.asyncio
async def test_replay_keeps_one_observation_per_repeat(tmp_path: Path) -> None:
    """ADR-045: repeats are kept, not pooled, so the live IQR has an axis."""
    clip = _clip(tmp_path)
    result = await replay(
        clip.audio_path,
        "balanced",
        endpoint="fake",
        repeats=3,
        corpus_clip=clip,
        session_factory=FakeProbeSession,
        out=tmp_path / "runs",
    )
    assert isinstance(result, RunResult)
    assert result.repeats == 3
    assert len(result.observations) == 3
    assert len(result.patches) == 3
    assert len(result.wall_s) == 3
    assert all(o.clip_id == "live-1" for o in result.observations)


@pytest.mark.asyncio
async def test_replay_refuses_without_the_sidecar(tmp_path: Path) -> None:
    """Guessing the sidecar from the audio path scores against another truth."""
    clip = _clip(tmp_path)
    with pytest.raises(ValueError, match="needs `corpus_clip`"):
        await replay(clip.audio_path, "balanced", endpoint="fake", repeats=1)


def test_the_timed_arbiter_measures_without_changing_the_decision(
    tmp_path: Path,
) -> None:
    """`dec_p99` must not come at the cost of a different control law."""
    from nod_core.arbiter import Arbiter

    plain = Arbiter(capabilities=MEASURED_CAPABILITIES)
    timed = TimedArbiter(capabilities=MEASURED_CAPABILITIES)
    assert timed.capabilities == plain.capabilities
    assert timed.state == plain.state
    assert timed.samples == []


@pytest.mark.asyncio
async def test_a_wordless_turn_scores_as_ambiguous_rather_than_guessing(
    tmp_path: Path,
) -> None:
    """A turn with no words has no derivable silence start (see `LiveBoundary`).

    The conservative direction is to record `fired_at_ms`, which no gap covers,
    so `governing_certain` reads the utterance as ambiguous and it drops out of
    `certain_only`. The alternative — inventing a start — would put a fabricated
    time into PCR's gap lookup.
    """
    clip = _clip(tmp_path)
    boundary = LiveBoundary(
        fired_at_ms=12345.0, silence_started_ms=12345.0, turn_order=1, words=()
    )
    observation = score_live_clip(clip, "nod", [boundary])
    assert observation.emitted_silence_start_ms == (12345.0,)
    assert not observation.utterances[-1].governing_certain([12345.0])


@pytest.mark.asyncio
async def test_concurrency_never_exceeds_the_measured_account_limit(
    tmp_path: Path,
) -> None:
    """ADR-042: the upstream refuses the surplus *after* accepting the socket.

    So an over-limit sweep does not fail loudly — it produces sessions that look
    healthy and score as silent clips. The CLI refuses instead.
    """
    from nod_bench.replay import main
    from nod_core.config import UPSTREAM_CONCURRENCY_LIMIT

    code = main(
        [
            "--live",
            "--concurrency",
            str(UPSTREAM_CONCURRENCY_LIMIT + 1),
            "--out",
            str(tmp_path),
        ]
    )
    assert code == 2


# --- the nospeaker ablation (survivor from the Gate 4a mutation run) --------
#
# Found by `make mutate MODULE=live`: "nospeaker: run the ablation warm"
# survived. The reason is worth recording, because it is not a missing
# assertion — it is a fake that cannot express the thing.
#
# `FakeProbeSession._turn` emits **one word per turn**, and gaps never span a
# turn boundary (CONTROL_SPEC §2.1), so a turn contributes `w - 1 = 0` gaps. The
# profiler therefore never warms against the offline fake, on a clip of any
# length, and every controlled arm is cold for its whole duration. `nod` and
# `nod-nospeaker` are consequently **identical offline**, measured: 6 turns, 3
# patches, 24 decides, on both.
#
# That identity is exactly the trap CLAUDE.md §5 records for `run_nod_clip` —
# the same zeros come from a correct ablation with nothing to ablate and from an
# ablation that never ran. So the ablation is tested in the two places where it
# is decidable: the wiring that selects the profiler, and the profiler's own
# behaviour when it *is* given gaps.


def test_the_nospeaker_arm_is_wired_to_the_cold_profiler(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Which profiler each controlled arm constructs. `nod-nospeaker` gets cold.

    A wiring test, deliberately: CLAUDE.md §5 records that a unit test of a
    function does not cover the wiring that reaches it, and here the behaviour is
    unobservable offline (see the section note) while the wiring is exactly what
    the mutation broke.
    """
    from nod_bench import replay as replay_module
    from nod_bench.replay import ColdProfiler
    from nod_core.profiler import Profiler

    built: list[str] = []

    class SpyProfiler(Profiler):
        def __init__(self) -> None:
            super().__init__()
            built.append("Profiler")

    class SpyCold(ColdProfiler):
        def __init__(self) -> None:
            super().__init__()
            built.append("ColdProfiler")

    monkeypatch.setattr(replay_module, "Profiler", SpyProfiler)
    monkeypatch.setattr(replay_module, "ColdProfiler", SpyCold)

    clip = _clip(tmp_path, turns=2, speech_ms=400)
    for arm, expected in (("nod", "Profiler"), ("nod-nospeaker", "ColdProfiler")):
        built.clear()
        asyncio.run(
            run_live_clip(
                clip,
                arm,  # type: ignore[arg-type]
                session_factory=FakeProbeSession,
                api_key="",
                policy=load_policy(POLICY),
                trace_dir=tmp_path / arm,
            )
        )
        assert built == [expected], (
            f"arm {arm} constructed {built}, expected [{expected!r}]: the arm "
            "would be measured under the wrong label"
        )


def test_the_cold_profiler_stays_cold_past_the_warm_threshold() -> None:
    """The ablation's own behaviour, on input that warms a real `Profiler`.

    Fed turns carrying enough inter-word gaps to cross `MIN_GAPS_FOR_WARM`, a
    real profiler reports warm and this one must not — otherwise `nod-nospeaker`
    would use the speaker axis on a long enough call and stop being an ablation.
    """
    from nod_bench.replay import ColdProfiler
    from nod_core.profiler import MIN_GAPS_FOR_WARM, Profiler
    from nod_core.types import Turn as CoreTurn
    from nod_core.types import Word

    def turn(order: int, words: int) -> CoreTurn:
        return CoreTurn(
            turn_order=order,
            end_of_turn=True,
            end_of_turn_confidence=0.7,
            transcript=" ".join(["w"] * words),
            words=tuple(
                Word(
                    text="w",
                    start_ms=order * 100_000 + n * 400,
                    end_ms=order * 100_000 + n * 400 + 200,
                    confidence=0.9,
                    is_final=True,
                )
                for n in range(words)
            ),
        )

    plain, cold = Profiler(), ColdProfiler()
    # 4 turns x 10 words = 36 gaps, comfortably past the threshold of 24.
    for order in range(1, 5):
        plain.observe_turn(turn(order, 10))
        cold.observe_turn(turn(order, 10))

    assert plain.features().n_gaps >= MIN_GAPS_FOR_WARM
    assert not plain.features().cold, "the fixture does not warm a real profiler"
    assert cold.features().cold, "the ablation warmed, so it is not an ablation"
    assert cold.features().n_gaps == plain.features().n_gaps, (
        "the ablation must neutralise the *report*, not stop accumulating: "
        "BENCH_SPEC §3 ablates an input, it does not run different code"
    )


# --- error 1008 must abort, not score (Gate 4b Step 0.3) -------------------


class RefusingSession(FakeProbeSession):
    """A fake that emits an `Error` frame mid-clip, as the real one does.

    ADR-042 measured the refusal arriving **after** a successful WebSocket
    upgrade, so this opens normally and fails once audio is flowing. That
    ordering is the whole hazard: a session that refused at connect would be
    obvious.
    """

    def __init__(self, *, error_code: int = 1008, **kwargs: object) -> None:
        """As `FakeProbeSession`, plus the code to refuse with."""
        super().__init__(**kwargs)  # type: ignore[arg-type]
        self._error_code = error_code
        self._frames_before_error = 20

    @override
    async def events(self) -> AsyncIterator[SessionBegin | Turn | Termination]:
        """Yield a `Begin`, then raise as the adapter does on an `Error` frame."""
        yield SessionBegin(session_id="refusing", expires_at_ms=0)
        await asyncio.sleep(0.2)
        raise UpstreamError(
            self._error_code,
            "Unauthorized Connection: Too many concurrent sessions"
            if self._error_code == CONCURRENCY_REFUSED_CODE
            else "something else went wrong",
        )


@pytest.mark.asyncio
async def test_a_1008_aborts_the_clip_rather_than_scoring_it(
    tmp_path: Path,
) -> None:
    """The silent-clip failure, injected once on purpose.

    Before the guard: `proxy.run()` raised inside a task nothing inspected,
    `suppress(BaseException)` discarded it, the collector timed out after ten
    seconds and the clip scored on however many boundaries had arrived. Fewer
    boundaries against a sidecar denominator reads as *an arm that cut nobody
    off* — the flattering direction, produced by the run failing.
    """
    clip = _clip(tmp_path)
    with pytest.raises(ConcurrencyRefusedError, match="over-subscribed"):
        await run_live_clip(
            clip,
            "nod",
            session_factory=RefusingSession,
            api_key="",
            policy=load_policy(POLICY),
            trace_dir=tmp_path / "traces",
        )


@pytest.mark.asyncio
async def test_a_1008_aborts_a_static_arm_too(tmp_path: Path) -> None:
    """The static path reaches the socket by a different route and needs its own."""
    clip = _clip(tmp_path)
    with pytest.raises(ConcurrencyRefusedError):
        await run_live_clip(
            clip,
            "balanced",
            session_factory=RefusingSession,
            api_key="",
            trace_dir=tmp_path / "traces",
        )


@pytest.mark.asyncio
async def test_a_non_1008_upstream_error_aborts_the_clip_but_is_not_fatal(
    tmp_path: Path,
) -> None:
    """Only 1008 means the sweep is over-subscribed.

    Any other upstream error still costs the clip its score — the companion
    direction, so the guard is not simply "abort on everything" wearing a
    concurrency label.
    """
    clip = _clip(tmp_path)

    def factory(**kwargs: object) -> RefusingSession:
        return RefusingSession(error_code=4001, **kwargs)

    with pytest.raises(LiveRunAbortedError) as caught:
        await run_live_clip(
            clip,
            "balanced",
            session_factory=factory,
            api_key="",
            trace_dir=tmp_path / "traces",
        )
    assert not isinstance(caught.value, ConcurrencyRefusedError)


@pytest.mark.asyncio
async def test_the_sweep_writes_nothing_after_an_abort(tmp_path: Path) -> None:
    """A partially-scored corpus is not a corpus.

    `bootstrap_points` and `repeat_points` both pair arms clip-by-clip, so a run
    missing whichever clips were in flight would publish a table whose rows were
    measured over different subsets of it.
    """
    from nod_bench.replay import _live_sweep

    corpus_dir = tmp_path / "corpus"
    corpus_dir.mkdir()
    clip = _clip(tmp_path)
    corpus = BuiltCorpus(
        corpus_id="abort-test",
        corpus_sha256="b" * 64,
        generator_version="0.1.0",
        seed=1,
        source_sha256="c" * 64,
        clips=(clip,),
    )
    args = argparse.Namespace(
        repeats=1,
        concurrency=1,
        out=tmp_path / "out",
        policy=Path("config/policy.yaml"),
    )
    written = io.StringIO()
    code = await _live_sweep(
        corpus,
        ["balanced"],
        args,
        api_key="",
        policy=None,
        out=written,
        session_factory=RefusingSession,
    )
    assert code == 1
    assert "ABORTED" in written.getvalue()

    # The *artifacts* must not exist. `out/traces/` legitimately does: the
    # partial session traces are the evidence of what went wrong and are worth
    # keeping. What must never appear is a results table, a chart or a manifest,
    # because those are the things a reader would take as a finished run.
    artifacts = [
        path
        for path in (tmp_path / "out").rglob("*")
        if path.is_file() and path.suffix in {".md", ".svg", ".json"}
    ]
    assert not artifacts, f"artifacts written from a partial sweep: {artifacts}"


# --- the Terminate flush (Gate 4b Step 0.4) --------------------------------


def test_a_wordless_finalised_turn_is_not_a_caller_turn() -> None:
    """Measured against the real service; see `is_caller_turn`."""
    from nod_bench.replay import is_caller_turn
    from nod_core.types import Word

    spoken = Turn(
        turn_order=0,
        end_of_turn=True,
        end_of_turn_confidence=0.8,
        transcript="this is a test recording",
        words=(
            Word(text="this", start_ms=0, end_ms=200, confidence=0.9, is_final=True),
        ),
    )
    flush = Turn(
        turn_order=1,
        end_of_turn=True,
        end_of_turn_confidence=0.8,
        transcript="",
        words=(),
    )
    assert is_caller_turn(spoken)
    assert not is_caller_turn(flush)


class FlushingSession(FakeProbeSession):
    """A fake that closes with an empty finalised turn, as the real one does."""

    @override
    async def terminate(self) -> None:
        """Emit the wordless flush turn, then terminate."""
        self._emit(
            Turn(
                turn_order=99,
                end_of_turn=True,
                end_of_turn_confidence=0.5,
                transcript="",
                words=(),
            )
        )
        await super().terminate()


@pytest.mark.asyncio
async def test_the_flush_turn_is_dropped_and_counted(tmp_path: Path) -> None:
    """It must not reach FRAG or TTL, and it must not vanish silently.

    Counting it gave FRAG exactly 2.000 on all six arms and a TTL p90 measuring
    the flush rather than the boundary — identical across arms, which reads as
    "no arm differs" rather than as a defect. Dropping it moves both figures the
    flattering way, so the count travels with the run.
    """
    clip = _clip(tmp_path)
    run = await run_live_clip(
        clip,
        "balanced",
        session_factory=FlushingSession,
        api_key="",
        trace_dir=tmp_path / "traces",
    )
    assert run.flush_turns == 1, "the flush was not seen"
    assert run.turns > 0, "the real boundaries went with it"
    assert all(
        end <= clip.total_ms + TERMINATION_TIMEOUT_S * 1000
        for end in run.observation.emitted_end_ms
    )
    from nod_bench.metrics import frag

    assert frag([run.observation]) < 2.0, (
        "FRAG still counts the flush as an emitted turn"
    )
