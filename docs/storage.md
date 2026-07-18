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
| `FLARE_UPLOAD_BACKEND` | *(unset)* | Online backend: `jsonl` (test stub) or unset/`null` = no upload (rows accumulate locally). |
| `FLARE_UPLOAD_JSONL` | `data/uploaded.jsonl` | Output file for the `jsonl` test backend. |

With no upload backend configured, frames still persist locally forever with
`synced = 0`, ready to offload once a real backend is wired.

## Quick test (no hardware)

```bash
# terminal 1 — fake 10 Hz telemetry
.venv/bin/python tools/fake_radio.py

# terminal 2 — run the dashboard against it, offloading to a JSONL file
FLARE_SERIAL_PORT=/tmp/ttyUSB0 FLARE_UPLOAD_BACKEND=jsonl \
    .venv/bin/python -m app.gui

# inspect
sqlite3 data/telemetry.db \
    "SELECT count(*) FROM frames; SELECT * FROM battery_voltage ORDER BY frame_id DESC LIMIT 3;"
```
