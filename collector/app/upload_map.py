# upload_map.py
#
# Maps each decoded collector dataclass (see payload_parsers.py) onto its name in
# the canonical CAN catalog, shared/can_messages.toml (vendored from the firmware
# repo). The uploader (uploader.py) uses this to send records tagged with the
# catalog's message + field names, so the server and web frontend can render them
# straight from the same schema with no mapping layer of their own.
#
# The parsers themselves are left untouched — this is a pure naming bridge. A test
# (collector/tests/test_upload_map.py) loads the catalog and asserts every mapped
# name actually exists in it, so a firmware rename can't silently drift.
#
# VERIFY notes below mark places where the collector's decoded value differs from
# the catalog field in *meaning or units* (not just name); the name mapping is
# still correct, but the semantics are worth confirming against firmware.

from dataclasses import dataclass, fields as dc_fields

from app.payload_parsers import (
    GpsData, KillSwitch, RearVcuStatus, SupplementalBattery,
    BmsStatus, BatteryVoltage, BatteryTemperature, BatteryCurrent,
    SteeringRequests, SteeringRequests2, FrontVcuDrive,
    MitsubaFrame0, MitsubaFrame1, MitsubaFrame2, MpptData, RadioStats,
)


@dataclass(frozen=True)
class MsgMap:
    """How one collector dataclass maps onto the shared catalog.

    - toml_name: the `name` of the [[message]] / [[radio_packet]] in the catalog.
    - fields:    collector field name -> output (catalog) field name, for EVERY
                 field of the dataclass.
    - local:     output names deliberately NOT present in the catalog (the collector
                 exposes something the catalog doesn't). The drift test skips these.
    """
    toml_name: str
    fields: dict
    local: frozenset = frozenset()


# collector field -> catalog field. Where the names already match, they map to
# themselves; the point is that EVERY collector field has an explicit, tested target.
UPLOAD_MAP: dict[type, MsgMap] = {
    # --- telemetry-internal radio packets ---
    GpsData: MsgMap("GpsPacket", {
        "latitude":   "latitude",
        "longitude":  "longitude",
        "speed":      "speed",
        "satellites": "num_satellites",
    }),
    MpptData: MsgMap("MpptPacket", {
        # mppt_index identifies which controller; the catalog encodes that in the
        # base CAN id (0x600/0x610/0x620), so there is no catalog field for it.
        "mppt_index":     "mppt_index",   # local (VERIFY: index, not on the wire)
        "input_current":  "input_current",
        "input_voltage":  "input_voltage",
        "output_current": "output_current",
        "output_voltage": "output_voltage",
    }, local=frozenset({"mppt_index"})),
    RadioStats: MsgMap("RadioStatsPacket", {
        "queue_used":       "queue_used",
        "queue_capacity":   "queue_capacity",
        "queue_high_water": "queue_high_water",
        "enqueued":         "enqueued",
        "dropped":          "dropped",
        "sent":             "sent",
        "mean_interval_ms": "mean_interval_ms",
    }),

    # --- custom Solar Gators boards ---
    KillSwitch: MsgMap("kill_frame", {
        "car_killed": "killed_status",   # VERIFY: collector bool vs enum CarKilledStatus (DEAD=1)
    }),
    RearVcuStatus: MsgMap("rearvcu_statuses_frame", {
        "mc_enabled":       "mc_enabled",
        "mc_forward":       "direction_requested",       # VERIFY: bool vs enum Direction (FORWARD=0)
        "mc_power_mode":    "mc_power_mode_requested",
        "array_contactors": "array_contactors",
    }),
    SupplementalBattery: MsgMap("supp_batt_frame", {
        "voltage":     "supp_batt_voltage_mv",   # VERIFY: collector sends Volts; catalog field is mV
        "current_raw": "supp_batt_current",
    }),

    # --- BMS (DistributedBMS) ---
    BmsStatus: MsgMap("bms_status", {
        "fault_code":            "bms_faults",
        "contactor_closed":      "contactors_state",     # VERIFY: bool vs uint8 (0/1)
        "daughter_board_status": "daughter_board_status",
    }),
    BatteryVoltage: MsgMap("bms_battery_voltage", {
        "total_voltage":     "pack_voltage",     # VERIFY: collector Volts; catalog raw is Vx100
        "high_cell_voltage": "high_cell_mv",     # VERIFY: collector Volts; catalog raw is mV
        "high_cell_index":   "high_cell_index",
        "low_cell_voltage":  "low_cell_mv",      # VERIFY: collector Volts; catalog raw is mV
        "low_cell_index":    "low_cell_index",
    }),
    BatteryTemperature: MsgMap("bms_battery_temperature", {
        "high_temp":       "high_temp",
        "high_temp_index": "high_temp_index",
        "avg_temp":        "avg_temp",
    }),
    BatteryCurrent: MsgMap("bms_battery_current", {
        "current": "current",
    }),

    # --- steering wheel ---
    SteeringRequests: MsgMap("steering_requests_frame", {
        "turn_signal":         "turn_signals_requested",
        "direction_forward":   "direction_requested",             # VERIFY: bool vs enum Direction
        "array_close_request": "array_contactors_requested_closed",
        "horn":                "horn_requested_on",
        # Collector decodes byte 4 as "headlights"; the catalog names byte 4
        # "blink_phase". Left as a local field pending firmware confirmation.
        "headlights":          "headlights",                      # local (VERIFY vs blink_phase)
        "regen_strength":      "regen_percent_requested",
        "power_mode":          "mc_power_mode_requested",
    }, local=frozenset({"headlights"})),
    SteeringRequests2: MsgMap("steering_requests_frame_2", {
        "cruise_on": "is_cc_on",
    }),

    # --- front VCU ---
    FrontVcuDrive: MsgMap("tb_frame", {
        "throttle":       "throttle_data",
        "fan_horn_power": "fh_power",
        "lights_power":   "lights_power",
        "brakes_pressed": "brake_state",
    }),

    # --- Mitsuba motor controller ---
    MitsubaFrame0: MsgMap("mitsuba_frame0", {
        "battery_voltage":       "battery_voltage",
        "battery_current":       "battery_current",
        "battery_current_minus": "battery_current_minus",
        "motor_current":         "motor_current",
        "fet_temp":              "fet_temp",
        "motor_rpm":             "motor_rpm",
        "pwm_duty":              "pwm_duty",
        "lead_angle":            "lead_angle",
    }),
    MitsubaFrame1: MsgMap("mitsuba_frame1", {
        "power_mode":           "power_mode",
        "pwm_mode":             "pwm_mode",
        "accelerator_position": "accelerator_position",
        "regen_vr_position":    "regen_vr_position",
        "digit_sw_position":    "digit_sw_position",
        "output_target":        "output_target",
        "drive_action":         "drive_action",
        "regen_active":         "regen_active",
    }),
    MitsubaFrame2: MsgMap("mitsuba_frame2", {
        "error_bits":     "error_flags",
        "overheat_level": "overheat_level",
    }),
}


def to_upload(parsed) -> tuple[str, dict] | None:
    """Return (catalog_message_name, {catalog_field: value}) for a decoded message.

    Returns None for message types with no mapping (which shouldn't happen for any
    type in PARSED_MESSAGES — the test guarantees full coverage).
    """
    m = UPLOAD_MAP.get(type(parsed))
    if m is None:
        return None
    values = {out: getattr(parsed, coll) for coll, out in m.fields.items()}
    return m.toml_name, values


def _collector_fields(cls) -> set:
    return {f.name for f in dc_fields(cls)}
