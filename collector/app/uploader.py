# uploader.py
#
# Store-and-forward uploader: drains un-synced rows from the local SQLite store
# (see storage.py) to an online database whenever a connection is available.
#
# The online database is not chosen yet, so this defines a small pluggable
# `TelemetryBackend` interface plus a file-based stub (`JsonlFileBackend`) that
# exercises the whole drain path end-to-end. Wiring a real DB later means adding
# one backend class and a branch in `make_backend()` — nothing else changes.

import abc
import json
import logging
import os
import socket
import sqlite3
import threading
import time
import urllib.error
import urllib.request
from urllib.parse import urlparse

from app import parsed_tables, upload_map
from app.payload_parsers import parse_payload
from app.storage import _configure_connection

logger = logging.getLogger(__name__)

_BATCH_SIZE      = 200
_POLL_INTERVAL   = 2.0     # seconds between drain attempts when caught up
_BACKOFF_MAX     = 60.0    # cap for exponential backoff after failures


# ---------------------------------------------------------------------------
# Backend interface + stubs
# ---------------------------------------------------------------------------

class TelemetryBackend(abc.ABC):
    """A destination the local store can offload batches of records to."""

    @abc.abstractmethod
    def upload(self, batch: list[dict]) -> None:
        """Send a batch of records. Must raise on any failure so the uploader
        leaves the rows un-synced and retries them later."""

    def is_reachable(self) -> bool:
        """Cheap pre-check; return False to skip an upload attempt entirely."""
        return True

    def close(self) -> None:
        pass


class JsonlFileBackend(TelemetryBackend):
    """Testing/offline-export stub: append each record as a line of JSON.

    Proves the store -> drain -> mark-synced loop without a real database.
    """

    def __init__(self, path: str):
        self.path = path
        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)

    def upload(self, batch: list[dict]) -> None:
        with open(self.path, "a") as f:
            for rec in batch:
                f.write(json.dumps(rec) + "\n")
            f.flush()
            os.fsync(f.fileno())


class RestApiBackend(TelemetryBackend):
    """Store-and-forward to the telemetry website's ingest endpoint over HTTPS.

    POSTs each batch as JSON to `<url>` with an optional bearer token. Uses only
    the standard library (urllib) so the collector gains no new dependency. Any
    non-2xx response or network error raises, leaving the rows un-synced for the
    uploader loop to retry with backoff.
    """

    def __init__(self, url: str, token: str | None = None, timeout: float = 10.0):
        self.url = url
        self.token = token
        self.timeout = timeout
        parsed = urlparse(url)
        self._host = parsed.hostname
        self._port = parsed.port or (443 if parsed.scheme == "https" else 80)

    def is_reachable(self) -> bool:
        if not self._host:
            return False
        return tcp_reachable(self._host, self._port, timeout=3.0)

    def upload(self, batch: list[dict]) -> None:
        body = json.dumps({"records": batch}).encode("utf-8")
        req = urllib.request.Request(self.url, data=body, method="POST")
        req.add_header("Content-Type", "application/json")
        if self.token:
            req.add_header("Authorization", f"Bearer {self.token}")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                # urlopen already raises on 4xx/5xx; guard anyway.
                if resp.status // 100 != 2:
                    raise RuntimeError(f"ingest returned HTTP {resp.status}")
        except urllib.error.HTTPError as err:
            raise RuntimeError(f"ingest HTTP {err.code}: {err.reason}") from err
        except urllib.error.URLError as err:
            raise RuntimeError(f"ingest unreachable: {err.reason}") from err


def tcp_reachable(host: str, port: int, timeout: float = 3.0) -> bool:
    """Best-effort connectivity probe for real network backends."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def make_backend() -> TelemetryBackend | None:
    """Select a backend from the environment.

    Returns None (uploader stays idle, rows accumulate locally) unless
    FLARE_UPLOAD_BACKEND names a configured backend. Add real backends
    (influx/rest/postgres) here as new branches.
    """
    name = os.environ.get("FLARE_UPLOAD_BACKEND", "").strip().lower()
    if name in ("", "null", "none", "off"):
        return None
    if name == "jsonl":
        path = os.environ.get("FLARE_UPLOAD_JSONL") or os.path.join("data", "uploaded.jsonl")
        logger.info("uploader: JSONL backend -> %s", path)
        return JsonlFileBackend(path)
    if name == "rest":
        url = os.environ.get("FLARE_UPLOAD_URL", "").strip()
        if not url:
            logger.error("uploader: FLARE_UPLOAD_BACKEND=rest but FLARE_UPLOAD_URL "
                         "is unset; uploads disabled")
            return None
        token = os.environ.get("FLARE_UPLOAD_TOKEN") or None
        logger.info("uploader: REST backend -> %s", url)
        return RestApiBackend(url, token)
    logger.warning("uploader: unknown FLARE_UPLOAD_BACKEND=%r, uploads disabled", name)
    return None


# ---------------------------------------------------------------------------
# Uploader thread
# ---------------------------------------------------------------------------

class UploaderThread:
    """Background store-and-forward sync loop.

    Owns its own SQLite connection (opened on its thread). Selects un-synced
    frames, hands them to the backend, and only marks them synced once the
    backend confirms — so a crash or network drop mid-upload never loses data.
    """

    def __init__(self, store, backend: TelemetryBackend,
                 batch_size: int = _BATCH_SIZE, poll_interval: float = _POLL_INTERVAL):
        self._store = store
        self._backend = backend
        self._batch_size = batch_size
        self._poll_interval = poll_interval
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self):
        self._thread = threading.Thread(
            target=self._run, name="telemetry-uploader", daemon=True
        )
        self._thread.start()

    def stop(self, timeout: float = 5.0):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)
        self._backend.close()

    # ------------------------------------------------------------------ loop

    def _run(self):
        try:
            conn = sqlite3.connect(self._store.db_path)
            _configure_connection(conn)
        except sqlite3.Error as err:
            logger.error("uploader disabled: could not open %s: %s",
                         self._store.db_path, err)
            return

        logger.info("uploader: started (%s)", type(self._backend).__name__)
        backoff = self._poll_interval
        try:
            while not self._stop.is_set():
                if not self._backend.is_reachable():
                    self._wait(backoff)
                    backoff = min(backoff * 2, _BACKOFF_MAX)
                    continue

                try:
                    sent = self._drain_once(conn)
                except Exception as err:      # backend/network failure
                    logger.warning("uploader: batch failed (%s), backing off %.0fs",
                                   err, backoff)
                    self._wait(backoff)
                    backoff = min(backoff * 2, _BACKOFF_MAX)
                    continue

                backoff = self._poll_interval          # success resets backoff
                if sent < self._batch_size:
                    # Caught up — idle until more rows accumulate.
                    self._wait(self._poll_interval)
        finally:
            try:
                conn.close()
            except sqlite3.Error:
                pass
            logger.info("uploader: stopped")

    def _drain_once(self, conn: sqlite3.Connection) -> int:
        """Upload one batch of un-synced frames. Returns rows sent."""
        rows = conn.execute(
            "SELECT f.id, s.session_uuid, f.ts_utc, f.ts_mono, f.can_id, f.payload "
            "FROM frames f "
            "JOIN sessions s ON s.id = f.session_id "
            "WHERE f.synced = 0 "
            "ORDER BY f.id LIMIT ?",
            (self._batch_size,),
        ).fetchall()

        if not rows:
            return 0

        batch = [self._to_record(r) for r in rows]
        self._backend.upload(batch)          # raises on failure -> rows stay unsynced

        ids = [r[0] for r in rows]
        marks = ",".join("?" * len(ids))
        conn.execute(f"UPDATE frames SET synced = 1 WHERE id IN ({marks})", ids)
        # Keep each parsed subtable's synced flag consistent with its frame.
        for table in parsed_tables.TABLE_NAMES:
            conn.execute(
                f'UPDATE "{table}" SET synced = 1 WHERE frame_id IN ({marks})', ids)
        conn.commit()
        logger.debug("uploader: synced %d frames", len(ids))
        return len(ids)

    @staticmethod
    def _to_record(row) -> dict:
        """Build the JSON record for one frame, decoded into catalog names.

        The raw payload is always included (lossless, re-decodable). When the CAN
        id decodes to a known message, `msg_type` and `fields` carry the catalog's
        message + field names (via app.upload_map), so the server and web frontend
        render straight from shared/can_messages.toml with no mapping of their own.
        """
        fid, suid, ts_utc, ts_mono, can_id, payload = row
        payload = bytes(payload)
        rec = {
            # Stable, idempotent id so re-uploads dedupe server-side.
            "id":           f"{suid}:{fid}",
            "session_uuid": suid,
            "ts_utc":       ts_utc,
            "ts_mono":      ts_mono,
            "can_id":       can_id,
            "payload":      payload.hex(),
            "msg_type":     None,
            "fields":       None,
        }
        parsed = parse_payload(can_id, payload)
        if parsed is not None:
            mapped = upload_map.to_upload(parsed)
            if mapped is not None:
                rec["msg_type"], rec["fields"] = mapped
        return rec

    def _wait(self, seconds: float):
        # Interruptible sleep so stop() is responsive.
        self._stop.wait(seconds)
