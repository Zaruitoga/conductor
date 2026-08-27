"""config.py — Global orchestrator settings."""

import os

# ── Where the runtime data lives ─────────────────────────────────────────────
# `sessions/`, `params/` and `mappings/` are what *this machine* recorded, not
# what a branch says — which is why they are gitignored, and why a git worktree
# (an agent's, typically) does not contain them.  Resolving the root here rather
# than assuming it relative to the current working directory is what makes a
# worktree see the real takes with nothing to install and no link to lay by hand.
#
# The link was the previous workaround and it is a trap: `SessionManager.__init__`
# does `os.makedirs(..., exist_ok=True)`, so *any* import of `core.py` —
# `python3 -m tests.run` included — recreates an empty `sessions/`, after which
# `ln -s` quietly builds `sessions/sessions` and one believes the problem solved
# in front of an empty folder.  Resolution has no such failure mode: the
# makedirs then lands on the resolved root.

_HERE = os.path.dirname(os.path.abspath(__file__))

# The env override, for deliberately isolating an agent or an experiment.
DATA_ENV = "CONDUCTOR_DATA"


def _main_checkout(repo: str) -> str | None:
    """
    The main checkout `repo` is a worktree of, or None if it is not one.

    A worktree's `.git` is a **file**, not a directory, holding one line:

        gitdir: /Users/…/conductor/.git/worktrees/<name>

    so the main checkout is deducible from the worktree alone — what precedes
    `/.git/worktrees/` — which is the property that makes this need no
    configuration and hold for every worktree present and future.

    Everything that is shaped like one and is not (a submodule's `.git` file,
    which points at `.git/modules/<name>`; a bare repository, which has no
    checkout to speak of; an unreadable or truncated file) answers None, and the
    caller falls back to today's behaviour.  Detection fails by falling back,
    never by raising: this runs while `config` is being imported.
    """
    dot_git = os.path.join(repo, ".git")
    try:
        if not os.path.isfile(dot_git):
            return None                       # a directory (main checkout), or nothing
        with open(dot_git) as f:
            line = f.readline().strip()
    except (OSError, UnicodeDecodeError):
        return None

    if not line.startswith("gitdir:"):
        return None
    gitdir = line[len("gitdir:"):].strip()
    if not gitdir:
        return None
    # git 2.48+ can write this relative (`git worktree add --relative-paths`),
    # and it is relative to the worktree — never to the current directory, which
    # is the very dependency being removed here.
    if not os.path.isabs(gitdir):
        gitdir = os.path.join(repo, gitdir)

    # `realpath`, not `normpath`, and for `confine()`'s own reason: normpath
    # collapses `..` textually, which walks straight through a symlinked
    # component and can name a directory that is not the one the path reaches —
    # here, a `.git/worktrees` sitting beside the wrong parent.  The storage
    # layer compares realpaths throughout, so the root is one too.
    parent, sep, _name = os.path.realpath(gitdir).rpartition(os.sep + "worktrees" + os.sep)
    if not sep or os.path.basename(parent) != ".git":
        return None
    main = os.path.dirname(parent)
    return main if main and os.path.isdir(main) else None


def _resolve_data_root(repo: str = _HERE) -> str:
    """
    The directory `sessions/`, `params/` and `mappings/` live under.

    Empty in the main checkout — deliberately, so `os.path.join(root, "sessions")`
    is the bare relative `sessions/` it has always been and nothing about that
    case changes.
    """
    override = os.environ.get(DATA_ENV, "").strip()
    if override:
        return os.path.realpath(os.path.expanduser(override))
    return _main_checkout(repo) or ""


DATA_ROOT = _resolve_data_root()


def data_path(name: str) -> str:
    """Where one runtime-data directory lives; relative in the main checkout."""
    return os.path.join(DATA_ROOT, name)


# Network
UDP_HOST = "0.0.0.0"
UDP_PORT = 4210          # ESP32 sends sensor data here

WS_HOST  = "0.0.0.0"
WS_PORT  = 8081          # downstream clients (Three.js, Ableton…) connect here

API_HOST = "0.0.0.0"
API_PORT = 8000          # FastAPI control panel + REST API

# The ESP32 advertises this mDNS hostname; it is resolved to an IP at startup
# (EspConfigurator.resolve), so the ESP's DHCP address no longer needs to be
# hardcoded. A literal IPv4 here (e.g. "10.0.0.42") is used as-is, bypassing mDNS.
ESP_HOST    = "imu-cyrwheel.local"
CONFIG_PORT = 4211             # config port: PC → ESP commands (remote side)

# Local port we bind to receive CFG_ACK replies. Normally the same as
# CONFIG_PORT (the ESP answers to the port it was addressed from), but kept
# separate so a simulator can listen on its own port on this very machine.
CONFIG_LOCAL_PORT = 4211

# How long to wait for a CFG_ACK after a command. The firmware now yields to its
# config poll every ~50 ms (DRAIN_BUDGET_MS), so commands are acked in <100 ms
# (measured ~54 ms). 1 s leaves ample margin for an occasional flash write
# (saveNVS on set_simple/set_super) or a WiFi hiccup.
CONFIG_ACK_TIMEOUT_S = 1.0

# ESP health monitoring
HEARTBEAT_TIMEOUT_S = 6.0   # no heartbeat for this long ⇒ ESP considered offline
RATE_TOLERANCE      = 0.25  # a stream is "conform" if measured Hz ≥ expected × (1 − this)

# OSC bridge: bus → Ableton Live (AbletonOSC remote script). Retargetable at
# runtime via PATCH /api/osc/settings — these are only the boot defaults.
OSC_HOST        = "127.0.0.1"   # where AbletonOSC listens for commands
OSC_SEND_PORT   = 11000         # AbletonOSC's command port
OSC_LISTEN_PORT = 11001         # AbletonOSC's reply port — we listen here
OSC_RATE_HZ     = 30.0          # default cap on continuous-route sends

# Torus geometry — the single source, read by model/signals/wheel.py,
# GET /api/config and simulator/motion.py alike. These are *measurements* of
# the wheel, not settings: they were deliberately taken off the parameter
# surface (ADR 0004), because a pose track is precomputed from them and moving
# either mid-séance would silently invalidate every track already on disk.
# Swapping wheels means editing here and restarting.
R_TORE = 1.0             # major radius (metres)
r_TORE = 0.05            # tube radius (metres)

# Pipeline
DEGENERATE_THRESHOLD = 1e-6   # u_perp below which the wheel is considered flat

# Largest gap between two consecutive ts_esp_us that is still treated as real
# elapsed time by TorusPositionStage. Above it the value is a time-base
# discontinuity (ESP reboot, long dropout, or the packet straddling the
# live↔replay switch), not motion: integrating it would jump px/py by metres.
# 0.5 s ≈ 50 missed packets at 100 Hz — a genuine gap that long is not worth
# integrating blind anyway.
MAX_DT_S = 0.5

# ── Simulator (development) ──────────────────────────────────────────────────
# The `simulator` package impersonates the ESP32 over real UDP sockets, so the
# whole chain (parsing, layout, health, pipeline, CSV, WS) can be exercised
# without hardware. Selected with the SIM environment variable:
#
#   SIM=1 | SIM=embed   talk to the local fake ESP *and* run it in-process
#   SIM=extern          talk to the local fake ESP, started in another terminal
#                       (python3 -m simulator)
#   unset               production: real hardware, nothing below applies
#
# The simulator listens on its own config port because the orchestrator already
# owns CONFIG_LOCAL_PORT (4211) on this machine.
SIM_CONFIG_PORT = 4311
_SIM_MODE       = os.getenv("SIM", "")
SIM_ENABLED     = _SIM_MODE in ("1", "embed", "extern")
SIM_EMBEDDED    = _SIM_MODE in ("1", "embed")
SIM_SCENARIO    = os.getenv("SIM_SCENARIO", "coin")   # see simulator/motion.py

if SIM_ENABLED:
    ESP_HOST    = "127.0.0.1"
    CONFIG_PORT = SIM_CONFIG_PORT
