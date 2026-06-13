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

import core
from api.models import (
    HostConfig, SimpleSlotConfig, SuperSlotConfig,
    SessionCreate, SessionUpdate, TakeStart, TakeUpdate, PlaybackRequest,
)

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


@router.get("/session")
async def get_session() -> dict:
    """Active session meta (REST fallback for the WS push)."""
    return {"session": core.session_dict()}


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
async def update_take(session: str, take: str, req: TakeUpdate) -> dict:
    rec = core.csv_logger
    if rec.active and rec._meta and rec._meta.name == take:
        raise HTTPException(409, "Take is being recorded — stop it first")
    try:
        meta = core.session_manager.update_take(
            session, take, req.model_dump(exclude_none=True)
        )
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
    if not os.path.exists(sm.csv_path(sm.take_path(req.session, req.take))):
        raise HTTPException(404, f"Take not found: {req.session}/{req.take}")
    await core.playback_engine.start(
        req.session, req.take, core.queue, core.PIPELINE_STAGES, req.speed, req.loop
    )
    return {"active": True, "session": req.session, "take": req.take,
            "speed": req.speed, "loop": req.loop}


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
