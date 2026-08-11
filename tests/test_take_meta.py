"""
tests/test_take_meta.py — A take's alignment, and the schema change that brought it.

Two things are pinned here, and they are the same thing seen from both ends.

**The alignment** is the pair of anchors locating a take's start of movement in
its video: `onset_imu_s` on the take's own timeline, `onset_video_s` in the
video.  Both are stored, never their difference — the offset is a residue, the
anchors are facts, and the video one is the single number in the whole device
that no machine can reproduce (ADR 0001).  An alignment is indivisible: half of
it locates nothing, so a patch carrying one anchor is refused rather than
written.

**The tolerance** is what let the two fields it replaces be deleted at all.
`TakeMeta(**json.load(f))` raises `TypeError` on a key the dataclass no longer
declares — and `list_takes()` swallows that, so every take recorded before this
change would have *disappeared from the panel* instead of reporting anything.
The rule is therefore asserted against a `take.json` written by the previous
version, verbatim: a take already on disk is never judged by the current schema.

The marker it replaces is checked by its absence at the boundary that mattered
(`POST /api/recording/marker`), since a route still answering is the one way a
caller could still be wired to it.
"""

import json
import os
import shutil
import tempfile
from dataclasses import asdict

import core
from api.models import TakeUpdate
from storage.session_manager import ACTIVE_FILE, SessionManager, TakeMeta

from tests.test_paths import _app, _call


SESSION = "2026-06-13_14-30_trianon"
TAKE    = "001_premier-essai"

# A take.json exactly as the version before this change wrote it: the two video
# sync fields the alignment replaces, and no anchor.
LEGACY_TAKE_JSON = {
    "name": TAKE, "index": 1, "title": "premier essai", "performer": "félix",
    "figures": [], "notes": "", "started_at": "2026-06-13T14:31:02",
    "ended_at": "2026-06-13T14:33:40", "first_ts_rx_us": 1_000_000,
    "last_ts_rx_us": 1_158_000, "packet_count": 15_800, "imu_config": None,
    "video_file": "", "sync_marker_ts_us": 1_042_000, "video_sync_time_s": 6.5,
    "extra": {},
}


class _Tree:
    """A sessions root holding one take, with `core`'s manager pointed at it."""

    def __enter__(self):
        self.base = tempfile.mkdtemp(prefix="conductor-take-meta-")
        self.root = os.path.join(self.base, "sessions")
        self.sm   = SessionManager(self.root)
        with open(os.path.join(self.root, ACTIVE_FILE), "w") as f:
            f.write(SESSION)

        self._saved_sm = core.session_manager
        core.session_manager = self.sm
        return self

    def __exit__(self, *exc):
        core.session_manager = self._saved_sm
        shutil.rmtree(self.base, ignore_errors=True)

    def make_take(self, raw: dict, session: str = SESSION,
                  take: str = TAKE) -> str:
        """A take as it exists on disk, its take.json written verbatim."""
        take_dir = os.path.join(self.root, session, "takes", take)
        os.makedirs(take_dir, exist_ok=True)
        with open(os.path.join(self.root, session, "session.json"), "w") as f:
            json.dump({"name": session, "title": "essai"}, f)
        open(os.path.join(take_dir, "raw.csv"), "w").close()
        with open(os.path.join(take_dir, "take.json"), "w") as f:
            json.dump(raw, f)
        return take_dir

    def patch(self, body: dict, session: str = SESSION,
              take: str = TAKE) -> tuple[int, str]:
        return _call(_app(), "PATCH", f"/api/sessions/{session}/takes/{take}", body)

    def reread(self, session: str = SESSION, take: str = TAKE) -> TakeMeta:
        """Read the take back the way a restarted orchestrator would: from disk,
        through a manager that has never seen it."""
        return SessionManager(self.root).load_take(
            os.path.join(self.root, session, "takes", take))


# ── The tolerance: a take on disk is never judged by the current schema ──────

def test_a_take_recorded_before_the_anchors_still_lists():
    """
    The criterion the whole deletion rests on.  `sync_marker_ts_us` and
    `video_sync_time_s` are gone from `TakeMeta`; every take.json already on
    disk still has them.  Without the filter, `load_take` raises `TypeError`,
    `list_takes()` swallows it, and the take is simply not there any more —
    a data loss that looks like nothing at all.
    """
    with _Tree() as t:
        t.make_take(LEGACY_TAKE_JSON)

        takes = t.sm.list_takes(SESSION)
        assert [x["name"] for x in takes] == [TAKE], \
            "a take recorded before this change vanished from the listing"

        meta = t.sm.load_take(os.path.join(t.root, SESSION, "takes", TAKE))
        assert meta.packet_count == 15_800, "its metadata came back wrong"
        assert meta.onset_imu_s is None and meta.onset_video_s is None, \
            "an old take is not aligned — it has no anchors, not zeroed ones"


def test_a_take_json_missing_keys_still_loads():
    """
    The other direction, and the one a *future* field will take: a take.json
    written before a key existed simply has no value for it, and must load with
    that field's default rather than raise.  `name` is filled from the directory,
    which is where a take's name actually lives — `take.json` only echoes it.
    """
    with _Tree() as t:
        t.make_take({"name": TAKE})
        meta = t.sm.load_take(os.path.join(t.root, SESSION, "takes", TAKE))
        assert meta.name == TAKE and meta.index == 0 and meta.figures == []

        t.make_take({}, take="002_coin")
        meta = t.sm.load_take(os.path.join(t.root, SESSION, "takes", "002_coin"))
        assert meta.name == "002_coin", "the directory names the take"

        assert [x["name"] for x in t.sm.list_takes(SESSION)] == [TAKE, "002_coin"]


def test_a_take_json_that_is_not_an_object_is_still_skipped_quietly():
    """
    The failure the tolerance must not turn into a crash.  A corrupt take.json
    used to raise `TypeError`, which `list_takes()` catches; filtering keys on a
    JSON array would raise `AttributeError` instead, and take the whole listing —
    every other take included — down with it.
    """
    with _Tree() as t:
        t.make_take(LEGACY_TAKE_JSON)
        corrupt = os.path.join(t.root, SESSION, "takes", "002_coin")
        os.makedirs(corrupt, exist_ok=True)
        open(os.path.join(corrupt, "raw.csv"), "w").close()
        with open(os.path.join(corrupt, "take.json"), "w") as f:
            json.dump(["not", "an", "object"], f)

        assert [x["name"] for x in t.sm.list_takes(SESSION)] == [TAKE], \
            "one unreadable take.json took the takes beside it with it"


# ── The alignment: two anchors, together or not at all ──────────────────────

def test_the_two_anchors_survive_a_restart():
    """
    The acceptance criterion, end to end: a PATCH posting both anchors, read
    back identically by a manager that has never seen the take — which is what
    an orchestrator restart amounts to.
    """
    with _Tree() as t:
        t.make_take(asdict(TakeMeta(name=TAKE, index=1)))

        status, text = t.patch({"onset_imu_s": 12.345, "onset_video_s": 4.5})
        assert status == 200, f"PATCH → {status} {text}"

        meta = t.reread()
        assert meta.onset_imu_s == 12.345 and meta.onset_video_s == 4.5

        # …and what is on disk is the two anchors themselves, never the offset
        # between them: that difference is a residue, recomputable from either
        # side, and storing it would make two facts out of one.
        with open(os.path.join(t.root, SESSION, "takes", TAKE, "take.json")) as f:
            raw = json.load(f)
        assert raw["onset_imu_s"] == 12.345 and raw["onset_video_s"] == 4.5
        assert not any("offset" in k or "sync" in k for k in raw), \
            f"take.json still carries a derived or legacy sync field: {sorted(raw)}"


def test_an_alignment_is_indivisible():
    """
    Half an alignment locates nothing.  Refusing it is what keeps "not yet
    aligned" a state needing no field of its own — either both anchors are on
    disk, or neither is — and it is refused at the request model, so the rule
    holds for any caller, not just this route.
    """
    for half in ({"onset_imu_s": 12.3}, {"onset_video_s": 4.5}):
        try:
            TakeUpdate(**half)
        except Exception:
            pass
        else:
            raise AssertionError(f"TakeUpdate accepted a half alignment: {half}")

    assert TakeUpdate(onset_imu_s=12.3, onset_video_s=4.5).onset_imu_s == 12.3
    assert TakeUpdate(title="x").onset_imu_s is None, \
        "a patch that says nothing about the alignment must stay legal"

    with _Tree() as t:
        t.make_take(asdict(TakeMeta(name=TAKE, index=1)))

        status, text = t.patch({"onset_imu_s": 12.3})
        assert status == 422, f"PATCH with one anchor → {status} {text}"
        assert t.reread().onset_imu_s is None, "the refused patch wrote anyway"

        # A patch touching anything else is untouched by the rule.
        status, text = t.patch({"notes": "roulade ratée"})
        assert status == 200, f"PATCH notes → {status} {text}"


def test_an_anchor_keeps_its_fraction_of_a_second():
    """
    Seconds, and floating ones.  A field that quantised to the second would be
    thirty frames of error on a device whose entire point is naming one frame —
    and zero is a legal anchor, distinct from "no anchor", which is what makes
    the round trip worth asserting on 0.0 rather than a convenient number.
    """
    with _Tree() as t:
        t.make_take(asdict(TakeMeta(name=TAKE, index=1)))

        status, text = t.patch({"onset_imu_s": 0.0, "onset_video_s": 0.033})
        assert status == 200, f"PATCH → {status} {text}"

        meta = t.reread()
        assert meta.onset_video_s == 0.033
        assert meta.onset_imu_s == 0.0 and meta.onset_imu_s is not None, \
            "an anchor at zero is an alignment, not the absence of one"


# ── The marker it replaces ──────────────────────────────────────────────────

def test_the_marker_route_is_gone():
    """
    The device removed with it was wired end to end and read by no frontend at
    all.  Its route is the one thing a caller left behind could still reach, so
    that is what is asserted: nothing answers there any more.
    """
    status, text = _call(_app(), "POST", "/api/recording/marker")
    assert status == 404, f"POST /api/recording/marker → {status} {text}"


def main() -> None:
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"  ok  {name}")


if __name__ == "__main__":
    main()
