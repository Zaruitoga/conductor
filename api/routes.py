"""
api/routes.py — REST endpoints for the control panel.

Thin wrappers over the singletons in core.py. Blocking EspConfigurator calls
run via asyncio.to_thread so the event loop stays responsive (same pattern as
the old keyboard interface's run_in_executor).
"""

import asyncio
import time

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect

import core
from api.models import HostConfig, SimpleSlotConfig, SuperSlotConfig, PlaybackRequest

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


@router.get("/status")
async def get_status() -> dict:
    """Orchestrator status (REST fallback for the WS push)."""
    return core.status_dict()


@router.get("/live")
async def get_live() -> dict:
    """Live stream metrics (REST fallback for the WS push)."""
    return core.monitor.snapshot()


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


# ── Recording ─────────────────────────────────────────────────────────────

@router.post("/recording/start")
async def recording_start() -> dict:
    if core.csv_logger.active:
        raise HTTPException(409, "Recording already active")
    if core.playback_engine.active:
        raise HTTPException(409, "Cannot record during playback")
    session_dir, meta = core.session_manager.new_session()
    core.csv_logger.start(session_dir, meta)
    return {"active": True, "session": meta.name}


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


# ── Playback ──────────────────────────────────────────────────────────────

@router.get("/sessions")
async def list_sessions() -> dict:
    """Sessions with metadata (date, duration, packet count, sync marker)."""
    sm = core.session_manager
    out = []
    for name in sm.list_sessions():
        info = {"name": name}
        try:
            meta = sm.load_meta(sm.session_path(name))
            info.update(
                started_at=meta.started_at,
                packet_count=meta.packet_count,
                duration_s=round((meta.last_ts_rx_us - meta.first_ts_rx_us) / 1e6, 1),
                has_marker=meta.sync_marker_ts_us > 0,
            )
        except Exception:
            pass  # older/missing session.json — fall back to bare name
        out.append(info)
    return {"sessions": out}


@router.post("/playback/start")
async def playback_start(req: PlaybackRequest) -> dict:
    if core.csv_logger.active:
        raise HTTPException(409, "Stop recording before starting playback")
    if core.playback_engine.active:
        raise HTTPException(409, "Playback already active")
    if req.name not in core.session_manager.list_sessions():
        raise HTTPException(404, f"Session not found: {req.name}")
    await core.playback_engine.start(
        req.name, core.queue, core.PIPELINE_STAGES, req.speed, req.loop
    )
    return {"active": True, "session": req.name, "speed": req.speed, "loop": req.loop}


@router.post("/playback/stop")
async def playback_stop() -> dict:
    if not core.playback_engine.active:
        raise HTTPException(409, "No active playback")
    core.playback_engine.stop()
    return {"active": False}


@router.get("/playback/status")
async def playback_status() -> dict:
    """Playback state with progress (REST fallback for the WS push)."""
    return core.playback_dict()
