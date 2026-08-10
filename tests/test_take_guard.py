"""
tests/test_take_guard.py — Recording one take must not lock every other take's twin.

`PATCH /api/sessions/{session}/takes/{take}` refuses to edit the take the CSV
logger currently has open, which is right: its `take.json` is rewritten on stop,
so a patch landing mid-recording would be overwritten without a trace.

What the guard may not do is answer that question from the take name alone.
Take directories are `NNN_slug` and the counter restarts at 001 in *every*
session (`SessionManager._next_take_index`), so `001_essai` is one of the most
common names on disk — recording it in tonight's session used to make every
past session's `001_essai` unpatchable, with a 409 blaming a recording in a
session the user was not even looking at.

The comparison therefore has to include the session, which is what
`api/routes.py:_is_being_recorded` does — the same helper the pose-track
endpoint uses, and the reason this fix is a call site rather than a second copy
of the rule.

Both directions are asserted, because a guard that never fires would pass a
test for the bug while being far worse than the bug: the frontier here is
narrow, and only one of the two takes named `001_essai` is actually being
written to.
"""

import json
import os
import shutil
import tempfile
from dataclasses import asdict

import core
from storage.session_manager import ACTIVE_FILE, SessionManager, TakeMeta

from tests.test_paths import _app, _call


# ── A sessions tree with the same take name in two sessions ─────────────────

SESSION_A = "2026-06-13_14-30_trianon"   # the one that is recording
SESSION_B = "2026-06-12_20-05_atelier"   # last night's, being annotated
TAKE      = "001_essai"                  # the name they share


class _Tree:
    """Two sessions, each holding a take called `001_essai`, and A is active."""

    def __enter__(self):
        self.base = tempfile.mkdtemp(prefix="conductor-take-guard-")
        self.root = os.path.join(self.base, "sessions")
        self.sm   = SessionManager(self.root)
        for session in (SESSION_A, SESSION_B):
            self._make_take(session, TAKE)
        self.set_active(SESSION_A)

        # Swap the singletons the route reads. The manager is a real one over a
        # real tree, so `active_session()` is exercised rather than stubbed.
        self._saved_sm     = core.session_manager
        self._saved_active = core.csv_logger.active
        self._saved_meta   = core.csv_logger._meta
        core.session_manager = self.sm
        return self

    def __exit__(self, *exc):
        core.session_manager     = self._saved_sm
        core.csv_logger.active   = self._saved_active
        core.csv_logger._meta    = self._saved_meta
        shutil.rmtree(self.base, ignore_errors=True)

    def _make_take(self, session: str, take: str) -> None:
        take_dir = os.path.join(self.root, session, "takes", take)
        os.makedirs(take_dir, exist_ok=True)
        with open(os.path.join(self.root, session, "session.json"), "w") as f:
            json.dump({"name": session, "title": "essai"}, f)
        open(os.path.join(take_dir, "raw.csv"), "w").close()
        with open(os.path.join(take_dir, "take.json"), "w") as f:
            json.dump(asdict(TakeMeta(name=take, index=1)), f)

    def set_active(self, session: str | None) -> None:
        path = os.path.join(self.root, ACTIVE_FILE)
        if session is None:
            if os.path.exists(path):
                os.remove(path)
            return
        with open(path, "w") as f:
            f.write(session)

    def recording(self, take: str | None) -> None:
        """Put the CSV logger in the state it has while writing `take`."""
        core.csv_logger.active = take is not None
        core.csv_logger._meta  = None if take is None else TakeMeta(name=take, index=1)

    def patch(self, session: str, take: str, body=None) -> tuple[int, str]:
        return _call(_app(), "PATCH", f"/api/sessions/{session}/takes/{take}",
                     body or {"title": "annoté après coup"})


# ── The bug ─────────────────────────────────────────────────────────────────

def test_another_sessions_take_of_the_same_name_stays_editable():
    """
    The defect itself.  Recording `001_essai` in session A said nothing whatever
    about session B's `001_essai`, and the user was told to "stop it first" for
    a recording that has no bearing on the take they are annotating.
    """
    with _Tree() as t:
        t.recording(TAKE)

        status, text = t.patch(SESSION_B, TAKE)
        assert status == 200, f"PATCH {SESSION_B}/{TAKE} → {status} {text}"

        # …and the patch really landed, rather than being a 200 that wrote nothing.
        meta = t.sm.load_take(t.sm.take_path(SESSION_B, TAKE))
        assert meta.title == "annoté après coup"


def test_the_take_actually_being_recorded_is_still_refused():
    """
    The half that must not regress.  Widening the guard until it never fires
    would pass the test above and lose the property it exists for: this take's
    take.json is rewritten on stop, so a patch here is silently discarded.
    """
    with _Tree() as t:
        t.recording(TAKE)

        status, text = t.patch(SESSION_A, TAKE)
        assert status == 409, f"PATCH {SESSION_A}/{TAKE} → {status} {text}"
        assert "recorded" in text

        meta = t.sm.load_take(t.sm.take_path(SESSION_A, TAKE))
        assert meta.title == "", "the refused patch wrote anyway"


def test_a_different_take_in_the_recording_session_stays_editable():
    """
    The mirror of the first case: same session, different take.  The name half
    of the comparison has to keep doing its work — dropping it would lock the
    whole session while any take of it records.
    """
    with _Tree() as t:
        t._make_take(SESSION_A, "002_coin")
        t.recording("002_coin")

        status, text = t.patch(SESSION_A, TAKE)
        assert status == 200, f"PATCH {SESSION_A}/{TAKE} → {status} {text}"


def test_nothing_is_locked_while_nothing_is_recording():
    with _Tree() as t:
        t.recording(None)

        for session in (SESSION_A, SESSION_B):
            status, text = t.patch(session, TAKE)
            assert status == 200, f"PATCH {session}/{TAKE} → {status} {text}"


def test_a_recording_with_no_active_session_locks_nothing():
    """
    The state the helper must not raise on.  `active_session()` returns None
    whenever the `.active` pointer is gone — a closed session, a half-written
    tree — and an attribute access on that None would turn a metadata edit into
    a 500.  No session open means no take can be proven to be recording.
    """
    with _Tree() as t:
        t.recording(TAKE)
        t.set_active(None)

        status, text = t.patch(SESSION_A, TAKE)
        assert status == 200, f"PATCH {SESSION_A}/{TAKE} → {status} {text}"


def main() -> None:
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"  ok  {name}")


if __name__ == "__main__":
    main()
