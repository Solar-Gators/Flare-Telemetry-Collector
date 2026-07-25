# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Repo shape

Ground-station telemetry for the Solar Gators' Flare solar car. Two halves that
**share no imports** — the only coupling is a JSON upload contract and the vendored
catalog `shared/can_messages.toml` (read as data, never imported):

- `collector/` — offline-first PySide6 desktop app on the chase-car laptop.
  RFD900x radio → decode CAN frames → local SQLite → store-and-forward upload.
- `server/` — self-contained FastAPI website deployed to a VPS. Ingest → Postgres →
  live dashboard (Leaflet map + Chart.js) + WebSocket + history replay.
- `shared/` — `can_messages.toml`, the single source of truth for message ids,
  byte/bit layout, scaling, units, and fault-bit enums.

## Commands

The collector uses the **shared root `.venv`** (one level up from `collector/`).
The server has its **own** `.venv` for local (non-Docker) dev.

```bash
# --- collector (run from collector/) ---
../.venv/bin/python -m app.gui               # launch the dashboard (real entry point)
../.venv/bin/python tools/fake_radio.py      # fake 10 Hz telemetry on a PTY (no hardware)
FLARE_SERIAL_PORT=/tmp/ttyUSB0 FLARE_UPLOAD_BACKEND=jsonl ../.venv/bin/python -m app.gui

# tests (the only test suite in the repo — a drift guard, no hardware needed)
../.venv/bin/python -m pytest tests/test_upload_map.py   # with pytest
../.venv/bin/python tests/test_upload_map.py             # standalone, no pytest

# --- server (local dev, SQLite, no Docker; run from server/) ---
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
DATABASE_URL="sqlite:///./data/telemetry.db" \
FLARE_INGEST_TOKEN=dev FLARE_VIEW_PASSWORD=dev SECRET_KEY=dev \
    .venv/bin/uvicorn main:app --reload

# --- server (production, run from server/) ---
docker compose up -d --build                 # FastAPI + Postgres + Caddy (HTTPS)

# --- catalog re-sync after firmware changes ---
shared/sync.sh                               # re-vendor from ../Flare-Firmware, then run the drift test
```

Note: `collector/main.py` is a legacy headless entry with a hardcoded `COM4` port —
the actual application is the PySide6 GUI at `app/gui.py` (`python -m app.gui`).

## Architecture notes that span multiple files

**The catalog is the contract, and drift is tested.** The collector's parsers
(`app/payload_parsers.py`) decode CAN frames into dataclasses using their *own*
field names. `app/upload_map.py` (`UPLOAD_MAP`) bridges every dataclass field to
its catalog name in `shared/can_messages.toml` *before* upload, so the server and
frontend render straight from the catalog with no CAN knowledge of their own.
`tests/test_upload_map.py` fails if the map references a name the catalog lacks, if
a decoded message type is unmapped, or if the map's fields don't exactly cover a
dataclass — **always re-run it after `shared/sync.sh`**.

**Adding a new decoded CAN message** touches four places, all name-driven, no SQL:
1. Write the dataclass + decoder in `app/payload_parsers.py` and dispatch it in
   `parse_payload()`.
2. Add the dataclass to `PARSED_MESSAGES` in `app/parsed_tables.py` — this
   auto-generates its SQLite child table (schema + INSERT derived from the
   dataclass fields; `CamelCase` → `snake_case`).
3. Add a `MsgMap` entry to `UPLOAD_MAP` in `app/upload_map.py` mapping every field
   to a catalog name (mark collector-only fields as `local=`).
4. Ensure the message exists in `shared/can_messages.toml` (sync from firmware).
The drift test then passes and the message flows end-to-end.

**Collector storage is offline-first / store-and-forward.** `app/storage.py`
(`TelemetryStore`) writes every CRC-valid frame to local SQLite via a queued
single-writer thread (the serial thread never blocks on disk; WAL mode lets the
uploader read concurrently). Raw `payload` is always stored (lossless, re-decodable
if parsers change). `app/uploader.py` (`UploaderThread`) drains rows with
`synced=0` to an online backend and only marks them synced on confirmed upload, so
a crash or dropped link never loses data. The upload backend is pluggable
(`make_backend()`): `rest` (the website), `jsonl` (test stub), or unset (accumulate
locally). Records carry a stable id `"{session_uuid}:{frame_id}"` for idempotent
dedupe. `tools/upload_backlog.py` backfills an existing local DB concurrently
without the GUI/radio.

**Server is CAN-map-agnostic by design.** `db.py` uses one generic `frames` table
with a JSON `fields` column (JSONB on Postgres, JSON text on SQLite), so any message
the collector uploads is stored with no schema change; idempotent on `uid`. `main.py`
keeps an in-memory `Hub` (latest-value-per-channel map + WebSocket client set) —
**this is why the deploy must stay single-worker** (do not raise uvicorn
`--workers`). `schema.py` serves the catalog at `/api/schema`; the frontend
(`static/app.js`) builds all tiles/charts/fault-decoders from it, nothing hardcoded.

**Auth is intentionally minimal, no user DB:** one shared team password →
HMAC-signed session cookie (dashboard routes), plus a separate bearer
`FLARE_INGEST_TOKEN` for the collector's uploads. See `server/auth.py`.

**GPS filtering must stay consistent across halves.** Both the collector uploader
and the server drop low-satellite / null-island fixes using `FLARE_GPS_MIN_SATS`
(default 4) — keep the two thresholds matched.

## Key environment variables

Collector: `FLARE_SERIAL_PORT` (override auto-detect), `FLARE_SERIAL_BAUD`,
`FLARE_STORAGE_ENABLED`, `FLARE_DB_PATH`, `FLARE_UPLOAD_BACKEND` (`rest`/`jsonl`),
`FLARE_UPLOAD_URL`, `FLARE_UPLOAD_TOKEN`, `FLARE_GPS_MIN_SATS`.
Server: `DATABASE_URL`, `FLARE_INGEST_TOKEN`, `FLARE_VIEW_PASSWORD`, `SECRET_KEY`,
`FLARE_CATALOG_PATH`, `FLARE_SESSION_TTL`. Prod values live in `server/.env`
(copy from `.env.example`, never committed).

More detail: `collector/docs/storage.md`, `server/README.md`, `shared/README.md`.
