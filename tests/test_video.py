"""
tests/test_video.py — Serving a take's video, and reporting what its folder holds.

This is the one module whose subject genuinely *is* the HTTP boundary, so it is
the one that goes through `TestClient`.  What the endpoint has to get right is a
status line and three headers — a `Content-Type` that comes from our table and
not from the machine's, a `206` with a `Content-Range`, a `404` that says which
of two absences it means — and testing under the handler would leave every one
of those unexercised where it applies.

`TestClient(app)` is instantiated **without** a `with` block, deliberately: the
context manager runs the lifespan, which boots the UDP socket, the WS server, the
ESP configurator and the processing loop.  None of that is involved in serving a
file, and booting it to exercise an extension whitelist would be a seam badly
placed.  The routes here touch `core.session_manager` and nothing else, so
pointing that singleton at a temporary tree is the whole fixture.

The one case `TestClient` cannot express is a literal `..` in the URL — httpx
normalises it away before sending, so such a test would be testing httpx.  That
one keeps using `test_paths._call`, the raw ASGI client, for the reason its own
docstring gives.

Two rules are pinned here that are easy to get backwards:

  * **the whitelist is the MIME table** — one list doing both jobs, deciding what
    the endpoint may serve and what `Content-Type` it serves it with, so an
    extension can never be servable without a declared type or vice versa;
  * **the scan proposes, `video_file` records.** The listing reports both, and no
    `GET` ever writes: adoption is an explicit `PATCH` by the alignment page, and
    the day a video is matched to a take automatically it plugs in exactly there.
"""

import json
import mimetypes
import os
import shutil
import tempfile
from dataclasses import asdict

from starlette.testclient import TestClient

import core
from storage.paths import VIDEO_EXTENSIONS, VIDEO_MEDIA_TYPES, video_media_type
from storage.session_manager import SessionManager, TakeMeta

from tests.test_paths import _app, _call


SESSION = "2026-06-13_14-30_trianon"
TAKE    = "001_premier-essai"

# Nothing decodes this — the endpoint serves bytes and names their type; what a
# browser does with them afterwards is a codec question no header settles.
VIDEO_BYTES = bytes(range(256)) * 40          # 10 240 o


class _Tree:
    """A sessions root with `core`'s manager pointed at it, and a client on it."""

    def __enter__(self):
        self.base    = tempfile.mkdtemp(prefix="conductor-video-")
        self.root    = os.path.join(self.base, "sessions")
        self.outside = os.path.join(self.base, "outside")
        os.makedirs(self.outside)
        with open(os.path.join(self.outside, "secret.mp4"), "wb") as f:
            f.write(b"not yours")

        self.sm = SessionManager(self.root)
        self._saved_sm = core.session_manager
        core.session_manager = self.sm

        # No `with`: the lifespan stays unrun. See the module docstring.
        self.client = TestClient(_app())
        return self

    def __exit__(self, *exc):
        core.session_manager = self._saved_sm
        shutil.rmtree(self.base, ignore_errors=True)

    def make_take(self, take: str = TAKE, session: str = SESSION,
                  meta: dict | None = None) -> str:
        """A take as it exists on disk: raw.csv is what makes it one."""
        take_dir = os.path.join(self.root, session, "takes", take)
        os.makedirs(take_dir, exist_ok=True)
        with open(os.path.join(self.root, session, "session.json"), "w") as f:
            json.dump({"name": session, "title": "essai"}, f)
        open(os.path.join(take_dir, "raw.csv"), "w").close()
        with open(os.path.join(take_dir, "take.json"), "w") as f:
            json.dump(meta or asdict(TakeMeta(name=take, index=1)), f)
        return take_dir

    def put_file(self, take_dir: str, name: str,
                 data: bytes = VIDEO_BYTES) -> str:
        path = os.path.join(take_dir, name)
        with open(path, "wb") as f:
            f.write(data)
        return path

    def url(self, take: str = TAKE, session: str = SESSION) -> str:
        return f"/api/sessions/{session}/takes/{take}/video"

    def adopt(self, filename: str, take: str = TAKE, session: str = SESSION):
        """What the alignment page does with the scan's answer: a PATCH."""
        return self.client.patch(f"/api/sessions/{session}/takes/{take}",
                                 json={"video_file": filename})


def _tree_state(root: str) -> dict:
    """Every file under `root` with its bytes — what a GET must leave untouched."""
    state = {}
    for dirpath, _dirs, files in os.walk(root):
        for name in files:
            path = os.path.join(dirpath, name)
            with open(path, "rb") as f:
                state[os.path.relpath(path, root)] = f.read()
    return state


# ── The whitelist and the table are the same list ───────────────────────────

def test_the_extension_whitelist_is_the_media_type_table():
    """
    One list, two jobs.  Splitting them is how an extension ends up servable with
    no declared type (so guessed, machine-dependently) or storable and then
    unservable — a `video_file` the panel accepts and the player never gets.
    """
    assert VIDEO_EXTENSIONS == frozenset(VIDEO_MEDIA_TYPES), \
        "an extension is servable exactly when the table names its type"

    assert video_media_type("clap.mp4")   == "video/mp4"
    assert video_media_type("clap.m4v")   == "video/mp4"
    assert video_media_type("clap.MOV")   == "video/quicktime"
    assert video_media_type("clap.webm")  == "video/webm"

    # Never a type guessed to be near-right: the HTML spec explicitly rescues a
    # parameterless application/octet-stream (the browser sniffs the container),
    # while a recognised-but-wrong type makes the <video> fail outright.
    assert video_media_type("clap.xyz") == "application/octet-stream"


# ── Serving ─────────────────────────────────────────────────────────────────

def test_a_video_is_served_with_the_type_our_table_gives_it():
    """
    The `Content-Type` comes from our table, and the proof is that the machine's
    own table is poisoned while the request runs.  `mimetypes` initialises from
    system files — here `/etc/apache2/mime.types` supplies `.m4v` and `.mkv`,
    which Python's built-in table does not — so a guess is a different answer on
    a different machine, which is the whole reason for a table of our own.
    """
    cases = (("clap.mp4",    "video/mp4"),
             ("clap.m4v",    "video/mp4"),
             ("Prise 3.MOV", "video/quicktime"),
             ("clap.webm",   "video/webm"))
    with _Tree() as t:
        for i, (filename, expected) in enumerate(cases, start=1):
            take = f"{i:03d}_essai"
            take_dir = t.make_take(take)
            t.put_file(take_dir, filename)
            r = t.adopt(filename, take)
            assert r.status_code == 200, r.text

            saved = mimetypes.types_map.get(".mp4")
            mimetypes.add_type("application/x-machine-guess", ".mp4")
            try:
                r = t.client.get(t.url(take))
            finally:
                if saved:
                    mimetypes.add_type(saved, ".mp4")

            assert r.status_code == 200, r.text
            assert r.headers["content-type"] == expected, \
                f"{filename} served as {r.headers['content-type']}"
            assert r.content == VIDEO_BYTES, "the bytes came back altered"
            assert r.headers.get("accept-ranges") == "bytes"
            assert "content-disposition" not in r.headers, \
                "a download prompt where a <video> element is expected"


def test_a_range_request_gets_206_and_a_content_range():
    """
    The seek the alignment page's frame stepping rests on.  Nothing here is our
    code — starlette's FileResponse has answered ranges since 0.39.0 — which is
    exactly why it is asserted rather than assumed: the day the endpoint stops
    being a FileResponse, stepping through a video breaks silently and nowhere
    near here.
    """
    with _Tree() as t:
        take_dir = t.make_take()
        t.put_file(take_dir, "clap.mp4")
        t.adopt("clap.mp4")

        r = t.client.get(t.url(), headers={"Range": "bytes=0-99"})
        assert r.status_code == 206, r.text
        assert r.headers["content-range"] == f"bytes 0-99/{len(VIDEO_BYTES)}"
        assert r.content == VIDEO_BYTES[:100]

        r = t.client.get(t.url(), headers={"Range": "bytes=5000-5009"})
        assert r.status_code == 206
        assert r.content == VIDEO_BYTES[5000:5010]

        r = t.client.get(t.url(), headers={"Range": "bytes=-50"})
        assert r.status_code == 206
        assert r.headers["content-range"] == \
            f"bytes {len(VIDEO_BYTES) - 50}-{len(VIDEO_BYTES) - 1}/{len(VIDEO_BYTES)}"


def test_no_video_and_no_take_are_two_different_404s():
    """
    Four absences, four answers — the alignment page has a different gesture for
    each (copy the rushes over, pick another take, look at what the folder
    actually holds).  A single "404" for all of them would make them one.
    """
    with _Tree() as t:
        take_dir = t.make_take()                       # stored video_file is ""

        r = t.client.get(t.url())
        assert r.status_code == 404, r.text
        no_video = r.json()["detail"]

        r = t.client.get(t.url(take="009_jamais"))
        assert r.status_code == 404, r.text
        no_take = r.json()["detail"]

        # A stored name whose file was moved away afterwards: the take has a
        # video, the folder does not.
        t.adopt("clap.mp4")
        r = t.client.get(t.url())
        assert r.status_code == 404, r.text
        missing = r.json()["detail"]

        assert len({no_video, no_take, missing}) == 3, \
            f"three absences, indistinguishable: {no_video!r} {no_take!r} {missing!r}"

        # …and it is the take's own emptiness, not a coincidence: putting the
        # file back is all it takes.
        t.put_file(take_dir, "clap.mp4")
        assert t.client.get(t.url()).status_code == 200


def test_an_extension_outside_the_whitelist_is_refused_even_when_the_file_is_there():
    """
    Validating `video_file` on the way in is not enough: a take.json already on
    disk was written by some other version, or by hand, and `load_take` is
    deliberately tolerant of what it finds there.  The name is therefore judged
    again at the moment it becomes a path — otherwise the endpoint publishes
    whatever the metadata points at, `raw.csv` and `take.json` included, which is
    the very thing a static mount was rejected for.
    """
    # `.mkv` and `.avi` are here rather than among the playable ones on purpose:
    # the whitelist is the media-type table, and nothing in it names them.
    decoys = ("notes.txt", "clip.mkv", "archive.avi", "clip.mp4.exe")

    with _Tree() as t:
        for stored in decoys + ("raw.csv", "take.json"):
            take = "001_essai"
            take_dir = t.make_take(take, meta={"name": take, "index": 1,
                                               "video_file": stored})
            if stored in decoys:
                t.put_file(take_dir, stored, data=b"secret payload")

            r = t.client.get(t.url(take))
            assert r.status_code == 400, f"video_file={stored!r} → {r.status_code}"
            assert b"secret payload" not in r.content
            # The two the take is actually made of: publishing either is what a
            # static mount would have done, and the reason there is none.
            assert b'"video_file"' not in r.content
            shutil.rmtree(os.path.dirname(take_dir))


def test_a_traversal_through_video_file_is_refused_at_the_http_boundary():
    """
    The net #11 hung under `take_path()`, exercised where it actually catches.
    This endpoint is what removes the accidental defence the fixed `raw.csv` /
    `take.json` suffixes provided: `video_file` is a free string, so without
    confinement a PATCH followed by this GET is an arbitrary file read.

    Both layers are probed — a name that is plainly a path, and a name that is
    perfectly well-shaped but resolves out of the take through a symlink, which
    only `realpath` sees.
    """
    with _Tree() as t:
        secret = os.path.join(t.outside, "secret.mp4")

        for stored in ("../../../etc/passwd", "/etc/passwd", "../secret.mp4",
                       "a/b.mp4", "..", secret):
            take = "001_essai"
            t.make_take(take, meta={"name": take, "index": 1, "video_file": stored})
            r = t.client.get(t.url(take))
            assert r.status_code == 400, f"video_file={stored!r} → {r.status_code}"
            assert b"not yours" not in r.content
            shutil.rmtree(os.path.join(t.root, SESSION))

        # Well-shaped, inside the take by its name, outside it once resolved —
        # the case only realpath sees, and the reason the check is not a string
        # test.  Stored by hand: the PATCH validator has no objection either.
        take_dir = t.make_take()
        os.symlink(secret, os.path.join(take_dir, "escape.mp4"))
        assert t.adopt("escape.mp4").status_code == 200
        r = t.client.get(t.url())
        assert r.status_code == 400, \
            f"a symlink out of the take was served: {r.status_code}"
        assert b"not yours" not in r.content


def test_a_malformed_session_or_take_name_is_422():
    """
    Layer 1, at the boundary: the shape rule is declared on the parameters, so a
    name that is not one is refused before this handler exists as far as the
    request is concerned.

    The dot segments go through the raw ASGI client on purpose — httpx removes
    `.` and `..` from a path before sending, so a `TestClient` version of those
    would be asserting something about httpx (see tests/test_paths.py).
    """
    with _Tree() as t:
        for session, take in (("a b", TAKE), (SESSION, "prise n°1"),
                              ("a" * 200, TAKE), (SESSION, "-")):
            r = t.client.get(t.url(take=take, session=session))
            assert r.status_code == 422, \
                f"{session}/{take} → {r.status_code} {r.text}"

    app = _app()
    for path in ("/api/sessions/a/takes/../video",
                 "/api/sessions/../takes/001_a/video",
                 "/api/sessions/a/takes/./video"):
        status, text = _call(app, "GET", path)
        assert status == 422, f"GET {path} → {status} {text}"


# ── The listing: what is stored, and what the folder holds ──────────────────

def test_the_listing_reports_the_stored_name_and_the_scan_without_writing():
    """
    The scan proposes, `video_file` records — and the two are reported side by
    side because only the page can tell whether they should agree.  No `GET`
    adopts: writing the scan's answer into take.json here would make a listing a
    mutation, and would silently settle a choice (which of two files, and is this
    the right one) that the operator is the one looking at.
    """
    with _Tree() as t:
        aligned = t.make_take("001_avec-video",
                              meta={"name": "001_avec-video", "index": 1,
                                    "video_file": "clap.mp4"})
        t.put_file(aligned, "clap.mp4")
        t.put_file(aligned, "autre.MOV")
        t.put_file(aligned, "notes.txt", data=b"pas une video")

        orphan = t.make_take("002_rushes-copies")   # a file, no stored name yet
        t.put_file(orphan, "prise.mp4")

        t.make_take("003_sans-video")               # neither

        before = _tree_state(t.root)
        takes = t.client.get("/api/sessions").json()["sessions"][0]["takes"]
        assert _tree_state(t.root) == before, "a GET wrote to the sessions tree"

        by_name = {x["name"]: x for x in takes}
        assert set(by_name) == {"001_avec-video", "002_rushes-copies",
                                "003_sans-video"}

        assert by_name["001_avec-video"]["video_file"] == "clap.mp4"
        assert by_name["001_avec-video"]["videos_found"] == ["autre.MOV", "clap.mp4"], \
            "the scan reports the folder, whitelist-filtered, whatever is stored"

        assert by_name["002_rushes-copies"]["video_file"] == ""
        assert by_name["002_rushes-copies"]["videos_found"] == ["prise.mp4"], \
            "a copied-in rush must be proposable before anything is stored"

        assert by_name["003_sans-video"]["video_file"] == ""
        assert by_name["003_sans-video"]["videos_found"] == []


def test_the_scan_never_proposes_a_file_the_endpoint_would_refuse():
    """
    Same settlement `list_takes()` makes about a take symlinked out of the tree:
    proposing it would promise a service that will not happen, since the check
    that refuses to serve it is the one that refused to list it.
    """
    with _Tree() as t:
        take_dir = t.make_take()
        t.put_file(take_dir, "clap.mp4")
        os.symlink(os.path.join(t.outside, "secret.mp4"),
                   os.path.join(take_dir, "escape.mp4"))
        os.makedirs(os.path.join(take_dir, "dossier.mp4"))

        assert t.sm.scan_videos(take_dir) == ["clap.mp4"]


def main() -> None:
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"  ok  {name}")


if __name__ == "__main__":
    main()
