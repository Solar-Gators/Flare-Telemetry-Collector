# payload_parsers.py
#
# Decoders for the Flare CAN map (2026). Each CAN message the telemetry board
# forwards over the radio is decoded into a small dataclass ("struct") of
# engineering-unit values. parse_payload(msg_id, payload) dispatches on the CAN
# ID and returns the matching dataclass, or None for IDs we don't decode.
#
# Layout notes taken from "Flare CAN Map 2026". Endianness/scaling that the doc
# left implicit is marked with "VERIFY" — confirm against firmware before trust.

import logging
import struct
from dataclasses import dataclass

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# GPS (telemetry-internal, not part of the car CAN map)
# ---------------------------------------------------------------------------

GPS_PACKET_ID  = 0x10000000   # telem-internal ID the firmware sends GPS under (21B <ddfB)
RADIO_STATS_ID = 0x10000001   # telem-internal ID (19B <HBHIIIH)

@dataclass
class GpsData:
    latitude: float
    longitude: float
    speed: float
    satellites: int


def parse_gps_payload(payload: bytes) -> GpsData:
    if len(payload) != 21:
        raise ValueError(f"GPS payload should be 21 bytes, got {len(payload)}")
    latitude, longitude, speed, satellites = struct.unpack("<ddfB", payload)
    return GpsData(latitude, longitude, speed, satellites)


# ---------------------------------------------------------------------------
# CAN IDs
# ---------------------------------------------------------------------------

# Custom Solar Gators boards (11-bit standard IDs)
ID_KILL_SWITCH        = 0x010   # from Telem
ID_REAR_VCU_STATUS    = 0x020   # from Rear VCU
ID_SUPP_BATTERY       = 0x021   # from Rear VCU
ID_BMS_STATUS         = 0x040   # from BMS
ID_BATTERY_VOLTAGE    = 0x041   # from BMS
ID_BATTERY_TEMP       = 0x042   # from BMS
ID_BATTERY_CURRENT    = 0x043   # from BMS
ID_STEERING_REQUESTS  = 0x064   # from Steering Wheel
ID_STEERING_REQUESTS2 = 0x065   # from Steering Wheel
ID_FRONT_VCU_DRIVE    = 0x080   # from Front VCU
ID_SPEED              = 0x0A0   # from Telem

# Mitsuba motor controller (29-bit extended IDs)
ID_MITSUBA_REQUEST = 0x08F89540   # Rear VCU -> Mitsuba
ID_MITSUBA_FRAME0  = 0x08850225   # Mitsuba -> bus
ID_MITSUBA_FRAME1  = 0x08950225
ID_MITSUBA_FRAME2  = 0x08A50225


# ---------------------------------------------------------------------------
# BMS fault bit masks (message 0x040, bytes 0-1)
# ---------------------------------------------------------------------------

BMS_FAULTS = {
    0x0001: "OVERVOLTAGE",
    0x0002: "UNDERVOLTAGE",
    0x0004: "CELL_IMBALANCE",
    0x0008: "OVERTEMPERATURE",
    0x0010: "BATTERY_OVERCURRENT",
    0x0020: "AUX_OVERCURRENT",
    0x0040: "FLEET_DATA_STALE",
    0x0080: "EMERGENCY_SHUTDOWN",
}


# ---------------------------------------------------------------------------
# Custom board messages
# ---------------------------------------------------------------------------

@dataclass
class KillSwitch:
    """0x010 — 1 = car killed (open contactors, disable throttle, strobe)."""
    car_killed: bool


def _parse_kill(p: bytes) -> KillSwitch:
    return KillSwitch(car_killed=bool(p[0]))


@dataclass
class RearVcuStatus:
    """0x020 — motor-controller + array contactor status from the rear VCU."""
    mc_enabled: bool          # byte 0: 1 = MC enabled
    mc_forward: bool          # byte 1: 1 = forward, 0 = reverse
    mc_power_mode: bool       # byte 2: 1 = power, 0 = eco
    array_contactors: int     # byte 3: 0 both open, 1 precharge closed, 2 main closed


def _parse_rear_vcu_status(p: bytes) -> RearVcuStatus:
    return RearVcuStatus(
        mc_enabled=bool(p[0]),
        mc_forward=bool(p[1]),
        mc_power_mode=bool(p[2]),
        array_contactors=p[3],
    )


@dataclass
class SupplementalBattery:
    """0x021 — supplemental (12 V) battery."""
    voltage: float            # V (bytes 0-1, uint16 mV, little-endian)
    current_raw: int          # bytes 2-3, uint16 — scaling TBD in the doc


def _parse_supp_battery(p: bytes) -> SupplementalBattery:
    millivolts, current_raw = struct.unpack_from("<HH", p, 0)
    return SupplementalBattery(voltage=millivolts / 1000.0, current_raw=current_raw)


@dataclass
class BmsStatus:
    """0x040 — BMS fault code + contactor / daughter-board state.

    NOTE: the map's byte table and its bullet list disagree on byte positions
    for contactor vs daughter-board state (table: contactor=byte2,
    daughter=byte3; bullets: contactor=byte3, daughter=byte4). This follows the
    byte table. VERIFY against firmware.
    """
    fault_code: int               # bytes 0-1, uint16 big-endian (see BMS_FAULTS)
    contactor_closed: bool        # byte 2: 0 off, 1 on
    daughter_board_status: int    # byte 3: bitfield, bit0=lowest .. bit5=highest


def _parse_bms_status(p: bytes) -> BmsStatus:
    fault_code = struct.unpack_from(">H", p, 0)[0]
    return BmsStatus(
        fault_code=fault_code,
        contactor_closed=bool(p[2]),
        daughter_board_status=p[3],
    )


@dataclass
class BatteryVoltage:
    """0x041 — pack + cell voltages from the BMS."""
    total_voltage: float          # V   (bytes 0-1, uint16 * 0.01, big-endian)
    high_cell_voltage: float      # V   (bytes 2-3, uint16 mV, big-endian)
    high_cell_index: int          # byte 4
    low_cell_voltage: float       # V   (bytes 5-6, uint16 mV, big-endian)
    low_cell_index: int           # byte 7


def _parse_battery_voltage(p: bytes) -> BatteryVoltage:
    total, high_mv, high_idx, low_mv, low_idx = struct.unpack_from(">HHBHB", p, 0)
    return BatteryVoltage(
        total_voltage=total / 100.0,
        high_cell_voltage=high_mv / 1000.0,
        high_cell_index=high_idx,
        low_cell_voltage=low_mv / 1000.0,
        low_cell_index=low_idx,
    )


@dataclass
class BatteryTemperature:
    """0x042 — pack temperatures from the BMS."""
    high_temp: float              # °C  (bytes 0-1, uint16 * 0.1, big-endian)
    high_temp_index: int          # byte 2
    avg_temp: float               # °C  (bytes 3-4, uint16 * 0.1, big-endian)


def _parse_battery_temp(p: bytes) -> BatteryTemperature:
    high, high_idx, avg = struct.unpack_from(">HBH", p, 0)
    return BatteryTemperature(
        high_temp=high / 10.0,
        high_temp_index=high_idx,
        avg_temp=avg / 10.0,
    )


@dataclass
class BatteryCurrent:
    """0x043 — pack current from the BMS."""
    current: float                # A (bytes 0-3, IEEE-754 float; VERIFY endianness)


def _parse_battery_current(p: bytes) -> BatteryCurrent:
    # BMS uint16s are big-endian (MSB first), so assume big-endian float too.
    return BatteryCurrent(current=struct.unpack_from(">f", p, 0)[0])


@dataclass
class SteeringRequests:
    """0x064 — driver requests (not truth values) from the steering wheel."""
    turn_signal: int          # byte 0: 0 off, 1 left, 2 right, 3 hazards
    direction_forward: bool   # byte 1: 1 forward, 0 reverse
    array_close_request: bool # byte 2: 1 request closed, 0 request open
    horn: bool                # byte 3
    headlights: bool          # byte 4
    regen_strength: int       # byte 5: 0-100 percent
    power_mode: bool          # byte 6: 1 power, 0 eco


def _parse_steering_requests(p: bytes) -> SteeringRequests:
    return SteeringRequests(
        turn_signal=p[0],
        direction_forward=bool(p[1]),
        array_close_request=bool(p[2]),
        horn=bool(p[3]),
        headlights=bool(p[4]),
        regen_strength=p[5],
        power_mode=bool(p[6]),
    )


@dataclass
class SteeringRequests2:
    """0x065 — cruise-control request from the steering wheel."""
    cruise_on: bool           # byte 0


def _parse_steering_requests2(p: bytes) -> SteeringRequests2:
    return SteeringRequests2(cruise_on=bool(p[0]))


@dataclass
class FrontVcuDrive:
    """0x080 — throttle / brake / accessory power from the front VCU."""
    throttle: int             # bytes 0-1, uint16 DAC 0..65535 (little-endian)
    fan_horn_power: int       # bytes 2-3, uint16 (little-endian)
    lights_power: int         # bytes 4-5, uint16 (little-endian)
    brakes_pressed: bool      # byte 7


def _parse_front_vcu_drive(p: bytes) -> FrontVcuDrive:
    throttle, fan_horn, lights = struct.unpack_from("<HHH", p, 0)
    return FrontVcuDrive(
        throttle=throttle,
        fan_horn_power=fan_horn,
        lights_power=lights,
        brakes_pressed=bool(p[7]),
    )


@dataclass
class SpeedMessage:
    """0x0A0 — vehicle speed from the telemetry board."""
    speed_knots: int          # byte 0, uint8


def _parse_speed(p: bytes) -> SpeedMessage:
    return SpeedMessage(speed_knots=p[0])


# ---------------------------------------------------------------------------
# Mitsuba motor controller (Intel/little-endian bit packing)
# ---------------------------------------------------------------------------

def _bits(value: int, start: int, length: int) -> int:
    """Extract `length` bits starting at bit `start` (bit 0 = LSB of byte 0)."""
    return (value >> start) & ((1 << length) - 1)


@dataclass
class MitsubaFrame0:
    """0x08850225 — Mitsuba primary status frame."""
    battery_voltage: float        # V
    battery_current: float        # A
    battery_current_minus: bool   # True = discharging into MC ("Minus")
    motor_current: float          # A
    fet_temp: float               # °C
    motor_rpm: int                # RPM
    pwm_duty: float               # %
    lead_angle: float             # degrees


def _parse_mitsuba_frame0(p: bytes) -> MitsubaFrame0:
    v = int.from_bytes(p[:8], "little")
    return MitsubaFrame0(
        battery_voltage=_bits(v, 0, 10) * 0.5,
        battery_current=_bits(v, 10, 9) * 1.0,
        battery_current_minus=bool(_bits(v, 19, 1)),
        motor_current=_bits(v, 20, 10) * 1.0,
        fet_temp=_bits(v, 30, 5) * 5.0,
        motor_rpm=_bits(v, 35, 12),
        pwm_duty=_bits(v, 47, 10) * 0.5,
        lead_angle=_bits(v, 57, 7) * 0.5,
    )


@dataclass
class MitsubaFrame1:
    """0x08950225 — Mitsuba mode / target frame (5 bytes)."""
    power_mode: bool          # bit 0: 1 power, 0 eco
    pwm_mode: bool            # bit 1: 1 PWM mode, 0 current mode
    accelerator_position: float   # %
    regen_vr_position: float      # %
    digit_sw_position: int
    output_target: int        # raw (0.5A/LSB current mode, 0.5%/LSB PWM mode)
    drive_action: int         # bits 36-37: 0 stop, 2 forward, 3 reverse
    regen_active: bool        # bit 38: 1 regen, 0 drive


def _parse_mitsuba_frame1(p: bytes) -> MitsubaFrame1:
    v = int.from_bytes(p[:5], "little")
    return MitsubaFrame1(
        power_mode=bool(_bits(v, 0, 1)),
        pwm_mode=bool(_bits(v, 1, 1)),
        accelerator_position=_bits(v, 2, 10) * 0.5,
        regen_vr_position=_bits(v, 12, 10) * 0.5,
        digit_sw_position=_bits(v, 22, 4),
        output_target=_bits(v, 26, 10),
        drive_action=_bits(v, 36, 2),
        regen_active=bool(_bits(v, 38, 1)),
    )


# Mitsuba Frame2 error bits (0x08A50225). bit -> name.
MITSUBA_FRAME2_ERRORS = {
    0:  "ANALOG_SENSOR",
    1:  "MOTOR_CURRENT_SENSOR_U",
    2:  "MOTOR_CURRENT_SENSOR_W",
    3:  "FET_THERMISTOR",
    5:  "BATTERY_VOLTAGE_SENSOR",
    6:  "BATTERY_CURRENT_SENSOR",
    7:  "BATTERY_CURRENT_SENSOR_ADJUST",
    8:  "MOTOR_CURRENT_SENSOR_ADJUST",
    9:  "ACCELERATOR_POSITION",
    11: "CONTROLLER_VOLTAGE_SENSOR",
    16: "POWER_SYSTEM",
    17: "OVER_CURRENT",
    19: "OVER_VOLTAGE",
    21: "OVER_CURRENT_LIMIT",
    24: "MOTOR_SYSTEM",
    25: "MOTOR_LOCK",
    26: "HALL_SENSOR_SHORT",
    27: "HALL_SENSOR_OPEN",
}


@dataclass
class MitsubaFrame2:
    """0x08A50225 — Mitsuba error frame (5 bytes)."""
    error_bits: int           # raw 40-bit field (decode with MITSUBA_FRAME2_ERRORS)
    overheat_level: int       # bits 32-33: 0 normal, 1-2 levels, 3 over heat

    @property
    def active_errors(self) -> list[str]:
        return [name for bit, name in MITSUBA_FRAME2_ERRORS.items()
                if self.error_bits & (1 << bit)]


def _parse_mitsuba_frame2(p: bytes) -> MitsubaFrame2:
    v = int.from_bytes(p[:5], "little")
    return MitsubaFrame2(error_bits=v, overheat_level=_bits(v, 32, 2))


# ---------------------------------------------------------------------------
# MPPT frame (repacked from CAN by the telemetry board)
# ---------------------------------------------------------------------------
# The firmware folds each MPPT's input/output CAN frames into a single 16-byte
# radio packet tagged with the controller's base CAN id (0x600/0x610/0x620),
# carrying four little-endian floats: input current, input voltage,
# output current, output voltage.

# TEMP(revert): using old telem-internal MPPT IDs. Real base IDs are below.
MPPT_IDS = {0x600: 1, 0x610: 2, 0x620: 3}   # base can_id -> index


@dataclass
class MpptData:
    """0x1000000n — MPPT input/output measurements (4 LE floats)."""
    mppt_index: int           # 1, 2, 3 (firmware numbering)
    input_current: float      # A
    input_voltage: float      # V
    output_current: float     # A
    output_voltage: float     # V


def _parse_mppt_data(index: int, p: bytes) -> MpptData:
    in_v, in_a, out_v, out_a = struct.unpack_from("<ffff", p, 0)
    return MpptData(index, in_a, in_v, out_a, out_v)


# ---------------------------------------------------------------------------
# Radio-link diagnostics (telem-internal)
# ---------------------------------------------------------------------------
# The telemetry board reports the health of its own CAN->radio bridge under a
# telem-internal ID. 19-byte little-endian packet (see [[radio_packet]]
# RadioStatsPacket in Flare-Firmware/docs/can_messages.toml). Lets the ground
# station see when CAN traffic outruns the radio (dropped climbing, queue
# saturating).



@dataclass
class RadioStats:
    """0x10000004 — radio-link health from the telemetry board's TX bridge."""
    queue_used: int           # bytes 0-1,  uint16 — frames waiting in the TX queue now
    queue_capacity: int       # byte 2,     uint8  — TX queue depth (32)
    queue_high_water: int     # bytes 3-4,  uint16 — peak queue_used since boot
    enqueued: int             # bytes 5-8,  uint32 — total frames queued since boot
    dropped: int              # bytes 9-12, uint32 — total frames dropped (queue full)
    sent: int                 # bytes 13-16, uint32 — total frames sent over UART
    mean_interval_ms: int     # bytes 17-18, uint16 — mean gap between sent frames (0 = stalled)


def _parse_radio_stats(p: bytes) -> RadioStats:
    (queue_used, queue_capacity, queue_high_water,
     enqueued, dropped, sent, mean_interval_ms) = struct.unpack_from("<HBHIIIH", p, 0)
    return RadioStats(
        queue_used=queue_used,
        queue_capacity=queue_capacity,
        queue_high_water=queue_high_water,
        enqueued=enqueued,
        dropped=dropped,
        sent=sent,
        mean_interval_ms=mean_interval_ms,
    )


# ---------------------------------------------------------------------------
# Dispatch
# ---------------------------------------------------------------------------

_PARSERS = {
    ID_KILL_SWITCH:        _parse_kill,
    ID_REAR_VCU_STATUS:    _parse_rear_vcu_status,
    ID_SUPP_BATTERY:       _parse_supp_battery,
    ID_BMS_STATUS:         _parse_bms_status,
    ID_BATTERY_VOLTAGE:    _parse_battery_voltage,
    ID_BATTERY_TEMP:       _parse_battery_temp,
    ID_BATTERY_CURRENT:    _parse_battery_current,
    ID_STEERING_REQUESTS:  _parse_steering_requests,
    ID_STEERING_REQUESTS2: _parse_steering_requests2,
    ID_FRONT_VCU_DRIVE:    _parse_front_vcu_drive,
    ID_SPEED:              _parse_speed,
    ID_MITSUBA_FRAME0:     _parse_mitsuba_frame0,
    ID_MITSUBA_FRAME1:     _parse_mitsuba_frame1,
    ID_MITSUBA_FRAME2:     _parse_mitsuba_frame2,
}


def _dispatch(msg_id: int, payload: bytes):
    if msg_id == GPS_PACKET_ID:
        return parse_gps_payload(payload)

    if msg_id == RADIO_STATS_ID:
        return _parse_radio_stats(payload)

    parser = _PARSERS.get(msg_id)
    if parser is not None:
        return parser(payload)

    # MPPT frame (one 16-byte message per MPPT).
    mppt_index = MPPT_IDS.get(msg_id)
    if mppt_index is not None:
        return _parse_mppt_data(mppt_index, payload)

    return None


def parse_payload(msg_id: int, payload: bytes):
    """Decode a CAN frame into its dataclass, or None if the ID is unknown.

    Malformed payloads for a known ID are logged and return None rather than
    raising, so a bad frame never takes down the serial thread.
    """
    try:
        return _dispatch(msg_id, payload)
    except (struct.error, ValueError, IndexError) as err:
        logger.warning("payload parse failed for canID=0x%X (%dB): %s",
                       msg_id, len(payload), err)
        return None
