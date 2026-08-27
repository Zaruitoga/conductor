"""
tests/test_data_root.py — Runtime data belongs to the machine, not to a branch.

`sessions/`, `params/` and `mappings/` are gitignored on purpose: they are what
this machine recorded, not what a branch says.  The unintended consequence was
that from an agent's worktree they did not exist at all — the panel and the viz
listed zero takes, and nothing touching a replay, a pose track, a video or an
alignment could be checked by hand.

`config.data_path()` resolves that root instead of assuming it sits under the
current working directory.  What this module pins down is the resolution itself
and, above all, its two fallbacks: a *main* checkout must keep exactly today's
relative paths, and a copy with no `.git` at all — an archive, a `pip download`,
a folder someone duplicated — must start rather than raise.

Everything here happens in a temp directory holding a *fake* `.git`.  The real
repository is never read and no runtime directory is ever created, which is the
condition for testing a resolution that otherwise points straight at the takes.
"""

import os
import shutil
import tempfile

import config
from model.params import PARAMS_DIR
from osc.routes import MAPPINGS_DIR
from storage.paths import UnsafePath, confine
from storage.session_manager import SESSIONS_DIR


# ── Fake checkouts ──────────────────────────────────────────────────────────

class _Fake:
    """A temp dir shaped like a checkout, plus the main one a worktree names."""

    def __enter__(self):
        self.base      = tempfile.mkdtemp(prefix="conductor-root-")
        self.main      = os.path.join(self.base, "conductor")
        self.worktree  = os.path.join(self.base, "wt", "strange-hertz")
        os.makedirs(os.path.join(self.main, ".git", "worktrees"))
        os.makedirs(self.worktree)
        return self

    def __exit__(self, *exc):
        shutil.rmtree(self.base, ignore_errors=True)

    def dot_git_file(self, where: str, line: str) -> str:
        with open(os.path.join(where, ".git"), "w") as f:
            f.write(line)
        return where

    def worktree_of_main(self, name: str = "strange-hertz") -> str:
        """The real shape: one `gitdir:` line pointing into main's .git."""
        gitdir = os.path.join(self.main, ".git", "worktrees", name)
        os.makedirs(gitdir, exist_ok=True)
        return self.dot_git_file(self.worktree, f"gitdir: {gitdir}\n")


class _NoEnv:
    """CONDUCTOR_DATA unset (or set) for the duration, restored after."""

    def __init__(self, value: str | None = None):
        self.value = value

    def __enter__(self):
        self.before = os.environ.get("CONDUCTOR_DATA")
        if self.value is None:
            os.environ.pop("CONDUCTOR_DATA", None)
        else:
            os.environ["CONDUCTOR_DATA"] = self.value
        return self

    def __exit__(self, *exc):
        if self.before is None:
            os.environ.pop("CONDUCTOR_DATA", None)
        else:
            os.environ["CONDUCTOR_DATA"] = self.before


# ── The three shapes a checkout can have ────────────────────────────────────

def test_a_worktree_resolves_to_its_main_checkout():
    """
    The whole point.  A worktree's `.git` is a *file* holding one line —
    `gitdir: <main>/.git/worktrees/<name>` — and the main checkout is what
    precedes it.  Nothing is configured, nothing is installed, and it holds for
    every worktree present and future on any machine.
    """
    with _NoEnv(), _Fake() as fake:
        wt = fake.worktree_of_main()
        assert config._resolve_data_root(wt) == os.path.realpath(fake.main)

        # …and the data therefore lands beside the main checkout's takes, not
        # inside the worktree, which is the failure this ticket is about.
        root = config._resolve_data_root(wt)
        assert os.path.join(root, "sessions").startswith(os.path.realpath(fake.main))
        assert not os.path.join(root, "sessions").startswith(os.path.realpath(wt))


def test_a_main_checkout_keeps_todays_relative_paths():
    """
    `.git` is a directory: unchanged behaviour, down to the string.  A resolved
    absolute path here would be a change nobody asked for — the main checkout's
    `sessions/` already is the data.
    """
    with _NoEnv(), _Fake() as fake:
        assert config._resolve_data_root(fake.main) == ""
        assert os.path.join(config._resolve_data_root(fake.main), "sessions") == "sessions"


def test_a_copy_without_any_git_starts_rather_than_raising():
    """
    An archive, a `pip download`, a duplicated folder.  Detection *fails* by
    falling back to today's behaviour, never by an exception on import — this
    runs at import time of `config`, so raising here means nothing starts.
    """
    with _NoEnv(), tempfile.TemporaryDirectory() as bare:
        assert config._resolve_data_root(bare) == ""


# ── Everything that is shaped like a worktree and is not one ────────────────

def test_a_gitdir_line_that_is_not_a_worktree_falls_back():
    """
    A submodule's `.git` is a file too, and it points at `.git/modules/<name>`.
    The marker is `.git/worktrees/`, so this is not a worktree and the fallback
    is today's behaviour rather than a root invented from the wrong half of a
    path.
    """
    with _NoEnv(), _Fake() as fake:
        for line in ("gitdir: /Users/x/conductor/.git/modules/sub",
                     "gitdir:",
                     "gitdir: \n",
                     "ref: refs/heads/main",
                     "",
                     "\x00\x01 not text at all"):
            fake.dot_git_file(fake.worktree, line)
            assert config._resolve_data_root(fake.worktree) == "", \
                f"{line!r} was taken for a worktree"


def test_a_git_file_that_cannot_be_read_falls_back_rather_than_raising():
    """
    The other half of "a copy must start": the `.git` file is *there* and the
    read itself fails — bytes that are not text, or permissions that refuse it.
    This runs while `config` is being imported, so an exception escaping here
    does not degrade a listing, it stops the orchestrator from starting at all.
    """
    with _NoEnv(), _Fake() as fake:
        with open(os.path.join(fake.worktree, ".git"), "wb") as f:
            f.write(b"\xff\xfegitdir: /somewhere/.git/worktrees/w\n")
        assert config._resolve_data_root(fake.worktree) == ""

        fake.worktree_of_main()                      # a real one again…
        dot_git = os.path.join(fake.worktree, ".git")
        os.chmod(dot_git, 0o000)                     # …that we may no longer read
        try:
            if not os.access(dot_git, os.R_OK):      # not true when run as root
                assert config._resolve_data_root(fake.worktree) == ""
        finally:
            os.chmod(dot_git, 0o644)


def test_a_bare_repository_has_no_checkout_to_resolve_to():
    """
    A bare repo's worktrees live in `<repo>.git/worktrees/<name>` — the same
    `/worktrees/` in the middle and no checkout at all in front of it.  Taking
    "what precedes" literally would hand back `<repo>`'s *parent* directory, a
    perfectly real folder, and drop the takes next to it.  The `.git` component
    is what tells the two apart, and it has to be checked on a directory that
    exists or the test proves nothing.
    """
    with _NoEnv(), _Fake() as fake:
        bare = os.path.join(fake.base, "conductor.git", "worktrees", "w")
        os.makedirs(bare)
        fake.dot_git_file(fake.worktree, f"gitdir: {bare}\n")
        assert config._resolve_data_root(fake.worktree) == "", \
            "a bare repository's parent directory was taken for a checkout"


def test_a_main_checkout_that_is_gone_falls_back():
    """
    The worktree outlived what it points at (main moved, or was deleted).  A
    root that does not exist would have `os.makedirs` recreate it somewhere
    nobody is looking — the empty-folder trap this ticket exists to close.
    """
    with _NoEnv(), _Fake() as fake:
        gone = os.path.join(fake.base, "disparu")
        fake.dot_git_file(fake.worktree,
                          f"gitdir: {gone}/.git/worktrees/strange-hertz\n")
        assert config._resolve_data_root(fake.worktree) == ""


def test_a_relative_gitdir_is_resolved_against_the_worktree():
    """
    `git worktree add --relative-paths` (git 2.48+) writes a relative `gitdir:`.
    Resolving it against the current working directory instead of against the
    worktree would give a different answer depending on where `main.py` was
    launched, which is the very dependency being removed.
    """
    with _NoEnv(), _Fake() as fake:
        os.makedirs(os.path.join(fake.main, ".git", "worktrees", "rel"), exist_ok=True)
        rel = os.path.relpath(os.path.join(fake.main, ".git", "worktrees", "rel"),
                              fake.worktree)
        fake.dot_git_file(fake.worktree, f"gitdir: {rel}\n")
        assert config._resolve_data_root(fake.worktree) == os.path.realpath(fake.main)


def test_a_relative_gitdir_is_resolved_rather_than_normalised():
    """
    The reason `confine()` calls `realpath` and not `normpath`, one module over:
    `normpath` collapses `..` *textually*, which walks straight through a
    symlinked component and lands somewhere the path never reaches.

    Here the worktree is opened through a link, so the same `../conductor/…`
    line names two different directories depending on which one is used — and
    both exist, which is what makes the wrong answer silent: the takes would be
    read from a decoy checkout that has every right to be there.
    """
    with _NoEnv(), tempfile.TemporaryDirectory() as base:
        true_main = os.path.join(base, "real", "conductor")
        decoy     = os.path.join(base, "conductor")      # where `..` lands textually
        for main in (true_main, decoy):
            os.makedirs(os.path.join(main, ".git", "worktrees", "w"))

        worktree = os.path.join(base, "real", "wt")
        os.makedirs(worktree)
        through_link = os.path.join(base, "lien")
        os.symlink(worktree, through_link)

        with open(os.path.join(worktree, ".git"), "w") as f:
            f.write("gitdir: ../conductor/.git/worktrees/w\n")

        assert config._resolve_data_root(through_link) == os.path.realpath(true_main), \
            "`..` was collapsed textually, through the link"


# ── The override ────────────────────────────────────────────────────────────

def test_conductor_data_wins_over_both_shapes():
    """
    The deliberate isolation: an agent or an experiment writing nowhere near the
    real takes.  It has to win over a worktree *and* over a main checkout, or it
    would only be an override in the case one happened to test it in.
    """
    with _Fake() as fake, tempfile.TemporaryDirectory() as elsewhere:
        wt = fake.worktree_of_main()
        with _NoEnv(elsewhere):
            for repo in (wt, fake.main):
                assert config._resolve_data_root(repo) == os.path.realpath(elsewhere)


def test_an_empty_conductor_data_is_not_an_override():
    """`CONDUCTOR_DATA=` in a shell profile must not silently mean the cwd."""
    with _Fake() as fake:
        wt = fake.worktree_of_main()
        for blank in ("", "   "):
            with _NoEnv(blank):
                assert config._resolve_data_root(wt) == os.path.realpath(fake.main)


# ── What the three roots are made of ────────────────────────────────────────

def test_the_three_runtime_roots_share_one_resolution():
    """
    `sessions/`, `params/` and `mappings/` are the same kind of thing — what
    this machine produced — so they resolve the same way or the fix is half
    done: a profile saved in a séance would still be invisible from the next
    branch.
    """
    assert SESSIONS_DIR == config.data_path("sessions")
    assert PARAMS_DIR   == config.data_path("params")
    assert MAPPINGS_DIR == config.data_path("mappings")

    # And `data_path` is a join on one root, so the main-checkout case really is
    # the bare relative name it has always been.
    assert os.path.join("", "sessions") == "sessions"


# ── The barrier the new root must not have moved ────────────────────────────

def test_containment_still_holds_under_an_absolute_root():
    """
    Checked rather than assumed (the ticket says so).  `confine()` realpaths both
    the root and the candidate, so an absolute root — or one reached through a
    symlink, which is what a worktree's resolved main checkout can be — changes
    nothing: it is still the barrier that keeps a name from outside inside
    `sessions/`.
    """
    with tempfile.TemporaryDirectory() as base:
        real = os.path.join(base, "real")
        os.makedirs(os.path.join(real, "sessions"))
        link = os.path.join(base, "lien")
        os.symlink(real, link)

        for root in (os.path.join(real, "sessions"),      # absolute
                     os.path.join(link, "sessions")):     # …reached through a link
            assert confine(root, "001_essai") == \
                os.path.join(os.path.realpath(real), "sessions", "001_essai")
            for bad in ("/etc/passwd", "..", "../..", "a/../..", ""):
                try:
                    confine(root, bad)
                except UnsafePath:
                    pass
                else:
                    raise AssertionError(f"{bad!r} escaped an absolute root")


def main() -> None:
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"  ok  {name}")


if __name__ == "__main__":
    main()
