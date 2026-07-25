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
import contextlib
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
from app.payload_parsers import GpsData, parse_payload
from app.storage import _configure_connection

logger = logging.getLogger(__name__)

_BATCH_SIZE      = 200
_POLL_INTERVAL   = 2.0     # seconds between drain attempts when caught up
_BACKOFF_MAX     = 60.0    # cap for exponential backoff after failures
_DB_RETRY_S      = 10.0    # wait before retrying a failed database open

# GPS fixes with too few satellites (or garbage coordinates) are inaccurate; they
# are dropped from upload — never stored online or plotted. Override the minimum
# with FLARE_GPS_MIN_SATS (must match the server's for consistency).
GPS_MIN_SATS = int(os.environ.get("FLARE_GPS_MIN_SATS", "4"))


def _gps_accurate(g: GpsData) -> bool:
    """True if a GPS fix has enough satellites and sane, non-null-island coords."""
    if g.satellites < GPS_MIN_SATS:
        return False
    if not (-90 <= g.latitude <= 90 and -180 <= g.longitude <= 180):
        return False
    if abs(g.latitude) < 1.0 and abs(g.longitude) < 3.0:   # null-island / acquiring
        return False
    return True


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
    """Best-effort connectivity probe for real network backends.

    Catches Exception, not just OSError: a probe is a hint, never a reason to
    fail. Name resolution in particular can raise beyond the socket errors —
    UnicodeError from IDNA encoding, for one — and an uncaught raise here used
    to kill the uploader thread outright (see UploaderThread._run).
    """
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except Exception:
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
        timeout = float(os.environ.get("FLARE_UPLOAD_TIMEOUT", "10"))
        logger.info("uploader: REST backend -> %s", url)
        return RestApiBackend(url, token, timeout=timeout)
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
        # Link state, for logging and for the GUI's upload indicator. Starts
        # "offline" so the first pass probes before spending a batch on a link
        # that may not exist — the chase car often boots with no signal.
        self._online = False
        self._last_success: float | None = None   # time.time() of last synced batch
        self._failures = 0                        # consecutive failed attempts
        self._sent_total = 0
        self._last_error: str | None = None
        self._lock = threading.Lock()

    def status(self) -> dict:
        """Snapshot of link state for the UI. Safe to call from any thread."""
        with self._lock:
            return {
                "online": self._online,
                "last_success": self._last_success,
                "failures": self._failures,
                "sent_total": self._sent_total,
                "last_error": self._last_error,
                "alive": self._thread is not None and self._thread.is_alive(),
            }

    def start(self):
        self._thread = threading.Thread(
            target=self._run, name="telemetry-uploader", daemon=True
        )
        self._thread.start()

    def stop(self, timeout: float = 5.0):
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout)
        # Shutdown must not raise: this runs while the GUI is closing, and a
        # backend that fails to close cleanly should not take the app down with
        # it. Anything unsent is already durable in SQLite.
        with contextlib.suppress(Exception):
            self._backend.close()

    # ------------------------------------------------------------------ loop

    def _run(self):
        """Drain loop. Runs until stop() and MUST NOT die for any other reason.

        Losing connectivity is the normal case, not an error: the car spends
        whole runs out of coverage and the backlog is expected to sit locally
        until a link returns. So every step — the reachability probe, the
        database open, the drain — is inside the try, and the only exit is
        self._stop. An uncaught exception here previously killed the thread and
        stranded the backlog until the app was restarted, which is precisely the
        failure this loop exists to prevent.
        """
        logger.info("uploader: started (%s)", type(self._backend).__name__)
        conn: sqlite3.Connection | None = None
        backoff = self._poll_interval
        try:
            while not self._stop.is_set():
                try:
                    if conn is None:
                        # Retried rather than fatal: the DB may be briefly locked
                        # by the writer thread at startup.
                        conn = sqlite3.connect(self._store.db_path)
                        _configure_connection(conn)

                    # Probe only while we believe we're offline. Once the link is
                    # up, skipping it saves a TCP round-trip per batch, which
                    # roughly halves the time to clear a large backlog; a failed
                    # upload flips us back to offline and the probe resumes.
                    if not self._online and not self._backend.is_reachable():
                        raise ConnectionError("backend not reachable")

                    sent = self._drain_once(conn)
                except Exception as err:
                    self._note_failure(err, backoff)
                    if conn is not None and isinstance(err, sqlite3.Error):
                        with contextlib.suppress(Exception):
                            conn.close()
                        conn = None
                        backoff = max(backoff, _DB_RETRY_S)
                    self._wait(backoff)
                    backoff = min(backoff * 2, _BACKOFF_MAX)
                    continue

                self._note_success(sent)
                backoff = self._poll_interval          # success resets backoff
                if sent < self._batch_size:
                    # Caught up — idle until more rows accumulate.
                    self._wait(self._poll_interval)
        finally:
            if conn is not None:
                with contextlib.suppress(Exception):
                    conn.close()
            logger.info("uploader: stopped (%d frames sent this run)", self._sent_total)

    # ------------------------------------------------------------ link state

    def _note_failure(self, err: Exception, backoff: float):
        """Record a failed attempt. Logs the transition loudly, then quietly.

        A multi-hour outage would otherwise fill the log with one identical
        warning per retry, burying anything else the operator needs to see.
        """
        with self._lock:
            was_online = self._online
            self._online = False
            self._failures += 1
            self._last_error = f"{type(err).__name__}: {err}"
            failures = self._failures
        if was_online or failures == 1:
            logger.warning("uploader: upload failed (%s) — backlog is safe locally, "
                           "retrying (next in %.1fs, backing off to %.0fs) until the "
                           "link returns", err, backoff, _BACKOFF_MAX)
        else:
            logger.debug("uploader: still offline after %d attempts (%s)", failures, err)

    def _note_success(self, sent: int):
        with self._lock:
            was_offline = not self._online
            failures = self._failures
            self._online = True
            self._failures = 0
            self._last_error = None
            if sent:
                self._last_success = time.time()
                self._sent_total += sent
        if was_offline and failures:
            logger.info("uploader: link restored after %d failed attempts — "
                        "draining backlog", failures)

    def _drain_once(self, conn: sqlite3.Connection) -> int:
        """Upload one batch of un-synced frames. Returns rows sent.

        Drains NEWEST-first (id DESC): live frames always upload immediately, and
        any historical backlog fills in behind them instead of delaying live data.
        The server dedupes on the stable id, so upload order doesn't affect
        correctness. (A dropped connection still can't lose data — unsent rows
        stay synced=0 and are retried.)
        """
        rows = conn.execute(
            "SELECT f.id, s.session_uuid, f.ts_utc, f.ts_mono, f.can_id, f.payload "
            "FROM frames f "
            "JOIN sessions s ON s.id = f.session_id "
            "WHERE f.synced = 0 "
            "ORDER BY f.id DESC LIMIT ?",
            (self._batch_size,),
        ).fetchall()

        if not rows:
            return 0

        # Inaccurate GPS fixes are dropped (record is None) but still marked synced
        # below so they don't retry forever.
        batch = [rec for rec in (self._to_record(r) for r in rows) if rec is not None]
        if batch:
            self._backend.upload(batch)      # raises on failure -> rows stay unsynced

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
    def _to_record(row) -> dict | None:
        """Build the JSON record for one frame, decoded into catalog names.

        Returns None to drop the frame from upload — used for inaccurate GPS fixes
        (too few satellites / garbage coordinates), which we don't store online or
        plot. The raw payload of everything else is always included (lossless,
        re-decodable). When the CAN id decodes to a known message, `msg_type` and
        `fields` carry the catalog's names (via app.upload_map), so the server and
        web frontend render straight from shared/can_messages.toml.
        """
        fid, suid, ts_utc, ts_mono, can_id, payload = row
        payload = bytes(payload)
        parsed = parse_payload(can_id, payload)
        if isinstance(parsed, GpsData) and not _gps_accurate(parsed):
            return None
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
        if parsed is not None:
            mapped = upload_map.to_upload(parsed)
            if mapped is not None:
                rec["msg_type"], rec["fields"] = mapped
        return rec

    def _wait(self, seconds: float):
        # Interruptible sleep so stop() is responsive.
        self._stop.wait(seconds)
