# Telemetry storage & offload

The collector is **offline-first**: every CRC-valid frame is written to a local
SQLite database the moment it is decoded, so it works standalone with no
internet. A background uploader drains not-yet-synced rows to an online database
whenever a connection is available (**store-and-forward**) and only marks rows
synced once the upload is confirmed, so a crash or dropped link never loses data.

- `app/storage.py` — `TelemetryStore`: local SQLite store (queue-backed writer
  thread; the serial thread never blocks on disk).
- `app/uploader.py` — `TelemetryBackend` interface + `UploaderThread`. The online
  database isn't chosen yet, so a real backend is a drop-in: add a class and a
  branch in `make_backend()`.

## Schema (`data/telemetry.db`)

- `sessions(id, session_uuid, started_utc, note)` — one row per app run.
- `frames(id, session_id, ts_utc, ts_mono, can_id, payload, synced)` — every
  decoded frame, raw and lossless (re-decodable if parsers change).
- **One parsed subtable per CAN message type**, keyed by `frame_id`, e.g.
  `gps_data`, `battery_voltage`, `battery_temperature`, `bms_status`,
  `mppt_input`, `mitsuba_frame0`, `steering_requests`, … Each has `ts_utc`, a
  column per decoded field, and a `synced` flag. These are generated
  automatically from the parser dataclasses (`app/parsed_tables.py`) — add a
  dataclass to `PARSED_MESSAGES` and it gets a table with no hand-written SQL.

Uploaded records carry a stable id `"{session_uuid}:{frame_id}"` so re-uploads
dedupe server-side.

## Environment variables

| Variable | Default | Meaning |
| --- | --- | --- |
| `FLARE_STORAGE_ENABLED` | `1` | Set to `0`/`false`/`off` to disable local storage entirely. |
| `FLARE_DB_PATH` | `data/telemetry.db` | Path to the local SQLite database. |
| `FLARE_UPLOAD_BACKEND` | *(unset)* | Online backend: `rest` (telemetry website), `jsonl` (test stub), or unset/`null` = no upload (rows accumulate locally). |
| `FLARE_UPLOAD_JSONL` | `data/uploaded.jsonl` | Output file for the `jsonl` test backend. |
| `FLARE_UPLOAD_URL` | *(unset)* | `rest` backend: full ingest URL, e.g. `https://telemetry.example.org/api/ingest`. Required for `rest`. |
| `FLARE_UPLOAD_TOKEN` | *(unset)* | `rest` backend: bearer token sent as `Authorization: Bearer …`; must match the server's `FLARE_INGEST_TOKEN`. |

With no upload backend configured, frames still persist locally forever with
`synced = 0`, ready to offload once a real backend is wired.

### `rest` backend

Store-and-forward to the telemetry website (`../server/`). The uploader decodes
each frame and sends records tagged with the shared catalog's message/field names
(`shared/can_messages.toml`, via `app/upload_map.py`), plus the raw payload hex so
nothing is lost. Each record carries a stable id `"{session_uuid}:{frame_id}"` so
re-uploads dedupe server-side. Example:

```bash
FLARE_UPLOAD_BACKEND=rest \
FLARE_UPLOAD_URL=https://telemetry.example.org/api/ingest \
FLARE_UPLOAD_TOKEN=super-secret-token \
    ../.venv/bin/python -m app.gui
```

The uploaded record shape is documented in `../server/README.md`.

## Quick test (no hardware)

Run these from the `collector/` directory (the shared `.venv` lives one level up
at the repo root):

```bash
# terminal 1 — fake 10 Hz telemetry
../.venv/bin/python tools/fake_radio.py

# terminal 2 — run the dashboard against it, offloading to a JSONL file
FLARE_SERIAL_PORT=/tmp/ttyUSB0 FLARE_UPLOAD_BACKEND=jsonl \
    ../.venv/bin/python -m app.gui

# inspect
sqlite3 data/telemetry.db \
    "SELECT count(*) FROM frames; SELECT * FROM battery_voltage ORDER BY frame_id DESC LIMIT 3;"
```
