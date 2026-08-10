"""Jarvis application entrypoint."""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .api import (
    auth,
    autonomy,
    bridge,
    browser,
    chat,
    device,
    hud,
    identity,
    skills,
    voice,
)
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

    from .agent.selfimprove import get_improver

    get_improver().start()

    log.info("Jarvis ready")
    yield
    await get_improver().stop()
    await get_llm().aclose()
    if settings.browser_enabled:
        # Chromium is a child process, not a coroutine — without this it survives
        # the reload and the next start finds the port and profile still held.
        from .integrations.browser import get_browser

        await get_browser().close()


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
app.include_router(browser.router)
app.include_router(hud.router)


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
    class RevalidatingStatic(StaticFiles):
        """Serve scripts with must-revalidate.

        The modules import each other, so a browser holding one from cache while
        fetching another fresh produces an import error and a completely dead
        page. ETags still make the revalidation a 304 in the normal case, so
        this costs a conditional request rather than a download.
        """

        def file_response(self, *args, **kwargs):
            response = super().file_response(*args, **kwargs)
            if str(getattr(response, "path", "")).endswith((".js", ".css")):
                response.headers["Cache-Control"] = "no-cache, must-revalidate"
            return response

    app.mount("/static", RevalidatingStatic(directory=WEB_DIR), name="static")

    @app.get("/")
    async def index():
        # Never cached. It is the entry point and it carries the recovery code,
        # so a stale copy cannot be repaired by anything the app itself does —
        # the fix lives inside the file that is stuck.
        return FileResponse(
            WEB_DIR / "index.html",
            headers={"Cache-Control": "no-store, must-revalidate"},
        )

    @app.get("/reset")
    async def reset():
        """A page that empties every cache and unregisters the service worker.

        The escape hatch for a browser holding files it will not let go of. It
        depends on nothing already cached, so it works when the app itself
        cannot start — which is exactly when it is needed.
        """
        return HTMLResponse(
            """<!doctype html><meta charset=utf-8>
<title>Resetting Jarvis</title>
<style>body{background:#0b0f14;color:#cfe6ff;font:15px/1.5 -apple-system,sans-serif;
padding:40px 24px;text-align:center}</style>
<h2>Clearing cached files…</h2><p id=s>Working.</p>
<script>
(async function () {
  var out = [];
  try {
    if (window.caches) {
      var keys = await caches.keys();
      await Promise.all(keys.map(function (k) { return caches.delete(k); }));
      out.push(keys.length + ' cache(s) cleared');
    }
    if (navigator.serviceWorker) {
      var regs = await navigator.serviceWorker.getRegistrations();
      await Promise.all(regs.map(function (r) { return r.unregister(); }));
      out.push(regs.length + ' worker(s) removed');
    }
  } catch (e) { out.push('error: ' + e.message); }
  document.getElementById('s').textContent = out.join(', ') + ' — reopening…';
  setTimeout(function () { location.replace('/?reset=' + Date.now()); }, 1200);
})();
</script>""",
            headers={"Cache-Control": "no-store"},
        )

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
