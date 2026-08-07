"""Jarvis application entrypoint."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .api import auth, autonomy, bridge, chat, device, identity, skills, voice
from .config import get_settings
from .db import init_db
from .llm.client import get_llm
from .tools.base import load_all_tools

log = logging.getLogger(__name__)
WEB_DIR = Path(__file__).resolve().parents[2] / "web"


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    logging.basicConfig(
        level=settings.log_level.upper(),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    await init_db()
    load_all_tools()

    from .skills import ensure_builtins

    await ensure_builtins()

    for feature in ("auth", "llm"):
        missing = settings.missing_for(feature)
        if missing:
            log.warning("%s is not configured — missing %s", feature, ", ".join(missing))

    log.info("Jarvis ready")
    yield
    await get_llm().aclose()


app = FastAPI(title="Jarvis", version="0.1.0", lifespan=lifespan)

# The PWA is served from this same origin, so CORS only matters if you point a
# separate frontend at the API. Kept permissive-but-credentialed-off deliberately.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router)
app.include_router(chat.router)
app.include_router(device.router)
app.include_router(bridge.router)
app.include_router(autonomy.router)
app.include_router(voice.router)
app.include_router(identity.router)
app.include_router(skills.router)


@app.get("/api/health")
async def health():
    settings = get_settings()
    from .tools.base import registry

    return {
        "status": "ok",
        "app": settings.app_name,
        "tools": len(registry.all()),
        "features": {
            f: not settings.missing_for(f) for f in ("llm", "mail", "calendar", "messages", "auth")
        },
        "autonomy_enabled": settings.autonomy_enabled,
    }


if WEB_DIR.is_dir():
    app.mount("/static", StaticFiles(directory=WEB_DIR), name="static")

    @app.get("/")
    async def index():
        return FileResponse(WEB_DIR / "index.html")

    @app.get("/manifest.webmanifest")
    async def manifest():
        return FileResponse(WEB_DIR / "manifest.webmanifest")

    @app.get("/sw.js")
    async def service_worker():
        # Must be served from the root scope or it can't control the whole app.
        return FileResponse(WEB_DIR / "sw.js", media_type="application/javascript")

else:

    @app.get("/")
    async def index_missing():
        return JSONResponse({"error": "web/ directory not found"}, status_code=500)
