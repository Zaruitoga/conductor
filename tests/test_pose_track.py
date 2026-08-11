"""
tests/test_pose_track.py — The pose track: format, stamp, and one producer.

The track is what makes sweeping a take possible without replaying it, so the
properties that matter are the ones a sweep would silently suffer from:

  * a pose written then read back is the same pose, and a missing component
    stays missing rather than becoming a position of zero;
  * a track being computed right now reads up to its last whole record — the
    sweep must be alive to the limit reached, not wait for the end;
  * the geometry it was computed at is readable, and a disagreement with
    config.py is *detected*: a 5 % error on the radii moves every position by
    5 %, and nothing else in the file would ever hint at it;
  * the track holds exactly what `model.feed()` produced from row 0, whatever
    the live model happens to be doing — one producer, one answer.

The last one is checked against the simulator, like tests/test_model.py: the
motion is prescribed in closed form and the gyro derived from that same
attitude, so a take built from it is a genuine input rather than a recording of
this pipeline's own output.
"""

import asyncio
import os
import shutil
import tempfile

import numpy as np

import config
from model.engine import Model
from model.registry import SIGNALS
from simulator.motion import WheelMotion
from storage.csv_logger import CSVLogger
from storage.pose_track import (
    COLUMNS, HEADER_STRUCT, RECORD_STRUCT, PoseTrackService, PoseTrackWriter,
    compute_pose_track, read_header, read_poses,
)
from storage.session_manager import SessionManager

HZ = 100.0

_TMP_DIRS: list[str] = []


def _tmp_track() -> str:
    """A path for a throwaway track file, removed when the module finishes."""
    d = tempfile.mkdtemp(prefix="conductor-posetrack-")
    _TMP_DIRS.append(d)
    return os.path.join(d, "pose.bin")


def _f32(v):
    """What a float becomes once stored — the pose columns are f4."""
    return None if v is None else float(np.float32(v))


def _super(motion, t, seq):
    """
    One bundled datagram, attitude and gyro sampled at the same instant.

    `dep_slots` is not decoration: CSVLogger skips a super packet that arrived
    before the layout was known, so without it the take would record nothing at
    all and every assertion below would be about an empty file.
    """
    gx, gy, gz = motion.gyro(t)
    qw, qx, qy, qz = motion.quaternion(t)
    ts = int(round(t * 1e6))
    return {"type": "super_0", "typeId": 0x10, "seq": seq,
            "ts_esp_us": ts, "ts_rx_us": ts, "dep_slots": [0, 6],
            "gyro_x": gx, "gyro_y": gy, "gyro_z": gz,
            "game_rv_qw": qw, "game_rv_qx": qx,
            "game_rv_qy": qy, "game_rv_qz": qz}


def _packets(scenario="coin", seconds=4.0, hz=HZ):
    motion = WheelMotion(scenario=scenario)
    return [_super(motion, i / hz, i) for i in range(int(seconds * hz))]


class _Take:
    """A throwaway session with one recorded take, written by the real logger."""

    def __init__(self, packets, scenario="coin"):
        self.root = tempfile.mkdtemp(prefix="conductor-posetrack-")
        self.sm   = SessionManager(os.path.join(self.root, "sessions"))
        session   = self.sm.create_session(title="bench")
        take_dir, meta = self.sm.new_take(title="essai")

        logger = CSVLogger(self.sm)
        logger.start(take_dir, meta)
        for p in packets:
            logger.write(p)
        logger.stop()

        self.session  = session.name
        self.take     = meta.name
        self.take_dir = take_dir
        self.csv      = self.sm.csv_path(take_dir)
        self.pose     = self.sm.pose_path(take_dir)

    def close(self):
        shutil.rmtree(self.root, ignore_errors=True)


# ── The file format ──────────────────────────────────────────────────────────

def test_a_written_track_reads_back_the_same_poses():
    path = _tmp_track()
    written = [
        (0.0,    1.0, 0.0, 0.0, 0.0,       0.0,  0.0, 1.05),
        (0.01,   0.7071067811865476, 0.7071067811865476, 0.0, 0.0,
         1.2345678, -9.87654, 0.98765),
        (123.456, 0.5, -0.5, 0.5, -0.5,   -1234.5, 4321.0, 1.0),
    ]
    with PoseTrackWriter(path, config.R_TORE, config.r_TORE) as w:
        for record in written:
            w.append(*record)

    poses = read_poses(path)

    assert poses["t"] == [r[0] for r in written], "the timeline must be exact"
    for i, name in enumerate(("qw", "qx", "qy", "qz", "x", "y", "z"), start=1):
        assert poses[name] == [_f32(r[i]) for r in written], f"{name} did not survive"


def test_a_missing_component_stays_a_hole():
    """
    A wheel with no gyro has no horizontal position at all. Storing that as 0
    would read downstream as a wheel sitting at the origin — a plausible fact
    that is not true — so it is a hole, exactly like ScopeRing's NaN.
    """
    path = _tmp_track()
    with PoseTrackWriter(path, config.R_TORE, config.r_TORE) as w:
        w.append(0.0, 1.0, 0.0, 0.0, 0.0, None, None, 1.05)

    poses = read_poses(path)
    assert poses["x"] == [None] and poses["y"] == [None]
    assert poses["z"] == [_f32(1.05)], "the closed-form height is still known"


def test_the_record_size_is_what_the_sizing_assumed():
    """~3.2 MB for 15 min at 100 Hz is what made a file next to the take cheap."""
    assert RECORD_STRUCT.size == 36
    assert HEADER_STRUCT.size == 28
    fifteen_minutes = 15 * 60 * int(HZ)
    assert abs(HEADER_STRUCT.size + fifteen_minutes * RECORD_STRUCT.size - 3.24e6) < 1e4


# ── The geometry stamp ───────────────────────────────────────────────────────

def test_the_geometry_stamp_is_read_back():
    path = _tmp_track()
    with PoseTrackWriter(path, 1.6, 0.032) as w:
        w.append(0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 1.6)

    header = read_header(path)
    assert header.wheel_R == 1.6 and header.wheel_r == 0.032
    assert header.geometry_matches(1.6, 0.032)


def test_a_geometry_disagreement_is_detected_not_silent():
    """
    The one case that survives geometry leaving the tunable surface: someone
    edits config.py, and the tracks on disk are at the old scale. Sixteen bytes
    are what make it noticeable instead of a five-percent lie.
    """
    take = _Take(_packets(seconds=1.0))
    try:
        compute_pose_track(take.csv, take.pose, wheel_R=1.6, wheel_r=0.032)
        header = read_header(take.pose)

        assert not header.geometry_matches(config.R_TORE, config.r_TORE)
        # …and the service says so rather than quietly recomputing.
        svc = PoseTrackService(take.sm)
        geometry = svc.status(take.session, take.take)["geometry"]
        assert geometry["matches"] is False
        assert geometry["R_TORE"] == 1.6
        assert geometry["current"]["R_TORE"] == config.R_TORE
    finally:
        take.close()


# ── A track being written right now ──────────────────────────────────────────

def test_an_unfinished_track_reads_up_to_its_last_whole_record():
    """
    The sweep is alive to the limit reached. A run that died leaves a file that
    reads fine and knows it is unfinished — which is what makes recomputing it
    safe rather than a guess.
    """
    path = _tmp_track()
    try:
        with PoseTrackWriter(path, config.R_TORE, config.r_TORE, flush_every=1) as w:
            for i in range(7):
                w.append(i / HZ, 1.0, 0.0, 0.0, 0.0, float(i), 0.0, 1.05)
            raise RuntimeError("the orchestrator died here")
    except RuntimeError:
        pass

    header = read_header(path)
    assert header.records == 7
    assert header.complete is False, "an interrupted track must not claim to be done"
    assert read_poses(path)["x"] == [float(i) for i in range(7)]


def test_a_half_written_record_is_not_read():
    """
    Fixed-size records plus integer division are the whole guard: a reader
    arriving mid-append takes the whole records and leaves the torn tail.
    """
    path = _tmp_track()
    with PoseTrackWriter(path, config.R_TORE, config.r_TORE) as w:
        for i in range(4):
            w.append(i / HZ, 1.0, 0.0, 0.0, 0.0, float(i), 0.0, 1.05)

    with open(path, "ab") as f:
        f.write(b"\x00" * (RECORD_STRUCT.size - 1))       # an append caught in flight

    assert read_header(path).records == 4
    assert len(read_poses(path)["t"]) == 4


def test_an_absent_track_is_not_an_error():
    missing = _tmp_track()
    assert read_header(missing) is None
    assert read_poses(missing) == {c: [] for c in COLUMNS}


# ── Windowing and thinning ───────────────────────────────────────────────────

def test_a_window_selects_by_take_time_and_a_stride_keeps_both_ends():
    """
    A stride, not ScopeRing's min/max envelope: the min and max of a quaternion
    component over forty ticks is not a rotation anything could render. The last
    record is kept because the end of the window is where the cursor is going.
    """
    path = _tmp_track()
    with PoseTrackWriter(path, config.R_TORE, config.r_TORE) as w:
        for i in range(1000):
            w.append(i / HZ, 1.0, 0.0, 0.0, 0.0, float(i), 0.0, 1.05)

    window = read_poses(path, start=2.0, end=3.0)
    assert window["t"][0] == 2.0 and window["t"][-1] == 3.0
    assert len(window["t"]) == 101

    thin = read_poses(path, points=50)
    assert len(thin["t"]) <= 51, "the budget is a budget"
    assert thin["t"][0] == 0.0
    assert thin["t"][-1] == 999 / HZ, "the far end of the window was dropped"


# ── One producer, and what it produces ───────────────────────────────────────

def test_the_track_holds_what_the_model_produced_from_row_zero():
    """
    The track is `model.feed()` going forward and nothing else (ADR 0003). Fed
    the same take, a plain Model must land on exactly the same poses — anything
    else would be a second computation path, which is the one thing precomputing
    is not allowed to become.
    """
    packets = _packets("spiral", seconds=4.0)
    take = _Take(packets)
    try:
        records = compute_pose_track(take.csv, take.pose)

        reference = []
        model = Model(bus=None, max_gap_us=int(config.MAX_DT_S * 1e6))
        for p in packets:
            frame = model.feed(p)
            if frame is not None:
                reference.append(frame)

        assert records == len(reference) > 300
        poses = read_poses(take.pose)
        for i, frame in enumerate(reference):
            assert poses["t"][i] == frame.t_us / 1e6
            for name in ("qw", "qx", "qy", "qz", "x", "y", "z"):
                assert poses[name][i] == _f32(frame.pose.get(name)), \
                    f"{name} disagrees with the model at tick {i}"
    finally:
        take.close()


def test_the_positions_match_the_analytic_reference():
    """
    Straight-line rolling has a closed form, so the track can be checked against
    something outside this pipeline entirely rather than against itself.
    """
    take = _Take(_packets("straight", seconds=6.0))
    try:
        compute_pose_track(take.csv, take.pose)
        poses = read_poses(take.pose)

        motion = WheelMotion(scenario="straight")
        ref = motion.reference(poses["t"][-1])
        assert abs(poses["x"][-1] - ref["px"]) < 1e-4
        assert abs(poses["y"][-1] - ref["py"]) < 1e-4
        assert abs(poses["z"][-1] - (config.R_TORE + config.r_TORE)) < 1e-6
    finally:
        take.close()


def test_computing_a_track_leaves_the_live_model_alone():
    """
    A track must not depend on which signals someone switched off in the Signaux
    tab, nor move the error counters the panel reads: it is a fact about the
    take, not a view of the current session.
    """
    take = _Take(_packets(seconds=2.0))
    SIGNALS.set_enabled("pos_x", False)
    SIGNALS.errors["pos_x"] = 3
    try:
        compute_pose_track(take.csv, take.pose)
        poses = read_poses(take.pose)

        assert any(v is not None for v in poses["x"]), \
            "a disabled signal in the live registry emptied the track"
        assert SIGNALS.errors["pos_x"] == 3, "the batch run moved live counters"
        assert SIGNALS.disabled == {"pos_x"}, "the batch run touched live switches"
    finally:
        SIGNALS.set_enabled("pos_x", True)
        SIGNALS.errors.pop("pos_x", None)
        take.close()


def test_a_take_is_computed_once_however_often_it_is_opened():
    """
    Two producers of the same file would differ subtly — the live model enters a
    take with an accumulated offset a run from row 0 does not have. So opening a
    take twice while it computes must not start a second run.
    """
    take = _Take(_packets(seconds=2.0))

    async def scenario():
        svc = PoseTrackService(take.sm)
        first  = await svc.ensure(take.session, take.take)
        second = await svc.ensure(take.session, take.take)
        assert first["status"] == second["status"] == "computing"
        assert len(svc._tasks) == 1, "a second producer was started"

        while svc.status(take.session, take.take)["status"] == "computing":
            await asyncio.sleep(0.01)

        done = svc.status(take.session, take.take)
        assert done["status"] == "ready" and done["complete"] is True
        assert done["records"] == 200
        assert abs(done["duration_s"] - 1.99) < 1e-6
        assert done["error"] is None

        # Opening it again finds the track and recomputes nothing.
        mtime = os.path.getmtime(take.pose)
        again = await svc.ensure(take.session, take.take)
        assert again["status"] == "ready"
        assert os.path.getmtime(take.pose) == mtime
        return svc

    try:
        asyncio.run(scenario())
    finally:
        take.close()


def test_reading_a_track_still_filling_gives_poses_and_progress_together():
    """
    What the sweep actually calls. The reply must carry the poses *and* how far
    the computation has got, agreeing with each other: a `records` that did not
    match the poses beside it would leave a caller unable to tell "still
    filling" from "something is wrong". And a track mid-computation must serve
    what it has rather than refuse — the sweep is alive to the limit reached.
    """
    take = _Take(_packets(seconds=3.0))

    async def scenario():
        svc = PoseTrackService(take.sm)
        await svc.ensure(take.session, take.take)

        served_while_filling = False
        while True:
            body = await svc.read(take.session, take.take)
            assert body["count"] == body["records"], \
                "the progress and the poses disagree"
            assert len(body["poses"]["qw"]) == body["count"]
            if body["status"] == "computing":
                served_while_filling = True
                assert body["complete"] is False
            else:
                break
            await asyncio.sleep(0.005)

        assert served_while_filling, "the track finished before it could be read"
        assert body["status"] == "ready" and body["complete"] is True
        assert body["count"] == 300
        assert body["duration_s"] == body["poses"]["t"][-1]
        assert body["geometry"]["matches"] is True

        # A window narrows the poses without touching what progress reports —
        # they answer different questions.
        window = await svc.read(take.session, take.take, start=1.0, end=1.2)
        assert window["records"] == 300 and window["count"] == 21

    try:
        asyncio.run(scenario())
    finally:
        take.close()


def test_a_finished_track_reads_ready_even_while_its_task_is_still_registered():
    """
    The window that makes "computing and complete" possible, held open.

    `PoseTrackWriter.close` stamps the completion flag from inside the worker
    thread, and `_compute`'s `finally` unregisters the task only once
    `asyncio.to_thread` has returned to the loop — so a track can be finished
    on disk while its task is still there. Deciding "computing" from the
    registry alone puts `status: computing` next to `complete: True` in one
    reply, which is the disagreement `read` exists to prevent, one field over.

    Reproducing that by racing the real computation is what made the
    end-to-end test above flaky under load; the state it lands in is
    reconstructed here instead, the way test_osc.py drives `_cadence_step`
    rather than timing a sleep.
    """
    take = _Take(_packets(seconds=1.0))

    async def scenario():
        svc = PoseTrackService(take.sm)
        key = (take.session, take.take)

        await svc.ensure(*key)
        # Waited out on the registry, not on the status: the status is now the
        # thing under test, and it goes "ready" *inside* the window — while the
        # real task is still there to pop the stand-in installed below.
        while key in svc._tasks:
            await asyncio.sleep(0.01)

        pending = asyncio.ensure_future(asyncio.sleep(60))
        svc._tasks[key] = pending
        try:
            body = await svc.read(*key)
            assert body["complete"] is True
            assert body["status"] == "ready", \
                "a finished track reported itself as still filling"
            assert body["count"] == body["records"] == 100

            # And nothing starts a second run over it: `ensure` was already
            # weighing the header, which is why it is the side that was right.
            again = await svc.ensure(*key)
            assert again["status"] == "ready"
            assert svc._tasks[key] is pending, "a second producer was started"
        finally:
            svc._tasks.pop(key, None)
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)

    try:
        asyncio.run(scenario())
    finally:
        take.close()


def test_deleting_a_broken_track_lets_the_next_open_recompute_it():
    """
    A file that is not a pose track is remembered so it is not overwritten — it
    is the only evidence of what went wrong. But that memory must die with the
    file, or the documented recovery (delete it and reopen the take) would do
    nothing at all.
    """
    take = _Take(_packets(seconds=2.0))

    async def scenario():
        svc = PoseTrackService(take.sm)
        with open(take.pose, "wb") as f:
            f.write(b"not a pose track at all, really")

        broken = await svc.ensure(take.session, take.take)
        assert broken["status"] == "failed" and broken["error"]
        assert broken["records"] == 0, "nothing was read from it"

        os.remove(take.pose)
        await svc.ensure(take.session, take.take)
        while svc.status(take.session, take.take)["status"] == "computing":
            await asyncio.sleep(0.01)

        done = svc.status(take.session, take.take)
        assert done["status"] == "ready" and done["records"] == 200
        assert done["error"] is None

    try:
        asyncio.run(scenario())
    finally:
        take.close()


def test_an_unfinished_track_is_recomputed_on_the_next_open():
    """A run that died leaves `complete` unset — that flag is the whole point."""
    take = _Take(_packets(seconds=2.0))

    async def scenario():
        svc = PoseTrackService(take.sm)
        try:
            with PoseTrackWriter(take.pose, config.R_TORE, config.r_TORE, 1) as w:
                w.append(0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 1.05)
                raise RuntimeError("died")
        except RuntimeError:
            pass

        assert svc.status(take.session, take.take)["status"] == "partial"
        await svc.ensure(take.session, take.take)
        while svc.status(take.session, take.take)["status"] == "computing":
            await asyncio.sleep(0.01)
        assert svc.status(take.session, take.take)["records"] == 200

    try:
        asyncio.run(scenario())
    finally:
        take.close()


def test_a_take_with_no_attitude_yields_an_empty_but_finished_track():
    """
    No attitude means no tick, so there is nothing to precompute — and saying so
    plainly beats leaving a caller to wait for a track that will never grow.
    """
    ts = lambda i: i * 10_000
    take = _Take([{"type": "gyro", "typeId": 0x01, "seq": i,
                   "ts_esp_us": ts(i), "ts_rx_us": ts(i),
                   "x": 0.1, "y": 0.2, "z": 0.3} for i in range(50)])
    try:
        assert compute_pose_track(take.csv, take.pose) == 0
        header = read_header(take.pose)
        assert header.records == 0 and header.complete is True
        assert read_poses(take.pose)["t"] == []
    finally:
        take.close()


def main() -> None:
    try:
        for name, fn in sorted(globals().items()):
            if name.startswith("test_") and callable(fn):
                fn()
                print(f"  ok  {name}")
    finally:
        for d in _TMP_DIRS:
            shutil.rmtree(d, ignore_errors=True)
        _TMP_DIRS.clear()


if __name__ == "__main__":
    main()
