# main.py — FastAPI telemetry server.
#
# Ingests decoded frames from the collector, stores them (db.py), keeps a live
# "latest value per channel" map in memory, pushes updates to browser dashboards
# over a WebSocket, and serves history/track/schema for charts and the map.
#
# Self-contained: imports nothing from collector/. The only shared artifact is the
# vendored catalog, read via schema.py.

import asyncio
import contextlib
import os

from fastapi import (
    Depends, FastAPI, HTTPException, Request, WebSocket, WebSocketDisconnect,
)
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool

import auth
import db
import schema
import settings

_HERE = os.path.dirname(os.path.abspath(__file__))
_STATIC = os.path.join(_HERE, "static")


# --------------------------------------------------------------------------- state

def channel_for(msg_type: str, fields: dict | None) -> str:
    """Live-view key. MPPT packets split by controller index; everything else
    is keyed by its catalog message name."""
    if msg_type == "MpptPacket" and fields and fields.get("mppt_index") is not None:
        return f"MpptPacket:{fields['mppt_index']}"
    return msg_type


class Hub:
    """In-memory latest-value map + set of live WebSocket clients."""

    def __init__(self):
        self.latest: dict[str, dict] = {}
        self._clients: set[WebSocket] = set()
        self._lock = asyncio.Lock()

    def apply(self, event: dict) -> dict:
        ch = channel_for(event["msg_type"], event.get("fields"))
        item = {
            "channel": ch,
            "msg_type": event["msg_type"],
            "ts_utc": event["ts_utc"],
            "fields": event.get("fields") or {},
        }
        # Keep only the newest sample per channel.
        prev = self.latest.get(ch)
        if prev is None or item["ts_utc"] >= prev["ts_utc"]:
            self.latest[ch] = item
        return item

    async def register(self, ws: WebSocket):
        async with self._lock:
            self._clients.add(ws)

    async def unregister(self, ws: WebSocket):
        async with self._lock:
            self._clients.discard(ws)

    async def broadcast(self, message: dict):
        async with self._lock:
            clients = list(self._clients)
        dead = []
        for ws in clients:
            try:
                await ws.send_json(message)
            except Exception:
                dead.append(ws)
        if dead:
            async with self._lock:
                for ws in dead:
                    self._clients.discard(ws)


hub = Hub()


@contextlib.asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    # Seed the latest map from recent history so a fresh dashboard isn't blank.
    for row in reversed(db.recent_frames(1000)):   # oldest -> newest so newest wins
        if row.get("msg_type"):
            hub.apply({"msg_type": row["msg_type"], "ts_utc": row["ts_utc"],
                       "fields": row.get("fields") or {}})
    yield


app = FastAPI(title="Flare Telemetry", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=_STATIC), name="static")


# ----------------------------------------------------------------------- auth deps

def require_view(request: Request):
    if not auth.valid_cookie(request.cookies.get(settings.COOKIE_NAME)):
        raise HTTPException(status_code=401, detail="login required")


def require_ingest(request: Request):
    if not auth.check_ingest_token(request.headers.get("authorization")):
        raise HTTPException(status_code=401, detail="invalid ingest token")


# --------------------------------------------------------------------------- ingest

@app.post("/api/ingest")
async def ingest(request: Request, _=Depends(require_ingest)):
    body = await request.json()
    records = body.get("records", []) if isinstance(body, dict) else []
    result = await run_in_threadpool(db.store_batch, records)

    live = [hub.apply(ev) for ev in result["events"]]
    if live:
        await hub.broadcast({"type": "telemetry", "events": live})
    return {"ingested": result["count"]}


# ------------------------------------------------------------------------ viewer API

@app.get("/api/schema")
def api_schema(_=Depends(require_view)):
    return schema.catalog_json()


@app.get("/api/latest")
def api_latest(_=Depends(require_view)):
    return {"channels": hub.latest}


@app.get("/api/sessions")
async def api_sessions(_=Depends(require_view)):
    return {"sessions": await run_in_threadpool(db.list_sessions)}


@app.get("/api/history")
async def api_history(_=Depends(require_view), session: str | None = None,
                      type: str | None = None, since: float | None = None,
                      until: float | None = None, limit: int = 5000):
    limit = max(1, min(limit, 50000))
    rows = await run_in_threadpool(db.history, session, type, since, until, limit)
    return {"rows": rows}


@app.get("/api/track")
async def api_track(_=Depends(require_view), session: str | None = None):
    return {"points": await run_in_threadpool(db.track, session)}


# ----------------------------------------------------------------------------- auth

@app.post("/api/login")
async def login(request: Request):
    body = await request.json()
    if not auth.check_password(str(body.get("password", ""))):
        raise HTTPException(status_code=401, detail="wrong password")
    resp = JSONResponse({"ok": True})
    resp.set_cookie(
        settings.COOKIE_NAME, auth.make_cookie(),
        max_age=settings.SESSION_TTL, httponly=True, samesite="lax",
        secure=request.url.scheme == "https",
    )
    return resp


@app.post("/api/logout")
async def logout():
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(settings.COOKIE_NAME)
    return resp


# ------------------------------------------------------------------------ websocket

@app.websocket("/ws")
async def ws(websocket: WebSocket):
    if not auth.valid_cookie(websocket.cookies.get(settings.COOKIE_NAME)):
        await websocket.close(code=1008)   # policy violation
        return
    await websocket.accept()
    await hub.register(websocket)
    # Prime the client with current values on connect.
    await websocket.send_json({"type": "latest", "channels": hub.latest})
    try:
        while True:
            await websocket.receive_text()   # ignore inbound; keeps the socket open
    except WebSocketDisconnect:
        pass
    finally:
        await hub.unregister(websocket)


# --------------------------------------------------------------------------- pages

@app.get("/")
def index(request: Request):
    if auth.valid_cookie(request.cookies.get(settings.COOKIE_NAME)):
        return FileResponse(os.path.join(_STATIC, "index.html"))
    return FileResponse(os.path.join(_STATIC, "login.html"))


@app.get("/healthz")
def healthz():
    return {"ok": True}
