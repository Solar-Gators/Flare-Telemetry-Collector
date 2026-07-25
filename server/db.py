# db.py — storage layer (SQLAlchemy Core), Postgres in prod / SQLite in dev.
#
# One generic frame table with a JSON `fields` column keeps the server agnostic to
# the CAN map: any message the collector uploads is stored without a schema change.
# Idempotent on `uid` ("{session_uuid}:{frame_id}") so re-uploads dedupe.

import math
import os

from sqlalchemy import (
    JSON, Column, Float, Index, Integer, MetaData, String, Table,
    create_engine, func, insert, select, text, update,
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
    # Session-scoped, per-type time scans (history/replay of one session).
    Index("idx_frames_session_type_ts", "session_uuid", "msg_type", "ts_utc"),
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
    # create_all only adds indexes to brand-new tables; add the session/type/ts
    # composite to an existing (prod) frames table too. Idempotent on both
    # dialects. Not CONCURRENTLY — a one-time brief lock at startup is fine here.
    with engine.begin() as conn:
        conn.execute(text(
            "CREATE INDEX IF NOT EXISTS idx_frames_session_type_ts "
            "ON frames (session_uuid, msg_type, ts_utc)"
        ))


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
                # Carried so derived.py can tell one collector launch from the
                # next: its coulomb counter is only valid within a session.
                "session_uuid": suid,
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


def history(session_uuid: str | list[str] | None, msg_type: str | None,
            since: float | None, until: float | None,
            limit: int = 5000) -> list[dict]:
    q = _apply_filters(select(frames.c.ts_utc, frames.c.fields),
                       session_uuid, msg_type, since, until)
    q = q.order_by(frames.c.ts_utc.asc()).limit(limit)
    with engine.begin() as conn:
        rows = conn.execute(q).mappings().all()
    return [{"ts_utc": r["ts_utc"], "fields": r["fields"]} for r in rows]


def _norm_sessions(s) -> list[str] | None:
    """Accept a single uuid, a list of them, or None -> list | None.

    Sessions are merged into logical "runs" for display (see main.py), so reads
    can span several session_uuids at once.
    """
    if not s:
        return None
    if isinstance(s, str):
        return [s]
    out = [x for x in s if x]
    return out or None


def _apply_filters(q, session_uuid, msg_type, since, until):
    sessions_ = _norm_sessions(session_uuid)
    if sessions_:
        q = (q.where(frames.c.session_uuid == sessions_[0]) if len(sessions_) == 1
             else q.where(frames.c.session_uuid.in_(sessions_)))
    if msg_type:
        q = q.where(frames.c.msg_type == msg_type)
    if since is not None:
        q = q.where(frames.c.ts_utc >= since)
    if until is not None:
        q = q.where(frames.c.ts_utc <= until)
    return q


def _group_expr(group: str):
    """Dialect-portable extraction of a JSON field, for bucket sub-keying."""
    if engine.dialect.name == "postgresql":
        return func.jsonb_extract_path_text(frames.c.fields, group)
    return func.json_extract(frames.c.fields, "$." + group)


def time_bounds(session_uuid: str | list[str] | None, msg_type: str | None,
                since: float | None = None, until: float | None = None):
    """(min_ts, max_ts) for the filtered set, or (None, None) if empty.

    Lets a downsampled query derive its own window when the caller didn't supply
    one — without a window there is no bucket size, and the query would otherwise
    fall back to returning the OLDEST `limit` rows.
    """
    q = _apply_filters(select(func.min(frames.c.ts_utc), func.max(frames.c.ts_utc)),
                       session_uuid, msg_type, since, until)
    with engine.begin() as conn:
        row = conn.execute(q).first()
    return (row[0], row[1]) if row else (None, None)


def history_downsampled(session_uuid: str | list[str] | None, msg_type: str | None,
                        since: float | None, until: float | None,
                        bucket: float, agg: str = "last",
                        group: str | None = None) -> list[dict]:
    """Time-bucketed history for one message type.

    agg="last" (the default) buckets IN SQL: a window function keeps the newest
    row per bucket, so the database returns ~one row per bucket spanning the whole
    window. This is what makes long, high-rate channels correct — the old
    "scan N raw rows then fold in Python" approach silently truncated a window to
    its OLDEST HISTORY_SCAN_CAP rows, so a busy channel's chart stopped partway
    through the session while a slow one (GPS) covered it all.

    agg="avg" still folds in Python (averaging arbitrary JSON fields isn't
    portable in SQL) and therefore remains subject to HISTORY_SCAN_CAP.

    group: a field name to sub-key buckets by (e.g. "mppt_index"), so a single
    msg_type that carries several interleaved sources keeps one representative
    row per (bucket, group) instead of collapsing to whichever arrived last.
    """
    if bucket <= 0:
        return history(session_uuid, msg_type, since, until, settings.HISTORY_SCAN_CAP)

    if agg != "avg":
        # ---- SQL-side bucketing: last row per (bucket[, group]) ----
        partition = [func.floor(frames.c.ts_utc / bucket)]
        if group:
            partition.append(_group_expr(group))
        rn = func.row_number().over(
            partition_by=partition, order_by=frames.c.ts_utc.desc()
        ).label("rn")
        inner = _apply_filters(
            select(frames.c.ts_utc, frames.c.fields, rn), session_uuid, msg_type, since, until
        ).subquery()
        q = (select(inner.c.ts_utc, inner.c.fields)
             .where(inner.c.rn == 1)
             .order_by(inner.c.ts_utc.asc())
             .limit(settings.HISTORY_SCAN_CAP))
        with engine.begin() as conn:
            rows = conn.execute(q).mappings().all()
        return [{"ts_utc": r["ts_utc"], "fields": r["fields"]} for r in rows]

    # ---- agg="avg": fold in Python (capped scan) ----
    q2 = _apply_filters(select(frames.c.ts_utc, frames.c.fields),
                        session_uuid, msg_type, since, until)
    q2 = q2.order_by(frames.c.ts_utc.asc()).limit(settings.HISTORY_SCAN_CAP)

    with engine.begin() as conn:
        rows = conn.execute(q2).mappings().all()

    buckets: dict = {}                     # bucket key -> accumulator
    for r in rows:
        fields = r["fields"] or {}
        bkey = math.floor(r["ts_utc"] / bucket)
        key = (bkey, fields.get(group)) if group else bkey
        acc = buckets.get(key)
        if acc is None:
            acc = {"ts_utc": r["ts_utc"], "last": fields, "sums": {}, "counts": {}}
            buckets[key] = acc
        acc["ts_utc"] = r["ts_utc"]
        acc["last"] = fields
        for k, v in fields.items():
            if isinstance(v, (int, float)) and not isinstance(v, bool):
                acc["sums"][k] = acc["sums"].get(k, 0.0) + v
                acc["counts"][k] = acc["counts"].get(k, 0) + 1

    out = []
    for key in sorted(buckets, key=lambda k: (k[0], str(k[1])) if group else k):
        acc = buckets[key]
        merged = dict(acc["last"])
        for k, total in acc["sums"].items():
            merged[k] = total / acc["counts"][k]
        out.append({"ts_utc": acc["ts_utc"], "fields": merged})
    return out


def state_at(session_uuid: str | list[str] | None, t: float, lookback: float | None = None,
             split_field: str | None = None) -> list[dict]:
    """Dashboard state at an instant: the newest row per message type at-or-before
    `t`, within a bounded lookback window.

    Bucketing is done in SQL (same window-function pattern as
    history_downsampled) so this returns a few dozen rows — one per channel —
    rather than dragging the whole lookback window into Python. A 120 s window on
    a busy car can hold tens of thousands of frames, so that distinction matters
    when the user is dragging a scrubber.

    split_field sub-keys the partition by a JSON field (e.g. "mppt_index") so a
    msg_type carrying several interleaved sources yields one row per source. It's
    passed in by the caller — db.py stays agnostic to the CAN map.

    A channel whose last frame predates the window simply doesn't appear, and the
    dashboard renders it as N/A at `t`.
    """
    if lookback is None:
        lookback = settings.REPLAY_LOOKBACK_S

    partition = [frames.c.msg_type]
    if split_field:
        partition.append(_group_expr(split_field))
    rn = func.row_number().over(
        partition_by=partition, order_by=frames.c.ts_utc.desc()
    ).label("rn")

    inner = _apply_filters(
        select(frames.c.msg_type, frames.c.ts_utc, frames.c.fields, rn),
        session_uuid, None, t - lookback, t,
    ).where(frames.c.msg_type.isnot(None)).subquery()

    q = select(inner.c.msg_type, inner.c.ts_utc, inner.c.fields).where(inner.c.rn == 1)
    with engine.begin() as conn:
        rows = conn.execute(q).mappings().all()
    return [{"msg_type": r["msg_type"], "ts_utc": r["ts_utc"], "fields": r["fields"]} for r in rows]


# ------------------------------------------------------------------------- export

def count_frames(session_uuid: str | list[str] | None,
                 since: float | None = None, until: float | None = None) -> int:
    q = _apply_filters(select(func.count()).select_from(frames),
                       session_uuid, None, since, until)
    with engine.begin() as conn:
        return int(conn.execute(q).scalar() or 0)


def distinct_msg_types(session_uuid: str | list[str] | None,
                       since: float | None = None,
                       until: float | None = None) -> list[str]:
    """Which message types the export scope actually contains.

    Lets the CSV carry a column per field of those types only, instead of a
    column for every message in the catalog (most of which the car never sends).
    """
    q = _apply_filters(select(frames.c.msg_type).distinct(),
                       session_uuid, None, since, until)
    q = q.where(frames.c.msg_type.isnot(None))
    with engine.begin() as conn:
        return sorted(r[0] for r in conn.execute(q))


def sample_field_names(session_uuid: str | list[str] | None,
                       since: float | None = None, until: float | None = None,
                       per_type: int = 200) -> dict[str, list[str]]:
    """{msg_type: field names it actually carries}, sampled from recent rows.

    The CSV's columns come from the DATA, not from the catalog: the collector
    uploads fields the catalog doesn't name (MpptPacket.mppt_index says which
    controller a row came from — the catalog encodes that in the CAN id, so it
    has no field for it). Deriving columns from the catalog alone would bury it
    in the catch-all column and make MPPT rows unseparable in a spreadsheet.

    One indexed LIMIT query per type (idx_frames_type_ts), so this stays cheap
    even scoped to the whole table. Any field missed by the sample still lands in
    the catch-all, so nothing is ever dropped.
    """
    out: dict[str, list[str]] = {}
    with engine.begin() as conn:
        for t in distinct_msg_types(session_uuid, since, until):
            q = _apply_filters(select(frames.c.fields), session_uuid, t, since, until)
            q = q.order_by(frames.c.ts_utc.desc()).limit(per_type)
            names: list[str] = []
            seen: set[str] = set()
            for (f,) in conn.execute(q):
                for k in (f or {}):
                    if k not in seen:
                        seen.add(k)
                        names.append(k)
            out[t] = names
    return out


def iter_frames(session_uuid: str | list[str] | None,
                since: float | None = None, until: float | None = None,
                chunk: int = 5000):
    """Stream every frame in scope, for CSV export.

    Uses a server-side cursor (stream_results) so exporting the whole table never
    materialises a million rows in memory — the response is generated as the
    rows arrive.

    Ordering: by ts_utc when scoped to a session/run (a bounded sort), but by
    (session_uuid, ts_utc) when unscoped, which rides idx_frames_session_ts and
    avoids a disk sort of the entire table. An unscoped dump is therefore
    grouped by session and time-ordered within each.
    """
    q = _apply_filters(
        select(frames.c.ts_utc, frames.c.session_uuid, frames.c.msg_type,
               frames.c.can_id, frames.c.fields),
        session_uuid, None, since, until)
    q = (q.order_by(frames.c.ts_utc.asc()) if _norm_sessions(session_uuid)
         else q.order_by(frames.c.session_uuid.asc(), frames.c.ts_utc.asc()))

    with engine.connect().execution_options(
            stream_results=True, yield_per=chunk) as conn:
        for r in conn.execute(q).mappings():
            yield r


def is_valid_gps(fields: dict | None) -> bool:
    """True if a GPS fix looks real (enough satellites, sane, not null-island).

    No-fix readings report 0 satellites and garbage coordinates (0,0 or wild
    values), which otherwise draw wild lines across the map.
    """
    if not fields:
        return False
    lat, lon = fields.get("latitude"), fields.get("longitude")
    sats = fields.get("num_satellites")
    if lat is None or lon is None:
        return False
    if sats is None or sats < settings.GPS_MIN_SATS:
        return False
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return False
    if abs(lat) < 1.0 and abs(lon) < 3.0:      # null-island / still acquiring
        return False
    return True


def track(session_uuid: str | list[str] | None, limit: int = 20000) -> list[dict]:
    """Valid GPS points for the map polyline: [{ts_utc, lat, lon, speed}]."""
    rows = history(session_uuid, settings.GPS_MSG_TYPE, None, None, limit)
    points = []
    for r in rows:
        f = r["fields"] or {}
        if not is_valid_gps(f):
            continue
        points.append({
            "ts_utc": r["ts_utc"],
            "lat": f["latitude"],
            "lon": f["longitude"],
            "speed": f.get("speed"),
        })
    return points
