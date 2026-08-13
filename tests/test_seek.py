"""
tests/test_seek.py — Resuming a take at a chosen instant.

The whole mechanism rests on one claim, and it is measurable exactly: a model
re-fed for a few seconds before an instant is in the *same state* as one fed
from row 0, for everything except the horizontal position.  If that is false the
design is wrong, not merely slow — so it is checked here against the simulator,
whose attitude is prescribed in closed form and whose gyro is central-differenced
from that same attitude (simulator/motion.py).  Real takes cannot serve: they are
under `sessions/`, which is gitignored.

What "agrees" means below is **0,1 % of the signal's declared full scale**.  A
purely relative tolerance is meaningless for a signal that passes through zero —
and half of these do, every revolution.  The declared range is the resolution
anything downstream reads the signal at, so it is the honest yardstick.  The
tolerance is not taken on trust either: `test_a_warm_up_too_short_does_not_converge`
runs the same comparison with a fraction of the window and requires it to *fail*,
which is what makes the passing case evidence rather than a loose bound.

Like tests/test_playback_timing.py, this exercises the arithmetic of the instant
aimed at and of the window — never the replay loop's `asyncio.sleep` cadence,
which is not what any of this can get wrong.  `_settle` is called directly, the
way tests/test_osc.py calls `_cadence_step`.
"""

import asyncio
import csv
import math
import os
import shutil
import tempfile

import config
import core
from model.bus import ModelBus
from model.detectors import DetectorRegistry, DetectorSpec
from model.engine import Model
from model.params import PARAMS, ParamStore
from model.registry import SIGNALS
from model.types import EVENT, META
from simulator.motion import WheelMotion
from storage.csv_logger import CSVLogger
from storage.playback_engine import PlaybackEngine, row_index_at, row_to_packet
from storage.pose_track import PoseTrackService, PoseTrackWriter, compute_pose_track
from storage.seek import (
    MIN_WINDOW_S, WARMUP_TAUS, warm_to, warmup_window_s,
)
from storage.session_manager import SessionManager

# 25 Hz, not 100: the window is derived from τ, and a τ means the same thing at
# any sample rate by construction (`ctx.alpha`, asserted in tests/test_model.py),
# so a quarter of the packets prove the same claim in a quarter of the time.
HZ      = 25.0
SECONDS = 40.0
TARGET_S = 30.0        # far from the spiral's upright instants (t = 0, 20, 40 s)

_TMP_DIRS: list[str] = []


# ── A take, written by the real logger, read by the real decoder ─────────────

def _super(motion, t, seq):
    """
    One bundled datagram: attitude, gyro and accel at the same instant.

    `dep_slots` is not decoration — CSVLogger skips a super packet that arrived
    before the layout was known, so without it the take would be empty.
    """
    gx, gy, gz = motion.gyro(t)
    ax, ay, az = motion.accel(t)
    qw, qx, qy, qz = motion.quaternion(t)
    ts = int(round(t * 1e6))
    return {"type": "super_0", "typeId": 0x10, "seq": seq,
            "ts_esp_us": ts, "ts_rx_us": ts, "dep_slots": [0, 1, 6],
            "gyro_x": gx, "gyro_y": gy, "gyro_z": gz,
            "accel_x": ax, "accel_y": ay, "accel_z": az,
            "game_rv_qw": qw, "game_rv_qx": qx,
            "game_rv_qy": qy, "game_rv_qz": qz}


class _Take:
    """A throwaway session holding one recorded take."""

    def __init__(self, scenario="spiral", seconds=SECONDS, hz=HZ):
        self.root = tempfile.mkdtemp(prefix="conductor-seek-")
        _TMP_DIRS.append(self.root)
        self.sm  = SessionManager(os.path.join(self.root, "sessions"))
        session  = self.sm.create_session(title="banc")
        take_dir, meta = self.sm.new_take(title="saut")

        motion = WheelMotion(scenario=scenario)
        logger = CSVLogger(self.sm)
        logger.start(take_dir, meta)
        for i in range(int(seconds * hz)):
            logger.write(_super(motion, i / hz, i))
        logger.stop()

        self.motion  = motion          # kept: its reference() is ground truth
        self.session = session.name
        self.take    = meta.name
        self.csv     = self.sm.csv_path(take_dir)
        self.pose    = self.sm.pose_path(take_dir)
        with open(self.csv, newline="") as f:
            self.rows = list(csv.DictReader(f))


_TAKE: _Take | None = None


def _take() -> _Take:
    """The take every model-level test below shares — recorded once."""
    global _TAKE
    if _TAKE is None:
        _TAKE = _Take()
    return _TAKE


def _engine(take: _Take) -> PlaybackEngine:
    """An engine with the take loaded, and no replay running."""
    engine = PlaybackEngine(take.sm)
    engine._load(take.csv)
    return engine


def _row_at(take: _Take, t_s: float) -> int:
    return int(round(t_s * HZ))


def _window(take: _Take, from_s: float, to_s: float) -> tuple[list[dict], float]:
    """
    A span of the take's rows, and the take time they start at — the pair
    `PlaybackEngine.warmup_rows` hands over, for the tests that do not need an
    engine.  The origin matters: a warm-up anchored at zero would put the model
    somewhere else in the take entirely.
    """
    return take.rows[_row_at(take, from_s):_row_at(take, to_s)], from_s


def _feed(model, rows) -> None:
    for row in rows:
        packet = row_to_packet(row)
        if packet is not None:
            model.feed(packet)


def _next_frame(model, rows):
    """Feed rows until one produces a tick, and return that frame."""
    for row in rows:
        packet = row_to_packet(row)
        if packet is None:
            continue
        frame = model.feed(packet)
        if frame is not None:
            return frame
    raise AssertionError("no tick came out of the remaining rows")


# ── What "agrees" means ──────────────────────────────────────────────────────

def _tolerance(name: str) -> float:
    """0,1 % of the signal's declared full scale — see the module docstring."""
    spec = SIGNALS.spec(name)
    if spec is None or not spec.range:
        return 1e-3
    return 1e-3 * abs(spec.range[1] - spec.range[0])


def _disagreements(reference, warmed) -> dict[str, tuple]:
    """Signals where the two frames do not agree, with both values."""
    out = {}
    for name, ref in reference.signals.items():
        got = warmed.signals.get(name)
        if ref is None or got is None:
            if ref is not got:
                out[name] = (ref, got)
            continue
        if abs(ref - got) > _tolerance(name):
            out[name] = (ref, got)
    return out


# ── The window ───────────────────────────────────────────────────────────────

def test_the_window_follows_the_declared_taus():
    """
    Derived, not chosen: raising a time constant has to widen the window on its
    own, or whichever signal outgrew it converges silently short.
    """
    store = ParamStore()
    store.declare("slow_env_s", default=2.0, min=0.1, max=30.0, unit="s", tau=True)
    store.declare("a_threshold", default=99.0, min=0.0, max=999.0)

    assert store.max_tau_s() == 2.0, "a threshold is not a memory"
    assert warmup_window_s(store) == WARMUP_TAUS * 2.0

    store.set("slow_env_s", 6.0)
    assert warmup_window_s(store) == WARMUP_TAUS * 6.0

    # Every declared τ turned right down still leaves `accel_shock_ms2`'s
    # hardcoded 0.5 s baseline, which no store can see. Hence the floor.
    store.set("slow_env_s", 0.1)
    assert warmup_window_s(store) == MIN_WINDOW_S


def test_the_models_own_time_constants_are_declared_as_such():
    """
    A guard on the flag, not a second list of τ.

    It cannot prove a τ was tagged — only the declaration knows that — but it
    catches the one mistake that is easy to make: adding `something_tau_s` next
    to the code that reads it and forgetting `tau=True`, which would shorten
    every warm-up from then on without a word.
    """
    specs = {s["name"]: s for s in PARAMS.schema()}
    for name, spec in specs.items():
        if "_tau_" in name:
            assert spec["tau"], f"{name} looks like a τ and is not declared as one"

    assert specs["motion_tau_slow_s"]["tau"]
    # Not named "tau" at all, and the longest memory in the model as it stands.
    assert specs["mag_trust_release_s"]["tau"]
    assert PARAMS.max_tau_s() >= specs["motion_tau_slow_s"]["value"]


# ── The instant aimed at ─────────────────────────────────────────────────────

def test_a_target_outside_the_take_lands_on_its_edges():
    """Past the end plays the end, before the start plays the start."""
    offsets = [i * 10_000 for i in range(500)]          # 5 s at 100 Hz

    assert row_index_at(offsets, -12.0) == 0
    assert row_index_at(offsets, 99.0) == 499
    assert row_index_at(offsets, 2.5) == 250
    assert row_index_at([], 3.0) == 0, "an unloaded take must not raise"

    engine = _engine(_take())
    assert engine.clamp_time(-3.0) == 0.0
    assert engine.clamp_time(1e6) == engine.total_s
    assert engine.clamp_time(12.5) == 12.5


def test_the_target_is_a_row_of_the_take_not_the_time_asked_for():
    """
    A take is sampled; a cursor is not.  The replay resumes on a real row, and
    the warm-up is told *that* instant — otherwise the pose planted and the
    packets replayed would be a fraction of a period apart.
    """
    take   = _take()
    engine = _engine(take)
    engine.active = True

    seen = []

    async def on_seek(t_s):
        seen.append(t_s)

    engine.seek(2.517)                       # between two rows at 25 Hz
    i, t0 = asyncio.run(engine._settle(0, on_seek, 1.0, 0.0))

    assert seen == [2.52], "the warm-up was aimed between two samples"
    assert i == _row_at(take, 2.52)
    assert engine.elapsed_s == 2.52
    assert engine.seek_target_s is None, "the request was not consumed"


def test_only_the_last_request_of_a_drag_costs_a_warm_up():
    """Dragging a cursor produces a stream of these; one slot, not a queue."""
    engine = _engine(_take())

    assert engine.seek(1e6) == engine.total_s     # bounded on the way in
    engine.seek(4.0)
    assert engine.seek_target_s == 4.0


# ── The warm-up itself ───────────────────────────────────────────────────────

_REFERENCE: tuple | None = None


def _reference():
    """
    The take played from row 0 to the target: (row index, its take time, frame).

    Computed once and kept — it is the same run for every comparison below, and
    it is the most expensive thing in this module.
    """
    global _REFERENCE
    if _REFERENCE is None:
        take   = _take()
        engine = _engine(take)
        i      = row_index_at(engine._offsets, TARGET_S)
        full   = Model(bus=None)
        _feed(full, take.rows[:i])
        _REFERENCE = (i, engine._offsets[i] / 1e6,
                      _next_frame(full, take.rows[i:]))
    return _REFERENCE


def _warmed_and_full(window_s: float):
    """
    The same instant reached two ways: from row 0, and from `window_s` before it.

    Both frames come out of the *same row* — the one the replay would resume on
    — so anything they disagree about is state, not input.
    """
    take   = _take()
    engine = _engine(take)
    i, target, reference = _reference()

    rows, origin = engine.warmup_rows(target, window_s)
    warm, report = warm_to(Model(bus=None), rows, target,
                           origin_s=origin, window_s=window_s)

    return reference, _next_frame(warm, take.rows[i:]), report, warm


def test_a_warmed_model_agrees_with_a_run_from_row_zero():
    reference, warmed, report, _ = _warmed_and_full(warmup_window_s())

    # Same instant on the same timeline: a warm-up that anchored its clock at
    # zero would land the replay at a `frame.t` of a few seconds, whatever the
    # cursor said.
    assert reference.t_us == warmed.t_us, "the two frames are not the same instant"
    assert report.rows == int(warmup_window_s() * HZ)

    # The attitude is stateless — it comes out of the packet — so the whole
    # quaternion is not merely close but identical.
    for c in ("qw", "qx", "qy", "qz"):
        assert reference.pose[c] == warmed.pose[c]

    off = _disagreements(reference, warmed)
    assert set(off) <= {"pos_x", "pos_y"}, f"did not converge: {off}"

    # And not merely equal to the other run: the height is analytic in the
    # simulator, so the warmed model is also checked against something outside
    # this pipeline altogether — two runs agreeing on the same wrong number
    # would be no comfort at all.
    truth = _take().motion.reference(warmed.t_us / 1e6)
    assert abs(warmed.signals["pos_z"] - truth["pz"]) < 1e-9


def test_the_horizontal_position_is_the_one_thing_that_never_converges():
    """
    Not a shortcoming of the window — a path integral has no forgetting to
    exploit.  It is why the pose track exists at all (ADR 0004), so this failure
    is asserted rather than tolerated: the day it converges by itself, the seed
    is dead weight and someone should be told.
    """
    reference, warmed, _, _ = _warmed_and_full(warmup_window_s())

    off = _disagreements(reference, warmed)
    assert "pos_x" in off and "pos_y" in off
    assert warmed.pose["x"] != reference.pose["x"]
    # The height is a closed form, not an integral, and comes back exactly.
    assert abs(warmed.pose["z"] - reference.pose["z"]) < 1e-12


def test_a_warm_up_too_short_does_not_converge():
    """
    The control on the tolerance above. With a fifth of a second of warm-up the
    slow envelopes are still at their first sample, and the comparison must
    notice — otherwise the passing case would only mean the bound is loose.
    """
    reference, warmed, _, _ = _warmed_and_full(0.2)

    off = _disagreements(reference, warmed)
    assert set(off) - {"pos_x", "pos_y"}, "the tolerance cannot see an unwarmed model"


def test_the_cost_of_a_jump_is_the_window_and_nothing_else():
    """
    What makes a jump affordable: the rows re-fed are set by the window and the
    sample rate, never by the take's length or by how far the cursor moved.
    """
    engine = _engine(_take())
    window = 5.0

    early, early_from = engine.warmup_rows(8.0, window)
    late,  late_from  = engine.warmup_rows(35.0, window)

    assert len(early) == len(late) == int(window * HZ)
    assert (early_from, late_from) == (3.0, 30.0)
    assert len(engine._rows) == int(SECONDS * HZ), "the take is far longer"

    # Landing inside the first window is bounded by row 0 — cheaper, never more.
    head, from_s = engine.warmup_rows(2.0, window)
    assert len(head) == int(2.0 * HZ) and from_s == 0.0


# ── Nothing of the warm-up escapes ───────────────────────────────────────────

def _always_fires() -> DetectorRegistry:
    detectors = DetectorRegistry()
    detectors.add(DetectorSpec(name="cries_wolf", fn=lambda ctx: {"n": 1},
                               source="", needs=(), params=(), doc=""))
    return detectors


def test_no_event_of_the_warm_up_reaches_the_bus():
    """
    Re-feeding makes the detectors fire — that is the point, their armed state
    is part of what is being restored.  Those events are not observations of
    anything happening now, and a jump that let them out would send a handful of
    impacts into Live at every drag of the cursor (ADR 0004).
    """
    take   = _take()
    bus, events = ModelBus(), []
    bus.subscribe_sync("events", (EVENT,), lambda kind, obj: events.append(obj))

    live = Model(bus=bus, detectors=_always_fires())
    _feed(live, take.rows[:40])
    fired_live = len(events)
    assert fired_live > 0, "the detector never fired — the test proves nothing"

    rows, origin = _window(take, 4.0, 16.0)
    warm, _ = warm_to(live, rows, 16.0, origin_s=origin)

    assert len(events) == fired_live, "the warm-up published on the live bus"
    assert warm.bus is None, "a twin with a bus is one filter away from a leak"


def test_the_event_numbering_survives_the_substitution():
    """
    `Event.id` is monotonic over the whole process — it is what lets a consumer
    prove it missed nothing (model/types.py).  A fresh instance starts its own
    numbering at zero, so a jump would reissue ids already seen, and the next
    one after the substitution has to be the next one, with no gap: a gap is
    read downstream as a lost event.
    """
    take = _take()
    bus, events = ModelBus(), []
    bus.subscribe_sync("events", (EVENT,), lambda kind, obj: events.append(obj))

    live = Model(bus=bus, detectors=_always_fires())
    _feed(live, take.rows[:40])
    assert live._event_id == len(events) > 0

    rows, origin = _window(take, 4.0, 16.0)
    warm, _ = warm_to(live, rows, 16.0, origin_s=origin)
    assert warm._event_id == 0, "a fresh instance starts its own numbering"

    warm.continue_from(live)
    assert warm._event_id == live._event_id
    assert warm.seq == live.seq, "a jump is the same pass, not a new run"

    # And the first event out of the substituted model carries on the count —
    # `continue_from` took over the bus as well, so it publishes where the
    # model it replaced did.
    assert warm.bus is bus
    frame = _next_frame(warm, take.rows[_row_at(take, 16.0):])
    assert events[-1].id == live._event_id + 1
    assert frame.seq == live.seq + 1


# ── The seed ─────────────────────────────────────────────────────────────────

def _with_track() -> _Take:
    """The shared take, with its pose track computed once."""
    take = _take()
    if not os.path.exists(take.pose):
        compute_pose_track(take.csv, take.pose)
    return take


def test_the_seed_puts_the_wheel_where_the_take_left_it():
    """
    Without it the wheel teleports at the instant play is pressed, between the
    position the sweep is showing and the one the integrator happened to land
    on.  The track *is* the state exponential forgetting does not give back.
    """
    take   = _with_track()
    engine = _engine(take)
    i, target, reference = _reference()
    window = warmup_window_s()

    rows, origin = engine.warmup_rows(target, window)
    seeded, report = warm_to(Model(bus=None), rows, target, origin_s=origin,
                             track_path=take.pose, window_s=window)
    assert report.seeded, report.reason
    assert report.geometry_matches
    frame = _next_frame(seeded, take.rows[i:])

    # The track stores the pose as f4 and holds the tick *before* the one the
    # replay resumes on, so agreement is to the millimetre, not to the bit.
    assert abs(frame.pose["x"] - reference.pose["x"]) < 1e-3
    assert abs(frame.pose["y"] - reference.pose["y"]) < 1e-3

    unseeded, _ = warm_to(Model(bus=None), rows, target, origin_s=origin,
                          window_s=window)
    naked = _next_frame(unseeded, take.rows[i:])
    assert abs(naked.pose["x"] - reference.pose["x"]) > 1e-2, (
        "the position came back on its own — the seed would be proving nothing"
    )


def test_a_take_with_no_track_still_seeks():
    """
    A track that was never computed, or is not readable, is a degradation and
    not a fault: the jump happens, the wheel simply resumes from where the
    warm-up's own integration landed. The reason is reported rather than raised.
    """
    take = _take()
    rows, origin = _window(take, 4.0, 16.0)

    _, absent = warm_to(Model(bus=None), rows, 16.0, origin_s=origin,
                        track_path=None)
    assert not absent.seeded and absent.reason
    assert absent.geometry_matches is None

    missing = os.path.join(take.root, "nope.bin")
    _, gone = warm_to(Model(bus=None), rows, 16.0, origin_s=origin,
                      track_path=missing)
    assert not gone.seeded and gone.reason

    junk = os.path.join(take.root, "junk.bin")
    with open(junk, "wb") as f:
        f.write(b"not a pose track at all, really not")
    _, unreadable = warm_to(Model(bus=None), rows, 16.0, origin_s=origin,
                            track_path=junk)
    assert not unreadable.seeded and "illisible" in unreadable.reason


def test_a_track_still_filling_seeds_only_where_it_reaches():
    """
    A partial track holds real poses up to where it stopped.  Before that point
    it is as good as a finished one; past it, its last record is from somewhere
    else in the take and planting it would be worse than not seeding at all.
    """
    take    = _take()
    partial = os.path.join(take.root, "partial.bin")
    writer  = PoseTrackWriter(partial, config.R_TORE, config.r_TORE)
    for i in range(500):                      # 20 s at 25 Hz, no completion flag
        writer.append(i / HZ, 1.0, 0.0, 0.0, 0.0, 3.0 + i, 7.0, 0.5)
    writer._file.flush()

    rows, origin = _window(take, 4.0, 16.0)
    _, inside = warm_to(Model(bus=None), rows, 16.0, origin_s=origin,
                        track_path=partial)
    assert inside.seeded, inside.reason

    rows, origin = _window(take, 22.0, 34.0)          # past where it stopped
    _, beyond = warm_to(Model(bus=None), rows, 34.0, origin_s=origin,
                        track_path=partial)
    assert not beyond.seeded and "incomplète" in beyond.reason


def test_a_track_at_another_geometry_seeds_and_says_so():
    """
    Its positions are wrong by that ratio — and they are the very positions the
    sweep is drawing from the same file.  Refusing to seed would trade a known
    scale error for a visible teleport, so ADR 0004's rule applies: report the
    mismatch, do not repair it behind the operator's back.
    """
    take  = _take()
    stale = os.path.join(take.root, "stale.bin")
    with PoseTrackWriter(stale, config.R_TORE * 1.05, config.r_TORE) as writer:
        for i in range(500):
            writer.append(i / HZ, 1.0, 0.0, 0.0, 0.0, 3.0, 7.0, 0.5)

    rows, origin = _window(take, 4.0, 16.0)
    warm, report = warm_to(Model(bus=None), rows, 16.0, origin_s=origin,
                           track_path=stale)

    assert report.seeded
    assert report.geometry_matches is False
    assert math.isclose(warm.node_state("pos_x")["px"], 3.0)


# ── The substitution, as the orchestrator performs it ────────────────────────

def test_the_live_model_is_replaced_and_the_timeline_says_so():
    """
    The wiring, end to end: `core.seek_model` is what the replay loop awaits.

    Three things have to happen together here, and none of them belongs to the
    warm-up itself — they are about the *live* model.  The instance is swapped
    with no `await` in between, so `processing_loop` cannot slip a packet
    between the two; the event numbering carries over; and a `reset` meta goes
    out because the timeline has just moved, which is what makes `ScopeRing`
    drop a history that would otherwise straddle the jump and `OscBridge`
    forget a deadband from before it.
    """
    take   = _with_track()
    engine = _engine(take)
    engine.session, engine.take = take.session, take.take

    saved = (core.model, core.bus, core.playback_engine, core.pose_tracks,
             core.last_seek)
    bus, seen = ModelBus(), []
    bus.subscribe_sync("probe", (EVENT, META),
                       lambda kind, obj: seen.append((kind, obj)))
    try:
        core.bus             = bus
        core.model           = Model(bus=bus, detectors=_always_fires())
        core.playback_engine = engine
        core.pose_tracks     = PoseTrackService(take.sm)
        core.last_seek       = None

        _feed(core.model, take.rows[:40])
        before = core.model
        fired  = [obj for kind, obj in seen if kind == EVENT]
        assert fired, "no event before the jump — the leak check proves nothing"

        asyncio.run(core.seek_model(TARGET_S))

        assert core.model is not before, "the live model was not replaced"
        assert core.model.bus is bus, "the new model publishes nowhere"
        assert core.model._event_id == before._event_id
        assert [obj for kind, obj in seen if kind == EVENT] == fired, (
            "the warm-up's events reached the bus"
        )
        assert any(kind == META and obj.topic == "reset" for kind, obj in seen)

        report = core.last_seek
        assert report["error"] is None, report["error"]
        assert report["seeded"], report["reason"]
        assert report["rows"] == int(warmup_window_s() * HZ)
    finally:
        (core.model, core.bus, core.playback_engine, core.pose_tracks,
         core.last_seek) = saved


def main() -> None:
    try:
        for name, fn in sorted(globals().items()):
            if name.startswith("test_") and callable(fn):
                fn()
                print(f"  ok  {name}")
    finally:
        for d in _TMP_DIRS:
            shutil.rmtree(d, ignore_errors=True)


if __name__ == "__main__":
    main()
