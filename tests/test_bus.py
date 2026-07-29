"""
tests/test_bus.py — The loss policies, which are the bus's whole reason to exist.

A frame may be dropped; an event may not.  These check that the difference is
real rather than documented.
"""

import asyncio

from model.bus import ModelBus, LOSSY, RELIABLE, _LOSSY_BACKLOG
from model.types import RAW, FRAME, EVENT


def _run(coro):
    return asyncio.run(coro())


def test_inline_subscriber_is_called_during_publish():
    bus = ModelBus()
    seen = []
    bus.subscribe_sync("ring", [FRAME], lambda kind, obj: seen.append((kind, obj)))

    bus.publish(FRAME, "a")
    # No await anywhere: an inline subscriber has already run by now. That is the
    # property the scope ring and the event log rely on to be loss-free.
    assert seen == [(FRAME, "a")]


def test_publish_filters_by_kind():
    bus = ModelBus()
    frames, events = [], []
    bus.subscribe_sync("f", [FRAME], lambda k, o: frames.append(o))
    bus.subscribe_sync("e", [EVENT], lambda k, o: events.append(o))

    bus.publish(FRAME, "f1")
    bus.publish(EVENT, "e1")
    bus.publish(RAW, "r1")

    assert frames == ["f1"]
    assert events == ["e1"]


def test_a_raising_subscriber_does_not_break_publishing():
    bus = ModelBus()
    survivor = []

    def boom(kind, obj):
        raise RuntimeError("detector exploded")

    bus.subscribe_sync("broken", [FRAME], boom)
    bus.subscribe_sync("fine", [FRAME], lambda k, o: survivor.append(o))

    bus.publish(FRAME, "f1")
    bus.publish(FRAME, "f2")

    assert survivor == ["f1", "f2"]


def test_lossy_subscriber_discards_the_oldest():
    async def scenario():
        bus = ModelBus()
        got = []

        async def slow(kind, obj):
            got.append(obj)

        sub = bus.subscribe("viewer", [FRAME], slow, policy=LOSSY)

        # Publish without ever yielding: the draining task cannot run, so the
        # backlog saturates exactly as it would behind a stalled browser.
        overflow = 8
        for i in range(_LOSSY_BACKLOG + overflow):
            bus.publish(FRAME, i)

        assert sub.dropped == overflow
        await asyncio.sleep(0.05)

        # What survived is the *freshest* window, which is the point: for a
        # real-time view a backlog is worse than a gap.
        assert got == list(range(overflow, _LOSSY_BACKLOG + overflow))
        assert sub.overflows == 0
        await bus.close()

    _run(lambda: scenario())


def test_reliable_subscriber_keeps_everything_a_lossy_one_would_have_dropped():
    async def scenario():
        bus = ModelBus()
        got = []

        async def handler(kind, obj):
            got.append(obj)

        sub = bus.subscribe("osc", [EVENT], handler, policy=RELIABLE)

        n = _LOSSY_BACKLOG * 10          # far past what a lossy budget allows
        for i in range(n):
            bus.publish(EVENT, i)

        assert sub.dropped == 0
        assert sub.overflows == 0
        await asyncio.sleep(0.05)
        assert got == list(range(n)), "a trigger was lost"
        await bus.close()

    _run(lambda: scenario())


def test_stats_report_backlog_and_losses():
    async def scenario():
        bus = ModelBus()

        async def handler(kind, obj):
            pass

        bus.subscribe("viewer", [FRAME], handler, policy=LOSSY)
        for i in range(_LOSSY_BACKLOG + 5):
            bus.publish(FRAME, i)

        s = bus.stats()
        assert s["published"][FRAME] == _LOSSY_BACKLOG + 5
        viewer = next(x for x in s["subscribers"] if x["name"] == "viewer")
        assert viewer["dropped"] == 5
        assert viewer["policy"] == LOSSY
        await bus.close()

    _run(lambda: scenario())


def main() -> None:
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"  ok  {name}")


if __name__ == "__main__":
    main()
