"""
storage/paths.py — What a session, take or video name is allowed to be.

`os.path.join` drops every component preceding an absolute one, so a name that
arrives from outside can *become* the whole path:

    os.path.join("sessions", "a", "takes", "/etc/passwd") == "/etc/passwd"

No `..`, no encoding: a leading `/` is enough.  What used to bound the damage was
accidental — the callers glue a fixed `raw.csv` / `take.json` onto the result —
and the first endpoint serving a file named in metadata removes it.

Two independent layers, in the order they run:

  1. **shape** — `NAME_PATTERN`, declared on the request models and the path
     parameters, so a malformed name is a 422 before a handler sees it;
  2. **containment** — `confine()`, called by `SessionManager`'s path builders,
     which closes every caller at once rather than route by route.

Neither layer trusts the other, and neither is where a name *already on disk* is
judged: a take that exists must keep listing even if something named it oddly.
"""

import os
import re

# One path segment: the characters `_slug()` emits plus the `NNN_` and
# `<date>_<time>_` prefixes the manager itself builds, and at least one
# alphanumeric among them.
#
# That last clause is what excludes the literal `..` (and `.`, `...`, `-`, `_`)
# without a negative look-ahead — pydantic v2 compiles this pattern with the Rust
# regex engine, which has no look-around at all.  The anchors are not decoration
# either: pydantic's `pattern` is a *search*, so an unanchored version would find
# "a" inside "a/b" and accept it.
NAME_PATTERN = r"^[A-Za-z0-9._-]*[A-Za-z0-9][A-Za-z0-9._-]*$"
MAX_NAME_LEN = 128

# Extensions a browser can be asked to play. The list is the point: `video_file`
# is a free `str` edited through PATCH, and it is what a future GET .../video
# would open.
VIDEO_EXTENSIONS = frozenset({".mp4", ".mov", ".m4v", ".mkv", ".webm", ".avi"})

_NAME_RE = re.compile(NAME_PATTERN)


class UnsafePath(ValueError):
    """
    A name that may not be turned into a path: it resolved outside the tree it
    was required to stay in, or it was never the shape of a name to begin with.

    One type rather than two, because every caller wants the same answer — the
    routes turn it into a 400, the listings treat it like an unreadable entry.
    """


def is_name(name: object) -> bool:
    """True if `name` is a legal session or take directory name."""
    return (isinstance(name, str)
            and len(name) <= MAX_NAME_LEN
            and _NAME_RE.fullmatch(name) is not None)


def is_video_filename(name: object) -> bool:
    """
    True if `name` is a bare filename with a known video extension.

    Looser than `is_name()` on purpose — this file was named by a camera or by
    the user, not by `_slug()`, and "Trianon prise 3.mov" is a perfectly ordinary
    thing to point at.  What it may not contain is either separator (whatever
    *this* platform considers one — the value is metadata that travels) or a `..`
    anywhere in it.
    """
    if not isinstance(name, str) or not name or len(name) > MAX_NAME_LEN:
        return False
    if "/" in name or "\\" in name or "\0" in name or ".." in name:
        return False
    stem, ext = os.path.splitext(name)
    return bool(stem) and ext.lower() in VIDEO_EXTENSIONS


def confine(root: str, name: str) -> str:
    """
    Resolve one untrusted `name` under a trusted `root`, or raise UnsafePath.

    The three layers Starlette already applies to StaticFiles, in that order:

      1. refuse an absolute name — `os.path.join` would silently drop `root`;
      2. `os.path.realpath`, which resolves `..` *and* symlinks (where `normpath`
         alone happily follows a link out of the tree while the string still
         looks like it stays inside);
      3. `os.path.commonpath` against the resolved root.

    One name against *its own* parent, deliberately: resolving a whole chain at
    once would accept `take_path("a", "..")`, which lands on the session
    directory — still inside sessions/, and still not a take.  Chaining the calls
    is what makes each segment stay where it belongs.

    The root itself is refused too: `sessions/` is not a session, and a
    containment test alone would let an empty name name it.
    """
    if not isinstance(name, str):
        raise UnsafePath(f"Not a path segment: {name!r}")
    if os.path.isabs(name):
        raise UnsafePath(f"Absolute path refused: {name!r}")
    if "\0" in name:
        raise UnsafePath("NUL byte in path segment")

    base = os.path.realpath(root)
    full = os.path.realpath(os.path.join(base, name))

    if full == base or os.path.commonpath([full, base]) != base:
        raise UnsafePath(f"{name!r} escapes {root!r}")
    return full
