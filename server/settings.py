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
