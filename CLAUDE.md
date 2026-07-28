# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

`conductor` is a Python asyncio orchestrator for a Cyr-wheel IMU project. It receives BNO08x sensor data from an ESP32 over UDP, runs each packet through a processing pipeline (currently computing the 3D torus-centre position via a no-slip rolling model), and broadcasts the enriched packets over WebSocket to downstream clients (Three.js visualiser, Ableton, etc.). It can record sessions to CSV and replay them as if they were live.

## Running

```bash
python3 main.py        # launches uvicorn → FastAPI control panel + REST API on API_PORT (8000)
```

The FastAPI lifespan boots all orchestrator subsystems (UDP receiver, WS server, ESP configurator, processing loop). Open `http://localhost:8000/` for the web control panel. There is no requirements file, build, lint, or test setup. Dependencies are installed ad hoc:

```bash
pip install numpy scipy websockets fastapi "uvicorn[standard]"   # stdlib: asyncio, struct, socket, csv, json
```

Requires Python 3.12+ (uses `X | None` union syntax and modern type hints).

### Control interface (REST API + web panel)

Control (ESP config, sessions, recording, playback) is exposed as a REST API under `/api/...` (see `api/routes.py`) and a vanilla HTML/JS panel served from `api/static/`. This replaced the old stdin keyboard interface. **Commands** are REST: ESP control (`POST /api/esp/host|simple|super`, `DELETE /api/esp/super/{slot}`), session lifecycle (`POST /api/session/start|close`, `PATCH /api/session`), take recording (`POST /api/recording/start|stop|marker`), take editing (`PATCH /api/sessions/{session}/takes/{take}`), playback (`GET /api/sessions`, `POST /api/playback/start|stop` with `{session, take, speed, loop}`).

**Observation is hybrid push/poll** (see `core.panel_snapshot`). The panel's primary channel is a **native FastAPI WebSocket at `/api/ws`** (`panel_ws` in `routes.py`, one push loop per client at ~4 Hz) that sends a merged snapshot: `{status, live, health, session, recording, playback, esp}`. The same per-section dicts are also exposed as REST GETs (`/api/status`, `/api/live`, `/api/health`, `/api/session`, `/api/recording/status`, `/api/playback/status`) which the frontend uses only as a **fallback** when the socket drops (`js/store.js` `startFallback`). All snapshot builders live in `core.py` (`status_dict`/`session_dict`/`recording_dict`/`playback_dict`/`panel_snapshot`) — single source of truth. Stream observation (per-type rates, liveness, latest values) is done backend-side by `LiveMonitor` (`transport/live_monitor.py`), fed from `processing_loop`.

**ESP health is unified** (`transport/esp_health.py`, `EspHealth`, snapshot key `health`). Single source of "is the ESP alive and behaving", fusing two signals so the UI shows one verdict (`online`/`degraded`/`offline`) instead of redundant indicators: (1) **presence + telemetry** from the periodic heartbeat packet (no heartbeat for `config.HEARTBEAT_TIMEOUT_S` ⇒ offline, independent of the sensor stream), and (2) **stream conformance** — it cross-checks the measured per-type rates (`LiveMonitor`) against what the configured ESP state (`configurator.state`, last CFG_ACK) says should arrive, flagging `missing`/`slow` streams (tolerance `config.RATE_TOLERANCE`). The panel renders this in one collapsible "ESP — Santé & connexion" card and drives the header status dot from `health.state`.

There is **no `GET /api/esp/state`**: the ESP config only changes via our own commands (each returns the full ACK), so `EspConfigurator.state` caches the last ACK (populated by the startup `set_host`) and it rides in the snapshot's `esp` field. Connection/ESP liveness is handled by the heartbeat packet + `EspHealth` (see above). The WSServer (8081) stays dedicated to downstream clients.

#### Panel frontend structure (`api/static/`)

No build step, no framework, no CDN — plain ES modules, so it works offline. `index.html` + `style.css` + `js/`:

- **`js/store.js`** — the single ingestion point for the snapshot. Panels subscribe per section (`on("health", fn)`). It owns the WS connection, the 1 s reconnect, the two-tier REST fallback, and the **rate ring buffers** (120 samples ≈ 30 s) that back the sparklines, since the backend only sends instantaneous rates.
- **`js/dom.js`** — the helpers that make a 4 Hz push non-destructive. `setText`/`setAttr` write only on change (so a text selection survives); `keyed()` reconciles a list by key instead of rebuilding it; `syncControl`/`trackDirty`/`clearDirty` **never overwrite a control the user is focused on or has edited but not yet submitted**. This last point matters: an ESP slot's checkbox or Hz field holds uncommitted input, and section-level change-gating alone does not protect it.
- **`js/panels/*.js`** — one module per region, each rendering from its snapshot section and wiring its own REST commands.
- **`js/api.js`** — `api()` fetch wrapper, `action()` command wrapper, stacking toasts.

Two sections, `session` and `esp`, are **change-gated in the store** because they drive form rebuilds; everything else re-renders every tick, which is safe given the write helpers above.

The layout is task-oriented rather than a uniform card grid: a persistent operations column (session strip, health, live, recording, playback, takes) and a collapsible configuration aside (ESP slots, super-slots, presets, session metadata). Breakpoints at 1180 px (aside moves above) and 900 px (single column). Keyboard shortcuts live in `js/shortcuts.js` (`R` rec, `M` marker, `Space` play/pause, `S` stop, `L` loop, `C` config, `?` help) and are suppressed while typing.

The playback progress bar is **read-only by design** — `PlaybackEngine` has no seek. Pause/resume state is always read back from `playback.paused`, never applied optimistically.

### 3D visualiser (`/viz/`)

A **second, independent page** is served from `api/viz/` and mounted at `/viz/` (see `api/app.py` — the mount must be registered *before* the catch-all `/` mount; `html=True` makes `/viz/` serve its `index.html`). It is not part of the control panel: it is a Three.js view of the wheel, ported from the old standalone `roue-cyr-visualisation-2` project, whose three-process chain (`serverUDP_V2.js` → `claude.py` → browser) the conductor has fully absorbed.

**No build step**, matching `api/static/`: three.js r160 and OrbitControls are vendored as raw ES modules in `api/viz/vendor/` and imported relatively (`OrbitControls.js` imports `./three.module.js`, so the two must stay co-located). No npm, no bundler, no CDN — it works offline.

It uses **two WebSockets plus REST**:

- `ws://<host>:WS_PORT/?types=computed` (8081) — the downstream packet stream, drives the wheel. Only packets with `type === "computed"` are used: `game_rv_qw/qx/qy/qz` for attitude, `px/py/pz` for position. Discriminate on **`type`, never `typeId`** — the pipeline rewrites `typeId` to 5, which collides with `0x05 = RV` in `protocol.TYPE_NAME`. The `?types=` filter is server-side (see "Key seams"); without it the page would also receive `gyro`/`game_rv`/`super_0`/`heartbeat`, i.e. 4× the messages to `JSON.parse` for nothing.
- `/api/ws` — the same 4 Hz panel snapshot as the control panel, for ESP health, active session and playback progress.
- REST for commands (playback start/pause/resume/stop, `GET /api/sessions`).

Nothing is hardcoded client-side: **`GET /api/config`** returns `{ws_port, geometry: {R_TORE, r_TORE}}` so the page gets the stream port and the torus dimensions from `config.py`. Both sockets reconnect automatically (~1 s). The pipeline frame is Z-up while Three.js is Y-up, hence the `qFix` -90°/X quaternion applied to both attitude and position. Camera-follow is on by default — a rolling wheel leaves the frame within seconds otherwise; the ground grid is snapped to whole `GRID_CELL` (2 m) steps under the wheel so it reads as fixed ground rather than a carpet being dragged along.

**Render cost is capped on purpose**: `setPixelRatio(Math.min(devicePixelRatio, 1.5))` and MSAA only below dpr 2. Do not "fix" this back to `devicePixelRatio` — on a Retina screen that is 4× the fragments for a fill-rate-bound scene (full-screen ground + grid), and a saturated main thread stops draining the packet socket in time. The HUD shows packets/s **and** fps precisely so the two failure modes stay distinguishable.

Playback from the viz uses the existing API plus **`POST /api/playback/pause|resume`**. `PlaybackEngine` has **no seek** — the progress bar is therefore read-only by design, not an oversight. Pause is an `asyncio.Event` in `_replay_loop`; since row deadlines are absolute (`t0_real + elapsed/speed`), resuming *shifts `t0_real` by the paused duration*, otherwise the backlog would replay in one burst.

## Architecture

`main.py` is a thin entry point that launches uvicorn. The real wiring lives in `core.py`, which owns the central `asyncio.Queue` and the shared singletons (`configurator`, `session_manager`, `csv_logger`, `playback_engine`, `layout`, `PIPELINE_STAGES`). `core.startup()` (called from the FastAPI lifespan in `api/app.py`) starts the UDP/WS endpoints and the `processing_loop` + `log_stats` tasks. The API route handlers (`api/routes.py`) import the same singletons from `core` — that shared-singleton module is the single source of truth for runtime state.

Data flow (live):
```
UDPReceiver ──▶ Queue ──▶ processing_loop ──▶ (CSV write) ──▶ pipeline stages ──▶ WSServer.broadcast
PlaybackEngine ─┘ (replays CSV onto the same Queue — pipeline/WS see no difference from live)
```

Config flow runs on a **separate port**: `EspConfigurator` talks to the ESP32 on port 4211 (commands + ACK replies), while sensor data arrives on port 4210. WebSocket clients connect on 8081. All ports/IPs live in `config.py`.

### Key seams

- **`processing_loop` (core.py)** — the single consumer. Feeds each packet to `monitor.observe` (live metrics) and writes it to CSV **before** the pipeline (raw data is preserved independently of the computation model), then runs it through `PIPELINE_STAGES` in order; the computed torus output is observed again before broadcast. A stage returning `None` drops the packet; an exception is caught, logged, and also drops the packet.

- **Fan-out never blocks the pipeline (`transport/ws_server.py`).** Each 8081 client owns a small bounded queue (`_CLIENT_QUEUE_SIZE = 8`, ~80 ms at 100 Hz) drained by its own writer task; `broadcast()` only does a non-blocking put and drops the *oldest* pending packet for a client that cannot keep up (counted in `status.ws.dropped`). This is not an optimisation but a correctness property: `broadcast()` used to `await send()` for every client inside `processing_loop`, so one slow browser stalled the whole orchestrator — measured with a single visualiser attached, the central queue grew past 6000 packets and the pipeline fell from ~380 to 186 packets/s, which is what made a paused replay keep playing on screen for seconds. For a real-time view the freshest packet is the one that matters, so dropping beats queueing. Clients may also narrow the stream with `?types=a,b` on the connection URL (no query string ⇒ everything, so existing consumers are untouched); serialisation is lazy, a packet nobody subscribes to is never turned into JSON.

- **Playback is exclusive over the pipeline.** The queue has two producers, and their `ts_esp_us` come from unrelated time bases (ESP uptime vs. recorded CSV), so interleaving them makes the dt `TorusPositionStage` derives from that field meaningless and its px/py integration diverges. `core.accept_live` is the admission gate: it is handed to `UDPReceiver` as an injected `accept(packet)` predicate (the receiver holds no policy of its own and never imports the engine), and it drops live packets at the socket while `playback_engine.active` — counted in `status.udp.muted`. **The heartbeat (0x20) is exempt**: it is live-only telemetry, never recorded and therefore never replayed, and without it `EspHealth` would declare the ESP offline a few seconds into every replay.

- **Pipeline stages** subclass `PipelineStage` (`pipeline/base.py`): `async process(packet) -> dict | None` and `async reset()`. To add a stage, create a module under `pipeline/` and append an instance to `PIPELINE_STAGES` in `core.py`. Stateful stages (e.g. integrators) **must** implement `reset()`. Reset happens at **both ends** of a replay: `PlaybackEngine` resets every stage at the start of each pass (and on each loop iteration), and `processing_loop` resets them again when it dequeues the `playback_end` sentinel — otherwise the take's final px/py would silently become live mode's starting offset. Doing it on the sentinel rather than in `stop()` is what orders the reset correctly against the live packets queued behind it.

  As a backstop for the one-packet windows either side of that switch, `_compute_dt` treats any dt that is negative or larger than `config.MAX_DT_S` as a time-base discontinuity: the packet is dropped (not integrated) but the reference advances, so the next packet resynchronises. This also covers ESP reboots and long dropouts.

- **`SuperSlotLayout` (transport/super_layout.py)** is shared mutable state, the trickiest part of the system. The ESP32 can bundle several sensors into one "super slot" packet. The receiver can only name those payload fields (`gyro_x`, `game_rv_qw`, …) if it knows the slot's dep list. That list is learned from the ESP config ACK: `EspConfigurator._recv_ack` calls `layout.update()` on the parsed state, and `protocol.parse_packet` reads it via `layout.get_deps()`. **Until the first ACK arrives**, super packets fall back to opaque `s0..sN` field names with `dep_slots=None` — and `CSVLogger` silently skips those rows. `core.startup()` calls `set_host` (whose ACK populates the layout) precisely so named decoding works immediately. Thread-safety relies on the GIL: the writer runs in a thread (`asyncio.to_thread`), the reader in the event loop.

### Wire protocol

The binary UDP protocol is firmware-coupled and lives in one place: **`transport/protocol.py`** (Python mirror of the firmware's `protocol.h`). It holds all struct layouts, type IDs, the slot↔sensor naming tables, and the pure `parse_packet` / `parse_ack` / `build_*` functions — no I/O, no state. The transport modules are thin shells over it: `udp_receiver.py` (asyncio socket → `parse_packet` → queue) and `esp_configurator.py` (`build_*` → socket → `parse_ack`, plus connection state). All use little-endian `struct` layouts. The 12-byte `DataHeader` is `<BBHII` (version, type, size, seq, ts_esp_us). Packet type IDs (0x01–0x08 simple sensors, 0x10–0x17 super slots, 0x20 heartbeat, 0x30 CFG_ACK) drive parsing in `parse_packet`. The heartbeat (0x20, 24-byte `<IIIiff` payload: uptime_ms, packets_sent, udp_errors, rssi_dbm, cpu_temp_c, battery_pct) replaced the old standalone battery packet — battery is now just one heartbeat field, and the heartbeat is observed/broadcast but **not** written to CSV.

`TorusPositionStage` rewrites a computed packet's `typeId` to `5` and `type` to `"computed"` — downstream WS clients distinguish computed-position packets by this.

### CSV format and the three field-name registries

`csv_logger.py` and `playback_engine.py` must agree on column layout, and both import the canonical super-field set (`ALL_SUPER_NAMED_FIELDS`) from `protocol.py`. The CSV has a fixed wide schema: common columns + Vec3 + Quat + **all** named super fields; only the fields relevant to a given packet are filled, the rest blank. Heartbeat (0x20) is absent from `PAYLOAD_FIELDS` in both files, so it is skipped on write and has nothing to replay. `PACKET_TYPES` in `playback_engine.py` duplicates `TYPE_NAME` from `protocol.py` — **keep them in sync**. Playback packets are reconstructed from named CSV fields and do **not** include `dep_slots`.

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
                                 at take start), video sync fields
```

A session is opened (`create_session`) before recording; takes require an open session (`new_take` raises otherwise → routes return 409). The `.active` pointer makes the open session **survive an orchestrator restart** — `active_session()` just re-reads it. `SessionManager.active_tree()` caches the active session+takes dict for the 4 Hz snapshot push (invalidated on every meta write). `list_sessions()` returns the full tree (sessions with nested take metadata); a take only appears once its `raw.csv` exists. The firmware version is manual until the ESP ACK protocol exposes it.

## Geometry / config knobs

`config.py` holds the torus geometry (`R_TORE` major radius, `r_TORE` tube radius) and `DEGENERATE_THRESHOLD` (below which the wheel is treated as flat and horizontal position is frozen).

### ESP32 network detection

The ESP has no fixed IP. `config.ESP_HOST` is its **mDNS hostname** (`imu-cyrwheel.local`); `core.startup()` resolves it to an IP via `EspConfigurator.resolve()` (OS resolver → Bonjour/mDNS on macOS, IPv4 only) instead of hardcoding the DHCP address. A literal IPv4 in `ESP_HOST` is used as-is (bypasses mDNS). Resolution failure is **non-fatal**: startup skips `SET_HOST`, and `log_stats` self-heals from the **data plane** — it adopts the source IP of incoming sensor packets (`UDPReceiver.last_esp_ip`) as the config send target whenever it differs, and issues `set_host` once if the ESP was never ACKed (that ACK also populates the super-slot layout). So detection succeeds if *either* mDNS works *or* any packet arrives. `_local_ip()` still auto-detects which host IP to tell the ESP to send to. The resolved address rides in the snapshot's `status.esp_net` (`{hostname, ip, resolved}`) and shows in the panel's ESP card.

## Simulator (developing without hardware)

The `simulator/` package impersonates the ESP32 **over real UDP sockets**, so everything below the socket runs unmodified: binary parsing, super-slot layout learning, `EspHealth`, the pipeline, CSV, WS, panel. This is the difference from `PlaybackEngine`, which injects straight into the Queue and therefore skips the transport and the whole config plane.

```bash
SIM=1 python3 main.py                      # in-process fake ESP, one command
python3 -m simulator --scenario coin       # or: standalone, then in another terminal…
SIM=extern python3 main.py                 # …point the orchestrator at it
```

`SIM` selects the mode in `config.py`: `1`/`embed` addresses `127.0.0.1` **and** starts the simulator inside `core.startup()`; `extern` only addresses it (a simulator is already running elsewhere); unset means production, and the package is then never imported. `SIM_SCENARIO` picks the motion. Standalone is the one to use when iterating on scenarios, since it restarts without bouncing the orchestrator.

**The config-port split.** `EspConfigurator` binds a local port *and* sends to a remote one; both were `CONFIG_PORT` (4211), which a local simulator cannot also bind. They are now separate: `CONFIG_LOCAL_PORT` (4211, where we receive ACKs) and `CONFIG_PORT` (4211 for real hardware, `SIM_CONFIG_PORT` = 4311 in SIM mode). The simulator replies to the datagram's **source address**, exactly as the firmware does, so the ACK lands on 4211 either way.

**Wire fidelity.** `simulator/wire.py` imports the `struct.Struct` objects from `protocol.py` and only composes them in reverse — no format string is ever duplicated, so the two directions cannot drift. The corollary is that a sim→parser round trip cannot detect a byte-layout error; the anchor for that remains the firmware's `protocol.h`. What it *does* exercise is field naming, dep ordering, layout propagation through the ACK, rates, and timestamps.

**Motion model** (`simulator/motion.py`). Attitude is prescribed analytically as `R(t) = Rz(ψ)·Rx(90°+λ)·Rz(φ)` — the pipeline's frame convention puts the wheel plane in the local xy-plane and the axle on local z, so `u_perp = cos λ` and `pz = R_TORE·cos λ + r_TORE` in closed form. The **gyro is central-differenced from that same attitude**, so ω_local and the emitted quaternion are consistent by construction: a downstream discrepancy is a transport or pipeline bug, never bad data. Scenarios: `static`, `straight` (line at `(R_TORE + r_TORE)·φ̇` — note the rolling radius includes the tube), `coin` (closed circle, constant `pz`), `spiral` (varying lean, crosses the near-degenerate region). `WheelMotion.reference()` returns ground truth, with `px`/`py` only where a closed form genuinely exists — `static` and `straight`; elsewhere `pz` alone, and the `coin` check is that the trajectory closes.

**Nominal behaviour only** — no fault injection. Killing the simulator already exercises the offline path, since the heartbeat simply stops. Boot config mirrors an ESP already set up for the torus pipeline (GYRO + GAME_RV at 100 Hz, super 0 = `[0, 6]`). Emission uses one asyncio task per stream on absolute deadlines; `asyncio.sleep` resolution caps faithful rates at roughly 200–500 Hz on macOS, and rates sag under CPU contention (still inside `RATE_TOLERANCE`).
