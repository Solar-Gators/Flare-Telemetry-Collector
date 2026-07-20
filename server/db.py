# db.py — storage layer (SQLAlchemy Core), Postgres in prod / SQLite in dev.
#
# One generic frame table with a JSON `fields` column keeps the server agnostic to
# the CAN map: any message the collector uploads is stored without a schema change.
# Idempotent on `uid` ("{session_uuid}:{frame_id}") so re-uploads dedupe.

import os

from sqlalchemy import (
    JSON, Column, Float, Index, Integer, MetaData, String, Table,
    create_engine, insert, select, update,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert

import settings

# JSONB on Postgres (indexable/typed), plain JSON text on SQLite.
_JSON = JSON().with_variant(JSONB, "postgresql")

engine = create_engine(settings.DATABASE_URL, future=True, pool_pre_ping=True)
metadata = MetaData()

sessions = Table(
    "sessions", metadata,
    Column("session_uuid", String, primary_key=True),
    Column("first_seen", Float, nullable=False),
    Column("last_seen", Float, nullable=False),
    Column("note", String),
)

frames = Table(
    "frames", metadata,
    Column("uid", String, primary_key=True),          # "{session_uuid}:{frame_id}"
    Column("session_uuid", String, nullable=False),
    Column("ts_utc", Float, nullable=False),
    Column("can_id", Integer),
    Column("msg_type", String),                        # catalog message name, or NULL
    Column("fields", _JSON),                           # {catalog_field: value}, or NULL
    Column("payload", String),                         # raw hex (lossless)
    Index("idx_frames_type_ts", "msg_type", "ts_utc"),
    Index("idx_frames_session_ts", "session_uuid", "ts_utc"),
)


def init_db() -> None:
    """Create tables. For SQLite, make sure the parent directory exists."""
    url = settings.DATABASE_URL
    if url.startswith("sqlite:///") and not url.startswith("sqlite:////"):
        path = url[len("sqlite:///"):]
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
    metadata.create_all(engine)


def _insert_ignore(rows: list[dict]):
    """Multi-row insert that skips rows whose uid already exists."""
    name = engine.dialect.name
    if name == "postgresql":
        return pg_insert(frames).values(rows).on_conflict_do_nothing(index_elements=["uid"])
    if name == "sqlite":
        return sqlite_insert(frames).values(rows).on_conflict_do_nothing(index_elements=["uid"])
    return insert(frames).values(rows)


def store_batch(records: list[dict]) -> dict:
    """Persist a batch of uploaded records. Returns decoded events + row count.

    Runs in a worker thread (called via run_in_threadpool). Malformed records
    (missing id/ts) are skipped rather than failing the whole batch.
    """
    frame_rows: list[dict] = []
    bounds: dict[str, tuple[float, float]] = {}   # session_uuid -> (min_ts, max_ts)
    events: list[dict] = []

    for r in records:
        uid = r.get("id")
        ts = r.get("ts_utc")
        if not uid or ts is None:
            continue
        suid = r.get("session_uuid") or uid.split(":", 1)[0]
        frame_rows.append({
            "uid": uid,
            "session_uuid": suid,
            "ts_utc": ts,
            "can_id": r.get("can_id"),
            "msg_type": r.get("msg_type"),
            "fields": r.get("fields"),
            "payload": r.get("payload"),
        })
        lo, hi = bounds.get(suid, (ts, ts))
        bounds[suid] = (min(lo, ts), max(hi, ts))
        if r.get("msg_type"):
            events.append({
                "msg_type": r["msg_type"],
                "ts_utc": ts,
                "fields": r.get("fields") or {},
            })

    if not frame_rows:
        return {"events": [], "count": 0}

    with engine.begin() as conn:
        for suid, (lo, hi) in bounds.items():
            _upsert_session(conn, suid, lo, hi)
        # Chunk to stay well under SQLite's bound-parameter limit.
        for i in range(0, len(frame_rows), 100):
            conn.execute(_insert_ignore(frame_rows[i:i + 100]))

    return {"events": events, "count": len(frame_rows)}


def _upsert_session(conn, suid: str, lo: float, hi: float) -> None:
    row = conn.execute(
        select(sessions.c.first_seen, sessions.c.last_seen)
        .where(sessions.c.session_uuid == suid)
    ).first()
    if row is None:
        conn.execute(insert(sessions).values(
            session_uuid=suid, first_seen=lo, last_seen=hi))
    else:
        conn.execute(update(sessions)
                     .where(sessions.c.session_uuid == suid)
                     .values(first_seen=min(row.first_seen, lo),
                             last_seen=max(row.last_seen, hi)))


# --------------------------------------------------------------------------- reads

def list_sessions() -> list[dict]:
    with engine.begin() as conn:
        rows = conn.execute(
            select(sessions).order_by(sessions.c.last_seen.desc())
        ).mappings().all()
    return [dict(r) for r in rows]


def recent_frames(limit: int = 1000) -> list[dict]:
    """Most recent decoded frames overall — used to seed the live 'latest' map."""
    with engine.begin() as conn:
        rows = conn.execute(
            select(frames.c.msg_type, frames.c.ts_utc, frames.c.fields)
            .where(frames.c.msg_type.isnot(None))
            .order_by(frames.c.ts_utc.desc())
            .limit(limit)
        ).mappings().all()
    return [dict(r) for r in rows]


def history(session_uuid: str | None, msg_type: str | None,
            since: float | None, until: float | None,
            limit: int = 5000) -> list[dict]:
    q = select(frames.c.ts_utc, frames.c.fields)
    if session_uuid:
        q = q.where(frames.c.session_uuid == session_uuid)
    if msg_type:
        q = q.where(frames.c.msg_type == msg_type)
    if since is not None:
        q = q.where(frames.c.ts_utc >= since)
    if until is not None:
        q = q.where(frames.c.ts_utc <= until)
    q = q.order_by(frames.c.ts_utc.asc()).limit(limit)
    with engine.begin() as conn:
        rows = conn.execute(q).mappings().all()
    return [{"ts_utc": r["ts_utc"], "fields": r["fields"]} for r in rows]


def track(session_uuid: str | None, limit: int = 20000) -> list[dict]:
    """GPS points for the map polyline: [{ts_utc, lat, lon, speed}]."""
    rows = history(session_uuid, settings.GPS_MSG_TYPE, None, None, limit)
    points = []
    for r in rows:
        f = r["fields"] or {}
        lat, lon = f.get("latitude"), f.get("longitude")
        if lat is None or lon is None:
            continue
        points.append({
            "ts_utc": r["ts_utc"],
            "lat": lat,
            "lon": lon,
            "speed": f.get("speed"),
        })
    return points
