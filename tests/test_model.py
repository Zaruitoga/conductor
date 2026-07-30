"""
tests/test_model.py — The model against analytic ground truth, and its promises.

The simulator prescribes attitude in closed form and derives the gyro from that
same attitude (simulator/motion.py), so its `reference()` is a genuine external
check rather than the pipeline grading its own homework.

Three claims are worth more than the physics here, because they are what the
whole redesign was for:

  * the same movement gives the same signals whatever the ESP is wired to,
  * the same input gives byte-identical output, twice,
  * a tuned time constant means the same thing at any sample rate.
"""

import math

from model.bus import ModelBus
from model.detectors import DETECTORS, DetectorRegistry, DetectorSpec, threshold
from model.engine import Model
from model.params import PARAMS
from model.registry import DYNAMIC, Registry, SignalSpec
from model.quantities import ACCEL, ATTITUDE_REL
from model.types import EVENT
from simulator.motion import WheelMotion
from config import R_TORE, r_TORE

HZ = 100.0

# ── Packet builders: the same motion, delivered three different ways ─────────


def _super(motion, t, seq, type_id=0x10, with_accel=False):
    """One bundled datagram — attitude and gyro sampled at the same instant."""
    gx, gy, gz = motion.gyro(t)
    qw, qx, qy, qz = motion.quaternion(t)
    ts = int(round(t * 1e6))
    p = {"type": f"super_{type_id - 0x10}", "typeId": type_id, "seq": seq,
         "ts_esp_us": ts, "ts_rx_us": ts,
         "gyro_x": gx, "gyro_y": gy, "gyro_z": gz,
         "game_rv_qw": qw, "game_rv_qx": qx, "game_rv_qy": qy, "game_rv_qz": qz}
    if with_accel:
        ax, ay, az = motion.accel(t)
        p.update(accel_x=ax, accel_y=ay, accel_z=az)
    return [p]


def _simple_pair(motion, t, seq):
    """Two separate datagrams — the same values, unbundled."""
    gx, gy, gz = motion.gyro(t)
    qw, qx, qy, qz = motion.quaternion(t)
    ts = int(round(t * 1e6))
    return [
        {"type": "gyro", "typeId": 0x01, "seq": seq, "ts_esp_us": ts,
         "ts_rx_us": ts, "x": gx, "y": gy, "z": gz},
        {"type": "game_rv", "typeId": 0x07, "seq": seq, "ts_esp_us": ts,
         "ts_rx_us": ts, "qw": qw, "qx": qx, "qy": qy, "qz": qz},
    ]


def _run(scenario="coin", seconds=6.0, hz=HZ, maker=_super):
    motion = WheelMotion(scenario=scenario)
    model  = Model(bus=None)
    frames = []
    for i in range(int(seconds * hz)):
        for packet in maker(motion, i / hz, i):
            frame = model.feed(packet)
            if frame is not None:
                frames.append(frame)
    return motion, model, frames


def _run_with_events(scenario="coin", seconds=6.0, hz=HZ, maker=_super):
    """Like `_run`, but with a real bus so events are observable too."""
    motion = WheelMotion(scenario=scenario)
    bus    = ModelBus()
    events = []
    bus.subscribe_sync("events", (EVENT,), lambda k, o: events.append(o))
    model  = Model(bus=bus)
    frames = []
    for i in range(int(seconds * hz)):
        for packet in maker(motion, i / hz, i):
            frame = model.feed(packet)
            if frame is not None:
                frames.append(frame)
    return motion, model, frames, events


# ── Physics, against the simulator's closed forms ────────────────────────────

def test_straight_position_matches_the_analytic_reference():
    motion, _, frames = _run("straight", seconds=10.0)
    last = frames[-1]
    ref  = motion.reference(last.t_us / 1e6)

    assert abs(last.signals["pos_x"] - ref["px"]) < 1e-6
    assert abs(last.signals["pos_y"] - ref["py"]) < 1e-6
    assert abs(last.signals["pos_z"] - ref["pz"]) < 1e-9


def test_straight_speed_is_the_rolling_radius_times_the_spin():
    """The contact sits R + r from the centre, so that is the rolling radius."""
    _, _, frames = _run("straight", seconds=4.0)
    expected = (R_TORE + r_TORE) * math.radians(180.0)

    for f in frames[100:]:
        assert abs(f.signals["speed_ms"] - expected) < 1e-6
        assert abs(f.signals["spin_rate_dps"] - 180.0) < 1e-6
        assert abs(f.signals["lean_deg"]) < 1e-9
        assert abs(f.signals["height_m"] - (R_TORE + r_TORE)) < 1e-9


def test_coin_holds_its_lean_and_its_height():
    _, _, frames = _run("coin", seconds=6.0)
    expected_height = R_TORE * math.cos(math.radians(20.0)) + r_TORE

    for f in frames[50:]:
        assert abs(f.signals["lean_deg"] - 20.0) < 1e-6
        assert abs(f.signals["height_m"] - expected_height) < 1e-9


def test_coin_precession_matches_the_scenario():
    _, _, frames = _run("coin", seconds=6.0)
    for f in frames[100:]:
        assert abs(f.signals["precession_rate_dps"] - 45.0) < 1e-3


def test_coin_spin_rate_accounts_for_the_lean():
    """
    ω about the axle is φ̇ − ψ̇·sin λ, not φ̇: part of the precession projects
    onto the axle. Checking the exact value rather than "about 180" is what
    would catch a signal that quietly reported the wrong component.
    """
    _, _, frames = _run("coin", seconds=4.0)
    expected = 180.0 - 45.0 * math.sin(math.radians(20.0))
    for f in frames[100:]:
        assert abs(f.signals["spin_rate_dps"] - expected) < 1e-6


def test_coin_trajectory_closes():
    """A rolling coin comes back to its start after one precession period."""
    period = 360.0 / 45.0
    _, _, frames = _run("coin", seconds=period + 0.5)

    start = (frames[0].signals["pos_x"], frames[0].signals["pos_y"])
    late  = [f for f in frames if f.t_us / 1e6 > period * 0.75]
    closest = min(
        late,
        key=lambda f: math.hypot(f.signals["pos_x"] - start[0],
                                 f.signals["pos_y"] - start[1]),
    )
    gap = math.hypot(closest.signals["pos_x"] - start[0],
                     closest.signals["pos_y"] - start[1])

    assert gap < 1e-3, f"trajectory did not close: {gap:.4f} m"
    assert abs(closest.t_us / 1e6 - period) < 0.05


def test_heading_is_perpendicular_to_the_axle():
    """A wheel travels across its axle — an independent check of both signals."""
    _, _, frames = _run("coin", seconds=4.0)
    for f in frames[100:]:
        delta = (f.signals["tilt_dir_deg"] - f.signals["heading_deg"]) % 360.0
        assert abs(delta - 90.0) < 1e-3


# ── The claims the redesign was for ──────────────────────────────────────────

def test_same_signals_whatever_the_esp_is_wired_to():
    """
    The headline property: bundled in super 0, split across simple slots, or
    bundled with an accelerometer in super 3 — the physics comes out identical.

    Before this redesign the model tested `typeId == 0x10` and demanded seven
    field names, so two of these three configurations produced nothing at all.
    """
    watched = ("lean_deg", "height_m", "spin_rate_dps", "speed_ms",
               "pos_x", "pos_y", "precession_rate_dps", "tilt_dir_deg")

    _, _, bundled = _run("coin", maker=_super)
    _, _, split   = _run("coin", maker=_simple_pair)
    _, _, fat     = _run("coin", maker=lambda m, t, s: _super(m, t, s, 0x13, True))

    assert len(bundled) == len(split) == len(fat)
    for a, b, c in zip(bundled, split, fat):
        for name in watched:
            assert a.signals[name] == b.signals[name] == c.signals[name], name


def test_redundant_sources_do_not_double_the_tick_rate():
    """
    An ESP sending a super slot *and* the same sensors as simple slots delivers
    every quantity twice — which is exactly what the simulator does, and what any
    configuration keeping the individual streams for recording will do.

    Untreated, each period produces two ticks a fraction of a millisecond apart,
    so dt alternates between ~0.8 ms and ~9 ms and every rate reads that as real.
    One source must own a quantity.
    """
    def both(motion, t, seq):
        return _super(motion, t, seq) + _simple_pair(motion, t, seq)

    _, model, doubled = _run("coin", seconds=3.0, maker=both)
    _, _, clean = _run("coin", seconds=3.0, maker=_super)

    assert len(doubled) == len(clean), "redundant packets produced extra ticks"
    # The bundled source wins, because only a bundle guarantees attitude and
    # gyro were sampled at the same instant.
    assert model.resolver.sources() == {"attitude_rel": "GAME_RV", "omega": "GYRO"}

    for a, b in zip(doubled[2:], clean[2:]):
        assert a.quality["dt_ms"] == b.quality["dt_ms"]
        assert a.signals["precession_rate_dps"] == b.signals["precession_rate_dps"]


def test_a_source_that_goes_quiet_hands_over():
    """
    Losing the preferred source must fall back, not freeze.

    The bundle owns the attitude while it is arriving; once it stops for longer
    than the gap tolerance, the standalone stream takes the quantity back.
    """
    motion = WheelMotion(scenario="coin")
    model  = Model(bus=None)

    for i in range(100):                       # bundle present
        for p in _super(motion, i / HZ, i) + _simple_pair(motion, i / HZ, i):
            model.feed(p)
    assert model.resolver.sources()["attitude_rel"] == "GAME_RV"
    bundled_before = model.resolver._samples["attitude_rel"].bundled
    assert bundled_before is True

    for i in range(100, 220):                  # the super slot stops
        for p in _simple_pair(motion, i / HZ, i):
            model.feed(p)

    assert model.resolver.sources()["attitude_rel"] == "GAME_RV"
    assert model.resolver._samples["attitude_rel"].bundled is False, \
        "the standalone stream never took over"


def test_adding_a_sensor_only_adds_signals():
    """Wiring the accelerometer in must not disturb anything already working."""
    _, _, without = _run("coin", maker=_super)
    _, _, with_accel = _run("coin", maker=lambda m, t, s: _super(m, t, s, 0x13, True))

    gained = set(with_accel[-1].signals) - set(without[-1].signals)
    assert gained == {"accel_norm_ms2", "accel_shock_ms2"}


def test_the_same_input_gives_the_same_output_twice():
    """
    Determinism, asserted rather than asserted-to-be-true.

    Nothing in the model may read the wall clock, so two runs over identical
    packets must agree exactly — which is what makes tuning a threshold against
    a recording mean anything.
    """
    _, _, first  = _run("spiral", seconds=6.0)
    _, _, second = _run("spiral", seconds=6.0)

    assert len(first) == len(second)
    for a, b in zip(first, second):
        assert a.t_us == b.t_us
        assert a.signals == b.signals
        assert a.pose == b.pose


def test_a_tuned_time_constant_survives_a_rate_change():
    """
    An envelope tuned at 25 Hz must behave the same at 400 Hz.

    This is why every filter is written with `ctx.alpha(tau)` rather than a
    fixed coefficient: otherwise raising the BNO rate would silently retune
    every envelope in the model, and yesterday's settings would be worthless.
    """
    probe_t = 5.0
    values = {}
    for hz in (25.0, 100.0, 400.0):
        _, _, frames = _run("spiral", seconds=6.0, hz=hz)
        nearest = min(frames, key=lambda f: abs(f.t_us / 1e6 - probe_t))
        values[hz] = nearest.signals["motion_slow"]

    spread = max(values.values()) - min(values.values())
    assert spread / values[100.0] < 0.005, f"envelope drifted with rate: {values}"


# ── Availability and containment ─────────────────────────────────────────────

def test_a_sensor_switched_off_stops_being_available():
    """
    Turning a sensor off mid-session must retire the signals that need it.

    Otherwise they keep reporting the last value they ever saw — a plausible,
    perfectly steady number that is simply no longer connected to anything, which
    is the worst kind of wrong on a stage.
    """
    motion = WheelMotion(scenario="coin")
    model  = Model(bus=None)

    def with_accel(m, t, seq):
        return _super(m, t, seq, 0x13, with_accel=True)

    for i in range(100):
        for p in with_accel(motion, i / HZ, i):
            model.feed(p)
    assert "accel_norm_ms2" in model.feed(with_accel(motion, 1.0, 100)[0]).signals

    # The accelerometer drops out; attitude and gyro carry on.
    last = None
    for i in range(101, 500):
        for p in _super(motion, i / HZ, i):
            last = model.feed(p) or last

    assert "accel" not in model.resolver.sources(model._last_tick_us)
    assert "accel_norm_ms2" not in last.signals
    assert "lean_deg" in last.signals, "unrelated signals must be untouched"


def test_a_missing_sensor_is_explained_not_silent():
    _, model, _ = _run("coin", seconds=1.0)      # gyro + game_rv only
    schema = {s["name"]: s for s in model.schema()["signals"]}

    shock = schema["accel_shock_ms2"]
    assert not shock["available"]
    assert "accel" in shock["reason"]
    assert "ACCEL" in shock["reason"], "the reason should name the slot to enable"

    azimuth = schema["azimuth_deg"]
    assert not azimuth["available"]
    assert "attitude_abs" in azimuth["reason"]
    assert "RV" in azimuth["reason"]


def test_switching_a_signal_off_switches_off_what_stands_on_it():
    _, model, _ = _run("coin", seconds=1.0)
    model.registry.set_enabled("speed_ms", False)
    try:
        avail = model.registry.availability(model.resolver.present())
        assert not avail["speed_ms"]["available"]
        assert avail["speed_ms"]["reason"] == "désactivé"
        # heading_deg depends on it, and says so rather than emitting nulls.
        assert not avail["heading_deg"]["available"]
        assert "speed_ms" in avail["heading_deg"]["reason"]
        # An unrelated branch is untouched.
        assert avail["lean_deg"]["available"]
    finally:
        model.registry.set_enabled("speed_ms", True)


def test_a_signal_that_raises_does_not_stop_the_frame():
    """A detector's bug costs its own value and nothing else."""
    registry = Registry()

    def boom(ctx):
        raise RuntimeError("bad maths")

    def fine(ctx):
        return 42.0

    for name, fn in (("boom", boom), ("fine", fine)):
        registry.add(SignalSpec(name=name, fn=fn, kind=DYNAMIC, unit="",
                                range=None, needs=(ATTITUDE_REL,), depends=(),
                                after=(), params=(), doc=""))

    motion = WheelMotion(scenario="coin")
    model  = Model(bus=None, registry=registry)
    frames = [f for i in range(50)
              for p in _super(motion, i / HZ, i)
              if (f := model.feed(p)) is not None]

    assert len(frames) == 50, "frames stopped coming"
    assert all(f.signals["boom"] is None for f in frames)
    assert all(f.signals["fine"] == 42.0 for f in frames)
    assert registry.errors["boom"] == 50
    assert "bad maths" in registry.last_error["boom"]


def test_reset_clears_the_integrators():
    _, model, frames = _run("straight", seconds=3.0)
    assert abs(frames[-1].signals["pos_x"]) > 1.0

    model.reset()
    motion = WheelMotion(scenario="straight")
    after = [f for i in range(3) for p in _super(motion, i / HZ, i)
             if (f := model.feed(p)) is not None]
    assert abs(after[0].signals["pos_x"]) < 1e-9


# ── Detectors: events built on top of signals ────────────────────────────────
# A synthetic detector over a synthetic, hand-driven signal, so the crossing
# logic in model/detectors.py can be checked exactly instead of hoped for from
# whatever the simulator's physics happens to produce.

_DRIVEN: dict = {"v": 0.0}
_T_ON   = PARAMS.declare("test_detector_on_v", default=5.0, min=0.0, max=100.0)
_T_OFF  = PARAMS.declare("test_detector_off_v", default=2.0, min=0.0, max=100.0)
_T_REFR = PARAMS.declare("test_detector_refractory_s", default=0.05, min=0.0, max=10.0)


def _driven_registry() -> Registry:
    registry = Registry()
    registry.add(SignalSpec(
        name="driven", fn=lambda ctx: _DRIVEN["v"], kind=DYNAMIC, unit="",
        range=None, needs=(ATTITUDE_REL,), depends=(), after=(), params=(), doc="",
    ))
    return registry


def _driven_detectors() -> DetectorRegistry:
    detectors = DetectorRegistry()
    detectors.add(DetectorSpec(
        name="fires", fn=threshold("driven", _T_ON, _T_OFF, _T_REFR),
        source="driven", needs=(ATTITUDE_REL,), params=(_T_ON, _T_OFF, _T_REFR),
        doc="",
    ))
    return detectors


def test_threshold_fires_once_per_crossing_and_respects_hysteresis():
    motion = WheelMotion(scenario="coin")
    bus, events = ModelBus(), []
    bus.subscribe_sync("events", (EVENT,), lambda k, o: events.append(o))
    model = Model(bus=bus, registry=_driven_registry(), detectors=_driven_detectors())

    # below on(5) → above on(5): fires once → stays above: no refire while
    # armed=False → drops to below off(2): rearms → above on(5) again: fires.
    schedule = [0.0] * 10 + [6.0] * 10 + [1.0] * 10 + [6.0] * 10
    for i, v in enumerate(schedule):
        _DRIVEN["v"] = v
        for p in _super(motion, i / HZ, i):
            model.feed(p)

    assert [e.name for e in events] == ["fires", "fires"]
    assert events[0].payload == {"value": 6.0}
    assert events[1].id == events[0].id + 1


def test_threshold_refractory_blocks_a_rapid_rearm_but_not_a_later_one():
    """
    Isolates the refractory window specifically, as distinct from hysteresis:
    a value that dips to `off` (re-arming) and crosses `on` again well within
    `refractory` must not refire yet — but the same crossing, given enough
    elapsed time, must.
    """
    motion = WheelMotion(scenario="coin")
    bus, events = ModelBus(), []
    bus.subscribe_sync("events", (EVENT,), lambda k, o: events.append(o))
    model = Model(bus=bus, registry=_driven_registry(), detectors=_driven_detectors())

    # t=0.01 fires; t=0.03 dips to `off` and re-arms; t=0.04 re-crosses `on`
    # just 0.03 s later — inside the 0.05 s refractory, so it must not refire.
    schedule = {0: 0.0, 1: 6.0, 2: 6.0, 3: 1.0, 4: 6.0}
    for i in range(5):
        _DRIVEN["v"] = schedule[i]
        for p in _super(motion, i / HZ, i):
            model.feed(p)
    assert len(events) == 1, "a rapid re-crossing inside the refractory window fired"

    # Held above `on` well past the 0.05 s refractory: the crossing blocked
    # above must fire as soon as the cooldown elapses.
    _DRIVEN["v"] = 6.0
    for i in range(5, 20):
        for p in _super(motion, i / HZ, i):
            model.feed(p)
    assert len(events) == 2, "expected the blocked crossing to fire once refractory elapsed"


def test_detector_availability_names_the_missing_quantity():
    reg = DetectorRegistry()
    reg.add(DetectorSpec(name="needs_accel", fn=lambda ctx: None, source="x",
                         needs=(ACCEL,), params=(), doc=""))

    absent = reg.availability(frozenset())
    assert not absent["needs_accel"]["available"]
    assert "accel" in absent["needs_accel"]["reason"]

    present = reg.availability(frozenset({ACCEL}))
    assert present["needs_accel"]["available"]


def test_a_detector_that_raises_does_not_stop_the_frame():
    """A detector's bug costs its own event and nothing else — same contract
    as a signal's (test_a_signal_that_raises_does_not_stop_the_frame)."""
    detectors = DetectorRegistry()

    def boom(ctx):
        raise RuntimeError("bad threshold")

    detectors.add(DetectorSpec(name="boom", fn=boom, source="driven",
                               needs=(ATTITUDE_REL,), params=(), doc=""))

    motion = WheelMotion(scenario="coin")
    model  = Model(bus=None, registry=_driven_registry(), detectors=detectors)
    frames = [f for i in range(50)
              for p in _super(motion, i / HZ, i)
              if (f := model.feed(p)) is not None]

    assert len(frames) == 50, "frames stopped coming"
    assert detectors.errors["boom"] == 50
    assert "bad threshold" in detectors.last_error["boom"]


def test_detector_name_cannot_collide_with_a_signal():
    """They would share ctx.state and corrupt each other's memory."""
    try:
        DETECTORS.add(DetectorSpec(name="lean_deg", fn=lambda ctx: None,
                                   source="x", needs=(), params=(), doc=""))
        assert False, "a detector named after a declared signal must be refused"
    except ValueError:
        pass


def test_no_spurious_impact_or_burst_on_steady_motion():
    """A steady, noiseless coin roll must not look like a shock or a burst."""
    def with_accel(m, t, s):
        return _super(m, t, s, 0x13, with_accel=True)

    _, _, _, events = _run_with_events("coin", seconds=10.0, maker=with_accel)
    names = {e.name for e in events}
    assert "impact" not in names
    assert "burst" not in names


def test_revolution_fires_once_per_turn_matching_the_prescribed_spin_rate():
    """
    spin_deg is exactly the prescribed spin angle φ(t) mod 360 — precession and
    lean cancel out of atan2(u[0], u[1]) by construction (see geometry.py's own
    comment on spin_deg) — so the wrap period is exactly 360/spin_dps,
    independent of lean or precession. That closed form, taken straight from
    WheelMotion's own constructor parameter rather than from any signal the
    model computes, is the external check here — the same role
    WheelMotion.reference() plays for position elsewhere in this file.
    """
    motion = WheelMotion(scenario="coin")     # spin_dps defaults to 180.0
    seconds = 10.0
    _, _, _, events = _run_with_events("coin", seconds=seconds, maker=_super)

    revolutions = [e for e in events if e.name == "revolution"]
    expected_period = 360.0 / 180.0
    expected_count  = int(seconds / expected_period)

    assert abs(len(revolutions) - expected_count) <= 1
    assert all(e.payload["direction"] == 1 for e in revolutions), \
        "a steadily forward-spinning wheel must wrap the same way every time"

    gaps = [b.t_us - a.t_us for a, b in zip(revolutions, revolutions[1:])]
    for gap in gaps:
        assert abs(gap / 1e6 - expected_period) < 1.0 / HZ, \
            f"revolution spacing drifted from the prescribed spin period: {gap / 1e6}s"

    ids = [e.id for e in revolutions]
    assert ids == sorted(set(ids)), "event ids must be unique and increasing"


def test_event_ids_survive_a_reset_but_seq_does_not():
    """
    Event.id is monotonic across the *process*, unlike Frame.seq — two replay
    passes must never hand out the same event id, or a consumer counting on the
    id to prove nothing was missed would misread a real loss as none.
    """
    _, model, frames, events = _run_with_events("coin", seconds=2.5, maker=_super)
    assert frames[-1].seq == len(frames)
    assert len(events) >= 1, "expected at least one revolution in 2.5 s at 2 s/turn"
    ids_before = [e.id for e in events]

    model.reset()

    motion2 = WheelMotion(scenario="coin")
    post_reset = []
    for i in range(250):                      # another 2.5 s: at least one more turn
        for p in _super(motion2, i / HZ, i):
            f = model.feed(p)
            if f is not None:
                post_reset.append(f)

    assert post_reset[0].seq == 1, "seq must restart after reset"
    assert len(events) > len(ids_before), "expected new events after the reset"
    assert min(e.id for e in events[len(ids_before):]) > max(ids_before), \
        "an event id was reused after reset"


def main() -> None:
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"  ok  {name}")


if __name__ == "__main__":
    main()
