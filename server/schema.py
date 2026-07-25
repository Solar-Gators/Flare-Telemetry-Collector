# schema.py — read the vendored CAN catalog and expose it to the API/frontend.
#
# shared/can_messages.toml is the single source of truth for what messages look
# like. The frontend fetches catalog_json() from /api/schema and builds its tiles,
# charts, and fault decoders from it — no message shapes are hardcoded in JS.

import functools
import tomllib

import settings


@functools.lru_cache(maxsize=1)
def _load() -> dict:
    with open(settings.CATALOG_PATH, "rb") as f:
        return tomllib.load(f)


def _fields(entry: dict) -> dict:
    """Flatten a message's byte- and bit-defined fields to {name: metadata}."""
    out: dict[str, dict] = {}
    for part in ("bytes", "bits"):
        for f in entry.get(part, []):
            out[f["name"]] = {
                "type": f.get("type", ""),
                "unit": f.get("unit"),
                "scale": f.get("scale"),
                "notes": f.get("notes"),
            }
    return out


@functools.lru_cache(maxsize=1)
def catalog_json() -> dict:
    """A frontend-friendly JSON view of the catalog.

    { "messages": { name: {id, sender, description, fields: {name: {...}}} },
      "enums":    { EnumName: {KEY: value, ...} } }
    """
    doc = _load()
    messages: dict[str, dict] = {}
    for section in ("message", "radio_packet"):
        for entry in doc.get(section, []):
            messages[entry["name"]] = {
                "id": entry.get("id"),
                "sender": entry.get("sender"),
                "description": entry.get("description"),
                "fields": _fields(entry),
            }
    return {"messages": messages, "enums": doc.get("enums", {})}
