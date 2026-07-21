#!/usr/bin/env python3
"""Fast, parallel backfill of the local telemetry store to the REST server.

No GUI/radio needed. Reads unsynced frames newest-first, decodes + catalog-names
them exactly like the app's uploader, and POSTs several batches CONCURRENTLY so a
big backlog isn't bottlenecked on one round-trip at a time. Only frames whose
batch is confirmed by the server are marked synced, so it is safe to Ctrl+C and
re-run — it resumes, and a dropped batch is retried, never lost.

Usage (from collector/):
    FLARE_UPLOAD_URL=http://<server-ip>/api/ingest \
    FLARE_UPLOAD_TOKEN=<token> \
        ../.venv/bin/python tools/upload_backlog.py

Tunables (env):
    FLARE_UPLOAD_WORKERS   concurrent POSTs           (default 4)
    FLARE_UPLOAD_BATCH     frames per POST            (default 500)
    FLARE_UPLOAD_TIMEOUT   per-request timeout, secs  (default 30 here)
"""
import concurrent.futures as cf
import logging
import os
import sqlite3
import sys
import time

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.uploader import UploaderThread, make_backend

logging.basicConfig(level=logging.ERROR, format="%(levelname)s %(name)s: %(message)s")

DB = os.environ.get("FLARE_DB_PATH", os.path.join("data", "telemetry.db"))
WORKERS = int(os.environ.get("FLARE_UPLOAD_WORKERS", "4"))
BATCH = int(os.environ.get("FLARE_UPLOAD_BATCH", "500"))
# Backfill tolerates a slow server better than it tolerates thrashing retries.
os.environ.setdefault("FLARE_UPLOAD_TIMEOUT", "30")

_SELECT = (
    "SELECT f.id, s.session_uuid, f.ts_utc, f.ts_mono, f.can_id, f.payload "
    "FROM frames f JOIN sessions s ON s.id = f.session_id "
    "WHERE f.synced = 0 ORDER BY f.id DESC LIMIT ?"
)


def main() -> int:
    os.environ.setdefault("FLARE_UPLOAD_BACKEND", "rest")
    backend = make_backend()
    if backend is None:
        print("No upload backend. Set FLARE_UPLOAD_BACKEND=rest, FLARE_UPLOAD_URL, "
              "FLARE_UPLOAD_TOKEN.", file=sys.stderr)
        return 2
    if not os.path.exists(DB):
        print(f"database not found: {DB}", file=sys.stderr)
        return 2

    conn = sqlite3.connect(DB)
    # Match the collector's pragmas and wait (don't error) if it briefly holds the
    # single SQLite write lock. Still: prefer NOT running this while capturing live.
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=30000")
    total_left = conn.execute("SELECT COUNT(*) FROM frames WHERE synced=0").fetchone()[0]
    if not total_left:
        print("Nothing to upload — no unsynced frames.")
        return 0

    print(f"Backfilling {total_left:,} frames  ({WORKERS} workers x {BATCH}/batch)")
    print(f"  from {DB}")
    print(f"  to   {os.environ.get('FLARE_UPLOAD_URL')}")

    def send(sub):
        # sub: list of row tuples -> POST; returns the ids on success, raises on failure.
        recs = [UploaderThread._to_record(r) for r in sub]
        backend.upload(recs)
        return [r[0] for r in sub]

    done = 0
    fails = 0
    start = time.monotonic()
    pool = cf.ThreadPoolExecutor(max_workers=WORKERS)
    try:
        while True:
            rows = conn.execute(_SELECT, (BATCH * WORKERS,)).fetchall()
            if not rows:
                break
            subs = [rows[i:i + BATCH] for i in range(0, len(rows), BATCH)]
            ok_ids, round_fail = [], 0
            for fut in cf.as_completed({pool.submit(send, s) for s in subs}):
                try:
                    ok_ids.extend(fut.result())
                except Exception:
                    round_fail += 1
            if ok_ids:
                marks = ",".join("?" * len(ok_ids))
                conn.execute(f"UPDATE frames SET synced=1 WHERE id IN ({marks})", ok_ids)
                conn.commit()
                done += len(ok_ids)
            rate = done / max(time.monotonic() - start, 1e-6)
            eta = (total_left - done) / rate if rate else 0
            print(f"\r  {done:,}/{total_left:,}  ({100*done/total_left:5.1f}%)  "
                  f"{rate:6.0f}/s  eta {eta/60:4.1f} min  fails {fails}      ",
                  end="", flush=True)
            if round_fail:
                fails += round_fail
                time.sleep(min(2 ** min(round_fail, 5), 30))   # server pushback
            elif not ok_ids:
                time.sleep(2)
    except KeyboardInterrupt:
        print("\ninterrupted — progress saved; re-run to resume.")
        return 1
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
        conn.close()

    print("\nDone — all frames uploaded.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
