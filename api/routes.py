"""
api/routes.py — REST endpoints for the control panel.

Thin wrappers over the singletons in core.py. Blocking EspConfigurator calls
run via asyncio.to_thread so the event loop stays responsive (same pattern as
the old keyboard interface's run_in_executor).
"""

import asyncio
import time

from fastapi import APIRouter, HTTPException

import core
from api.models import HostConfig, SimpleSlotConfig, SuperSlotConfig, PlaybackRequest

router = APIRouter(prefix="/api")


# ── Status / ESP state ────────────────────────────────────────────────────

@router.get("/status")
async def get_status() -> dict:
    """Overall orchestrator status — drives the frontend's connection banner."""
    udp = core.udp_protocol
    ws  = core.ws_server
    return {
        "mode":        core.current_mode(),
        "queue_depth": core.queue.qsize() if core.queue else 0,
        "udp": {
            "rx":          udp.stats["rx"]     if udp else 0,
            "errors":      udp.stats["errors"] if udp else 0,
            "last_esp_ip": udp.last_esp_ip     if udp else None,
        },
        "ws": {
            "tx":      ws.stats["tx"]   if ws else 0,
            "clients": len(ws.clients) if ws else 0,
        },
    }


@router.get("/esp/state")
async def get_esp_state() -> dict:
    """Full ESP config (simples, supers, host). reachable=False on ACK timeout."""
    state = await asyncio.to_thread(core.configurator.get_state)
    if state is None:
        return {"reachable": False}
    return {"reachable": True, **state}


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
    logger = core.csv_logger
    meta = logger._meta
    return {
        "active":       logger.active,
        "session":      meta.name if meta else None,
        "packet_count": meta.packet_count if meta else 0,
    }


# ── Playback ──────────────────────────────────────────────────────────────

@router.get("/sessions")
async def list_sessions() -> dict:
    return {"sessions": core.session_manager.list_sessions()}


@router.post("/playback/start")
async def playback_start(req: PlaybackRequest) -> dict:
    if core.csv_logger.active:
        raise HTTPException(409, "Stop recording before starting playback")
    if core.playback_engine.active:
        raise HTTPException(409, "Playback already active")
    if req.name not in core.session_manager.list_sessions():
        raise HTTPException(404, f"Session not found: {req.name}")
    await core.playback_engine.start(
        req.name, core.queue, core.PIPELINE_STAGES, req.speed
    )
    return {"active": True, "session": req.name, "speed": req.speed}


@router.post("/playback/stop")
async def playback_stop() -> dict:
    if not core.playback_engine.active:
        raise HTTPException(409, "No active playback")
    core.playback_engine.stop()
    return {"active": False}


@router.get("/playback/status")
async def playback_status() -> dict:
    return {"active": core.playback_engine.active}
