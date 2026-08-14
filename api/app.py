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
_ALIGN_DIR  = os.path.join(os.path.dirname(__file__), "align")


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


# The root mount is a catch-all, so every other page must be registered before
# it. html=True makes /viz/ and /align/ serve their own index.html.
#
# Three surfaces, three jobs that deliberately do not share a page: the panel is
# a show surface, the visualiser's main-thread budget is capped on purpose, and
# alignment is post-production work with a video decoder in it. `test_surfaces.py`
# pins the ordering, which fails silently otherwise.
app.mount("/viz",   StaticFiles(directory=_VIZ_DIR,   html=True), name="viz")
app.mount("/align", StaticFiles(directory=_ALIGN_DIR, html=True), name="align")
app.mount("/",      StaticFiles(directory=_STATIC_DIR), name="static")
