# settings.py — server configuration from the environment.
#
# This half of the repo is self-contained: it imports nothing from collector/.
# The only shared artifact is the vendored catalog shared/can_messages.toml, read
# as data (see schema.py).

import os

_HERE = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.abspath(os.path.join(_HERE, ".."))

# Database. SQLite by default for local dev; Postgres in production, e.g.
#   postgresql+psycopg://flare:password@db:5432/flare
DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///./data/telemetry.db")

# Bearer token the collector must present on POST /api/ingest.
INGEST_TOKEN = os.environ.get("FLARE_INGEST_TOKEN", "")

# Shared team password for viewing the dashboard.
VIEW_PASSWORD = os.environ.get("FLARE_VIEW_PASSWORD", "")

# Secret used to sign session cookies. Set a long random value in production.
SECRET_KEY = os.environ.get("SECRET_KEY", "dev-insecure-change-me")

# Vendored CAN catalog (single source of truth for message/field/enum shape).
CATALOG_PATH = os.environ.get(
    "FLARE_CATALOG_PATH", os.path.join(_REPO_ROOT, "shared", "can_messages.toml")
)

COOKIE_NAME = "flare_session"
SESSION_TTL = int(os.environ.get("FLARE_SESSION_TTL", str(7 * 24 * 3600)))  # seconds

# The message type carrying GPS fix (catalog name of the collector's GpsData).
GPS_MSG_TYPE = "GpsPacket"
# Minimum satellites for a GPS fix to be trusted (no-fix readings report 0 and
# garbage coordinates). Points below this, or near null-island, are excluded.
GPS_MIN_SATS = int(os.environ.get("FLARE_GPS_MIN_SATS", "4"))

# History/replay tuning. The frames table is one big generic store, so history
# is decimated in Python: a downsampled query scans at most HISTORY_SCAN_CAP raw
# rows (bounds memory on small hosts) and folds them into time buckets. Replay
# ("state at time T") only scans the REPLAY_LOOKBACK_S window before T.
# Each collector launch starts a new session_uuid, so one afternoon of testing
# becomes many short sessions. For display they are grouped into logical "runs":
# consecutive sessions separated by less than this gap belong to the same run.
# Purely a read-time view — nothing in the database is merged or rewritten.
RUN_GAP_S = float(os.environ.get("FLARE_RUN_GAP_S", "1800"))   # 30 minutes

HISTORY_SCAN_CAP = int(os.environ.get("FLARE_HISTORY_SCAN_CAP", "200000"))
HISTORY_DEFAULT_MAX_POINTS = int(os.environ.get("FLARE_HISTORY_MAX_POINTS", "2000"))
REPLAY_LOOKBACK_S = float(os.environ.get("FLARE_REPLAY_LOOKBACK_S", "120"))
