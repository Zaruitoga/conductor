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

**Observation is hybrid push/poll** (see `core.panel_snapshot`). The panel's primary channel is a **native FastAPI WebSocket at `/api/ws`** (`panel_ws` in `routes.py`, one push loop per client at ~4 Hz) that sends a merged snapshot: `{status, live, health, session, recording, playback, esp}`. The same per-section dicts are also exposed as REST GETs (`/api/status`, `/api/live`, `/api/health`, `/api/session`, `/api/recording/status`, `/api/playback/status`) which the frontend uses only as a **fallback** when the socket drops (`app.js` `startFallback`). All snapshot builders live in `core.py` (`status_dict`/`session_dict`/`recording_dict`/`playback_dict`/`panel_snapshot`) — single source of truth. Stream observation (per-type rates, liveness, latest values) is done backend-side by `LiveMonitor` (`transport/live_monitor.py`), fed from `processing_loop`.

**ESP health is unified** (`transport/esp_health.py`, `EspHealth`, snapshot key `health`). Single source of "is the ESP alive and behaving", fusing two signals so the UI shows one verdict (`online`/`degraded`/`offline`) instead of redundant indicators: (1) **presence + telemetry** from the periodic heartbeat packet (no heartbeat for `config.HEARTBEAT_TIMEOUT_S` ⇒ offline, independent of the sensor stream), and (2) **stream conformance** — it cross-checks the measured per-type rates (`LiveMonitor`) against what the configured ESP state (`configurator.state`, last CFG_ACK) says should arrive, flagging `missing`/`slow` streams (tolerance `config.RATE_TOLERANCE`). The panel renders this in one collapsible "ESP — Santé & connexion" card and drives the header status dot from `health.state`.

There is **no `GET /api/esp/state`**: the ESP config only changes via our own commands (each returns the full ACK), so `EspConfigurator.state` caches the last ACK (populated by the startup `set_host`) and it rides in the snapshot's `esp` field. The frontend rebuilds the ESP widgets only when that state actually changes, so the 4 Hz push never clobbers a value being typed. Connection/ESP liveness is handled by the heartbeat packet + `EspHealth` (see above). The WSServer (8081) stays dedicated to downstream clients.

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

- **Pipeline stages** subclass `PipelineStage` (`pipeline/base.py`): `async process(packet) -> dict | None` and `async reset()`. To add a stage, create a module under `pipeline/` and append an instance to `PIPELINE_STAGES` in `core.py`. Stateful stages (e.g. integrators) **must** implement `reset()` — `PlaybackEngine` resets every stage at the start of each replay pass (and on each loop iteration) so integration starts clean.

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
