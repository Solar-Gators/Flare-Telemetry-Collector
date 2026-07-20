# logging_setup.py

import collections
import logging
import logging.handlers
import os
from datetime import datetime

DEFAULT_LOG_DIR = "logs"

_configured = False
_logfile = None
_ring = None


class RingBufferHandler(logging.Handler):
    """Keeps the most recent formatted records in memory for a live log view.

    Stores (levelno, text) tuples in a bounded deque. deque.append is atomic in
    CPython, so the GUI thread can read it while the serial thread writes.
    """

    def __init__(self, capacity: int = 500):
        super().__init__()
        self.records = collections.deque(maxlen=capacity)

    def emit(self, record):
        try:
            self.records.append((record.levelno, self.format(record)))
        except Exception:
            self.handleError(record)


def get_log_buffer():
    """Return the ring buffer deque of (levelno, text), or None if unset."""
    return _ring.records if _ring is not None else None


def setup_logging(
    log_dir: str = DEFAULT_LOG_DIR,
    console_level: int = logging.WARNING,
    file_level: int = logging.WARNING,
    max_bytes: int = 5_000_000,
    backup_count: int = 5,
) -> str | None:
    """Configure root logging for the app.

    Every decode outcome (see radio_parser) is recorded: successes at DEBUG,
    failures at WARNING. The rotating file captures everything (DEBUG+); the
    console shows warnings and above so it isn't flooded at full packet rate.

    Returns the active log file path. Safe to call once per process; repeated
    calls are ignored.
    """
    global _configured, _logfile, _ring
    if _configured:
        return _logfile

    os.makedirs(log_dir, exist_ok=True)
    _logfile = os.path.join(
        log_dir, datetime.now().strftime("telemetry_%Y%m%d_%H%M%S.log")
    )

    fmt = logging.Formatter(
        "%(asctime)s.%(msecs)03d %(levelname)-7s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    root = logging.getLogger()
    root.setLevel(min(console_level, file_level))

    console = logging.StreamHandler()
    console.setLevel(console_level)
    console.setFormatter(fmt)
    root.addHandler(console)

    file_handler = logging.handlers.RotatingFileHandler(
        _logfile, maxBytes=max_bytes, backupCount=backup_count
    )
    file_handler.setLevel(file_level)
    file_handler.setFormatter(fmt)
    root.addHandler(file_handler)

    # In-memory tail for the GUI log overlay (compact format, captures DEBUG+).
    _ring = RingBufferHandler()
    _ring.setLevel(logging.DEBUG)
    _ring.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)-7s %(message)s", datefmt="%H:%M:%S",
    ))
    root.addHandler(_ring)

    _configured = True
    logging.getLogger(__name__).info("logging to %s", _logfile)
    return _logfile
