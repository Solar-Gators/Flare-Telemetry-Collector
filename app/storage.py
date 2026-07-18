# storage.py
#
# Offline-first local telemetry store (SQLite).
#
# Every decoded frame is written to a local, crash-safe SQLite database so the
# collector works fully standalone with no internet. A separate uploader
# (see uploader.py) later drains un-synced rows to an online database.
#
# Writes are funnelled through a queue to a single dedicated writer thread, so
# the serial thread never blocks on disk and only one connection ever writes on
# the store's behalf. WAL mode lets the uploader read/mark rows concurrently.

import logging
import os
import queue
import sqlite3
import threading
import time
import uuid

from app import parsed_tables

logger = logging.getLogger(__name__)

DEFAULT_DB_PATH = os.path.join("data", "telemetry.db")

# Writer batching: commit at least this often, or once this many rows are queued.
_COMMIT_INTERVAL = 0.5   # seconds
_COMMIT_ROWS     = 200

# Base tables. One parsed child table per decoded message type is appended from
# parsed_tables (auto-derived from the parser dataclasses).
_BASE_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    session_uuid TEXT    NOT NULL,
    started_utc  REAL    NOT NULL,
    note         TEXT
);

CREATE TABLE IF NOT EXISTS frames (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id INTEGER NOT NULL,
    ts_utc     REAL    NOT NULL,
    ts_mono    REAL    NOT NULL,
    can_id     INTEGER NOT NULL,
    payload    BLOB    NOT NULL,
    synced     INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_frames_synced ON frames(synced);
"""

_SCHEMA = _BASE_SCHEMA + "\n" + parsed_tables.schema_sql()


def _configure_connection(conn: sqlite3.Connection):
    """Apply the pragmas that make concurrent writer+uploader access safe."""
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=5000")


def storage_enabled() -> bool:
    """Storage is on unless FLARE_STORAGE_ENABLED is a falsey value."""
    return os.environ.get("FLARE_STORAGE_ENABLED", "1").lower() not in (
        "0", "false", "no", "off",
    )


class TelemetryStore:
    """Durable local store for decoded telemetry frames.

    Usage:
        store = TelemetryStore()
        store.start()
        ...
        store.record(can_id, payload, ts_utc, ts_mono, parsed)   # serial thread
        ...
        store.stop()

    All methods are safe to call even when the store failed to open — they
    become no-ops so the app keeps running without persistence.
    """

    def __init__(self, db_path: str | None = None):
        self.db_path = db_path or os.environ.get("FLARE_DB_PATH") or DEFAULT_DB_PATH
        self.session_uuid = uuid.uuid4().hex
        self.session_id: int | None = None

        self._queue: "queue.Queue" = queue.Queue()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._ok = False           # True once the DB opened and the writer runs

    # ------------------------------------------------------------------ lifecycle

    def start(self):
        """Create the schema, register a session row, and launch the writer.

        Setup uses a short-lived connection on this thread; the writer thread
        opens and solely owns its own connection (SQLite connections cannot be
        shared across threads).
        """
        try:
            parent = os.path.dirname(self.db_path)
            if parent:
                os.makedirs(parent, exist_ok=True)
            conn = sqlite3.connect(self.db_path)
            try:
                _configure_connection(conn)
                conn.executescript(_SCHEMA)
                cur = conn.execute(
                    "INSERT INTO sessions (session_uuid, started_utc) VALUES (?, ?)",
                    (self.session_uuid, time.time()),
                )
                self.session_id = cur.lastrowid
                conn.commit()
            finally:
                conn.close()
        except sqlite3.Error as err:
            logger.error("storage disabled: could not open %s: %s", self.db_path, err)
            self._ok = False
            return

        self._ok = True
        self._thread = threading.Thread(
            target=self._writer_loop, name="telemetry-store", daemon=True
        )
        self._thread.start()
        logger.info("storage: writing to %s (session %s)",
                    self.db_path, self.session_uuid)

    def stop(self, timeout: float = 5.0):
        """Flush queued rows and close the DB. Safe to call more than once."""
        if not self._ok:
            return
        self._stop.set()
        self._queue.put(None)          # wake the writer if idle
        if self._thread is not None:
            self._thread.join(timeout)
        self._ok = False

    @property
    def enabled(self) -> bool:
        return self._ok

    # ------------------------------------------------------------------ producer

    def record(self, can_id: int, payload: bytes, ts_utc: float,
               ts_mono: float, parsed=None):
        """Queue one decoded frame for persistence (called on the serial thread).

        Never blocks on disk and never raises into the caller.
        """
        if not self._ok:
            return
        self._queue.put((can_id, bytes(payload), ts_utc, ts_mono, parsed))

    # ------------------------------------------------------------------ writer

    def _writer_loop(self):
        try:
            conn = sqlite3.connect(self.db_path)
            _configure_connection(conn)
        except sqlite3.Error as err:
            logger.error("storage: writer could not open %s: %s", self.db_path, err)
            self._ok = False
            return

        pending = 0
        last_commit = time.monotonic()

        while True:
            timeout = max(0.0, _COMMIT_INTERVAL - (time.monotonic() - last_commit))
            try:
                item = self._queue.get(timeout=timeout)
            except queue.Empty:
                item = None

            if item is None:
                # Either a wake/stop sentinel or the commit-interval elapsed.
                if pending:
                    conn.commit()
                    pending = 0
                    last_commit = time.monotonic()
                if self._stop.is_set() and self._queue.empty():
                    break
                continue

            try:
                self._insert(conn, item)
                pending += 1
            except sqlite3.Error as err:
                logger.error("storage: insert failed: %s", err)

            if pending >= _COMMIT_ROWS:
                conn.commit()
                pending = 0
                last_commit = time.monotonic()

        try:
            if pending:
                conn.commit()
            conn.close()
        except sqlite3.Error:
            pass
        logger.info("storage: writer stopped")

    def _insert(self, conn: sqlite3.Connection, item):
        can_id, payload, ts_utc, ts_mono, parsed = item
        cur = conn.execute(
            "INSERT INTO frames (session_id, ts_utc, ts_mono, can_id, payload) "
            "VALUES (?, ?, ?, ?, ?)",
            (self.session_id, ts_utc, ts_mono, can_id, payload),
        )
        frame_id = cur.lastrowid

        # Also write the decoded fields into this message type's subtable.
        spec = parsed_tables.spec_for(parsed)
        if spec is not None:
            conn.execute(spec.insert_sql,
                         parsed_tables.row_values(spec, frame_id, ts_utc, parsed))

    # ------------------------------------------------------------------ retention

    def prune(self, older_than_days: float, only_synced: bool = True):
        """Delete old frames (and their parsed rows). Not run automatically.

        Opens its own short-lived connection so it can be called from any thread.
        """
        cutoff = time.time() - older_than_days * 86400.0
        cond = "ts_utc < ?" + (" AND synced = 1" if only_synced else "")
        conn = sqlite3.connect(self.db_path)
        try:
            _configure_connection(conn)
            for table in parsed_tables.TABLE_NAMES:
                conn.execute(
                    f'DELETE FROM "{table}" WHERE frame_id IN '
                    f"(SELECT id FROM frames WHERE {cond})", (cutoff,),
                )
            conn.execute(f"DELETE FROM frames WHERE {cond}", (cutoff,))
            conn.commit()
        finally:
            conn.close()
