"""
tests/test_processing_loop.py — The loop must never stop consuming.

It is the only consumer of the central queue.  If its task dies, the
orchestrator goes silently deaf: packets pile up, the panel keeps rendering the
last snapshot it received, and nothing says why.  During a show that failure
mode is far worse than a wrong number.

So the contract checked here is narrow and absolute: whatever the model does,
every packet is still recorded and still published, and the loop is still alive
for the next one.
"""

import asyncio

import core
from model.bus import ModelBus
from model.engine import Model
from model.types import FRAME, RAW


def _packet(seq=1, type_id=0x10):
    """A super-slot packet carrying attitude and gyro — enough to make a tick."""
    ts = seq * 10_000
    return {"type": "super_0", "typeId": type_id, "seq": seq,
            "ts_esp_us": ts, "ts_rx_us": ts,
            "gyro_x": 0.0, "gyro_y": 0.0, "gyro_z": 3.14,
            "game_rv_qw": 0.7071, "game_rv_qx": 0.7071,
            "game_rv_qy": 0.0, "game_rv_qz": 0.0}


async def _drive(packets, model=None):
    """
    Run the real processing_loop over `packets`, collecting what it published.

    Both the bus and the model are swapped for the duration: the loop and the
    model must publish to the *same* fan-out point for the frames to be visible,
    which is exactly the coupling this test is checking.
    """
    saved_bus, saved_model = core.bus, core.model
    saved_errors = dict(core.model_errors)

    bus = ModelBus()
    published = {RAW: [], FRAME: []}
    bus.subscribe_sync("probe", (RAW, FRAME),
                       lambda kind, obj: published[kind].append(obj))

    core.bus   = bus
    core.model = model if model is not None else Model(bus=bus)
    core.model_errors.update(count=0, last=None)

    q = asyncio.Queue()
    task = asyncio.ensure_future(core.processing_loop(q))
    try:
        for p in packets:
            await q.put(p)
        await q.join()
        alive = not task.done()
        errors = dict(core.model_errors)
    finally:
        task.cancel()
        core.bus, core.model = saved_bus, saved_model
        core.model_errors.update(saved_errors)
    return published, alive, errors


def test_every_packet_reaches_the_raw_stream():
    packets = [_packet(i) for i in range(1, 6)]
    published, alive, _ = asyncio.run(_drive(packets))

    assert len(published[RAW]) == 5
    assert [p["seq"] for p in published[RAW]] == [1, 2, 3, 4, 5]
    assert alive


def test_packets_that_make_a_tick_produce_frames():
    published, _, _ = asyncio.run(_drive([_packet(i) for i in range(1, 6)]))
    assert len(published[FRAME]) == 5
    # The first tick has no previous sample to measure elapsed time against, and
    # says so rather than inventing a dt.
    assert published[FRAME][0].quality["status"] == "first"
    assert published[FRAME][1].quality["status"] == "ok"


def test_a_heartbeat_is_streamed_but_makes_no_frame():
    """Telemetry is not motion: it belongs on the wire, not in the model."""
    heartbeat = {"type": "heartbeat", "typeId": 0x20, "seq": 1,
                 "ts_esp_us": 10_000, "ts_rx_us": 10_000, "uptime_ms": 1000}
    published, _, _ = asyncio.run(_drive([heartbeat]))

    assert len(published[RAW]) == 1
    assert published[FRAME] == []


def test_a_broken_engine_never_stops_the_loop():
    """
    The registry contains a failing *signal*; this covers the engine failing
    outright, which would otherwise kill the queue's only consumer.
    """
    class Exploding:
        def feed(self, packet):
            raise RuntimeError("engine exploded")

        def reset(self):
            pass

    packets = [_packet(i) for i in range(1, 8)]
    published, alive, errors = asyncio.run(_drive(packets, model=Exploding()))

    assert alive, "the loop died and the orchestrator would have gone deaf"
    assert len(published[RAW]) == 7, "the raw stream must survive a broken model"
    assert errors["count"] == 7
    assert "engine exploded" in errors["last"]


def test_the_playback_sentinel_resets_and_is_not_streamed():
    """
    The reset happens where the replay ends *in the stream*, so the take's last
    position cannot become live mode's starting offset.
    """
    class Counting:
        def __init__(self):
            self.resets = 0

        def feed(self, packet):
            return None

        def reset(self):
            self.resets += 1

    counter = Counting()
    packets = [_packet(1), {"typeId": "playback_end"}, _packet(2)]
    published, alive, _ = asyncio.run(_drive(packets, model=counter))

    assert counter.resets == 1
    assert alive
    # The sentinel is an internal marker, not something a client should see.
    assert [p["seq"] for p in published[RAW]] == [1, 2]


def main() -> None:
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"  ok  {name}")


if __name__ == "__main__":
    main()
