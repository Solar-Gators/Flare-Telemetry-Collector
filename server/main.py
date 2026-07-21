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

# The one message type that carries several interleaved sources on a single id;
# queries that must keep them apart (replay, downsampled history) sub-key by it.
MPPT_SPLIT_FIELD = "mppt_index"


def channel_for(msg_type: str, fields: dict | None) -> str:
    """Live-view key. MPPT packets split by controller index; everything else
    is keyed by its catalog message name."""
    if msg_type == "MpptPacket" and fields and fields.get(MPPT_SPLIT_FIELD) is not None:
        return f"MpptPacket:{fields[MPPT_SPLIT_FIELD]}"
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
        mt = row.get("msg_type")
        if not mt:
            continue
        if mt == settings.GPS_MSG_TYPE and not db.is_valid_gps(row.get("fields")):
            continue
        hub.apply({"msg_type": mt, "ts_utc": row["ts_utc"],
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

    # Raw frames are all stored (lossless); only valid fixes drive the live map.
    live = []
    for ev in result["events"]:
        if ev["msg_type"] == settings.GPS_MSG_TYPE and not db.is_valid_gps(ev.get("fields")):
            continue
        live.append(hub.apply(ev))
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


def group_runs(sessions: list[dict], gap_s: float) -> list[dict]:
    """Group consecutive sessions into logical "runs".

    Every collector launch mints a new session_uuid, so an afternoon of testing
    shows up as many short sessions. Sessions whose start follows the previous
    one's end by less than `gap_s` are presented as a single run. This is a
    read-time view only — nothing in the database is merged.

    `gaps` lists the [start, end] windows between member sessions, i.e. the
    stretches where no data was recorded (the frontend draws them dashed).
    """
    ordered = sorted(sessions, key=lambda s: s["first_seen"])
    runs: list[dict] = []
    for s in ordered:
        if runs and s["first_seen"] - runs[-1]["last_seen"] < gap_s:
            run = runs[-1]
            run["gaps"].append([run["last_seen"], s["first_seen"]])
            run["sessions"].append(s["session_uuid"])
            run["last_seen"] = max(run["last_seen"], s["last_seen"])
        else:
            runs.append({
                "id": s["session_uuid"],          # stable key = first member
                "first_seen": s["first_seen"],
                "last_seen": s["last_seen"],
                "sessions": [s["session_uuid"]],
                "gaps": [],
            })
    for r in runs:
        r["session_count"] = len(r["sessions"])
    runs.reverse()                                 # newest first, like sessions
    return runs


@app.get("/api/sessions")
async def api_sessions(_=Depends(require_view)):
    sessions = await run_in_threadpool(db.list_sessions)
    return {"sessions": sessions,
            "runs": group_runs(sessions, settings.RUN_GAP_S)}


def session_list(session: str | None, sessions: str | None) -> list[str] | None:
    """Resolve `session` (one uuid) or `sessions` (comma-separated, one run's
    members) into the list the db layer filters on."""
    if sessions:
        return [s for s in (x.strip() for x in sessions.split(",")) if s] or None
    return [session] if session else None


@app.get("/api/history")
async def api_history(_=Depends(require_view), session: str | None = None,
                      sessions: str | None = None,
                      type: str | None = None, since: float | None = None,
                      until: float | None = None, limit: int = 5000,
                      bucket: float | None = None, max_points: int | None = None,
                      agg: str = "last", group: str | None = None):
    sess = session_list(session, sessions)
    """Raw or time-bucketed history for one message type.

    Downsampling: an explicit `bucket` (seconds) wins; otherwise `max_points`
    over a known [since, until] window derives one. Without either, returns raw
    rows (oldest-first, capped by `limit`).
    """
    limit = max(1, min(limit, 50000))
    b = None
    if bucket is not None and bucket > 0:
        b = bucket
    elif max_points:
        # Derive the bucket from the window. If the caller didn't give one, ask
        # the data for its own bounds rather than falling through to the raw
        # path — that path returns the OLDEST `limit` rows, which silently
        # truncates a high-rate channel to a few minutes while a slow one (GPS)
        # appears to cover the whole day.
        if since is None or until is None:
            lo, hi = await run_in_threadpool(db.time_bounds, sess, type, since, until)
            since = since if since is not None else lo
            until = until if until is not None else hi
        if since is not None and until is not None and until > since:
            b = (until - since) / max(1, max_points)
    if b:
        rows = await run_in_threadpool(db.history_downsampled, sess, type, since, until, b, agg, group)
        return {"rows": rows, "downsampled": True, "bucket": b}
    rows = await run_in_threadpool(db.history, sess, type, since, until, limit)
    return {"rows": rows, "downsampled": False, "bucket": None}


@app.get("/api/track")
async def api_track(_=Depends(require_view), session: str | None = None,
                    sessions: str | None = None):
    return {"points": await run_in_threadpool(
        db.track, session_list(session, sessions))}


@app.get("/api/replay")
async def api_replay(t: float, _=Depends(require_view), session: str | None = None,
                     sessions: str | None = None, lookback: float | None = None):
    """Dashboard state at instant `t` — same shape as /api/latest, so the frontend
    renders a past moment with the very same card renderer.

    MPPT is split per controller via channel_for(), and invalid GPS fixes are
    dropped so the replay marker never jumps to null-island.
    """
    rows = await run_in_threadpool(
        db.state_at, session_list(session, sessions), t, lookback, MPPT_SPLIT_FIELD)
    channels: dict[str, dict] = {}
    for r in rows:
        mt, fields = r["msg_type"], r.get("fields") or {}
        if mt == settings.GPS_MSG_TYPE and not db.is_valid_gps(fields):
            continue
        ch = channel_for(mt, fields)
        channels[ch] = {"channel": ch, "msg_type": mt,
                        "ts_utc": r["ts_utc"], "fields": fields}
    return {"t": t, "channels": channels}


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

#  "/" serves DIFFERENT content for the same URL depending on the session cookie,
#  so it must never be cached. Without this the browser reuses the cached login
#  page after signing in and the user appears stuck on the login screen until
#  they force a refresh.
_NO_STORE = {
    "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
    "Pragma": "no-cache",
    "Vary": "Cookie",
}


@app.get("/")
def index(request: Request):
    page = "index.html" if auth.valid_cookie(request.cookies.get(settings.COOKIE_NAME)) else "login.html"
    return FileResponse(os.path.join(_STATIC, page), headers=_NO_STORE)


@app.get("/healthz")
def healthz():
    return {"ok": True}
