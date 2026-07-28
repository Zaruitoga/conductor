"""
api/app.py — FastAPI application.

Wires the orchestrator (core.py) into a FastAPI lifespan and serves the static
control panel plus the REST API. Contains no business logic.
"""

import logging
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

import core
from api.routes import router

log = logging.getLogger("api")

_STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")
_VIZ_DIR    = os.path.join(os.path.dirname(__file__), "viz")


@asynccontextmanager
async def lifespan(app: FastAPI):
    await core.startup()
    try:
        yield
    finally:
        await core.shutdown()


app = FastAPI(title="Conductor control panel", lifespan=lifespan)
app.include_router(router)


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(os.path.join(_STATIC_DIR, "index.html"))


# The root mount is a catch-all, so /viz must be registered before it.
# html=True makes /viz/ serve the visualiser's index.html.
app.mount("/viz", StaticFiles(directory=_VIZ_DIR, html=True), name="viz")
app.mount("/",    StaticFiles(directory=_STATIC_DIR), name="static")
