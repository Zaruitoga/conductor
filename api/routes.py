"""
api/routes.py — REST endpoints for the control panel.

Thin wrappers over the singletons in core.py. Blocking EspConfigurator calls
run via asyncio.to_thread so the event loop stays responsive (same pattern as
the old keyboard interface's run_in_executor).
"""

import asyncio
import os
import time

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect

import config
import core
from api.models import (
    HostConfig, PathSegment, SimpleSlotConfig, SuperSlotConfig,
    SessionCreate, SessionUpdate, TakeStart, TakeUpdate, PlaybackRequest,
    ParamUpdate, ProfileRequest, SignalToggle,
    OscRouteCreate, OscRouteUpdate, OscSettings, OscLiveRefresh,
)
from osc import targets as osc_targets
from storage.paths import UnsafePath

router = APIRouter(prefix="/api")

# Cadence of the panel observation push (seconds).
_WS_PUSH_INTERVAL = 0.25


# ── Observation: WS push (primary) + REST polling (fallback) ────────────────
# Both share the snapshot builders in core.py, so there is one source of truth.

@router.websocket("/ws")
async def panel_ws(ws: WebSocket) -> None:
    """Push the merged panel snapshot (~4 Hz) to one control-panel client."""
    await ws.accept()
    try:
        while True:
            await ws.send_json(core.panel_snapshot())
            await asyncio.sleep(_WS_PUSH_INTERVAL)
    except WebSocketDisconnect:
        pass


@router.get("/config")
async def get_config() -> dict:
    """
    What external clients need to configure themselves.

    Used by the 3D visualiser (/viz/) so it hardcodes neither the downstream
    stream port nor the wheel dimensions.  The geometry comes from the live
    parameters rather than from config.py: swapping wheels between two takes
    changes the model's numbers, and a visualiser drawing the old diameter would
    quietly disagree with the position it is rendering.
    """
    return {
        "ws_port":  config.WS_PORT,
        "geometry": {
            "R_TORE": core.model.params.get("wheel_R_m"),
            "r_TORE": core.model.params.get("wheel_r_m"),
        },
    }


@router.get("/status")
async def get_status() -> dict:
    """Orchestrator status (REST fallback for the WS push)."""
    return core.status_dict()


@router.get("/live")
async def get_live() -> dict:
    """Live stream metrics (REST fallback for the WS push)."""
    return core.monitor.snapshot()


@router.get("/health")
async def get_health() -> dict:
    """Unified ESP health verdict (REST fallback for the WS push)."""
    return core.esp_health.snapshot()


@router.get("/session")
async def get_session() -> dict:
    """Active session meta (REST fallback for the WS push)."""
    return {"session": core.session_dict()}


# ── Model: schema, parameters, signal switches ────────────────────────────
# The schema is what the panel builds itself from, so a signal declared in
# model/signals/ appears in the UI with no frontend change at all.

@router.get("/model/schema")
async def model_schema() -> dict:
    """
    Every declared signal and parameter, plus what the ESP is actually feeding.

    `quantities.configured` and `quantities.observed` differ when the ESP was
    told to send something that is not arriving — a distinct fault from never
    having asked for it, and the panel says which.
    """
    return core.model.schema()


@router.get("/model")
async def model_state() -> dict:
    """Model runtime state with the latest frame (REST fallback for the push)."""
    return core.model_dict()


@router.get("/model/history")
async def model_history(signals: str = "", window: float = 10.0,
                        points: int = 600) -> dict:
    """
    Full-rate history of the named signals, as min/max envelopes per column.

    This is the endpoint the scope polls, and the reason it exists: the 4 Hz
    panel push shows one sample in twenty-five, which is enough to read a number
    and useless for deciding where a detection should fire.  Envelopes rather
    than decimation, so a one-sample spike still reads as a spike.
    """
    names = [s for s in signals.split(",") if s] or core.scope.names
    return core.scope.history(names, window_s=window, points=points)


@router.get("/model/params")
async def get_params() -> dict:
    return core.model.params.snapshot()


@router.patch("/model/params")
async def set_params(req: ParamUpdate) -> dict:
    """
    Change tuning values live. Takes effect on the next tick, never mid-tick.

    Clamped to the bounds each parameter declared, so a slider or a typo cannot
    put the model in a state its author never considered.
    """
    try:
        applied = core.model.params.update(req.values)
    except KeyError as e:
        raise HTTPException(400, str(e))
    return {"applied": applied, "revision": core.model.params.revision}


@router.post("/model/params/reset")
async def reset_params() -> dict:
    core.model.params.reset_to_defaults()
    return core.model.params.snapshot()


@router.post("/model/params/save")
async def save_profile(req: ProfileRequest) -> dict:
    core.model.params.save_profile(req.name)
    return core.model.params.snapshot()


@router.post("/model/params/load")
async def load_profile(req: ProfileRequest) -> dict:
    try:
        core.model.params.load_profile(req.name)
    except FileNotFoundError:
        raise HTTPException(404, f"Profil introuvable : {req.name}")
    return core.model.params.snapshot()


@router.post("/model/signal")
async def toggle_signal(req: SignalToggle) -> dict:
    """
    Switch a signal off or back on.

    Its dependents follow automatically — the schema reports them as
    "dépend de <name>" rather than silently producing nulls.
    """
    try:
        core.model.registry.set_enabled(req.name, req.enabled)
    except KeyError as e:
        raise HTTPException(404, str(e))
    return {"name": req.name, "enabled": req.enabled}


@router.post("/model/reset")
async def reset_model() -> dict:
    """Clear every integrator and envelope — notably the drifting position."""
    core.model.reset()
    return {"reset": True}


# ── ESP control ───────────────────────────────────────────────────────────

@router.post("/esp/host")
async def set_host(cfg: HostConfig) -> dict:
    ip = cfg.ip or core._local_ip()
    ack = await asyncio.to_thread(core.configurator.set_host, ip)
    if ack is None:
        raise HTTPException(504, "ESP did not acknowledge SET_HOST")
    return {"ip": ip, "state": ack}


@router.post("/esp/simple")
async def set_simple(cfg: SimpleSlotConfig) -> dict:
    if cfg.hz <= 0:
        raise HTTPException(400, "hz must be > 0")
    rate_us = int(1e6 / cfg.hz)
    ack = await asyncio.to_thread(
        core.configurator.set_simple, cfg.slot, cfg.enabled, rate_us
    )
    if ack is None:
        raise HTTPException(504, "ESP did not acknowledge SET_SIMPLE")
    return {"state": ack}


@router.post("/esp/super")
async def set_super(cfg: SuperSlotConfig) -> dict:
    ack = await asyncio.to_thread(
        core.configurator.set_super, cfg.slot, cfg.deps, cfg.skip
    )
    if ack is None:
        raise HTTPException(504, "ESP did not acknowledge SET_SUPER")
    return {"state": ack}


@router.delete("/esp/super/{slot}")
async def del_super(slot: int) -> dict:
    ack = await asyncio.to_thread(core.configurator.del_super, slot)
    if ack is None:
        raise HTTPException(504, "ESP did not acknowledge DEL_SUPER")
    return {"state": ack}


# ── Session lifecycle ─────────────────────────────────────────────────────

@router.post("/session/start")
async def session_start(req: SessionCreate) -> dict:
    if core.session_manager.active_session() is not None:
        raise HTTPException(409, "A session is already open — close it first")
    meta = core.session_manager.create_session(
        title=req.title,
        location=req.location,
        equipment=req.equipment,
        comments=req.comments,
        firmware_version=req.firmware_version,
    )
    return {"session": core.session_dict()} if meta else {"session": None}


@router.patch("/session")
async def session_update(req: SessionUpdate) -> dict:
    try:
        core.session_manager.update_session(req.model_dump(exclude_none=True))
    except RuntimeError:
        raise HTTPException(409, "No active session")
    return {"session": core.session_dict()}


@router.post("/session/close")
async def session_close() -> dict:
    if core.csv_logger.active:
        raise HTTPException(409, "Stop the recording before closing the session")
    try:
        meta = core.session_manager.close_session()
    except RuntimeError:
        raise HTTPException(409, "No active session")
    return {"closed": meta.name}


# ── Recording (takes) ─────────────────────────────────────────────────────

@router.post("/recording/start")
async def recording_start(req: TakeStart) -> dict:
    if core.csv_logger.active:
        raise HTTPException(409, "Recording already active")
    if core.playback_engine.active:
        raise HTTPException(409, "Cannot record during playback")
    try:
        take_dir, meta = core.session_manager.new_take(
            title=req.title,
            performer=req.performer,
            figures=req.figures,
            notes=req.notes,
            imu_config=core.configurator.state,
        )
    except RuntimeError:
        raise HTTPException(409, "No active session — open one first")
    core.csv_logger.start(take_dir, meta)
    return {"active": True, "take": meta.name}


@router.post("/recording/stop")
async def recording_stop() -> dict:
    if not core.csv_logger.active:
        raise HTTPException(409, "No active recording")
    core.csv_logger.stop()
    return {"active": False}


@router.post("/recording/marker")
async def recording_marker() -> dict:
    if not core.csv_logger.active:
        raise HTTPException(409, "No active recording")
    ts = time.time_ns() // 1000
    core.csv_logger.mark_sync(ts)
    return {"sync_marker_ts_us": ts}


@router.get("/recording/status")
async def recording_status() -> dict:
    """Recording state (REST fallback for the WS push)."""
    return core.recording_dict()


# ── Sessions browser / take editing ───────────────────────────────────────

@router.get("/sessions")
async def list_sessions() -> dict:
    """Full tree: every session's metadata with its takes' metadata."""
    return {"sessions": core.session_manager.list_sessions()}


@router.patch("/sessions/{session}/takes/{take}")
async def update_take(session: PathSegment, take: PathSegment,
                      req: TakeUpdate) -> dict:
    """
    Patch one take's metadata.

    Both names are `PathSegment`, so a `..` — which Starlette passes through
    unnormalised outside StaticFiles — is a 422 before this body runs.  The
    UnsafePath catch below is the second layer: a well-shaped name can still
    resolve out of the tree through a symlink.
    """
    rec = core.csv_logger
    if rec.active and rec._meta and rec._meta.name == take:
        raise HTTPException(409, "Take is being recorded — stop it first")
    try:
        meta = core.session_manager.update_take(
            session, take, req.model_dump(exclude_none=True)
        )
    except UnsafePath as e:
        raise HTTPException(400, str(e))
    except FileNotFoundError:
        raise HTTPException(404, f"Take not found: {session}/{take}")
    return {"take": meta.name}


# ── Playback ──────────────────────────────────────────────────────────────

@router.post("/playback/start")
async def playback_start(req: PlaybackRequest) -> dict:
    if core.csv_logger.active:
        raise HTTPException(409, "Stop recording before starting playback")
    if core.playback_engine.active:
        raise HTTPException(409, "Playback already active")
    sm = core.session_manager
    try:
        csv_path = sm.csv_path(sm.take_path(req.session, req.take))
    except UnsafePath as e:
        raise HTTPException(400, str(e))
    if not os.path.exists(csv_path):
        raise HTTPException(404, f"Take not found: {req.session}/{req.take}")
    await core.playback_engine.start(
        req.session, req.take, core.queue, core.model.reset, req.speed, req.loop
    )
    return {"active": True, "session": req.session, "take": req.take,
            "speed": req.speed, "loop": req.loop}


@router.post("/playback/stop")
async def playback_stop() -> dict:
    if not core.playback_engine.active:
        raise HTTPException(409, "No active playback")
    core.playback_engine.stop()
    return {"active": False}


@router.post("/playback/pause")
async def playback_pause() -> dict:
    if not core.playback_engine.active:
        raise HTTPException(409, "No active playback")
    core.playback_engine.pause()
    return {"active": True, "paused": True}


@router.post("/playback/resume")
async def playback_resume() -> dict:
    if not core.playback_engine.active:
        raise HTTPException(409, "No active playback")
    core.playback_engine.resume()
    return {"active": True, "paused": False}


@router.get("/playback/status")
async def playback_status() -> dict:
    """Playback state with progress (REST fallback for the WS push)."""
    return core.playback_dict()


# ── OSC bridge: bus → Ableton Live, remapped by data, not by code ──────────
# Route definitions are edited here; runtime state (enabled, rate, AbletonOSC
# link health) rides in the panel snapshot's "osc" section — see
# core.osc_dict() / core.osc_routes_dict() for the single source of truth both
# this REST fallback and the WS push read from.

@router.get("/osc")
async def osc_state() -> dict:
    """OSC bridge runtime (REST fallback for the WS push)."""
    return core.osc_dict()


@router.get("/osc/routes")
async def osc_routes_list() -> dict:
    """Every route, annotated with whether its source still exists."""
    return core.osc_routes_dict()


@router.post("/osc/routes")
async def osc_route_create(req: OscRouteCreate) -> dict:
    try:
        core.osc_routes.create(**req.model_dump())
    except ValueError as e:
        raise HTTPException(400, str(e))
    return core.osc_routes_dict()


@router.patch("/osc/routes/{route_id}")
async def osc_route_update(route_id: str, req: OscRouteUpdate) -> dict:
    try:
        core.osc_routes.update(route_id, **req.model_dump(exclude_none=True))
    except KeyError as e:
        raise HTTPException(404, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))
    return core.osc_routes_dict()


@router.delete("/osc/routes/{route_id}")
async def osc_route_delete(route_id: str) -> dict:
    try:
        core.osc_routes.delete(route_id)
    except KeyError as e:
        raise HTTPException(404, str(e))
    return core.osc_routes_dict()


@router.post("/osc/routes/{route_id}/test")
async def osc_route_test(route_id: str) -> dict:
    """
    Sweep a route's output over ~1 s so whatever it is mapped to visibly moves
    in Live — the practical way to MIDI-learn or verify a mapping without
    moving the wheel.
    """
    try:
        await core.osc_bridge.test_route(route_id)
    except KeyError as e:
        raise HTTPException(404, str(e))
    except ValueError as e:
        raise HTTPException(400, str(e))
    return {"tested": route_id}


@router.post("/osc/routes/save")
async def osc_routes_save(req: ProfileRequest) -> dict:
    core.osc_routes.save_profile(req.name)
    return core.osc_routes_dict()


@router.post("/osc/routes/load")
async def osc_routes_load(req: ProfileRequest) -> dict:
    try:
        core.osc_routes.load_profile(req.name)
    except FileNotFoundError:
        raise HTTPException(404, f"Mapping introuvable : {req.name}")
    return core.osc_routes_dict()


@router.patch("/osc/settings")
async def osc_settings_update(req: OscSettings) -> dict:
    """Master enable, send-rate cap, and the AbletonOSC target — all live,
    no restart (osc/live.py's client is swapped, not the whole bridge)."""
    if req.enabled is not None:
        core.osc_bridge.set_enabled(req.enabled)
    if req.rate_hz is not None:
        if req.rate_hz <= 0:
            raise HTTPException(400, "rate_hz must be > 0")
        core.osc_bridge.rate_hz = req.rate_hz
    if req.host is not None or req.port is not None:
        core.osc_live.retarget(
            req.host if req.host is not None else core.osc_live.host,
            req.port if req.port is not None else core.osc_live.send_port,
        )
    return core.osc_dict()


@router.get("/osc/targets")
async def osc_targets_list() -> dict:
    """The catalog of known AbletonOSC destinations (osc/targets.py)."""
    return {"targets": osc_targets.schema()}


@router.get("/osc/live")
async def osc_live_state() -> dict:
    """Whatever track/device/parameter names have been discovered so far —
    instant, no round trip to Live. See POST .../refresh for that."""
    return {"online": core.osc_live.online, **core.osc_live.discovery_snapshot()}


@router.post("/osc/live/refresh")
async def osc_live_refresh(req: OscLiveRefresh) -> dict:
    """Re-query AbletonOSC at one level of the tree and update the cache."""
    if req.level == "tracks":
        await core.osc_live.refresh_tracks()
    elif req.level == "devices":
        if req.track is None:
            raise HTTPException(400, "track is required for level=devices")
        await core.osc_live.refresh_devices(req.track)
    elif req.level == "params":
        if req.track is None or req.device is None:
            raise HTTPException(400, "track and device are required for level=params")
        await core.osc_live.refresh_parameters(req.track, req.device)
    else:
        raise HTTPException(400, f"Unknown level: {req.level!r}")
    return {"online": core.osc_live.online, **core.osc_live.discovery_snapshot()}
