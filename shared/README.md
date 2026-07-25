# `shared/` — cross-half definitions

Files here are the **single source of truth** shared by both halves of this repo
(`collector/` and `server/`) and by the web frontend. They are **data, not code**,
so neither half imports the other — the collector, the FastAPI server, and the
browser dashboard all read these files independently.

## `can_messages.toml`

A **vendored copy** of the canonical CAN + radio message catalog that lives in the
firmware repo:

    ../Flare-Firmware/docs/can_messages.toml

It defines every CAN message and radio packet — ids, byte/bit layout, scaling,
units, and the shared enum / fault-bit tables. Consumers:

- **collector** — `collector/app/upload_map.py` maps each decoded message and field
  to its name in this catalog before upload; `collector/tests/test_upload_map.py`
  asserts the map only references names that exist here (drift guard).
- **server** — `server/schema.py` loads it and serves it at `/api/schema`.
- **frontend** — fetches `/api/schema` to build tiles/charts and decode fault bits.

### Re-syncing after firmware changes

This is a copy, so it can go stale when the firmware catalog changes. To re-sync:

    cp ../Flare-Firmware/docs/can_messages.toml shared/can_messages.toml

(or run `shared/sync.sh`). After syncing, run the collector mapping test — it fails
if a rename in the catalog broke the map:

    cd collector && ../.venv/bin/python -m pytest tests/test_upload_map.py
