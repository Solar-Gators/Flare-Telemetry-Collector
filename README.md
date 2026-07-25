# Flare Telemetry

Ground-station telemetry for the Solar Gators' Flare solar car. The repo is split
into two independent halves that share nothing by import — only a JSON upload
contract and a vendored message catalog.

```
                shared/can_messages.toml   (single source of truth, read as data)
                        |                    |
   ┌────────────────────┴──────┐   ┌─────────┴───────────────────────┐
   │ collector/                │   │ server/                          │
   │ desktop app (PySide6)     │   │ FastAPI website (deploys to VPS) │
   │ radio → decode → SQLite   │──►│ ingest → Postgres → live+history │
   │ store-and-forward upload  │   │ dashboard: map, charts, faults   │
   └───────────────────────────┘   └──────────────────────────────────┘
        runs on the chase-car laptop        runs in the cloud, team-only
```

## `collector/`
The offline-first collector: reads CAN frames over the RFD900x radio, decodes them
(`app/payload_parsers.py`), stores every frame in local SQLite, and — when a
`rest` upload backend is configured — drains decoded frames to the website. It maps
each message/field to its catalog name (`app/upload_map.py`) before upload so the
server needs no knowledge of the CAN map. See `collector/docs/storage.md`.

Run (from `collector/`, using the shared root `.venv`):
```bash
../.venv/bin/python -m app.gui
```

## `server/`
The self-contained telemetry website: ingests uploads, stores them in Postgres,
serves a live dashboard (Leaflet map + Chart.js) with a session picker and history
replay, and pushes live updates over a WebSocket. Team-only via a shared password.
Deploy with Docker Compose (FastAPI + Postgres + Caddy HTTPS). See `server/README.md`.

## `shared/`
`can_messages.toml` — a vendored copy of the firmware's canonical CAN + radio
message catalog (ids, layout, units, fault enums). Both halves and the web frontend
read it, so field names and fault tables can't drift. Re-sync with `shared/sync.sh`.
See `shared/README.md`.

## Data flow contract
The collector uploads records shaped like:
```json
{"id": "<session_uuid>:<frame_id>", "session_uuid": "...", "ts_utc": 0.0,
 "can_id": 65, "payload": "hex", "msg_type": "bms_battery_voltage",
 "fields": {"pack_voltage": 126.2}}
```
`id` is stable and idempotent so re-uploads dedupe; `msg_type`/`fields` use catalog
names; `payload` is always present (lossless). Full detail in `server/README.md`.
