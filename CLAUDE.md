# CLAUDE.md

`conductor` is a Python asyncio orchestrator for an instrumented Cyr wheel. It receives BNO08x IMU data from an ESP32 over UDP, interprets it into artistic signals, and publishes them to a Three.js visualiser, OSC → Ableton Live, and a web control panel. Sessions are recorded to CSV and replayed as if live.

The chain is **movement → meaningful signal → creative output**. The middle link, `model/`, is where the value is; everything else feeds it reliably or carries its output.

The design lives **in the module headers**: each file opens with the why of its decisions, the measurements behind its constants, and the traps it guards. Read the header of every file you are about to change — this document only holds what spans several of them.

## Workflow

- **A new feature, signal, detector, panel, OSC route or behaviour goes through a ticket first**; a bugfix of already-broken behaviour does not. When unsure, ticket. Nothing is filed on GitHub without the user approving its content.
- **One branch and one PR per ticket.** A backend-only diff merges with `gh pr merge` once `python3 -m tests.run` is green. **A diff touching a browser surface — `api/static/`, `api/viz/`, `api/align/` — waits for the user's review**: the suite has no JS coverage.
- Details: [docs/agents/workflow.md](docs/agents/workflow.md) · tracker and wayfinder maps: [docs/agents/issue-tracker.md](docs/agents/issue-tracker.md) · labels: [docs/agents/triage-labels.md](docs/agents/triage-labels.md).
- **Vocabulary**: [CONTEXT.md](CONTEXT.md) is the glossary (French) — *take*, *alignement*, *synchronisation*, *balayage*, *saut*, *mise en régime*, *piste de pose*, *amorçage*. Use its terms, not the synonyms it lists to avoid.
- **Decisions**: [docs/adr/](docs/adr/) — read the ADRs touching your area and say so explicitly when a change contradicts one. Measurements and research notes: [docs/research/](docs/research/).

## Running

```bash
pip install numpy scipy websockets fastapi "uvicorn[standard]" python-osc
python3 main.py              # FastAPI on :8000 — panel at /, visualiser at /viz/, alignment at /align/
SIM=1 python3 main.py        # with an in-process fake ESP32 over real UDP (simulator/)
```

Python 3.12+. No requirements file, build, lint or CI. Ports and addresses: `config.py`.

## Tests

```bash
python3 -m tests.run         # ~2 s, dependency-free: each module exposes main() and asserts
```

- **Ground truth is `simulator/motion.py`**: attitude in closed form, gyro derived from that same attitude, so `reference()` is an external check. `tests/test_model.py` is the module to extend when adding a signal. Real takes (`sessions/`) are gitignored and never test inputs.
- **Test the step, not the sleep.** A loop paced by `asyncio.sleep` is tested through the function it runs on each wake (`OscBridge._cadence_step`, `PlaybackEngine._settle`).
- **A test for a fix must go red without the fix** — check it against a copy with the fix removed.
- HTTP: `TestClient` **without** `with` (the lifespan would boot sockets and the processing loop); a raw ASGI scope when the URL holds `..`, which httpx normalises away (`test_paths.py`). The `StarletteDeprecationWarning` about httpx is noise.
- `test_data_root.py` works on a fake checkout in a temp dir and must never touch the real repository.
- A red run on a saturated machine may be the machine (`test_scope.py`'s 25 ms budget is load-sensitive): re-run, and compare with `main`, before calling it a regression.
- **Browser benches** sit outside `tests.run`: `(await import('/viz/_harness.js')).run()` and `(await import('/align/_harness.js')).run()` from the page's console. Run them, and any video or fps measurement, **in a foreground Chrome window** — a hidden document gets no `requestVideoFrameCallback`, pauses muted video and suspends `requestAnimationFrame`, so it measures power saving.

## Architecture

`main.py` launches uvicorn; `api/app.py`'s lifespan calls `core.startup()`. **`core.py` owns the central queue and every singleton** (`bus`, `model`, `configurator`, `session_manager`, `csv_logger`, `playback_engine`, `pose_tracks`, `layout`); route handlers import them from there.

```
UDPReceiver ──▶ Queue ──▶ processing_loop ──▶ CSV write (raw, before the model)
PlaybackEngine ─┘                         └──▶ bus.publish(RAW)
                                          └──▶ model.feed() ──▶ bus.publish(FRAME | EVENT | META)

bus subscribers:  WSServer (8081)  ·  ScopeRing  ·  OscBridge (→ AbletonOSC)
```

`PlaybackEngine` replays a CSV onto the same queue, so **live and replay run the same code** — a structural property, not a discipline. Two things run the model *off* this diagram, both `Model(bus=None)` in a worker thread so nothing reaches the bus: the pose track computation (`storage/pose_track.py`) and the seek warm-up twin (`storage/seek.py`).

The panel observes through one 4 Hz snapshot pushed on `/api/ws` (`core.panel_snapshot`, REST GETs as fallback); commands are REST under `/api/...` (`api/routes.py`). The visualiser additionally reads the downstream stream on `ws://…:8081/?types=frame,meta`.

## Invariants

These span modules, which is why they are here. Each is explained in the header of the file named.

**Model** (`model/`)

1. **All time comes from `Tick.t_us` / `ctx.dt`** — never `time.monotonic()`, `ts_rx_us` or the loop clock. This is what makes a replay at any speed reproduce live exactly. The uint32 ESP counter wraps every 71 min 35 s and `model/clock.py` is the only place that handles it. Wall-clock pacing is right only for what never feeds the model: the OSC send cadence, the video's seek debounce.
2. **Every filter is `ctx.alpha(tau)`**, never a fixed coefficient, so a tuned constant holds at any sample rate. A parameter used as a τ is declared `PARAMS.declare(..., tau=True)` — that flag sizes the seek's warm-up window.
3. **Ask for physics, not wiring**: `needs=(OMEGA,)`, never a packet `typeId`. `model/quantities.py` turns any ESP configuration into canonical quantities.
4. **No second model** (ADR 0003): any precomputed result comes from a forward `model.feed()` loop. No non-causal filter, no event moved after the fact, no normalisation over a whole take, and never an accessor on `Context` that returns a future value.
5. **`None` is not zero.** An unavailable signal sends no OSC; a missing pose component is NaN. A stopped wheel and a wheel reporting zero are different facts.
6. A signal or detector that raises costs its own output only; `model.feed` is wrapped in `processing_loop` so the engine itself can never kill the queue's only consumer (`status.model.engine_errors` should stay 0).

**Pipeline**

- **`processing_loop` never drops a packet** and writes the CSV **before** the model, so raw data never depends on the model of the day. Host-side loss is counted in `UDPReceiver` (`status.udp.dropped`).
- **Fan-out never blocks the model**: bus subscribers registered inline only serialise and append. Frames are droppable, **events and the reset meta never are** — never rate-cap, deadband or drop an event.
- **Playback is exclusive over the model**: `core.accept_live` mutes live packets during a replay (their timestamps come from another time base). The heartbeat (0x20) is exempt; it is never recorded.
- The model is reset at the start of each replay pass and when `processing_loop` dequeues the `playback_end` sentinel. A seek *replaces* the model instance, so hand out callables like `core.reset_model`, never a bound method of `core.model`.
- **Super-slot fields are named from the last CFG_ACK** (`transport/super_layout.py`). Before the first ACK, super packets are anonymous and not recorded — `core.startup()` sends `set_host` for that reason.

**Data**

- **The wire format lives only in `transport/protocol.py`**, the mirror of the firmware's `protocol.h`.
- **One CSV schema, one decoder**: columns are named for the quantity (`gyro_x`, `game_rv_qw`) whatever slot delivered it, from `protocol.PACKET_FIELDS`; reading back always goes through `playback_engine.row_to_packet`.
- **`take.json` loading stays tolerant** of unknown and missing keys (`load_take` filters on `TakeMeta`'s fields): it is what lets a field be retired without takes vanishing from the listing.
- **An alignment is both anchors** (`onset_imu_s`, `onset_video_s`), **never their difference**, written together or not at all. The automatic proposition is recomputed on demand and never stored (ADR 0001). **No `GET` writes**: the video scan proposes (`videos_found`), a `PATCH` adopts.
- **A sweep publishes nothing** — no frame, event, OSC — it reads the pose track (ADR 0004). A jump re-feeds a bus-less twin, then swaps it in (`core.seek_model`).
- **Names from outside go through two layers**: shape (`api/models.py:PathSegment`) and containment (`storage/paths.py:confine`). Validation rejects an *input*, never a take or profile already on disk.
- The video `Content-Type` comes from `storage/paths.py:VIDEO_MEDIA_TYPES`, which is also the extension whitelist — never `mimetypes`.
- **Wheel geometry (`R_TORE`, `r_TORE`) lives in `config.py` only**, not in the tunable params: pose tracks on disk are stamped with it.
- **Runtime data belongs to the machine**: `sessions/`, `params/`, `mappings/` are gitignored, and `config.DATA_ROOT` resolves them — a worktree sees the main checkout's data, `CONDUCTOR_DATA` isolates. Never `ln -s` them in.

**Browser surfaces** (`api/static/` panel, `api/viz/`, `api/align/`)

- **No build, no framework, no CDN** — plain ES modules, three.js vendored in `api/viz/vendor/`, everything works offline.
- **`/viz/` and `/align/` are mounted before the catch-all `/`** in `api/app.py`; a page mounted after it gets the panel's HTML with a 200 (`tests/test_surfaces.py`).
- The panel builds its signal, parameter and OSC pickers **from `GET /api/model/schema`** — declaring a signal or a param is the whole wiring, there is no frontend list.
- The 4 Hz push **never overwrites a control the user is focused on or has edited** (`js/dom.js`: `syncControl`, `trackDirty`). Each fact lives in one tab; only the verdict (connection dot, mode) is repeated in the topbar.
- The viz caps its render cost on purpose (`setPixelRatio(min(dpr, 1.5))`, MSAA below dpr 2) — leave it; the reasons are in `viz.js` and [docs/research/rendu-viz-mises-en-page.md](docs/research/rendu-viz-mises-en-page.md).

## Adding a signal or detector

One decorated function in `model/signals/`; the schema endpoint, the scope picker, the params panel and the OSC route list build themselves from it.

```python
@signal("lean_deg", kind=GEOMETRY, unit="deg", range=(0, 90),
        needs=(ATTITUDE_REL,), doc="Inclinaison du plan de la roue…")
def lean_deg(ctx):
    k = kinematics(ctx)
    if k is None:
        return None
    return math.degrees(math.atan2(abs(k.u[2]), k.u_perp))
```

`needs` = canonical quantities; `depends` = signals it cannot exist without; `after` = signals it only wants computed first. Order is derived topologically. Tunables are `PARAMS.declare(...)` next to the code that reads them. Prefer `atan2` to `acos` for angles. Detectors (`@detector`, `threshold()` in `model/detectors.py`) return a payload dict to fire, `None` otherwise, and go in `model/signals/detectors.py`, which is imported last. Add a check to `tests/test_model.py`.

## Where to read first

| Area | Files (headers) | Also |
|---|---|---|
| Model engine | `model/__init__.py`, `engine.py`, `registry.py`, `quantities.py`, `clock.py`, `params.py` | ADR 0003 |
| Signals, detectors | `model/signals/__init__.py`, `model/detectors.py` | |
| Scope | `model/scope.py`, `api/static/js/panels/scope.js` | |
| Bus, WS fan-out | `model/bus.py`, `model/types.py`, `transport/ws_server.py` | |
| ESP link, health | `transport/protocol.py`, `udp_receiver.py`, `esp_configurator.py`, `esp_health.py`, `super_layout.py` | |
| OSC | `osc/bridge.py`, `routes.py`, `targets.py`, `live.py` | check `targets.py` addresses against the installed AbletonOSC |
| Sessions, takes, CSV | `storage/session_manager.py`, `csv_logger.py`, `playback_engine.py` | |
| Pose track, seek | `storage/pose_track.py`, `storage/seek.py`, `core.seek_model` | ADR 0004 |
| Start of movement | `storage/onset.py` | ADR 0001, 0002 |
| Video endpoint, paths | `storage/paths.py`, `api/routes.py` | docs/research/serving-video.md |
| Panel | `api/static/js/main.js`, `store.js`, `dom.js`, `tabs.js` | |
| Visualiser | `api/viz/viz.js`, `sync-clock.js`, `layout.js`, `sweep.js`, `video.js`, `_harness.js` | |
| Alignment page | `api/align/align.js`, `video.js`, `curve.js`, `_harness.js` | ADR 0001, 0002; docs/research/frame-stepping-video.md |
| Simulator | `simulator/__init__.py`, `motion.py`, `wire.py` | |
| Data root | `config.py` | |
