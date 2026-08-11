# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

`conductor` is a Python asyncio orchestrator for a Cyr-wheel IMU project. It receives BNO08x sensor data from an ESP32 over UDP, interprets it into artistic signals (geometry, dynamics, events, and later states), and publishes the result to downstream outputs (Three.js visualiser, OSC to Ableton Live, later lighting). It can record sessions to CSV and replay them as if they were live.

The chain is **movement → meaningful signal → creative output**, and the middle link — the `model/` package — is where the value is. Everything else exists to feed it reliably or to carry its output.

## Running

```bash
python3 main.py        # launches uvicorn → FastAPI control panel + REST API on API_PORT (8000)
```

The FastAPI lifespan boots all orchestrator subsystems (UDP receiver, WS server, ESP configurator, processing loop). Open `http://localhost:8000/` for the web control panel. There is no requirements file, build or lint setup. Dependencies are installed ad hoc:

```bash
pip install numpy scipy websockets fastapi "uvicorn[standard]" python-osc   # stdlib: asyncio, struct, socket, csv, json
```

Requires Python 3.12+ (uses `X | None` union syntax and modern type hints).

### Tests

There is no pytest in this environment, so `tests/` is dependency-free: each module exposes `main()` and asserts its way through.

```bash
python3 -m tests.run
```

The suite is fast (~2 s) and covers the properties that are expensive to discover late: the 32-bit clock rollover, the bus loss policies, the WebSocket outbox priority, the processing loop's survival, the model checked against `simulator/motion.py`'s closed forms, and the OSC bridge's transform/deadband/rate-cap contract (`tests/test_osc.py`). **`test_model.py` is the one to extend when adding a signal** — the simulator prescribes attitude analytically and derives the gyro from that same attitude, so its `reference()` is a genuine external check rather than the model grading its own homework. **`test_osc.py` calls `OscBridge._cadence_step()` directly rather than running `run()`'s real `asyncio.sleep` loop** — exactly like `test_playback_timing.py` for the replay pacing loop, a wall-clock sleep is not what that code can get wrong, so the test exercises the logic that runs on each wake instead of timing the wake itself.

**`test_paths.py` drives the routes through a raw ASGI scope rather than `TestClient`** — an HTTP client normalises `..` away before sending, so a `TestClient` test of the path-traversal fix would be testing httpx, not the router. It is also the one test module that asserts something must *not* happen: a take already on disk keeps listing whatever a request would have been refused for (see `storage/paths.py`).

**`test_take_meta.py` holds a `take.json` written by the previous version, verbatim** (`LEGACY_TAKE_JSON`, with the retired sync-marker fields). It is what makes the tolerance rule above testable at all: rewriting that fixture with the current `TakeMeta` would assert nothing, since the failure it guards against is precisely a file the current schema did not write. It borrows `test_paths.py`'s `_app`/`_call` for the round trip through `PATCH`, and reads the take back through a *second* `SessionManager` — a restart, at the only level that matters here.

### Control interface (REST API + web panel)

Control (ESP config, sessions, recording, playback) is exposed as a REST API under `/api/...` (see `api/routes.py`) and a vanilla HTML/JS panel served from `api/static/`. This replaced the old stdin keyboard interface. **Commands** are REST: ESP control (`POST /api/esp/host|simple|super`, `DELETE /api/esp/super/{slot}`), session lifecycle (`POST /api/session/start|close`, `PATCH /api/session`), take recording (`POST /api/recording/start|stop`), take editing (`PATCH /api/sessions/{session}/takes/{take}`), playback (`GET /api/sessions`, `POST /api/playback/start|stop` with `{session, take, speed, loop}`). A take's precomputed poses are read with `GET /api/sessions/{session}/takes/{take}/pose` (see "The pose track").

**Observation is hybrid push/poll** (see `core.panel_snapshot`). The panel's primary channel is a **native FastAPI WebSocket at `/api/ws`** (`panel_ws` in `routes.py`, one push loop per client at ~4 Hz) that sends a merged snapshot: `{status, live, health, session, recording, playback, esp, model}`. The same per-section dicts are also exposed as REST GETs (`/api/status`, `/api/live`, `/api/health`, `/api/session`, `/api/recording/status`, `/api/playback/status`, `/api/model`) which the frontend uses only as a **fallback** when the socket drops (`js/store.js` `startFallback`). All snapshot builders live in `core.py` (`status_dict`/`session_dict`/`recording_dict`/`playback_dict`/`model_dict`/`panel_snapshot`) — single source of truth. Stream observation (per-type rates, liveness, latest values) is done backend-side by `LiveMonitor` (`transport/live_monitor.py`), fed from `processing_loop`. `LiveMonitor` watches **the wire and only the wire**; anything derived belongs to the model, which keeps its own state.

The 4 Hz snapshot carries the model's latest frame as a convenience, **not as a way to watch a signal**: at 4 Hz you see one sample in twenty-five, which is useless for setting a threshold. That is what the scope is for — see below.

**Model control is REST too**: `GET /api/model/schema` (every declared signal with its unit, range, dependencies and availability, plus every parameter), `GET|PATCH /api/model/params`, `POST /api/model/params/{save,load,reset}`, `POST /api/model/signal` (enable/disable one node), `POST /api/model/reset` (clear integrators).

#### The scope (`model/scope.py`, `js/panels/scope.js`)

`ScopeRing` is an **inline** bus subscriber, which is the whole point: it records every frame at the model's own rate, including during a fast replay where the WebSocket fan-out is deliberately dropping them to keep a browser current.

`GET /api/model/history?signals=a,b&window=10&points=600` returns **min/max envelopes per pixel column**, not decimated samples. Sending every 40th sample would smooth away exactly what a detector triggers on — a one-sample spike is a real impact, not noise to average out. The reduction is `np.fmin/fmax.reduceat`, which skips NaN, so a stretch where a signal could not be computed stays a *hole* in the trace rather than being dragged to zero (which would look like the wheel coming to rest, a state a detector is meant to recognise).

Storage is one preallocated float64 ring per signal plus a shared timestamp ring (`DEFAULT_CAPACITY = 24 000` samples ≈ 60 s at 400 Hz, 4 min at 100 Hz). A ring of frame dicts would be tens of megabytes and slow to query. **The ring is cleared on `meta.topic == "reset"`** — a replay restarts the timeline at zero, and keeping the old samples would make the timestamps non-monotonic and every windowed query nonsense.

The panel builds its signal picker and its parameter controls **entirely from `GET /api/model/schema`**: declaring a signal or a `PARAMS.declare(...)` is the whole wiring, there is no frontend list to keep in sync. An unavailable signal stays listed, greyed, with the reason next to it (`nécessite accel (active ACCEL)`) — knowing what to switch on beats the row simply not being there.

#### OSC bridge (`osc/`, `js/panels/osc.js`)

The point of this module is narrower than "emit OSC": it is that **remapping a signal or a detector's event to an OSC address never touches code**. The mapping lives entirely in `osc/routes.py`'s `RouteTable` — a JSON-backed CRUD table edited through `POST/PATCH/DELETE /api/osc/routes[/{id}]` and the panel's "OSC → Ableton Live" card — and is persisted as named profiles under `mappings/`, the same shape as `model/params.py`'s profiles under `params/`. `osc/targets.py` is a pure-data catalog of known AbletonOSC destinations (address + ordered index-argument template + natural output range); a route names a target instead of hardcoding a string, so adding a destination is one catalog entry, not a code change. `custom` is the escape hatch — any address, any argument template, including a Max for Live device. **Verify the catalog addresses against the installed AbletonOSC version** (github.com/ideoforms/AbletonOSC) — a mismatch costs one line there, not a redesign.

A route's structural validity (`in_min != in_max`, `deadband >= 0`, …) is checked once, at create/update time. Whether its `source` still names a signal or detector that *currently exists* is a different, time-varying question, answered fresh on every `GET /api/osc/routes` — exactly like a signal's own availability — so a profile written before a signal was renamed still loads in full; the route simply reports why it cannot fire instead of vanishing or crashing the load.

`osc/bridge.py`'s `OscBridge` treats the three bus topics as differently as `model/types.py` says they must be:

- **`frame`** — inline `subscribe_sync`, latest-wins: no backlog is kept between sends, on purpose. A separately cadenced task wakes on its own schedule and sends whatever the *current* frame is — deliberately not the bus's own LOSSY policy, which still queues a bounded backlog between sends, exactly what is not wanted here.
- **`event`** — `subscribe(policy=RELIABLE)`, sent the instant it arrives: **never rate-capped, never deadbanded**. A trigger is not a level the model samples every tick; missing one is the fault RELIABLE exists to prevent, and this bridge must not reintroduce a loss the bus already promised not to have.
- **`meta`** — inline, only `topic == "reset"` matters: it clears the deadband memory, so the first post-reset value sends even if it equals the value sent just before the reset — the same reasoning `ScopeRing` uses to clear its ring on the same topic.

**The send cadence is wall-clock, deliberately.** The cadenced sender uses `asyncio.sleep(1/rate_hz)`, never `ctx.t_us`. This is not an exception to "all time comes from `Tick.t_us`" (see below) — that rule protects the *model*'s reproducibility, and nothing in the bridge feeds back into it. Live absorbs a budget of messages per real second regardless of what the model's clock is doing: a replay at 4× must send OSC at the same rate a 1× replay would, not four times as many messages in a quarter of the time.

A signal that is `None` — unavailable, or computed-but-nothing-to-report — sends nothing, never a `0`. A wheel that stops is not the same fact as a wheel reporting zero speed, and collapsing the two would leave a detector on the Live end unable to tell a real zero from silence. A continuous route also applies a **deadband**: a value that hasn't moved past it since the last send is skipped and counted (`stats.skipped_deadband`), which is what keeps a stationary wheel from flooding Live at `rate_hz` for nothing.

`osc/live.py`'s `LiveLink` owns the AbletonOSC conversation: a fire-and-forget `send()` for the per-route sends and the MIDI-learn test sweep (`POST /api/osc/routes/{id}/test`, which bypasses deadband and rate cap on purpose — a test sweep should move smoothly regardless of the route's own live-performance settings), plus a request/reply path used only for discovery (track/device/parameter names, `GET/POST /api/osc/live[/refresh]`) and the periodic `/live/test` health probe that drives `online` in the snapshot. Discovery is cached and populated **on demand**, never eagerly — a set can have dozens of tracks, each with dozens of devices, and fetching every parameter up front would be a lot of round trips for names nobody has asked to see yet.

**ESP health is unified** (`transport/esp_health.py`, `EspHealth`, snapshot key `health`). Single source of "is the ESP alive and behaving", fusing two signals so the UI shows one verdict (`online`/`degraded`/`offline`) instead of redundant indicators: (1) **presence + telemetry** from the periodic heartbeat packet (no heartbeat for `config.HEARTBEAT_TIMEOUT_S` ⇒ offline, independent of the sensor stream), and (2) **stream conformance** — it cross-checks the measured per-type rates (`LiveMonitor`) against what the configured ESP state (`configurator.state`, last CFG_ACK) says should arrive, flagging `missing`/`slow` streams (tolerance `config.RATE_TOLERANCE`). The panel renders this in one collapsible "ESP — Santé & connexion" card and drives the header status dot from `health.state`.

There is **no `GET /api/esp/state`**: the ESP config only changes via our own commands (each returns the full ACK), so `EspConfigurator.state` caches the last ACK (populated by the startup `set_host`) and it rides in the snapshot's `esp` field. Connection/ESP liveness is handled by the heartbeat packet + `EspHealth` (see above). The WSServer (8081) stays dedicated to downstream clients.

#### Panel frontend structure (`api/static/`)

No build step, no framework, no CDN — plain ES modules, so it works offline. `index.html` + `style.css` + `js/`:

- **`js/store.js`** — the single ingestion point for the snapshot. Panels subscribe per section (`on("health", fn)`). It owns the WS connection, the 1 s reconnect, the two-tier REST fallback, and the **rate ring buffers** (120 samples ≈ 30 s) that back the sparklines, since the backend only sends instantaneous rates.
- **`js/dom.js`** — the helpers that make a 4 Hz push non-destructive. `setText`/`setAttr` write only on change (so a text selection survives); `keyed()` reconciles a list by key instead of rebuilding it; `syncControl`/`trackDirty`/`clearDirty` **never overwrite a control the user is focused on or has edited but not yet submitted**. This last point matters: an ESP slot's checkbox or Hz field holds uncommitted input, and section-level change-gating alone does not protect it.
- **`js/panels/*.js`** — one module per region, each rendering from its snapshot section and wiring its own REST commands.
- **`js/api.js`** — `api()` fetch wrapper, `action()` command wrapper, stacking toasts.
- **`js/tabs.js`** — the workspace tabs: `showTab`/`activeTab`/`onTabChange`, ARIA tablist with arrow-key navigation, active tab persisted in `localStorage` (an unknown name falls back to `scene` rather than showing a blank page).

Two sections, `session` and `esp`, are **change-gated in the store** because they drive form rebuilds; everything else re-renders every tick, which is safe given the write helpers above.

##### Workspace tabs

The panel serves three jobs that never happen at once — rigging, capture, creation — and they used to compete for one page: 11 top-level panels on screen at once, an ops column ~1700 px tall (so *Enregistrement* was below the fold), and a `position: sticky` aside 2500–3000 px tall, whose bottom could not be reached. Each job now gets a tab:

| Tab | Contents |
|---|---|
| **Scène** (default) | Recording control, OSC output + panic, wheel position, ESP telemetry, playback transport, read-only scope strip |
| **Captation** | Session strip and form, next-take metadata, playback browser, takes list |
| **Signaux** | Scope + picker, model parameters — the tuning loop, side by side |
| **Sortie** | OSC settings, mappings, routes |
| **Config** | ESP32 slots, super-slots, host |

**Hidden tabs keep their DOM**, so every module renders unconditionally and `R` starts a recording while you are looking at the OSC routes. What a module may *not* do is measure a hidden element — `clientWidth` is 0 under `hidden` — so anything that measures subscribes to `onTabChange` and skips invisible work.

**Each fact lives in exactly one tab.** Only the verdict (connection dot, mode badge) is repeated, in the persistent topbar, because it must be true everywhere. Two consequences worth keeping: the Scène transport appears **only while `playback.active`** (the engine has no "loaded but not started" state, so picking a take stays in Captation), and OSC *panic* lives on Scène only — it is a show-time gesture, not a setting.

Breakpoints at 1240 px and 900 px. Because `grid-template-areas` implies a column count, **both the with- and without-transport variants are restated at every breakpoint**; changing only `grid-template-columns` would disagree with the areas.

Keyboard shortcuts (`js/shortcuts.js`, suppressed while typing): `1`–`5` tabs, `R` rec, `Space` play/pause, `S` stop, `L` loop, `?` help.

The playback progress bar is **read-only by design** — `PlaybackEngine` has no seek. Pause/resume state is always read back from `playback.paused`, never applied optimistically.

##### Two things that were deleted, and why

**Per-sensor live cards.** They duplicated the health streams table — same rate, same sparkline, same client-side ring buffer — and their raw field chips (`gyro_x = 0.737` at 4 Hz) were debug output: unreadable while the wheel moves, and nothing downstream acts on a raw gyro component, which is what `model/` is for. What survives is the wheel position, which is model output and the one thing the streams table cannot show.

**ESP presets (`localStorage`).** The only feature with no backend counterpart. It could not travel to another machine, so it could never be part of a show runbook — and applying one fired up to ten sequential ACK-blocking POSTs that could half-fail, leaving the ESP in a state no preset described. Model params (`params/`) and OSC mappings (`mappings/`) are server-side; if ESP presets are ever wanted, they belong there.

The health *state* is likewise rendered in one place now (the topbar). It used to appear three times at once — topbar, panel badge, and an "État" tile. The streams table follows the verdict: open while the ESP misbehaves, closed while it does not, and left alone once the user works the disclosure themselves. Note that `<details>`'s `toggle` event is **asynchronous**, so distinguishing our own writes from a click needs a comparison against the last value written, not a flag cleared on the next line.

### 3D visualiser (`/viz/`)

A **second, independent page** is served from `api/viz/` and mounted at `/viz/` (see `api/app.py` — the mount must be registered *before* the catch-all `/` mount; `html=True` makes `/viz/` serve its `index.html`). It is not part of the control panel: it is a Three.js view of the wheel, ported from the old standalone `roue-cyr-visualisation-2` project, whose three-process chain (`serverUDP_V2.js` → `claude.py` → browser) the conductor has fully absorbed.

**No build step**, matching `api/static/`: three.js r160 and OrbitControls are vendored as raw ES modules in `api/viz/vendor/` and imported relatively (`OrbitControls.js` imports `./three.module.js`, so the two must stay co-located). No npm, no bundler, no CDN — it works offline.

It uses **two WebSockets plus REST**:

- `ws://<host>:WS_PORT/?types=frame` (8081) — the downstream stream, drives the wheel. Only `type === "frame"` messages are used; each carries `pose` (`qw/qx/qy/qz` + `x/y/z`) and `signals`. **`pose` is always present, `signals` depends on the ESP configuration** — hence the null-tolerant read: a wheel configured without a gyro still renders its orientation, it just has no position. The `?types=` filter is server-side (see "Key seams"); without it the page would also receive `gyro`/`game_rv`/`super_0`/`heartbeat`, i.e. several times the messages to `JSON.parse` for nothing.
- `/api/ws` — the same 4 Hz panel snapshot as the control panel, for ESP health, active session and playback progress.
- REST for commands (playback start/pause/resume/stop, `GET /api/sessions`).

Nothing is hardcoded client-side: **`GET /api/config`** returns `{ws_port, geometry: {R_TORE, r_TORE}}`, where the geometry comes from **`config.py`** — the same source the model reads and every pose track is stamped with, so a visualiser can never draw a diameter the positions it renders disagree with. Both sockets reconnect automatically (~1 s). The model frame is Z-up while Three.js is Y-up, hence the `qFix` -90°/X quaternion applied to both attitude and position. Camera-follow is on by default — a rolling wheel leaves the frame within seconds otherwise; the ground grid is snapped to whole `GRID_CELL` (2 m) steps under the wheel so it reads as fixed ground rather than a carpet being dragged along.

**Render cost is capped on purpose**: `setPixelRatio(Math.min(devicePixelRatio, 1.5))` and MSAA only below dpr 2. Do not "fix" this back to `devicePixelRatio` — on a Retina screen that is 4× the fragments for a fill-rate-bound scene (full-screen ground + grid), and a saturated main thread stops draining the packet socket in time. The HUD shows packets/s **and** fps precisely so the two failure modes stay distinguishable.

Playback from the viz uses the existing API plus **`POST /api/playback/pause|resume`**. `PlaybackEngine` has **no seek** — the progress bar is therefore read-only by design, not an oversight. Pause is an `asyncio.Event` in `_replay_loop`; since row deadlines are absolute (`t0_real + elapsed/speed`), resuming *shifts `t0_real` by the paused duration*, otherwise the backlog would replay in one burst.

## Architecture

`main.py` is a thin entry point that launches uvicorn. The real wiring lives in `core.py`, which owns the central `asyncio.Queue` and the shared singletons (`bus`, `model`, `configurator`, `session_manager`, `csv_logger`, `playback_engine`, `pose_tracks`, `layout`). `core.startup()` (called from the FastAPI lifespan in `api/app.py`) starts the UDP/WS endpoints and the `processing_loop` + `log_stats` tasks. The API route handlers (`api/routes.py`) import the same singletons from `core` — that shared-singleton module is the single source of truth for runtime state.

Data flow (live):
```
UDPReceiver ──▶ Queue ──▶ processing_loop ──▶ CSV write (raw, before the model)
PlaybackEngine ─┘                         └──▶ bus.publish(RAW)
                                          └──▶ model.feed() ──▶ bus.publish(FRAME | EVENT | META)

bus subscribers:  WSServer (8081)  ·  ScopeRing  ·  [event log]  ·  OscBridge (→ AbletonOSC)
```

`PlaybackEngine` replays a CSV onto the same Queue, so the model and every output see no difference from live — "same code live and replayed" is a structural property, not a discipline.

`PoseTrackService` is the one thing that runs the model **off** this diagram: it drives an isolated `Model(bus=None)` over a take's CSV in a worker thread to precompute that take's poses. Nothing it computes reaches the queue or the bus — see "The pose track" below.

Config flow runs on a **separate port**: `EspConfigurator` talks to the ESP32 on port 4211 (commands + ACK replies), while sensor data arrives on port 4210. WebSocket clients connect on 8081. All ports/IPs live in `config.py`.

### Key seams

- **`processing_loop` (core.py)** — the single consumer, and **it can never drop a packet**. It observes, writes to CSV **before** the model (raw data is preserved independently of the computation model of the day), publishes the raw packet, then feeds the model. `model.feed` is wrapped: the registry already contains a failing *node*, so an escape there means the engine itself broke, and letting it out would kill the queue's only consumer — the orchestrator would go silently deaf, which during a show is far worse than a wrong number. Counted in `status.model.engine_errors`; should stay at zero.

- **Fan-out never blocks the model (`transport/ws_server.py`).** The WS server is a **bus subscriber**, registered inline (`subscribe_sync`) because its handler only serialises and appends to per-client outboxes. Each client owns an outbox split in two: droppable messages (raw packets, frames — `_LOSSY_BACKLOG = 8`, ~80 ms at 100 Hz) and undroppable ones (events, meta — `_RELIABLE_BACKLOG = 512`). **A flood of frames can never evict a trigger, and the writer drains triggers first**: a saturated client loses smoothness and keeps its events, which is the right trade for a show. Two deques rather than a priority scan, so both directions are O(1).

  This is not an optimisation but a correctness property: `broadcast()` used to `await send()` for every client inside the processing loop, so one slow browser stalled the whole orchestrator — measured with a single visualiser attached, the central queue grew past 6000 packets and the pipeline fell from ~380 to 186 packets/s, which is what made a paused replay keep playing on screen for seconds. Clients may narrow the stream with `?types=a,b` (no query string ⇒ everything); serialisation is lazy, a message nobody subscribes to is never turned into JSON.

- **Playback is exclusive over the model.** The queue has two producers, and their `ts_esp_us` come from unrelated time bases (ESP uptime vs. recorded CSV), so interleaving them makes the dt the model derives from that field meaningless and its position integration diverges. `core.accept_live` is the admission gate: it is handed to `UDPReceiver` as an injected `accept(packet)` predicate (the receiver holds no policy of its own and never imports the engine), and it drops live packets at the socket while `playback_engine.active` — counted in `status.udp.muted`. **The heartbeat (0x20) is exempt**: it is live-only telemetry, never recorded and therefore never replayed, and without it `EspHealth` would declare the ESP offline a few seconds into every replay.

- **Model reset happens at both ends of a replay.** `PlaybackEngine` calls the `on_reset` callable it was handed (`core.model.reset`) at the start of each pass and on each loop iteration; `processing_loop` resets again when it dequeues the `playback_end` sentinel — otherwise the take's final position would silently become live mode's starting offset. Doing it on the sentinel rather than in `stop()` is what orders the reset correctly against the live packets queued behind it.

- **`SuperSlotLayout` (transport/super_layout.py)** is shared mutable state, the trickiest part of the system. The ESP32 can bundle several sensors into one "super slot" packet. The receiver can only name those payload fields (`gyro_x`, `game_rv_qw`, …) if it knows the slot's dep list. That list is learned from the ESP config ACK: `EspConfigurator._recv_ack` calls `layout.update()` on the parsed state, and `protocol.parse_packet` reads it via `layout.get_deps()`. **Until the first ACK arrives**, super packets fall back to opaque `s0..sN` field names with `dep_slots=None` — and `CSVLogger` silently skips those rows. `core.startup()` calls `set_host` (whose ACK populates the layout) precisely so named decoding works immediately. Thread-safety relies on the GIL: the writer runs in a thread (`asyncio.to_thread`), the reader in the event loop.

## The model (`model/`)

Where sensor data becomes something playable. Four pieces, wired together by `model/engine.py`:

```
clock.py       unwraps the ESP counter into the only timeline anything reads
quantities.py  turns packets into canonical quantities, whatever the ESP config
registry.py    runs the declared signals, in dependency order, each contained
bus.py         publishes to whoever subscribed
```

### The three rules that are easy to break

**1. All time comes from `Tick.t_us` / `ctx.dt`.** Never `time.monotonic()`, never `ts_rx_us` (wall clock), never the asyncio loop clock. This is what makes a replay reproduce a live run exactly, and a 4× replay reproduce a 1× replay exactly — asserted by `test_the_same_input_gives_the_same_output_twice`.

`ts_esp_us` is a **uint32** (`DataHeader` is `<BBHII`), so it wraps every **71 min 35 s**. `TimeBase` (`model/clock.py`) is the single place that is handled. A wrap and a reboot both look like the counter going backwards; they are separated by *plausibility* — a candidate wrap is accepted only when the unwrapped step is a credible inter-packet interval (≤ `max_gap_us`). A reboot from a high uptime would unwrap to a step of minutes, and is rejected as a discontinuity. Discontinuities **hold** the timeline rather than guessing an advance, because any guess would have to come from a clock the replay does not share.

**2. Every filter is written `ctx.alpha(tau)`, never a fixed coefficient.** `alpha = 1 − exp(−dt/tau)` makes a tuned time constant independent of the sample rate: a value found at 25 Hz behaves identically at 100 Hz and during a fast replay (measured within 0.04 % across 25–400 Hz). A naive `alpha = 0.1` would silently retune every envelope in the model each time the BNO configuration changed, making yesterday's settings worthless.

**3. Ask for physics, not for wiring.** A signal declares `needs=(OMEGA,)`, never `typeId == 0x10`. The old `TorusPositionStage` tested the packet type and demanded seven field names, so changing the ESP configuration broke the model — that was the defect this package exists to fix.

### Canonical quantities

`model/quantities.py` decants any packet — simple slot or *any* super slot — into quantities named for what they are: `attitude_rel` (GAME_RV → ARVR_RV; gravity-referenced, yaw relative and drifting, immune to steel), `attitude_abs` (RV → GEO_RV; magnetic yaw reference, absolute but corrupted near steel), `omega`, `accel`, `linear_accel`, `mag`.

**One source owns a quantity.** Redundancy is normal — an ESP with a super slot *and* the same sensors as simple slots delivers everything twice — and left alone it produces two ticks per period with a wildly irregular dt that every rate then reads as real. Ranking: the preference table first (a judgement about the sensor), then **bundled over standalone**, since only a super slot guarantees attitude and gyro were sampled at the same instant. An incumbent that goes quiet for longer than the tolerance loses its claim, so unplugging a sensor falls back instead of freezing.

**Presence is judged on recency.** A sensor switched off mid-session drops out of `present()` and its signals become unavailable — rather than reporting the last value they ever saw, a plausible steady number no longer connected to anything.

### Adding a signal

One addition, in `model/signals/`:

```python
@signal("lean_deg", kind=GEOMETRY, unit="deg", range=(0, 90),
        needs=(ATTITUDE_REL,), doc="Inclinaison du plan de la roue…")
def lean_deg(ctx):
    return math.degrees(math.atan2(abs(kinematics(ctx).u[2]), kinematics(ctx).u_perp))
```

The descriptor feeds `GET /api/model/schema`, and from there the panel and (later) the OSC route list. `needs` = canonical quantities, required. `depends` = signals it cannot exist without (unavailability propagates). `after` = signals it merely wants computed first — the azimuth wants the magnetic guard ahead of it but is perfectly computable without one. Execution order is **derived topologically**; hand-ordering pure functions is meaningless.

Tunable numbers are declared next to the code that reads them (`PARAMS.declare(...)` in `model/params.py`), which gives the API its schema and the panel its slider bounds. Values are clamped, versioned by `revision`, saved as named profiles under `params/`, and read at the top of a tick so a change lands on the next sample, never mid-computation.

**Failure is contained at the node.** A signal that raises yields `None` for its own value, increments its own counter, and the frame goes out regardless. A detector's bug costs its own output and nothing else.

**Numerical note:** prefer `atan2` to `acos` for angles. `lean_deg` used `acos(u_perp)`, which is ill-conditioned exactly where it matters most — near upright, its derivative diverges, so float noise became ~1e-6° of jitter on the signal an artist would want to read at a tenth of a degree. `atan2` brought that to 1.7e-14°.

### Detectors (`model/detectors.py`, `model/signals/detectors.py`)

A signal answers "what is true right now"; a detector answers "did something just happen". Mixing the two would pollute the frame — an event is not a continuous value, does not belong in `frame.signals`, and must not land in the scope's ring (which only ever sees `frame` and the reset `meta`, by design: an envelope is worth graphing, a trigger is worth counting).

```python
@detector("impact", source="accel_shock_ms2", needs=(ACCEL,),
          params=(P_IMPACT_ON, P_IMPACT_OFF, P_IMPACT_REFRACTORY),
          doc="Choc : l'accélération s'écarte brutalement de sa moyenne…")
def impact(ctx):
    return threshold("accel_shock_ms2", P_IMPACT_ON, P_IMPACT_OFF,
                      P_IMPACT_REFRACTORY)(ctx)
```

A detector function is `fn(ctx) -> dict | None`; returning a dict fires an event with that dict as its payload, returning `None` — the overwhelmingly common case, since an event is rare by nature — means nothing happened this tick. `threshold()` builds the common shape: hysteresis (fire once past `on`, only re-arm once the value has dropped to `off` or below) plus a `refractory` cooldown, all three read fresh every tick via `ctx.param()` so a threshold is retunable live, mid-show, exactly like anything else `PARAMS` governs. Not every detector is threshold-shaped — `revolution` fires on a sign change in `spin_deg`'s frame-to-frame delta (a wrap, not a level), which is why `threshold()` is a helper `detector()` functions can use, not something the decorator forces on them.

Detectors **share `ctx.state` with signals**, keyed by name — the engine already reuses one per-name namespace for both. A detector name colliding with a signal's would silently corrupt that signal's own memory (a running average, say), so `DetectorRegistry.add()` refuses a name already taken by a declared signal. `model/signals/__init__.py` imports `detectors` **last**, after every signal module, precisely so this check has every signal name to compare against by the time a detector is declared.

Failure and availability are contained exactly like a signal's: a detector that raises costs only its own output (counted, logged, the frame goes out regardless), and one whose `needs` are not currently arriving simply never fires — nothing is queued up to fire the moment the sensor comes back, since each tick asks fresh.

`Event.id` is monotonic **across the process lifetime**, unlike `Frame.seq` — it is what lets a consumer prove it missed nothing, so `model.reset()` clears every signal's and detector's state but deliberately leaves the engine's `_event_id` counter untouched.

### What the model emits

Three kinds, deliberately distinct because they must not be transported alike (`model/types.py`):

| Kind | Cadence | Contents | Transport |
|---|---|---|---|
| `frame` | one per tick | `t`, `seq`, `pose`, `quality`, `signals`, `states` | droppable — freshest wins |
| `event` | when it fires | `t`, monotonic `id`, `name`, `payload` | **never dropped** |
| `meta` | on change | schema, params, source changes | reliable |

`pose` is separate from `signals` on purpose: it is the geometric state every visual consumer needs, always present, not a tunable that could be switched off. A `None` in `signals` means "enabled but nothing to say right now" — distinct from *unavailable* (a configuration matter, explained in the schema) and from an *error* (a fault, counted).

The tick fires on the arrival of the **master** quantity — attitude, since every geometric signal descends from it. Elapsed time is measured **between ticks**, not between packets: with several streams interleaved, packets are milliseconds apart while ticks are one attitude period apart, and using the packet delta would make every rate several times too large.

### Wire protocol

The binary UDP protocol is firmware-coupled and lives in one place: **`transport/protocol.py`** (Python mirror of the firmware's `protocol.h`). It holds all struct layouts, type IDs, the slot↔sensor naming tables, and the pure `parse_packet` / `parse_ack` / `build_*` functions — no I/O, no state. The transport modules are thin shells over it: `udp_receiver.py` (asyncio socket → `parse_packet` → queue) and `esp_configurator.py` (`build_*` → socket → `parse_ack`, plus connection state). All use little-endian `struct` layouts. The 12-byte `DataHeader` is `<BBHII` (version, type, size, seq, ts_esp_us). Packet type IDs (0x01–0x08 simple sensors, 0x10–0x17 super slots, 0x20 heartbeat, 0x30 CFG_ACK) drive parsing in `parse_packet`. The heartbeat (0x20, 24-byte `<IIIiff` payload: uptime_ms, packets_sent, udp_errors, rssi_dbm, cpu_temp_c, battery_pct) replaced the old standalone battery packet — battery is now just one heartbeat field, and the heartbeat is observed/broadcast but **not** written to CSV.

Raw packets keep their own `type` on the bus (`gyro`, `super_0`, `heartbeat`…): they are the wire, not the model. The model's output uses `frame` / `event` / `meta` instead. The old scheme rewrote a computed packet's `typeId` to `5`, colliding with `0x05 = RV` in `TYPE_NAME`; that is gone.

### CSV format and the three field-name registries

`csv_logger.py` and `playback_engine.py` must agree on column layout, and both import the canonical super-field set (`ALL_SUPER_NAMED_FIELDS`) from `protocol.py`. The CSV has a fixed wide schema: common columns + Vec3 + Quat + **all** named super fields; only the fields relevant to a given packet are filled, the rest blank. Heartbeat (0x20) is absent from `PAYLOAD_FIELDS` in both files, so it is skipped on write and has nothing to replay. `PACKET_TYPES` in `playback_engine.py` duplicates `TYPE_NAME` from `protocol.py` — **keep them in sync**. Playback packets are reconstructed from named CSV fields and do **not** include `dep_slots`.

Reading the CSV back is **one function, `playback_engine.row_to_packet`** (module-level, not a method, precisely so it can be shared): both the replay loop and the pose-track computation go through it. Two decoders would be two chances to disagree about which column a super slot's gyro landed in.

### Sessions / Takes database

Recordings are organised as **sessions containing takes** (`storage/session_manager.py`):

```
sessions/
  .active                      ← name of the open session (plain text; removed on close)
  2026-06-13_14-30_trianon/    ← <date>_<time>_<slug(title)>
    session.json               ← SessionMeta: title, location, equipment, comments,
    takes/                       firmware_version (manual), program_version (auto: git describe)
      001_premier-essai/       ← <NNN>_<slug(take title)>, NNN auto-incremented
        raw.csv
        take.json              ← TakeMeta: title, performer, figures, notes, timestamps,
                                 packet_count, imu_config (auto snapshot of configurator.state
                                 at take start), video_file + the alignment anchors
        pose.bin               ← pose track (storage/pose_track.py), derived from raw.csv
```

A session is opened (`create_session`) before recording; takes require an open session (`new_take` raises otherwise → routes return 409). The `.active` pointer makes the open session **survive an orchestrator restart** — `active_session()` just re-reads it. `SessionManager.active_tree()` caches the active session+takes dict for the 4 Hz snapshot push (invalidated on every meta write). `list_sessions()` returns the full tree (sessions with nested take metadata); a take only appears once its `raw.csv` exists — `pose.bin` is derived and can always be deleted and recomputed. The firmware version is manual until the ESP ACK protocol exposes it.

#### The alignment (`onset_imu_s` / `onset_video_s`, ADR 0001)

A take's **alignment** is the pair of anchors locating its *start of movement* (wheel flat on the ground, then lifted sharply) in its video: `onset_imu_s` on the take's own timeline — floating seconds from its first sample, which is exactly `frame.t` — and `onset_video_s` in the video. **Both are stored, never their difference**: the offset is a residue either side recomputes, while the anchors are facts, and the video one is the single number in the device no machine can reproduce. Both are set through the existing `PATCH /api/sessions/{session}/takes/{take}`, and **an alignment is indivisible** — `TakeUpdate` refuses a body carrying one anchor without the other, which is what keeps "not yet aligned" a state needing no field of its own (no boolean, no confirmation timestamp).

The automatic **proposition** is stored nowhere: a number nobody can date (computed with which threshold, before or after the last change?) would acquire a durability that contradicts its definition, so it is recomputed on demand. The assumed consequence is that the takes list can show no "detectable" badge — it is served at 4 Hz from `active_tree()`, which recomputes nothing.

**`load_take` filters on `TakeMeta`'s declared fields**, so a `take.json` survives both a key it has never heard of and a key it is missing (`name` falls back to the directory, which is where a take's name actually lives). This is not politeness: `TakeMeta(**raw)` raises `TypeError` on a retired field, `list_takes()` swallows that — and the take **vanishes from the panel** instead of failing. It is the condition for ever removing a field, and it is what let the sync-marker device (`sync_marker_ts_us`, `video_sync_time_s`, `POST /api/recording/marker`, the `M` shortcut, the "marqueur ✓" badge) be deleted whole without evaporating the takes already on disk. A `take.json` that is not an object still raises `TypeError`, deliberately — that is the exception `list_takes()` is written to skip, and filtering keys on a JSON array would raise `AttributeError` and take the whole listing with it.

#### The pose track (`storage/pose_track.py`)

Sweeping a cursor through a take must move the wheel **without anything reaching the bus** — no frame, no event, no OSC (ADR 0004) — which rules out replaying it. So each take gets its poses precomputed beside it, one record per tick: `t`, quaternion, `x`, `y`, `z`. The pose **resolved**, not the raw columns: `raw.csv` files a simple slot under anonymous names (`qw`…`qz`) and a super slot under named ones (`game_rv_qw`), so a reader fetching attitude itself would have to replay `QuantityResolver`'s arbitration and know both layouts.

**Only the pose is cached, and that is the design, not an optimisation.** Every other signal is an `ctx.alpha(tau)` envelope that forgets its past in ~5 τ (so it can be recovered on demand by re-feeding a few seconds of take) *and* depends on a tunable — a cache of it would die on every slider move in the Signaux tab, which is exactly where one tunes. The pose is the exception in both directions: `pos_x`/`pos_y` are a path integral no amount of re-feeding recovers, and since the wheel dimensions left the parameter surface, nothing tunable enters it. A pose track survives a whole tuning session without going stale.

Layout, little-endian: a 28-byte header (magic `CYRPOSE1`, a flags byte, then the **geometry stamp** — `wheel_R` and `wheel_r` as two f8), then 36-byte records (`t` f8, then quaternion and position as f4). ~3.2 MB for 15 min at 100 Hz. `t` is f8 because it is the key everything is looked up by; the pose is f4 because 6e-8 on a unit quaternion is far below what anything can see, and it halves the file. A missing component is **NaN, never 0** — a wheel with no gyro has no horizontal position at all, and a nought there reads downstream as a wheel sitting at the origin.

**The geometry stamp is what makes an old track detectable.** Measured, a 5 % error on both radii moves `pos_x/y/z`, `height_m`, `contact_offset_m` and both movement envelopes by 5 %: it is the *absolute scale* that matters, not the R/r ratio (only `heading_deg` is sensitive to the ratio). Sixteen bytes cover the one case that survives geometry leaving the tunable surface — someone edits `config.py`, and the tracks on disk are at the old scale. A mismatch is **reported** (`geometry.matches` in the endpoint's reply), never silently repaired: recomputing behind the user's back would throw away the only clue.

**One producer.** `PoseTrackService` keeps one task per take, started at `POST /api/recording/stop` or, failing that, on the first `GET …/pose`. Capturing the poses live during the recording looked free — the model computes them anyway — but **nothing resets the model at the start of a take**, so the live integrator enters it carrying an accumulated offset a run from row 0 does not have. Two producers of the same file, differing subtly.

**Streamed as it is computed**, because the sweep must be alive to the limit reached: at 50–77× real time a second of computing buys a minute of take, so the computation outruns the cursor. Records are fixed-size and appended, the writer flushes every 100, and a reader takes however many *whole* records are on disk and ignores a torn tail. The header's `complete` flag is stamped only at the end by seeking back — so a run that died leaves a file that reads fine and is known to be unfinished, which is what makes recomputing it safe rather than a guess.

The two ways a track can be unusable are **remembered apart**, because they stop being true at different moments. A *computation that raised* sticks for the process: it would raise again on the same unchanged CSV, and a panel polling at 4 Hz must not spawn a doomed thread four times a second. A *file that is not a pose track* is remembered only while it is there — the memory is dropped as soon as the file is gone, which is what makes "delete `pose.bin` and reopen the take" a recovery that works. A transient `OSError` on the read is reported for that call and deliberately not remembered at all.

The computation drives a private `Model(bus=None, registry=SIGNALS.isolated(), detectors=DetectorRegistry())` in `asyncio.to_thread`. Measured, a 100 Hz ticker goes from a 16 ms p95 to 24 ms during one, without dropping out — CPython yields the GIL every 5 ms, so no subprocess is needed. The isolation is not decoration: a shared registry would make the file depend on which signals happen to be switched off in the Signaux tab and would move the error counters the panel reads. It is still `model.feed()` going forward, which is the whole condition ADR 0003 puts on a precomputed result — **the rule is not "no cache", it is "no second model"**.

`GET /api/sessions/{session}/takes/{take}/pose?start=&end=&points=` returns the poses **and** the progress (`status`, `records`, `duration_s`, `complete`, `geometry`), serving an incomplete track as it stands. Both halves come from **one header read**, so `records` and the poses beside it always agree — otherwise a caller could not tell "still filling" from "something is wrong". The bulk conversion runs in `asyncio.to_thread`: turning 90 000 records into Python floats is hundreds of milliseconds, and the loop it would otherwise run on is the one owning `processing_loop`. `points` thins by **stride**, not by `ScopeRing`'s min/max envelope: the min and max of a quaternion component over forty ticks is not a rotation anything could render. A take still being recorded is never computed — the run would finish early and stamp a truncated track complete — and that check compares **session and take**, since take names restart at 001 in every session.

#### The proposed start of movement (`storage/onset.py`)

A take is aligned to its video by one event — wheel still on the ground, then lifted sharply — and this module finds the inertial side of it. **The anchor is the first sample that ends a silence of at least 2 s**, silence being the raw gyro norm under 0.5 rad/s. Two constants, no hysteresis (it fires once; "≥ 2 s of silence" *is* the debounce), no flatness criterion.

Three things about it are load-bearing. **It only ever proposes** (ADR 0001): the gyro norm is read straight from the CSV, never from a model signal, so retuning anything in the Signaux tab cannot move an alignment already confirmed — and nothing is stored, because a proposition on disk is a number nobody can date. **It returns *every* candidate**, in order: the pattern happens two to four times per take and the rule's nuance is that the **first** rest counts, not the longest — so overruling it is a choice between candidates rather than free pointing. **0.5 rad/s comes from what a camera resolves** (17 mm per frame at the rim, ~3 px), not from taste: telling still from moving has two and a half orders of margin, but take 004 trembles at 0.02–0.29 rad/s for 580 ms before the real gesture, and a threshold of 0.15 anchors there — 14 frames early, silently.

Split in two on purpose: `read_gyro_norm` reads the CSV, `propose` decides, so a test of the rule is a synthetic `(t, |ω|)` array and not a fabricated file. The reader goes through **`playback_engine.row_to_packet`** — the CSV's only decoder — which is also what puts the curve on the same timeline a pose track is stamped with, so the alignment page can draw both against one cursor. It reads **both column layouts**: a simple `GYRO` slot files its vector under `x,y,z` and a super slot under `gyro_x…` (issue #12), and the reference session recorded the *first*.

`GET /api/sessions/{session}/takes/{take}/onset` returns `{candidats, motif, courbe, duree_s}`. The curve is a **dict of named channels** (`gyro_norm` today) and goes out **unreduced** (ADR 0002) — the deliberate opposite of `GET /api/model/history`: a take is a frozen file read once, so the browser reduces to min/max envelopes at the resolution it is looking at, and zooming (the whole activity of checking an alignment) costs nothing instead of a round trip per level. **"No gyro stream" is not "nothing detected"**: take 001 of the reference session is 171 rows of `GAME_RV` and the method does not apply to it — that distinction carries the alignment page's whole degraded state, so it comes out of the endpoint as a `motif` rather than an empty list. Unlike the pose track, a take being recorded is not refused: nothing is cached, so a growing CSV simply proposes from what is written so far.

**A name from outside can never leave `sessions/` (`storage/paths.py`).** `os.path.join` drops every component preceding an absolute one, so `take_path("a", "/etc/passwd")` *was* `/etc/passwd` — no `..` needed. Two independent layers now: a **shape** rule (`NAME_PATTERN`, declared as `api/models.py:PathSegment` on the path parameters and request fields, so a malformed name is a 422 no handler ever sees) and **containment** (`confine()`, called by `session_path()`/`take_path()`, which closes every caller at once rather than route by route — reject the absolute, `realpath` because it resolves `..` *and* symlinks where `normpath` follows a link straight out, then `commonpath`). Each segment is confined against **its own parent**, or `take_path("a", "..")` — the session directory — would pass. The builders therefore return absolute, symlink-resolved paths.

The two layers are deliberately not the same rule: **shape judges a request, containment judges a path**, and a name already on disk is judged by neither — `list_takes()` skips a take it cannot load *silently*, so a validation raising from there would delete takes from the panel instead of reporting an error. Only a take resolving outside the tree is dropped (it could not be replayed either), and with a log line. `video_file` is validated the same way — bare filename, whitelisted extension, resolved under its own take — because it is the free string that a future `GET .../video` would open, and the one that removes the accidental protection the fixed `raw.csv`/`take.json` suffixes provide today.

**The same two layers govern the two profile roots**, `params/` (`model/params.py`) and `mappings/` (`osc/routes.py`): `ProfileRequest.name` is a `PathSegment`, and both `profile_path()` builders call `confine()`, which closes `save_profile` and `load_profile` together. Two differences from a take are worth knowing. First, **a profile name is never slugified** — `save_profile("x")` writes `x.json` verbatim — so `PathSegment` is not merely a guard here, it *is* the naming rule, and a profile may no longer be called `réglages roue`. Second, the `.json` suffix is glued on **inside** the confined segment, which means a bare `..` resolves to a contained `...json` and only the shape layer refuses it: the one input where the two layers genuinely differ. `list_profiles()` deliberately does not go through `profile_path()` — same reason as `list_takes()`, a profile already saved must keep listing even when a request could no longer name it.

## Geometry / config knobs

`config.py` holds the wheel geometry (`R_TORE` major radius, `r_TORE` tube radius) and `DEGENERATE_THRESHOLD` (below which the wheel is treated as flat and horizontal directions are undefined). `model/signals/wheel.py`, `GET /api/config` and `simulator/motion.py` all read those two numbers from there, so there is exactly one of each in the process.

The geometry used to sit on a slider (`wheel_R_m` / `wheel_r_m` in `PARAMS`) and was **deliberately taken off it** (ADR 0004). They are *measurements* of the object, not settings to be tuned by ear, and a pose track is precomputed from them: moving either mid-séance would silently invalidate every track already on disk. Swapping wheels is now an edit here and a restart — the right price for a number the recordings depend on. A saved parameter profile still mentioning the old keys loads fine; `ParamStore.load_profile` drops names that no longer exist.

### ESP32 network detection

The ESP has no fixed IP. `config.ESP_HOST` is its **mDNS hostname** (`imu-cyrwheel.local`); `core.startup()` resolves it to an IP via `EspConfigurator.resolve()` (OS resolver → Bonjour/mDNS on macOS, IPv4 only) instead of hardcoding the DHCP address. A literal IPv4 in `ESP_HOST` is used as-is (bypasses mDNS). Resolution failure is **non-fatal**: startup skips `SET_HOST`, and `log_stats` self-heals from the **data plane** — it adopts the source IP of incoming sensor packets (`UDPReceiver.last_esp_ip`) as the config send target whenever it differs, and issues `set_host` once if the ESP was never ACKed (that ACK also populates the super-slot layout). So detection succeeds if *either* mDNS works *or* any packet arrives. `_local_ip()` still auto-detects which host IP to tell the ESP to send to. The resolved address rides in the snapshot's `status.esp_net` (`{hostname, ip, resolved}`) and shows in the panel's ESP card.

## Simulator (developing without hardware)

The `simulator/` package impersonates the ESP32 **over real UDP sockets**, so everything below the socket runs unmodified: binary parsing, super-slot layout learning, `EspHealth`, the model, CSV, WS, panel. This is the difference from `PlaybackEngine`, which injects straight into the Queue and therefore skips the transport and the whole config plane.

```bash
SIM=1 python3 main.py                      # in-process fake ESP, one command
python3 -m simulator --scenario coin       # or: standalone, then in another terminal…
SIM=extern python3 main.py                 # …point the orchestrator at it
```

`SIM` selects the mode in `config.py`: `1`/`embed` addresses `127.0.0.1` **and** starts the simulator inside `core.startup()`; `extern` only addresses it (a simulator is already running elsewhere); unset means production, and the package is then never imported. `SIM_SCENARIO` picks the motion. Standalone is the one to use when iterating on scenarios, since it restarts without bouncing the orchestrator.

**The config-port split.** `EspConfigurator` binds a local port *and* sends to a remote one; both were `CONFIG_PORT` (4211), which a local simulator cannot also bind. They are now separate: `CONFIG_LOCAL_PORT` (4211, where we receive ACKs) and `CONFIG_PORT` (4211 for real hardware, `SIM_CONFIG_PORT` = 4311 in SIM mode). The simulator replies to the datagram's **source address**, exactly as the firmware does, so the ACK lands on 4211 either way.

**Wire fidelity.** `simulator/wire.py` imports the `struct.Struct` objects from `protocol.py` and only composes them in reverse — no format string is ever duplicated, so the two directions cannot drift. The corollary is that a sim→parser round trip cannot detect a byte-layout error; the anchor for that remains the firmware's `protocol.h`. What it *does* exercise is field naming, dep ordering, layout propagation through the ACK, rates, and timestamps.

**Motion model** (`simulator/motion.py`). Attitude is prescribed analytically as `R(t) = Rz(ψ)·Rx(90°+λ)·Rz(φ)` — the model's frame convention puts the wheel plane in the local xy-plane and the axle on local z, so `u_perp = cos λ` and `pz = R_TORE·cos λ + r_TORE` in closed form. The **gyro is central-differenced from that same attitude**, so ω_local and the emitted quaternion are consistent by construction: a downstream discrepancy is a transport or model bug, never bad data. Scenarios: `static`, `straight` (line at `(R_TORE + r_TORE)·φ̇` — note the rolling radius includes the tube), `coin` (closed circle, constant `pz`), `spiral` (varying lean, crosses the near-degenerate region). `WheelMotion.reference()` returns ground truth, with `px`/`py` only where a closed form genuinely exists — `static` and `straight`; elsewhere `pz` alone, and the `coin` check is that the trajectory closes.

**Nominal behaviour only** — no fault injection. Killing the simulator already exercises the offline path, since the heartbeat simply stops. Boot config mirrors an ESP already set up for the wheel model (GYRO + GAME_RV at 100 Hz, super 0 = `[0, 6]`). Emission uses one asyncio task per stream on absolute deadlines; `asyncio.sleep` resolution caps faithful rates at roughly 200–500 Hz on macOS, and rates sag under CPU contention (still inside `RATE_TOLERANCE`).

## Agent skills

### Issue tracker

Issues live in GitHub Issues on `Zaruitoga/conductor`, via the `gh` CLI. See `docs/agents/issue-tracker.md`.

### Triage labels

Default 5-role vocabulary (needs-triage, needs-info, ready-for-agent, ready-for-human, wontfix). See `docs/agents/triage-labels.md`.

### Domain docs

Single-context layout: `CONTEXT.md` + `docs/adr/` at the repo root (created lazily as terms/decisions arise). See `docs/agents/domain.md`.

### Development workflow

One branch per ticket; backend-only changes auto-merge on a green `tests/run.py`, anything touching `api/static/` waits for manual review. New features go through a ticket, bugfixes don't. See `docs/agents/workflow.md`.
