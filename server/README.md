# Flare Telemetry — website (server half)

A self-contained FastAPI app that ingests decoded telemetry from the collector,
stores it (Postgres), pushes live updates to browser dashboards over a WebSocket,
and serves history + a live map. It shares **no imports** with `collector/`; the
only coupling is the JSON upload contract and the vendored catalog
`shared/can_messages.toml` (read as data).

```
collector (laptop)  --HTTPS POST /api/ingest-->  Caddy --> FastAPI --> Postgres
                                                              |-- WebSocket --> browsers
```

## What it exposes

| Route | Auth | Purpose |
| --- | --- | --- |
| `POST /api/ingest` | bearer `FLARE_INGEST_TOKEN` | collector uploads batches |
| `POST /api/login` | — | exchange team password for a session cookie |
| `GET /api/schema` | cookie | the catalog as JSON (frontend renders from it) |
| `GET /api/latest` | cookie | current value per channel |
| `GET /api/sessions` | cookie | list of runs for the picker |
| `GET /api/history?session=&type=&since=&until=&limit=` | cookie | rows for charts |
| `GET /api/track?session=` | cookie | GPS points for the map |
| `WS /ws` | cookie | live event stream |
| `GET /` | — | dashboard (or login page if not signed in) |

Auth is intentionally simple: one shared **team password** → an HMAC-signed cookie,
and a separate **ingest token** for the collector. No user database.

## Upload record shape (`POST /api/ingest`)

```json
{"records": [
  {
    "id": "<session_uuid>:<frame_id>",     // stable + idempotent (server dedupes on it)
    "session_uuid": "abc123",
    "ts_utc": 1700000000.5,
    "can_id": 65,
    "payload": "a1b2c3",                     // raw hex, always included (lossless)
    "msg_type": "bms_battery_voltage",       // catalog message name, or null if undecoded
    "fields": {"pack_voltage": 126.2, ...}   // catalog field names, or null
  }
]}
```

`msg_type` / `fields` use the names in `shared/can_messages.toml`, produced by the
collector's `app/upload_map.py`. Frames with an unknown CAN id still arrive with
`msg_type: null` and the raw `payload`, so nothing is lost.

## Deploy to a cheap VPS (~$5/mo)

Any small Linux box works (DigitalOcean/Hetzner/Linode, 1 vCPU / 1 GB is plenty).

1. **Create the droplet** (Ubuntu 22.04+) and point your domain's DNS `A` record at
   its IP (skip if you don't have a domain yet — see step 3).

2. **Install Docker:**
   ```bash
   curl -fsSL https://get.docker.com | sh
   ```

3. **Get the code and configure:**
   ```bash
   git clone <this-repo> flare && cd flare/server
   cp .env.example .env
   # generate secrets:
   openssl rand -hex 32   # -> FLARE_INGEST_TOKEN
   openssl rand -hex 32   # -> SECRET_KEY
   nano .env              # set DOMAIN, POSTGRES_PASSWORD, FLARE_VIEW_PASSWORD, tokens
   ```
   No domain yet? Set `DOMAIN=:80` in `.env` to serve plain HTTP on the droplet IP
   for a first test (add a real domain later for HTTPS).

4. **Launch:**
   ```bash
   docker compose up -d --build
   ```
   Caddy fetches a Let's Encrypt certificate automatically for a real `DOMAIN`.
   Tables are created on first start — no migration step.

5. **Open** `https://<your-domain>/`, sign in with `FLARE_VIEW_PASSWORD`.

### Point the collector at it

On the collector laptop (see `collector/docs/storage.md`):
```bash
FLARE_UPLOAD_BACKEND=rest \
FLARE_UPLOAD_URL=https://<your-domain>/api/ingest \
FLARE_UPLOAD_TOKEN=<the FLARE_INGEST_TOKEN from .env> \
    ../.venv/bin/python -m app.gui
```
The collector is offline-first: it keeps storing locally and drains to the server
whenever the link is up, so a dropped connection never loses data.

## Local development (no Docker, SQLite)

```bash
cd server
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
DATABASE_URL="sqlite:///./data/telemetry.db" \
FLARE_INGEST_TOKEN=dev FLARE_VIEW_PASSWORD=dev SECRET_KEY=dev \
    .venv/bin/uvicorn main:app --reload
```
Then POST a sample batch to `http://127.0.0.1:8000/api/ingest` with
`Authorization: Bearer dev`, or run the collector's `rest` backend against it.

## Operations

- **Logs:** `docker compose logs -f web`
- **Update:** `git pull && docker compose up -d --build`
- **Backup:** `docker compose exec db pg_dump -U flare flare > backup.sql`
- **Re-sync the catalog** after firmware changes: `../shared/sync.sh`, then rebuild.
- Do **not** raise uvicorn `--workers`: the live map/WebSocket hub is in-process and
  must stay single-worker. One process comfortably handles a car's frame rate.
