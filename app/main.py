"""Playback Hub : dashboard unifié des plugins Playback Reporting d'Emby et de Jellyfin."""
import asyncio
import base64
import logging
import os
import secrets
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import collector, config, db, stats

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("playback-hub")
STATIC = os.path.join(os.path.dirname(__file__), "static")


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init()
    servers = config.load_servers()
    if not servers:
        log.warning("Aucun serveur configuré : renseigner EMBY_URL/EMBY_API_KEY et/ou JELLYFIN_URL/JELLYFIN_API_KEY")
    for s in servers:
        log.info("Serveur %s : %s", s.label, s.url)
    task = asyncio.create_task(collector.run_forever())
    yield
    task.cancel()


app = FastAPI(title="Playback Hub", lifespan=lifespan, docs_url=None, redoc_url=None)


@app.middleware("http")
async def basic_auth(request: Request, call_next):
    if config.AUTH_USER and config.AUTH_PASSWORD and request.url.path != "/healthz":
        ok = False
        header = request.headers.get("authorization", "")
        if header.lower().startswith("basic "):
            try:
                user, _, password = base64.b64decode(header[6:]).decode().partition(":")
                ok = secrets.compare_digest(user, config.AUTH_USER) and \
                    secrets.compare_digest(password, config.AUTH_PASSWORD)
            except Exception:
                ok = False
        if not ok:
            return Response(status_code=401, headers={"WWW-Authenticate": 'Basic realm="Playback Hub"'})
    return await call_next(request)


@app.get("/healthz")
def healthz():
    return {"ok": True}


@app.get("/api/dashboard")
def api_dashboard(days: int = 30, source: str = "all", person: str = ""):
    return stats.dashboard(max(0, min(days, 3660)), source, person)


@app.get("/api/status")
def api_status():
    return stats.status()


@app.post("/api/sync")
async def api_sync():
    await collector.sync_all()
    return stats.status()


@app.get("/api/users")
def api_users():
    return stats.list_users()


class AliasBody(BaseModel):
    server: str
    user_id: str
    alias: str = ""


@app.put("/api/users/alias")
def api_alias(body: AliasBody):
    if not stats.set_alias(body.server, body.user_id, body.alias):
        raise HTTPException(404, "Utilisateur inconnu")
    return {"ok": True}


@app.get("/")
def index():
    return FileResponse(os.path.join(STATIC, "index.html"), headers={"Cache-Control": "no-cache"})


app.mount("/static", StaticFiles(directory=STATIC), name="static")
