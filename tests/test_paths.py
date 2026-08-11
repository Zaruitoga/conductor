"""
tests/test_paths.py — A name from the outside can never leave the directory it names.

Three roots, one rule: `sessions/`, `params/` and `mappings/`.

`os.path.join` drops every component preceding an absolute one, so
`take_path("a", "/etc/passwd")` used to *be* `/etc/passwd` — no `..`, no
encoding, a leading `/` was enough.  What bounded the damage was accidental: the
callers glue a fixed `raw.csv` / `take.json` onto the result.  Nobody chose that,
nothing documented it, and the video endpoint would have removed it.

Two independent layers are checked here, in the order they run:

  1. **shape**, declared on the routes, so a malformed name is a 422 before any
     handler sees it;
  2. **containment**, inside `session_path()` / `take_path()` and the two
     `profile_path()` builders, which closes every caller at once — including
     the ones not written yet.

The layer that matters most is the one that must *not* fire: `list_takes()`
swallows a take whose metadata fails to load, so a validation raising from that
path would make real takes vanish from the panel instead of showing an error.
The rule this file pins down is that validation rejects an *input*, never a take
already on disk.

The profile stores repeat the whole shape one directory over, and the same rule
holds there with an extra edge: a profile name is never slugified on the way in,
so what is already on disk is likelier still to be a name a request may no longer
send.  It keeps listing regardless.
"""

import asyncio
import json
import os
import re
import shutil
import tempfile

from fastapi import FastAPI

from api.models import PlaybackRequest, ProfileRequest, TakeUpdate
from api.routes import router
from model.params import ParamStore
from osc.routes import RouteTable
from storage.paths import NAME_PATTERN, UnsafePath, confine, is_name, is_video_filename
from storage.session_manager import SessionManager, TakeMeta, _slug

from dataclasses import asdict


# ── A throwaway sessions/ tree ──────────────────────────────────────────────

class _Tree:
    """A temp dir holding a sessions root and an 'outside' the tree must not reach."""

    def __enter__(self):
        self.base    = tempfile.mkdtemp(prefix="conductor-paths-")
        self.root    = os.path.join(self.base, "sessions")
        self.outside = os.path.join(self.base, "outside")
        os.makedirs(self.outside)
        with open(os.path.join(self.outside, "secret.txt"), "w") as f:
            f.write("not yours")
        self.sm = SessionManager(self.root)
        return self

    def __exit__(self, *exc):
        shutil.rmtree(self.base, ignore_errors=True)

    def make_take(self, session: str, take: str) -> str:
        """A take as it exists on disk: raw.csv + take.json, nothing else."""
        take_dir = os.path.join(self.root, session, "takes", take)
        os.makedirs(take_dir, exist_ok=True)
        with open(os.path.join(self.root, session, "session.json"), "w") as f:
            json.dump({"name": session, "title": "essai"}, f)
        open(os.path.join(take_dir, "raw.csv"), "w").close()
        with open(os.path.join(take_dir, "take.json"), "w") as f:
            json.dump(asdict(TakeMeta(name=take, index=1)), f)
        return take_dir


# ── Layer 2: containment ────────────────────────────────────────────────────

def test_an_absolute_name_is_refused():
    """The defect itself: os.path.join('sessions', 'a', '/etc/passwd') is '/etc/passwd'."""
    with _Tree() as t:
        assert os.path.join(t.root, "a", "takes", "/etc/passwd") == "/etc/passwd"

        for bad in ("/etc/passwd", "/etc", "//etc/passwd"):
            try:
                t.sm.take_path("a", bad)
            except UnsafePath:
                pass
            else:
                raise AssertionError(f"take_path accepted an absolute take {bad!r}")

        try:
            t.sm.session_path("/etc")
        except UnsafePath:
            pass
        else:
            raise AssertionError("session_path accepted an absolute session")


def test_dot_dot_is_refused():
    with _Tree() as t:
        for session, take in (("a", ".."), ("a", "../../.."), ("..", "001_a"),
                              ("a/../../..", "001_a"), ("a", "../../../etc")):
            try:
                t.sm.take_path(session, take)
            except UnsafePath:
                pass
            else:
                raise AssertionError(f"take_path accepted {session!r}/{take!r}")


def test_a_symlink_out_of_the_tree_is_refused():
    """
    The case `normpath` alone lets through: every segment is well-formed, the
    string stays under sessions/, and the file read is still outside it.  Only
    resolving the link (realpath) sees it.
    """
    with _Tree() as t:
        os.makedirs(t.root, exist_ok=True)
        os.symlink(t.outside, os.path.join(t.root, "escape"))
        os.makedirs(os.path.join(t.root, "real", "takes"), exist_ok=True)
        os.symlink(t.outside, os.path.join(t.root, "real", "takes", "escape"))

        assert os.path.normpath(os.path.join(t.root, "escape")).startswith(t.root), \
            "normpath sees nothing wrong here — that is the point"

        for session, take in (("escape", "001_a"), ("real", "escape")):
            try:
                t.sm.take_path(session, take)
            except UnsafePath:
                pass
            else:
                raise AssertionError(f"take_path followed a symlink out: {session}/{take}")


def test_the_names_the_manager_itself_generates_pass():
    """
    The reference shape is not a regex someone liked: it is whatever `_slug`,
    `NNN_slug` and `<date>_<time>_<slug>` actually produce.  Asking the manager
    to make them is a real cross-check; restating the pattern would not be.
    """
    for title in ("Premier essai !", "Théâtre du Châtelet — essai 3", "", "...",
                  "roue 42 / passe 2"):
        with _Tree() as t:
            session = t.sm.create_session(title)
            take_dir, take = t.sm.new_take(title)

            assert is_name(session.name), f"session name {session.name!r} fails its own shape"
            assert is_name(take.name),    f"take name {take.name!r} fails its own shape"

            resolved = t.sm.take_path(session.name, take.name)
            assert os.path.realpath(take_dir) == resolved
            assert os.path.isdir(resolved)


def test_a_take_on_disk_never_disappears_from_the_listing():
    """
    The criterion that matters most.  `list_takes()` skips a take it cannot load,
    silently — so a validation raising from there would delete takes from the
    panel rather than report an error.  Nothing here is user input: the names
    come from `os.listdir` of the tree itself.
    """
    with _Tree() as t:
        t.make_take("2026-06-13_14-30_trianon", "001_premier-essai")
        t.make_take("2026-06-13_14-30_trianon", "002_coin")

        takes = t.sm.list_takes("2026-06-13_14-30_trianon")
        assert [x["name"] for x in takes] == ["001_premier-essai", "002_coin"]

        tree = t.sm.list_sessions()
        assert [s["name"] for s in tree] == ["2026-06-13_14-30_trianon"]
        assert len(tree[0]["takes"]) == 2


def test_a_take_whose_name_a_request_could_not_use_still_lists():
    """
    The other half of the same rule.  A take renamed by hand to something the
    API's shape check would refuse is *not* an input, so it keeps listing — it
    simply answers 422 if anyone tries to patch or replay it.  Getting this
    backwards is how a validation deletes takes from the panel.
    """
    with _Tree() as t:
        t.make_take("séance du 13 juin", "prise n°1")

        tree = t.sm.list_sessions()
        assert [s["name"] for s in tree] == ["séance du 13 juin"]
        assert [x["name"] for x in tree[0]["takes"]] == ["prise n°1"]
        assert not is_name("prise n°1"), "…and a request may still not name it"


def test_a_take_symlinked_out_of_the_tree_is_dropped_from_the_listing():
    """
    The one case where the two rules disagree, pinned so it stays a decision.

    A take symlinked onto another disk cannot be replayed (confine() refuses it),
    so listing it would promise a replay that will not happen.  It is dropped —
    but only it: the takes beside it are untouched, which is the failure mode
    this whole file exists to prevent.
    """
    with _Tree() as t:
        t.make_take("s1", "001_real")
        outside_take = os.path.join(t.outside, "003_elsewhere")
        os.makedirs(outside_take)
        open(os.path.join(outside_take, "raw.csv"), "w").close()
        with open(os.path.join(outside_take, "take.json"), "w") as f:
            json.dump(asdict(TakeMeta(name="003_elsewhere", index=3)), f)
        os.symlink(outside_take, os.path.join(t.root, "s1", "takes", "002_link"))

        assert [x["name"] for x in t.sm.list_takes("s1")] == ["001_real"]


def test_confine_reports_the_root_itself_as_outside():
    """`sessions/` is not a session; commonpath equality alone would allow it."""
    with _Tree() as t:
        try:
            confine(t.root, "")
        except UnsafePath:
            pass
        else:
            raise AssertionError("confine accepted the root as a session")


# ── Layer 1: shape ──────────────────────────────────────────────────────────

def test_the_shape_pattern_rejects_what_a_path_segment_must_never_be():
    good = ["2026-06-13_14-30_trianon", "001_premier-essai", "001", "a.b-c_d",
            "2026-08-10_21-15_test-1"]
    bad  = ["", "..", ".", "...", "/etc/passwd", "a/b", "a\\b", "..%2f..",
            "a b", "a\nb", "-", "_", "a/../b", "~"]

    for name in good:
        assert re.fullmatch(NAME_PATTERN, name), f"{name!r} should be a legal name"
        assert is_name(name)
    for name in bad:
        assert not re.fullmatch(NAME_PATTERN, name), f"{name!r} should be refused"
        assert not is_name(name)

    assert not is_name("a" * 200), "an unbounded name is still a bad idea"
    assert not is_name(None)


def test_video_filenames_are_a_filename_and_a_known_extension():
    for name in ("clap.mp4", "prise-3.MOV", "a.b.mkv", "001_take.webm"):
        assert is_video_filename(name), f"{name!r} should be accepted"
    for name in ("../../etc/passwd", "/etc/passwd", "a/b.mp4", "a\\b.mp4",
                 "..", "clip.exe", "clip", "clip.mp4.exe", ".mp4", ""):
        assert not is_video_filename(name), f"{name!r} should be refused"


def test_a_video_file_resolves_under_its_own_take():
    with _Tree() as t:
        take_dir = t.make_take("s1", "001_a")
        os.symlink(os.path.join(t.outside, "secret.txt"),
                   os.path.join(take_dir, "escape.mp4"))

        assert t.sm.video_path("s1", "001_a", "clap.mp4") == \
            os.path.join(os.path.realpath(take_dir), "clap.mp4")

        for bad in ("../../../etc/passwd", "/etc/passwd", "escape.mp4"):
            try:
                t.sm.video_path("s1", "001_a", bad)
            except (UnsafePath, ValueError):
                pass
            else:
                raise AssertionError(f"video_path accepted {bad!r}")


# ── The routes: a malformed name is a 422, a legal one still works ──────────

def _call(app, method: str, path: str, body=None) -> tuple[int, str]:
    """
    Minimal ASGI client — no httpx, and `path` is handed over verbatim.

    That last point is the whole reason this is not `TestClient`: an HTTP client
    normalises `..` away before sending, which is exactly the input under test.
    """
    payload = b"" if body is None else json.dumps(body).encode()
    scope = {
        "type": "http", "asgi": {"version": "3.0"}, "http_version": "1.1",
        "method": method, "scheme": "http", "path": path,
        "raw_path": path.encode(), "root_path": "", "query_string": b"",
        "client": ("127.0.0.1", 1), "server": ("127.0.0.1", 8000),
        "headers": [(b"host", b"127.0.0.1"),
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(payload)).encode())],
    }
    sent = [{"type": "http.request", "body": payload, "more_body": False}]
    status, chunks = [], []

    async def receive():
        return sent.pop(0) if sent else {"type": "http.disconnect"}

    async def send(msg):
        if msg["type"] == "http.response.start":
            status.append(msg["status"])
        elif msg["type"] == "http.response.body":
            chunks.append(msg.get("body", b""))

    asyncio.run(app(scope, receive, send))
    return status[0], b"".join(chunks).decode()


def _app() -> FastAPI:
    app = FastAPI()
    app.include_router(router)
    return app


def test_playback_start_refuses_a_name_that_is_not_one():
    app = _app()
    for body in ({"session": "/etc/passwd", "take": "001_a"},
                 {"session": "s", "take": "/etc/passwd"},
                 {"session": "..", "take": "001_a"},
                 {"session": "s", "take": "../../.."},
                 {"session": "s/../..", "take": "001_a"}):
        status, text = _call(app, "POST", "/api/playback/start", body)
        assert status == 422, f"{body} → {status} {text}"

    # …and a legal pair still reaches the handler: 404 (no such take here), which
    # is the proof the pattern did not simply forbid everything.
    status, _ = _call(app, "POST", "/api/playback/start",
                      {"session": "2026-06-13_14-30_trianon", "take": "001_a"})
    assert status == 404


def test_patch_take_refuses_an_unencoded_dot_dot_in_the_url():
    app = _app()
    status, text = _call(app, "PATCH", "/api/sessions/a/takes/..", {"title": "x"})
    assert status == 422, f"PATCH …/takes/.. → {status} {text}"

    status, text = _call(app, "PATCH", "/api/sessions/../takes/001_a", {"title": "x"})
    assert status == 422, f"PATCH …/sessions/.. → {status} {text}"

    status, _ = _call(app, "PATCH", "/api/sessions/2026-06-13_14-30_x/takes/001_a",
                      {"title": "x"})
    assert status == 404, "a legal pair must still reach the handler"


def test_pose_track_refuses_an_unencoded_dot_dot_in_the_url():
    """
    The same two positions as the PATCH route, on the other endpoint taking a
    session/take pair.  Containment always held here — `take_path()` calls
    `confine()` whatever the handler declares — so what this pins is the
    *answer*: a name that is a path must be refused the way its siblings refuse
    it, not raise UnsafePath out of the handler as a 500 with a traceback.
    """
    app = _app()
    status, text = _call(app, "GET", "/api/sessions/a/takes/../pose")
    assert status == 422, f"GET …/takes/../pose → {status} {text}"

    status, text = _call(app, "GET", "/api/sessions/../takes/001_a/pose")
    assert status == 422, f"GET …/sessions/../… → {status} {text}"

    status, _ = _call(app, "GET",
                      "/api/sessions/2026-06-13_14-30_x/takes/001_a/pose")
    assert status == 404, "a legal pair must still reach the handler"


def test_patch_take_refuses_a_video_file_that_is_a_path():
    app = _app()
    url = "/api/sessions/2026-06-13_14-30_x/takes/001_a"
    for bad in ("../../etc/passwd", "/etc/passwd", "a/b.mp4", "clip.exe", ".."):
        status, text = _call(app, "PATCH", url, {"video_file": bad})
        assert status == 422, f"video_file={bad!r} → {status} {text}"

    # An empty string is how the field is cleared, and must stay allowed.
    for ok in ("clap.mp4", ""):
        status, _ = _call(app, "PATCH", url, {"video_file": ok})
        assert status == 404, f"video_file={ok!r} should have reached the handler"


def test_the_request_models_reject_before_any_handler_runs():
    """The same rule, asserted on the models themselves — routes are not the
    only future caller of these bodies."""
    for kwargs in ({"session": "/etc/passwd", "take": "a"},
                   {"session": "..", "take": "a"},
                   {"session": "a", "take": ""}):
        try:
            PlaybackRequest(**kwargs)
        except Exception:
            pass
        else:
            raise AssertionError(f"PlaybackRequest accepted {kwargs}")

    try:
        TakeUpdate(video_file="../x.mp4")
    except Exception:
        pass
    else:
        raise AssertionError("TakeUpdate accepted a traversing video_file")

    assert TakeUpdate(video_file="clap.mp4").video_file == "clap.mp4"
    assert PlaybackRequest(session="s", take="001_a").speed == 1.0


# ── The same defect one directory over: profiles ────────────────────────────
#
# `ProfileRequest.name` reaches `os.path.join` in two stores that share nothing
# but this shape — `params/` for model parameters, `mappings/` for OSC routes.
# Both are checked together on purpose: a fix applied to one and forgotten in the
# other is the likeliest way this comes back.

def _stores(directory: str):
    """The two profile stores, which must answer identically about a name."""
    return (ParamStore(directory=directory), RouteTable(directory=directory))


def test_a_profile_name_that_is_a_path_is_refused():
    """
    The defect, restated for profiles: `save_profile("/tmp/pwned")` *was*
    `/tmp/pwned.json`.  As with takes, the only thing bounding it was accidental
    — here the fixed `.json` suffix, which nobody chose as a defence and which
    stops nothing a `.json` config file would not already be.
    """
    with tempfile.TemporaryDirectory() as tmp:
        assert os.path.join(tmp, "/tmp/pwned.json") == "/tmp/pwned.json", \
            "join still drops the root — that is the whole defect"

        for store in _stores(tmp):
            kind = type(store).__name__
            for bad in ("/tmp/pwned", "/etc/cron.d/x", "../../etc/x",
                        "../outside", "a/../../b"):
                try:
                    store.profile_path(bad)
                except UnsafePath:
                    pass
                else:
                    raise AssertionError(f"{kind}.profile_path accepted {bad!r}")

            # …and an ordinary name still resolves where it always did.
            assert store.profile_path("show1") == \
                os.path.join(os.path.realpath(tmp), "show1.json")

            # A bare `..` is the one input the two layers judge differently, and
            # it is worth being explicit about which one does the work.  Because
            # the suffix is glued on *inside* the confined segment, `..` names
            # `...json` — a real, contained file — so containment has no reason
            # to object.  The shape layer is what refuses it (see the route test
            # below), and this is the one place the two are not interchangeable.
            for inert in ("..", "."):
                assert store.profile_path(inert).startswith(os.path.realpath(tmp))
                assert not is_name(inert), \
                    f"{inert!r} must be refused by shape, since containment will not"


def test_saving_an_escaping_profile_writes_nothing():
    """
    Containment has to happen before the write, not merely be reported after it.
    """
    with tempfile.TemporaryDirectory() as base:
        store_dir = os.path.join(base, "store")
        target    = os.path.join(base, "pwned.json")

        for store in _stores(store_dir):
            try:
                store.save_profile(os.path.join(base, "pwned"))
            except UnsafePath:
                pass
            else:
                raise AssertionError(f"{type(store).__name__} saved outside its dir")
            assert not os.path.exists(target), "the escaping write happened anyway"


def test_a_profile_symlinked_out_of_its_directory_is_refused():
    """
    The layer the shape check cannot see: `evil` is a perfectly legal name, and
    only resolving the link finds it pointing elsewhere.
    """
    with tempfile.TemporaryDirectory() as base:
        store_dir = os.path.join(base, "store")
        os.makedirs(store_dir)
        outside = os.path.join(base, "secret.json")
        with open(outside, "w") as f:
            json.dump({"stolen": True}, f)
        os.symlink(outside, os.path.join(store_dir, "evil.json"))

        for store in _stores(store_dir):
            assert is_name("evil"), "the shape layer has no objection here"
            try:
                store.profile_path("evil")
            except UnsafePath:
                pass
            else:
                raise AssertionError(f"{type(store).__name__} followed a symlink out")


def test_a_profile_on_disk_never_disappears_from_the_listing():
    """
    The same rule as `list_takes()`, and the same way of getting it wrong.

    Profile names are *not* slugified on the way in — `save_profile("x")` writes
    `x.json` verbatim — so a profile saved before this shape rule existed can
    easily be named something a request may no longer send.  It keeps listing:
    validation rejects an input, never a file already saved.  Routing
    `list_profiles()` through `profile_path()` is exactly what would break this.
    """
    with tempfile.TemporaryDirectory() as tmp:
        for filename in ("réglages roue.json", "essai 3.json", "show1.json"):
            with open(os.path.join(tmp, filename), "w") as f:
                json.dump({}, f)

        for store in _stores(tmp):
            assert store.list_profiles() == ["essai 3", "réglages roue", "show1"], \
                f"{type(store).__name__} hid a profile that exists"

        assert not is_name("réglages roue"), "…and a request may still not name it"


def test_the_profile_stores_still_round_trip_a_legal_name():
    """The fix must not have closed the door on the ordinary case."""
    with tempfile.TemporaryDirectory() as tmp:
        store = ParamStore(directory=tmp)
        store.declare("seuil", default=2.5, min=0.0, max=10.0)
        store.set("seuil", 4.0)
        store.save_profile("2026-08-10_show")

        reloaded = ParamStore(directory=tmp)
        reloaded.declare("seuil", default=2.5, min=0.0, max=10.0)
        reloaded.load_profile("2026-08-10_show")
        assert reloaded.values()["seuil"] == 4.0
        assert reloaded.list_profiles() == ["2026-08-10_show"]


def test_profile_routes_refuse_a_name_that_is_not_one():
    """
    Layer 1 at the boundary: all four endpoints share `ProfileRequest`, so all
    four are checked — including `save`, where a traversal is a *write*.
    """
    app = _app()
    for path in ("/api/model/params/save", "/api/model/params/load",
                 "/api/osc/routes/save", "/api/osc/routes/load"):
        for name in ("/tmp/pwned", "..", "../../etc/x", "a/b", "a\\b", "",
                     "réglages roue", "a" * 200):
            status, text = _call(app, "POST", path, {"name": name})
            assert status == 422, f"POST {path} name={name!r} → {status} {text}"

    # A legal name still reaches the handler: 404, since no such profile exists
    # here.  Probed on `load` rather than `save` deliberately — `save` would
    # write into the repo's own params/, and a test must not leave one behind.
    for path in ("/api/model/params/load", "/api/osc/routes/load"):
        status, text = _call(app, "POST", path, {"name": "profil-inexistant"})
        assert status == 404, f"POST {path} → {status} {text}"


def test_a_profile_route_answers_400_rather_than_crashing_on_a_symlink():
    """
    Layer 2 at the boundary.  `evil` passes the shape check, so this reaches the
    handler and `confine()` raises inside it — and UnsafePath is a ValueError,
    which without the catch is a 500.  A containment refusal must be an answer,
    not a crash.
    """
    import core

    app = _app()
    with tempfile.TemporaryDirectory() as base:
        store_dir = os.path.join(base, "store")
        os.makedirs(store_dir)
        outside = os.path.join(base, "secret.json")
        with open(outside, "w") as f:
            json.dump({}, f)
        os.symlink(outside, os.path.join(store_dir, "evil.json"))

        for store, path in ((core.model.params, "/api/model/params/load"),
                            (core.osc_routes,   "/api/osc/routes/load")):
            original = store._dir
            store._dir = store_dir
            try:
                status, text = _call(app, "POST", path, {"name": "evil"})
                assert status == 400, f"POST {path} → {status} {text}"
            finally:
                store._dir = original


def test_the_profile_request_model_rejects_before_any_handler_runs():
    """Asserted on the model itself — the routes are not its only future caller."""
    for name in ("/tmp/pwned", "..", "a/b", "", "a b"):
        try:
            ProfileRequest(name=name)
        except Exception:
            pass
        else:
            raise AssertionError(f"ProfileRequest accepted {name!r}")

    assert ProfileRequest(name="2026-08-10_show").name == "2026-08-10_show"


def main() -> None:
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"  ok  {name}")


if __name__ == "__main__":
    main()
