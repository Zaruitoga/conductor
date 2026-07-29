"""
tests/test_ws_outbox.py — A saturated client loses smoothness, never triggers.

This is the per-client half of the loss policy (the bus handles the other half).
It matters most exactly when things are going badly: a browser that has stopped
draining is the normal state of a laptop during a show.
"""

import asyncio

from transport.ws_server import _Outbox, _LOSSY_BACKLOG, _RELIABLE_BACKLOG


def _run(coro_fn):
    return asyncio.run(coro_fn())


def test_droppable_messages_evict_each_other():
    box = _Outbox()
    overflow = 5
    for i in range(_LOSSY_BACKLOG + overflow):
        box.put(f"frame{i}", droppable=True)

    assert box.dropped == overflow
    assert box.forced == 0
    assert box.depth == _LOSSY_BACKLOG


def test_a_flood_of_frames_cannot_evict_a_trigger():
    box = _Outbox()
    box.put("impact", droppable=False)
    for i in range(_LOSSY_BACKLOG * 4):
        box.put(f"frame{i}", droppable=True)

    assert box.forced == 0, "a trigger was discarded by frame pressure"

    async def drain():
        return await box.get()

    assert _run(drain) == "impact"


def test_triggers_are_sent_before_frames():
    """
    A wedged socket that recovers must fire its triggers first: during a show the
    impact that just happened matters more than redrawing the wheel.
    """
    box = _Outbox()
    box.put("frameA", droppable=True)
    box.put("frameB", droppable=True)
    box.put("impact", droppable=False)

    async def drain_all():
        return [await box.get() for _ in range(3)]

    assert _run(drain_all) == ["impact", "frameA", "frameB"]


def test_undroppable_overflow_is_counted_not_silent():
    box = _Outbox()
    overflow = 3
    for i in range(_RELIABLE_BACKLOG + overflow):
        box.put(f"event{i}", droppable=False)

    # Reaching this means the socket is wedged, not busy. It is still a loss, and
    # the counter is what makes it provable rather than a mystery after the fact.
    assert box.forced == overflow
    assert box.depth == _RELIABLE_BACKLOG


def test_get_waits_for_a_message():
    async def scenario():
        box = _Outbox()

        async def put_later():
            await asyncio.sleep(0.01)
            box.put("late", droppable=True)

        task = asyncio.ensure_future(put_later())
        msg = await asyncio.wait_for(box.get(), timeout=1.0)
        await task
        return msg

    assert _run(scenario) == "late"


def main() -> None:
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"  ok  {name}")


if __name__ == "__main__":
    main()
