"""
tests/test_osc.py — The OSC bridge's promises, checked without a real Live.

The whole point of osc/ is "remap a signal to an OSC address without touching
code" (see CLAUDE.md). That promise rests on a handful of properties that are
easy to get subtly wrong: a stopped signal must never masquerade as a zero, a
stable value must not flood the wire, a trigger must never be capped or
smoothed the way a continuous value is, and a replay restart must not leave a
route believing it already sent the value it is about to send again.

`_FakeLive` plays the same role `_Outbox` plays in tests/test_ws_outbox.py: a
minimal stand-in exposing exactly the one method (`send`) the code under test
calls, so what is checked is OscBridge's own logic, not a real UDP socket or
AbletonOSC.

What is deliberately not tested here: `OscBridge.run()`'s own `asyncio.sleep`
cadence. Exactly like tests/test_playback_timing.py's pacing loop, real-time
sleeps are not what this code can get wrong — `_cadence_step()` is the part
worth checking, and it needs no clock to check it.
"""

import asyncio
import json
import tempfile
from dataclasses import replace

from model.bus import ModelBus
from model.types import EVENT, FRAME, META, Event, Frame, Meta
from osc import targets
from osc.bridge import OscBridge, read_source, transform
from osc.routes import KIND_EVENT, KIND_SIGNAL, RouteTable


# ── Fixtures ──────────────────────────────────────────────────────────────────


class _FakeLive:
    """Records every send; nothing else. See module docstring."""

    def __init__(self):
        self.sent = []   # list[(address, args)]

    def send(self, address, args):
        self.sent.append((address, list(args)))


def _frame(signals, pose=None, seq=0, t_us=0):
    return Frame(t_us=t_us, seq=seq, pose=pose or {}, signals=dict(signals))


def _signal_route(table, source="speed_ms", **overrides):
    fields = dict(
        kind=KIND_SIGNAL, source=source, target="custom", address="/test/addr",
        args=[], in_min=0.0, in_max=1.0, out_min=0.0, out_max=1.0,
        clamp=True, invert=False, deadband=0.0,
    )
    fields.update(overrides)
    return table.create(**fields)


def _event_route(table, source="impact", **overrides):
    fields = dict(
        kind=KIND_EVENT, source=source, target="custom", address="/test/event",
        args=[], payload_field=None,
    )
    fields.update(overrides)
    return table.create(**fields)


def _bridge(table=None, live=None, rate_hz=1000.0):
    return OscBridge(table or RouteTable(), live or _FakeLive(), rate_hz=rate_hz)


# ── transform(): the in→out mapping, clamp, invert ───────────────────────────


def test_transform_maps_the_input_range_onto_the_output_range():
    route = _signal_route(RouteTable(), in_min=0.0, in_max=10.0, out_min=0.0, out_max=100.0)
    assert transform(route, 0.0) == 0.0
    assert transform(route, 10.0) == 100.0
    assert transform(route, 5.0) == 50.0


def test_transform_clamps_outside_the_input_range_by_default():
    route = _signal_route(RouteTable(), in_min=0.0, in_max=10.0, out_min=0.0, out_max=100.0)
    assert transform(route, -5.0) == 0.0
    assert transform(route, 50.0) == 100.0


def test_transform_without_clamp_extrapolates_past_the_output_range():
    route = _signal_route(RouteTable(), in_min=0.0, in_max=10.0, out_min=0.0, out_max=100.0,
                          clamp=False)
    assert transform(route, 20.0) == 200.0
    assert transform(route, -10.0) == -100.0


def test_transform_invert_flips_the_output():
    route = _signal_route(RouteTable(), in_min=0.0, in_max=10.0, out_min=0.0, out_max=100.0,
                          invert=True)
    assert transform(route, 0.0) == 100.0
    assert transform(route, 10.0) == 0.0
    assert transform(route, 2.5) == 75.0


def test_transform_guards_a_degenerate_input_range_from_a_tolerantly_loaded_profile():
    """`in_min == in_max` cannot come from create()/update() — osc/routes.py's
    _validate refuses it — but a profile loaded tolerantly can still carry
    one (osc/routes.py's module docstring). transform() must survive it rather
    than divide by zero."""
    route = replace(_signal_route(RouteTable(), out_min=2.0, out_max=8.0),
                    in_min=5.0, in_max=5.0)
    assert transform(route, 5.0) == 2.0
    assert transform(route, 999.0) == 2.0


def test_read_source_understands_the_pose_prefix():
    frame = _frame({}, pose={"x": 1.5, "y": -2.0})
    assert read_source(frame, "pose.x") == 1.5
    assert read_source(frame, "pose.y") == -2.0
    assert read_source(frame, "pose.z") is None


# ── Deadband: stable once, crossing again ────────────────────────────────────


def test_a_stable_value_is_suppressed_by_the_deadband_after_its_first_send():
    live = _FakeLive()
    table = RouteTable()
    _signal_route(table, source="speed_ms", deadband=0.5,
                 in_min=0.0, in_max=10.0, out_min=0.0, out_max=10.0)
    bridge = _bridge(table, live)

    bridge._on_frame(FRAME, _frame({"speed_ms": 3.0}))
    bridge._cadence_step()
    bridge._on_frame(FRAME, _frame({"speed_ms": 3.1}))   # inside the deadband
    bridge._cadence_step()

    assert len(live.sent) == 1
    assert bridge.stats["skipped_deadband"] == 1


def test_a_crossing_past_the_deadband_sends_again():
    live = _FakeLive()
    table = RouteTable()
    _signal_route(table, source="speed_ms", deadband=0.5,
                 in_min=0.0, in_max=10.0, out_min=0.0, out_max=10.0)
    bridge = _bridge(table, live)

    bridge._on_frame(FRAME, _frame({"speed_ms": 3.0}))
    bridge._cadence_step()
    bridge._on_frame(FRAME, _frame({"speed_ms": 4.0}))   # past the deadband
    bridge._cadence_step()

    assert len(live.sent) == 2


# ── None means silence, never a zero ─────────────────────────────────────────


def test_an_unavailable_source_sends_nothing_not_a_zero():
    live = _FakeLive()
    table = RouteTable()
    _signal_route(table, source="speed_ms")
    bridge = _bridge(table, live)

    bridge._on_frame(FRAME, _frame({"speed_ms": None}))
    bridge._cadence_step()

    assert live.sent == []


def test_a_disabled_route_sends_nothing_even_with_a_fresh_value():
    live = _FakeLive()
    table = RouteTable()
    _signal_route(table, source="speed_ms", enabled=False)
    bridge = _bridge(table, live)

    bridge._on_frame(FRAME, _frame({"speed_ms": 5.0}))
    bridge._cadence_step()

    assert live.sent == []


# ── Rate cap: cadence calls decide the send count, not frame arrivals ───────


def test_the_send_rate_is_set_by_the_cadence_not_by_frame_arrival():
    """
    100 frames arrive — as if at 100 Hz — between calls to `_cadence_step()`.
    Only the latest is ever looked at, and only a call to `_cadence_step()`
    sends: this is the entire rate-cap mechanism (osc/bridge.py's module
    docstring). deadband=0 so a repeated value is never the reason a send is
    skipped, isolating the property being checked.
    """
    live = _FakeLive()
    table = RouteTable()
    _signal_route(table, source="speed_ms", deadband=0.0,
                 in_min=0.0, in_max=200.0, out_min=0.0, out_max=200.0)
    bridge = _bridge(table, live)

    for i in range(100):
        bridge._on_frame(FRAME, _frame({"speed_ms": float(i)}))

    for _ in range(5):
        bridge._cadence_step()

    assert len(live.sent) == 5
    assert bridge.stats["sent"] == 5
    assert all(args == [99.0] for _addr, args in live.sent), \
        "every send should see the latest frame, not an average or the first"


def test_a_cadence_step_with_no_frame_yet_sends_nothing():
    live = _FakeLive()
    table = RouteTable()
    _signal_route(table, source="speed_ms")
    bridge = _bridge(table, live)

    bridge._cadence_step()

    assert live.sent == []


# ── Events: never rate-capped, never deadbanded, never dropped by the bridge ─


def test_events_are_sent_immediately_regardless_of_the_signal_rate():
    live = _FakeLive()
    table = RouteTable()
    _event_route(table, source="impact")
    bridge = _bridge(table, live, rate_hz=1.0)   # would allow ~1 send/s if capped

    async def fire_many():
        for i in range(10):
            await bridge._on_event(EVENT, Event(id=i, t_us=i * 1000, name="impact"))

    asyncio.run(fire_many())

    assert len(live.sent) == 10
    assert bridge.stats["events_sent"] == 10


def test_events_ignore_the_deadband_even_when_the_value_repeats():
    live = _FakeLive()
    table = RouteTable()
    _event_route(table, source="impact", payload_field="value", deadband=100.0,
                out_min=0.0, out_max=1.0)
    bridge = _bridge(table, live)

    async def fire_twice():
        for _ in range(2):
            await bridge._on_event(
                EVENT, Event(id=1, t_us=0, name="impact", payload={"value": 5.0}))

    asyncio.run(fire_twice())

    assert len(live.sent) == 2, "a signal route would have deadbanded the second send"


def test_a_fire_only_event_route_carries_no_value_argument():
    live = _FakeLive()
    table = RouteTable()
    _event_route(table, source="marker", payload_field=None, args=[2, 0])
    bridge = _bridge(table, live)

    asyncio.run(bridge._on_event(EVENT, Event(id=1, t_us=0, name="marker")))

    assert live.sent == [("/test/event", [2, 0])]


def test_an_event_missing_its_declared_payload_field_is_skipped_not_sent_as_none():
    live = _FakeLive()
    table = RouteTable()
    _event_route(table, source="impact", payload_field="value")
    bridge = _bridge(table, live)

    asyncio.run(bridge._on_event(EVENT, Event(id=1, t_us=0, name="impact", payload={})))

    assert live.sent == []


def test_attach_wires_events_through_the_bus_reliable_and_undelayed():
    """The one integration test: attach() to a real ModelBus, not a direct
    call, to prove the wiring itself — subscribe_sync for frame/meta,
    subscribe(policy=RELIABLE) for event — matches model/bus.py's guarantee
    that an event is never dropped (tests/test_bus.py checks the bus side of
    that promise; this checks the bridge is actually plugged into it)."""
    async def scenario():
        live = _FakeLive()
        table = RouteTable()
        _event_route(table, source="impact")
        bridge = _bridge(table, live)
        bus = ModelBus()
        bridge.attach(bus)

        bus.publish(EVENT, Event(id=1, t_us=0, name="impact"))
        await asyncio.sleep(0.05)   # let the RELIABLE subscription's task drain

        assert len(live.sent) == 1
        await bus.close()

    asyncio.run(scenario())


# ── Reset: the deadband memory must not survive it ───────────────────────────


def test_reset_lets_the_first_post_reset_value_send_even_if_unchanged():
    live = _FakeLive()
    table = RouteTable()
    _signal_route(table, source="speed_ms", deadband=1.0,
                 in_min=0.0, in_max=10.0, out_min=0.0, out_max=10.0)
    bridge = _bridge(table, live)

    bridge._on_frame(FRAME, _frame({"speed_ms": 5.0}))
    bridge._cadence_step()
    assert len(live.sent) == 1

    bridge._on_meta(META, Meta(t_us=0, topic="reset"))
    bridge._on_frame(FRAME, _frame({"speed_ms": 5.0}))   # identical value
    bridge._cadence_step()

    assert len(live.sent) == 2, "a replay restart must resend even an unchanged value"


def test_a_non_reset_meta_does_not_clear_the_deadband_memory():
    live = _FakeLive()
    table = RouteTable()
    _signal_route(table, source="speed_ms", deadband=1.0,
                 in_min=0.0, in_max=10.0, out_min=0.0, out_max=10.0)
    bridge = _bridge(table, live)

    bridge._on_frame(FRAME, _frame({"speed_ms": 5.0}))
    bridge._cadence_step()
    bridge._on_meta(META, Meta(t_us=0, topic="params"))
    bridge._on_frame(FRAME, _frame({"speed_ms": 5.0}))
    bridge._cadence_step()

    assert len(live.sent) == 1, "only a reset topic should clear the deadband memory"


# ── Profiles: round-trip, and tolerance for a row that no longer parses ─────


def test_a_profile_round_trips_through_disk():
    with tempfile.TemporaryDirectory() as tmp:
        table = RouteTable(directory=tmp)
        r1 = _signal_route(table, source="speed_ms", label="vitesse")
        r2 = _event_route(table, source="impact", payload_field="value")

        table.save_profile("show1")

        reloaded = RouteTable(directory=tmp)
        reloaded.load_profile("show1")

        assert {r.id for r in reloaded.all()} == {r1.id, r2.id}
        assert reloaded.get(r1.id).describe() == r1.describe()
        assert reloaded.get(r2.id).describe() == r2.describe()
        assert reloaded.profile == "show1"


def test_load_profile_skips_a_malformed_row_without_failing_the_whole_load():
    with tempfile.TemporaryDirectory() as tmp:
        table = RouteTable(directory=tmp)
        good = _signal_route(table, source="speed_ms")
        table.save_profile("mixed")

        # Simulate a profile a previous version of Route could produce but the
        # current dataclass can't parse: a row missing every required field.
        path = table.profile_path("mixed")
        with open(path) as f:
            rows = json.load(f)
        rows.append({"id": "deadbeef"})
        with open(path, "w") as f:
            json.dump(rows, f)

        reloaded = RouteTable(directory=tmp)
        routes = reloaded.load_profile("mixed")

        assert [r.id for r in routes] == [good.id]


def test_a_vanished_source_is_flagged_invalid_but_still_loads():
    table = RouteTable()
    alive = _signal_route(table, source="speed_ms")
    ghost = _signal_route(table, source="a_signal_that_no_longer_exists")

    schema = table.schema(known_signals=frozenset({"speed_ms"}))
    by_id = {row["id"]: row for row in schema}

    assert by_id[alive.id]["valid"] is True
    assert by_id[ghost.id]["valid"] is False
    assert "a_signal_that_no_longer_exists" in by_id[ghost.id]["reason"]


# ── Targets catalog: pure data, still worth a sanity check ──────────────────


def test_target_catalog_orders_custom_last_and_alphabetical_before_it():
    schema = targets.schema()
    names_in_order = [t["name"] for t in schema]

    assert names_in_order[-1] == "custom"
    assert names_in_order[:-1] == sorted(names_in_order[:-1])


def test_a_fire_only_target_carries_no_natural_output_range():
    clip_fire = targets.get("clip_fire")
    assert clip_fire.event_only is True
    assert clip_fire.out is None
    assert clip_fire.args == (targets.ARG_TRACK, targets.ARG_CLIP)


def main() -> None:
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"  ok  {name}")


if __name__ == "__main__":
    main()
