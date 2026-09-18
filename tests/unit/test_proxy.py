"""`proxy.py`: the two paths CLAUDE.md §5 puts in the first tier, and the rest.

CLAUDE.md §5 is explicit about the split. `send_patch_upstream` and
`run_controller`'s `SAFE` path get full mutation discipline, because they fail
**invisibly and in the flattering direction**: a patch computed, traced and never
applied, or a controller that catches an exception and quietly holds last-known-
good for the rest of a session, both produce a run that looks like `nod` and
behaves like `balanced`. The arm would be measured under the wrong label and the
headline delta would be an artifact of a bug.

So the tests for those two assert what §5 asks for and not what is easy: that the
patch **reached the socket**, and that entering `SAFE` is **loud**. Both are
easy to get wrong in the same way — asserting the code took the branch rather
than that the branch had its effect.

Everything else here is the accepted-thinner tier: a broken audio pump is a
silent call and a broken rotation is a dropped session, and nobody ships either
by accident.
"""

from __future__ import annotations

import ast
import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from pathlib import Path
from typing import Final, override

import pytest

from nod_core.arbiter import WIRE_FIELDS, Arbiter
from nod_core.capabilities import UPDATABLE_FIELDS
from nod_core.profiler import Profiler
from nod_core.proxy import SessionProxy
from nod_core.trace import TraceSink
from nod_core.types import (
    Capabilities,
    ConfidenceField,
    ControllerState,
    Cut,
    KnobVerdict,
    NodMode,
    SessionBegin,
    Termination,
    Turn,
    Word,
)

PROXY_SOURCE: Final = (
    Path(__file__).resolve().parents[2] / "src" / "nod_core" / "proxy.py"
)

ALL_LIVE: Final = Capabilities(
    knobs=tuple((field, KnobVerdict.LIVE) for field in UPDATABLE_FIELDS),
    confidence_field=ConfidenceField.VARYING,
    force_endpoint=KnobVerdict.LIVE,
    has_word_timings=True,
)


class FakeUpstream:
    """An `SttSession` that records what reached it. INV-7: no network.

    Records rather than asserts, because "the patch reached the socket" is a
    statement about what arrived, and a mock that merely counts calls cannot tell
    a patch carrying the right values from one carrying none.
    """

    def __init__(
        self,
        *,
        frames: tuple[SessionBegin | Turn | Termination, ...] = (),
        reject: int = 0,
        session_id: str = "sess-1",
    ) -> None:
        self.id = session_id
        self.audio: list[bytes] = []
        self.configs: list[Mapping[str, float]] = []
        self.forced = 0
        self.closed = False
        self._frames = frames
        self._reject = reject

    async def send_audio(self, frame: bytes) -> None:
        self.audio.append(frame)

    async def update_configuration(self, patch: Mapping[str, float]) -> None:
        if self._reject > 0:
            self._reject -= 1
            msg = "upstream refused the update"
            raise RuntimeError(msg)
        self.configs.append(dict(patch))

    async def force_endpoint(self) -> None:
        self.forced += 1

    async def events(self) -> AsyncIterator[SessionBegin | Turn | Termination]:
        for frame in self._frames:
            yield frame

    async def aclose(self) -> None:
        self.closed = True


def turn(order: int, *, gap: int = 900, words: int = 4, ended: bool = True) -> Turn:
    """A turn whose inter-word gaps are `gap` ms."""
    built: list[Word] = []
    clock = 0
    for index in range(words):
        built.append(
            Word(
                text=f"w{index}",
                start_ms=clock,
                end_ms=clock + 200,
                confidence=0.9,
                is_final=True,
            )
        )
        clock += 200 + gap
    return Turn(
        turn_order=order,
        end_of_turn=ended,
        end_of_turn_confidence=0.4,
        transcript=" ".join(w.text for w in built),
        words=tuple(built),
    )


def build(
    tmp_path: Path,
    *,
    upstream: FakeUpstream | None = None,
    mode: NodMode = NodMode.ADAPT,
) -> tuple[SessionProxy, FakeUpstream, TraceSink]:
    """Wire a proxy over a fake upstream and a real trace sink."""
    socket = upstream if upstream is not None else FakeUpstream()
    sink = TraceSink("sess-1", directory=tmp_path)
    proxy = SessionProxy(
        upstream=socket,
        profiler=Profiler(),
        arbiter=Arbiter(capabilities=ALL_LIVE),
        trace=sink,
        mode=mode,
        ceiling_ms=2600,
    )
    return proxy, socket, sink


def traced(sink: TraceSink) -> list[str]:
    """The `kind` of every line queued on the sink, in order."""
    return [line.split('"kind":')[1].split('"')[1] for line in sink._queue]


# --- first tier: the patch has to reach the socket --------------------------


@pytest.mark.asyncio
async def test_a_decided_patch_reaches_the_socket_with_its_values(
    tmp_path: Path,
) -> None:
    """CLAUDE.md §5's first-tier requirement, stated as §5 states it.

    Not "a patch was computed" and not "update_configuration was called" — the
    values that arrived are what the service acts on, and a patch that is
    computed, traced and then sent empty produces a run labelled `nod` that
    behaves like `balanced`.
    """
    proxy, socket, _ = build(tmp_path)
    for order in range(1, 9):
        await proxy._handle_turn(turn(order))
    assert socket.configs, "no UpdateConfiguration reached the upstream at all"
    sent = socket.configs[-1]
    assert set(sent) <= {field for field, _ in WIRE_FIELDS}
    assert sent, "an empty payload reached the socket"
    for value in sent.values():
        assert value > 0.0
    # And what the socket received is what the proxy believes is in force.
    for field, attribute in WIRE_FIELDS:
        if field in sent:
            assert sent[field] == float(getattr(proxy.config_in_force, attribute))


@pytest.mark.asyncio
async def test_config_in_force_only_moves_on_a_confirmed_send(
    tmp_path: Path,
) -> None:
    """A rejected update must not leave the proxy believing it applied.

    This is the flattering-direction failure in its purest form: if
    `config_in_force` advanced on a send that failed, every later decision would
    be computed against a config the socket never had, and the trace would show a
    controller doing the right thing to a window that does not exist.
    """
    socket = FakeUpstream(reject=2)
    proxy, _, sink_ = build(tmp_path, upstream=socket)
    before = proxy.config_in_force
    for order in range(1, 9):
        await proxy._handle_turn(turn(order))
    assert not socket.configs
    assert proxy.config_in_force == before
    assert "config_rejected" in traced(sink_)


@pytest.mark.asyncio
async def test_a_rejected_update_is_retried_once_then_drops_to_observe(
    tmp_path: Path,
) -> None:
    """EC-32: one retry with the same values, then `observe` for the session.

    `observe` and not `off`: profiling and tracing stay on, so the console can
    show why the loop stopped closing and the trace still carries the decisions
    it would have sent. A session that goes dark on a transport failure tells the
    operator nothing.
    """
    socket = FakeUpstream(reject=1)
    proxy, _, _retry_sink = build(tmp_path, upstream=socket)
    for order in range(1, 9):
        await proxy._handle_turn(turn(order))
    # The first attempt failed and the retry succeeded, so the session is intact.
    assert socket.configs
    assert proxy.mode is NodMode.ADAPT
    assert proxy.rejected_updates == 1

    both = FakeUpstream(reject=99)
    proxy2, _, sink2 = build(tmp_path, upstream=both)
    for order in range(1, 9):
        await proxy2._handle_turn(turn(order))
    assert not both.configs
    assert proxy2.mode is NodMode.OBSERVE, "EC-32 requires the drop to observe"
    assert "mode_degraded" in traced(sink2)


@pytest.mark.asyncio
async def test_a_field_the_host_owns_is_dropped_from_the_payload(
    tmp_path: Path,
) -> None:
    """EC-33: Nod merges rather than fights, and the merge happens at the socket.

    Asserted on what arrived rather than on what was decided, because the arbiter
    already excludes host fields from `changed` and a second check there would
    pass while the proxy sent them anyway.
    """
    proxy, socket, _ = build(tmp_path)
    proxy.note_host_configuration(["max_turn_silence"])
    for order in range(1, 9):
        await proxy._handle_turn(turn(order))
    for payload in socket.configs:
        assert "max_turn_silence" not in payload, (
            "the proxy sent a field the host set within HOST_OVERRIDE_MS"
        )


@pytest.mark.asyncio
async def test_a_host_override_expires(tmp_path: Path) -> None:
    """EC-33's five seconds. Injected clock, so the test does not sleep."""
    proxy, _, _ = build(tmp_path)
    proxy.note_host_configuration(["max_turn_silence"], now=0.0)
    assert "max_turn_silence" in proxy._host_fields(now=4.9)
    assert "max_turn_silence" not in proxy._host_fields(now=5.1)


@pytest.mark.asyncio
async def test_observe_mode_decides_and_traces_but_sends_nothing(
    tmp_path: Path,
) -> None:
    """ARCHITECTURE §7: `observe` profiles and traces but sends no patches."""
    proxy, socket, _unused = build(tmp_path, mode=NodMode.OBSERVE)
    for order in range(1, 9):
        await proxy._handle_turn(turn(order))
    assert not socket.configs
    assert proxy.config_in_force.max_turn_silence_ms > 0
    assert proxy._profiler.features().n_gaps > 0, "observe must still profile"


# --- first tier: entering SAFE has to be loud -------------------------------


class ExplodingProfiler(Profiler):
    """A profiler that raises once, then behaves. EC-31.

    Once and not always, which matters: a profiler that explodes on every turn
    can never leave `SAFE`, so a test built on one cannot tell "the call
    continues" from "the controller latched". The first draft of this fixture
    raised every time and its recovery assertion was asserting the opposite of
    what the code does.
    """

    def __init__(self) -> None:
        super().__init__()
        self.exploded = 0

    @override
    def observe_turn(self, turn: Turn, *, agent_audio_ms: int = 0) -> Cut | None:
        self.exploded += 1
        if self.exploded == 1:
            msg = "profiler exploded"
            raise RuntimeError(msg)
        return super().observe_turn(turn, agent_audio_ms=agent_audio_ms)


@pytest.mark.asyncio
async def test_a_controller_exception_enters_safe_loudly_and_the_call_survives(
    tmp_path: Path,
) -> None:
    """INV-8 and EC-31, asserted on the effects rather than on the catch.

    Four separate claims, because "the code caught the error" is true of a bare
    `except: pass` and CLAUDE.md §5 names this exact path as one that fails
    invisibly:

    1. the arbiter is in `SAFE`, so the next turn emits nothing;
    2. a `controller_error` line reached the trace;
    3. the proxy counted it, so `dev` can re-raise after the call;
    4. nothing was sent upstream on the failing turn.

    Dropping any one of them leaves a session that looks like `nod` and behaves
    like `balanced` — which is the measurement error, not just the bug.
    """
    socket = FakeUpstream()
    sink = TraceSink("sess-1", directory=tmp_path)
    controller = Arbiter(capabilities=ALL_LIVE)
    proxy = SessionProxy(
        upstream=socket,
        profiler=ExplodingProfiler(),
        arbiter=controller,
        trace=sink,
        mode=NodMode.ADAPT,
        ceiling_ms=2600,
    )
    await proxy._handle_turn(turn(1))

    after_fault = controller.state
    assert after_fault is ControllerState.SAFE
    assert "controller_error" in traced(sink), (
        "entering SAFE was silent; INV-8 requires a controller_error event"
    )
    assert proxy.controller_errors == 1
    assert controller.errors == 1
    assert not socket.configs

    # And the call continues. The §6 machine walks SAFE -> COLD on the next turn
    # rather than latching, so a single controller fault costs one turn and not a
    # call — which is the whole of INV-8's "fail soft in a call".
    await proxy._handle_turn(turn(2))
    after_recovery = controller.state
    assert after_recovery is not ControllerState.SAFE, (
        "the arbiter latched in SAFE; §6 requires SAFE -> COLD on the next turn"
    )
    assert proxy.controller_errors == 1, "a healthy turn was counted as an error"


@pytest.mark.asyncio
async def test_a_transport_failure_is_not_reported_as_a_controller_error(
    tmp_path: Path,
) -> None:
    """The EC-31 boundary stops short of the socket, deliberately.

    A rejected `UpdateConfiguration` is EC-32's problem and has its own retry.
    Folding it into the controller's `except` would drop the speaker axis for the
    rest of the call over a dropped packet, and would report a transport error as
    a controller error in the trace the console renders.
    """
    socket = FakeUpstream(reject=99)
    proxy, _, sink = build(tmp_path, upstream=socket)
    for order in range(1, 9):
        await proxy._handle_turn(turn(order))
    kinds = traced(sink)
    assert "config_rejected" in kinds
    assert "controller_error" not in kinds
    assert proxy.controller_errors == 0
    assert proxy._arbiter.state is not ControllerState.SAFE


# --- INV-1: the audio path is sacred ----------------------------------------


def test_pump_audio_up_references_nothing_of_the_controller() -> None:
    """INV-1, by AST inspection, because the guarantee is an absence.

    "Audio forwarding must not await controller work" cannot be tested by running
    it — a passing call proves nothing about a slow controller on a different
    day. What can be checked is that the method body names neither the profiler
    nor the arbiter nor their queue, so there is nothing there to await.

    `decide` being synchronous (CLAUDE.md §6) is the other half: even a future
    edit that reached for the arbiter could not await it.
    """
    tree = ast.parse(PROXY_SOURCE.read_text(encoding="utf-8"))
    body = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "pump_audio_up"
    )
    named = {node.attr for node in ast.walk(body) if isinstance(node, ast.Attribute)}
    forbidden = {"_profiler", "_arbiter", "_controller_queue", "decide", "features"}
    assert not named & forbidden, f"pump_audio_up reaches for {named & forbidden}"


@pytest.mark.asyncio
async def test_audio_is_forwarded_verbatim_and_overflow_drops_the_oldest(
    tmp_path: Path,
) -> None:
    """EC-02 and ARCHITECTURE §3: bounded ring, drop-oldest, counted."""
    proxy, socket, _ = build(tmp_path)
    proxy.feed_audio(b"\x01\x02")
    proxy.feed_audio(b"\x03\x04")
    task = asyncio.create_task(proxy.pump_audio_up())
    await asyncio.sleep(0.05)
    await proxy.aclose()
    task.cancel()
    assert socket.audio[:2] == [b"\x01\x02", b"\x03\x04"], "frames were not verbatim"

    flooded, _, _ = build(tmp_path)
    capacity = flooded._client_audio.maxlen or 0
    for index in range(capacity + 5):
        flooded.feed_audio(bytes([index % 256]))
    assert flooded.dropped["audio"] == 5
    assert len(flooded._client_audio) == capacity


# --- the thinner tier: fan-out, rotation, shutdown --------------------------


@pytest.mark.asyncio
async def test_every_upstream_frame_reaches_the_client_unmodified(
    tmp_path: Path,
) -> None:
    """ARCHITECTURE §7: Nod adds to the stream and never rewrites it."""
    frames = (
        SessionBegin(session_id="sess-1", expires_at_ms=600_000),
        turn(1),
        Termination(session_id="sess-1", reason="done"),
    )
    proxy, _, _ = build(tmp_path, upstream=FakeUpstream(frames=frames))
    seen: list[object] = []

    async def collect() -> None:
        seen.extend([event async for event in proxy.client_events()])

    collector = asyncio.create_task(collect())
    await proxy.pump_events_down()
    await asyncio.wait_for(collector, timeout=1.0)
    assert seen == list(frames), "a frame was dropped, reordered or edited"


@pytest.mark.asyncio
async def test_the_stream_clock_comes_from_word_timings(tmp_path: Path) -> None:
    """CLAUDE.md §6 and EC-08: stream-relative milliseconds, never wall clock."""
    proxy, _, _ = build(tmp_path, upstream=FakeUpstream(frames=(turn(1),)))
    await proxy.pump_events_down()
    assert proxy._stream_ms == turn(1).words[-1].end_ms


@pytest.mark.asyncio
async def test_a_rotation_carries_the_profile_and_replays_the_config(
    tmp_path: Path,
) -> None:
    """EC-03: the caller sees nothing, which means the window does not move."""
    first = FakeUpstream(session_id="sess-1")
    second = FakeUpstream(session_id="sess-2")
    proxy, _, sink = build(tmp_path, upstream=first)
    for order in range(1, 9):
        await proxy._handle_turn(turn(order))
    carried = proxy._profiler.features()
    in_force = proxy.config_in_force

    proxy._reconnect = _returning(second)
    await proxy.rotate()

    assert proxy._profiler.features() == carried, "the profile did not survive"
    assert second.configs, "the config was not replayed on the new socket"
    replayed = second.configs[0]
    assert replayed["max_turn_silence"] == float(in_force.max_turn_silence_ms)
    assert first.closed, "the old socket was left open"
    assert "upstream_rotated" in traced(sink)


def _returning(session: FakeUpstream) -> Callable[[], Awaitable[FakeUpstream]]:
    async def factory() -> FakeUpstream:
        return session

    return factory


@pytest.mark.asyncio
async def test_rotation_without_a_factory_says_so_rather_than_failing_obscurely(
    tmp_path: Path,
) -> None:
    """EC-03 cannot be met by an object holding one socket, so it says that.

    At `expires_at` on a long call is the worst possible moment to discover a
    missing dependency, so the message names what is missing and why.
    """
    proxy, _, _ = build(tmp_path)
    with pytest.raises(RuntimeError, match="reconnect"):
        await proxy.rotate()


@pytest.mark.asyncio
async def test_aclose_leaves_no_orphan_tasks_and_is_idempotent(
    tmp_path: Path,
) -> None:
    """EC-05: flush the trace, cancel every task, release the socket, twice safely.

    The client disconnecting and the upstream terminating can both reach `aclose`,
    so a second call raising would turn an ordinary hangup into an error.
    """
    frames = (
        SessionBegin(session_id="sess-1", expires_at_ms=600_000),
        turn(1),
        Termination(session_id="sess-1", reason="client_gone"),
    )
    socket = FakeUpstream(frames=frames)
    proxy, _, _ = build(tmp_path, upstream=socket)
    before = len(asyncio.all_tasks())

    async def drain_client() -> None:
        async for _ in proxy.client_events():
            pass

    collector = asyncio.create_task(drain_client())
    await proxy.run()
    await asyncio.wait_for(collector, timeout=1.0)
    await proxy.aclose()
    await proxy.aclose()

    assert socket.closed
    await asyncio.sleep(0)
    assert len(asyncio.all_tasks()) <= before + 1, "a task was orphaned (EC-05)"


@pytest.mark.asyncio
async def test_a_per_connection_ceiling_below_the_floor_is_clamped(
    tmp_path: Path,
) -> None:
    """ADR-021's soft side, at the boundary the ADR names.

    `Voice.pacing_hint_ms` feeds this on a mid-session voice switch, so a value
    below the floor arrives from a real code path. INV-8 forbids dropping the call
    for it, and §4's ordering forbids honouring it.
    """
    proxy = SessionProxy(
        upstream=FakeUpstream(),
        profiler=Profiler(),
        arbiter=Arbiter(capabilities=ALL_LIVE),
        trace=TraceSink("sess-1", directory=tmp_path),
        mode=NodMode.ADAPT,
        ceiling_ms=200,
    )
    assert proxy._ceiling_ms == 1100


# --- gaps found by the Gate 4 mutation run ----------------------------------


@pytest.mark.asyncio
async def test_the_host_override_is_reapplied_at_the_socket(tmp_path: Path) -> None:
    """EC-33 defended twice, and the second defence is not redundant.

    Added because a mutation removing the proxy's own host filter survived: the
    arbiter already excludes host-owned fields from `changed`, so in the ordinary
    path `patch_fields` never names one and the filter looks like belt and braces.

    It is not. `note_host_configuration` is called from the server's socket
    handler while `_handle_turn` is mid-await, so the host can take a field
    **between the arbiter deciding and the proxy sending**. The arbiter's
    exclusion was computed against a snapshot that is already stale by then, and
    only a check at the socket can see the newer fact. Driven here by calling
    `send_patch_upstream` with a field the host has since claimed, which is
    exactly what that race produces.
    """
    proxy, socket, _ = build(tmp_path)
    for order in range(1, 9):
        await proxy._handle_turn(turn(order))
    assert socket.configs, "no baseline patch was sent"
    socket.configs.clear()

    # The race: the host claims a field after the decision, before the send.
    proxy.note_host_configuration(["max_turn_silence"])
    await proxy.send_patch_upstream(("min_turn_silence", "max_turn_silence"))
    assert socket.configs, "the whole patch was dropped rather than filtered"
    assert "max_turn_silence" not in socket.configs[-1], (
        "a field the host claimed between the decision and the send reached the "
        "socket; the arbiter's exclusion was computed on a stale snapshot"
    )
    assert "min_turn_silence" in socket.configs[-1], (
        "the host's claim on one field suppressed the other"
    )


@pytest.mark.asyncio
async def test_the_rate_cap_count_tracks_confirmed_sends_exactly(
    tmp_path: Path,
) -> None:
    """§5's session cap is counted by the proxy, so the count is the wiring.

    Added because a mutation dropping `self._patches_sent += 1` survived. The
    first attempt asserted `len(socket.configs) <= MAX_PATCHES` end to end and
    was **vacuous**: over 199 turns only 4 patches are ever emitted, because
    hysteresis suppresses everything once the window has converged. The cap was
    never the thing suppressing, so removing the count changed nothing observable.

    That is itself worth knowing, and is in the Gate 4 report: with §4's clamps
    bounding the window, ADR-020's and ADR-022's step caps bounding the approach,
    and §5's freeze guard damping oscillation, **`MAX_PATCHES = 24` looks
    unreachable on a monotone profile** and is reachable only through sustained
    oscillation, which the freeze guard exists to stop.

    So this asserts the property the mutation breaks rather than a consequence
    that never arrives: the count the proxy feeds the arbiter equals the number of
    sends the socket confirmed. Counted on confirmation and not on decision, so a
    run of rejected updates cannot spend the session's budget on patches that
    never landed.
    """
    proxy, socket, _ = build(tmp_path)
    for order in range(1, 40):
        await proxy._handle_turn(turn(order, gap=900 + order * 7))
    assert socket.configs, "no patch was sent, so there is nothing to count"
    assert proxy._patches_sent == len(socket.configs), (
        f"the proxy counted {proxy._patches_sent} patches against §5's cap but "
        f"{len(socket.configs)} reached the socket; the cap is fed by this count, "
        "so a miscount silently removes it"
    )

    # And a rejected send must not consume the budget.
    refused = FakeUpstream(reject=99)
    rejecting, _, _ = build(tmp_path, upstream=refused)
    for order in range(1, 12):
        await rejecting._handle_turn(turn(order, gap=900 + order * 7))
    assert not refused.configs
    assert rejecting._patches_sent == 0, (
        "rejected updates consumed the session's patch budget"
    )


@pytest.mark.asyncio
async def test_send_patch_upstream_never_propagates_a_transport_failure(
    tmp_path: Path,
) -> None:
    """EC-32 owns transport failures completely, which is why EC-31 can stop short.

    Added because a mutation widening the controller's `except` to cover the send
    survived — and it survived because it could never fire: `send_patch_upstream`
    catches everything itself. That makes the boundary's placement unobservable
    rather than untested, so this asserts the guarantee the placement rests on.

    If this ever fails, `_handle_turn`'s narrow `except` becomes load-bearing and
    a transport error starts being reported as a controller error, which drops
    the speaker axis for the rest of the call over a dropped packet.
    """
    socket = FakeUpstream(reject=99)
    proxy, _, _ = build(tmp_path, upstream=socket)
    for order in range(1, 9):
        await proxy._handle_turn(turn(order))
    # Direct call too, so the guarantee is not inferred from the wrapper above.
    await proxy.send_patch_upstream(("min_turn_silence", "max_turn_silence"))
    assert proxy.controller_errors == 0
