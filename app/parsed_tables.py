# parsed_tables.py
#
# One SQLite child ("sub") table per decoded CAN message type, so every message
# is queryable on its own (e.g. SELECT * FROM battery_voltage). The table schema
# and INSERTs are derived automatically from each parser dataclass, so adding a
# message is just: write the dataclass in payload_parsers.py and list it in
# PARSED_MESSAGES below — no hand-written SQL.
#
# Each table is: frame_id (FK to frames.id, PRIMARY KEY), ts_utc, one column per
# dataclass field, and a synced flag mirroring frames.synced.

import dataclasses
import re

from app.payload_parsers import (
    GpsData, KillSwitch, RearVcuStatus, SupplementalBattery,
    BmsStatus, BatteryVoltage, BatteryTemperature, BatteryCurrent,
    SteeringRequests, SteeringRequests2, FrontVcuDrive,
    MitsubaFrame0, MitsubaFrame1, MitsubaFrame2, MpptData,
)

# Every decoded dataclass that should get its own table. Order is cosmetic.
PARSED_MESSAGES = [
    GpsData, KillSwitch, RearVcuStatus, SupplementalBattery,
    BmsStatus, BatteryVoltage, BatteryTemperature, BatteryCurrent,
    SteeringRequests, SteeringRequests2, FrontVcuDrive,
    MitsubaFrame0, MitsubaFrame1, MitsubaFrame2, MpptData,
]


def _snake(name: str) -> str:
    """CamelCase -> snake_case (BatteryVoltage -> battery_voltage)."""
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()


def _sql_type(field_type) -> str:
    """Map a dataclass field's Python type to a SQLite column type."""
    name = field_type if isinstance(field_type, str) else getattr(field_type, "__name__", "")
    if name in ("bool", "int"):
        return "INTEGER"
    if name == "float":
        return "REAL"
    return "TEXT"


@dataclasses.dataclass(frozen=True)
class TableSpec:
    cls: type
    table: str
    fields: tuple           # dataclass field names, in order
    create_sql: str         # CREATE TABLE + index
    insert_sql: str         # parameterised INSERT


def _build(cls) -> TableSpec:
    table = _snake(cls.__name__)
    fields = dataclasses.fields(cls)
    names = tuple(f.name for f in fields)

    col_defs = ",\n    ".join(
        f'"{f.name}" {_sql_type(f.type)} NOT NULL' for f in fields
    )
    create_sql = (
        f'CREATE TABLE IF NOT EXISTS "{table}" (\n'
        f'    frame_id INTEGER PRIMARY KEY,\n'
        f'    ts_utc   REAL NOT NULL,\n'
        f'    {col_defs},\n'
        f'    synced   INTEGER NOT NULL DEFAULT 0\n'
        f');\n'
        f'CREATE INDEX IF NOT EXISTS "idx_{table}_synced" ON "{table}"(synced);'
    )

    cols = ", ".join(f'"{n}"' for n in names)
    placeholders = ", ".join("?" * (len(names) + 2))   # frame_id, ts_utc, + fields
    insert_sql = (
        f'INSERT INTO "{table}" (frame_id, ts_utc, {cols}) VALUES ({placeholders})'
    )

    return TableSpec(cls=cls, table=table, fields=names,
                     create_sql=create_sql, insert_sql=insert_sql)


SPECS = [_build(c) for c in PARSED_MESSAGES]
SPECS_BY_TYPE = {s.cls: s for s in SPECS}
TABLE_NAMES = [s.table for s in SPECS]


def schema_sql() -> str:
    """All CREATE TABLE/INDEX statements for the parsed subtables."""
    return "\n\n".join(s.create_sql for s in SPECS)


def spec_for(parsed):
    """Return the TableSpec for a decoded message instance, or None."""
    return SPECS_BY_TYPE.get(type(parsed))


def row_values(spec: TableSpec, frame_id: int, ts_utc: float, parsed) -> list:
    """Build the INSERT parameter list for `parsed` into its subtable."""
    return [frame_id, ts_utc, *(getattr(parsed, name) for name in spec.fields)]
