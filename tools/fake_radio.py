#!/usr/bin/env python3
"""Fake radio: emit COBS-framed telemetry on a virtual serial port.

Creates a pseudo-terminal (PTY) and symlinks it to a friendly path (default
/tmp/ttyUSB0), then streams a realistic bundle of Flare CAN-map frames at 10 Hz
(GPS, BMS, battery, VCUs, steering, MPPTs, Mitsuba, ...) so the dashboard and
parsers have something to display without real hardware. Occasionally injects a
corrupt frame and an unknown-CAN-ID frame to exercise the decode logging.

Payload byte layouts here mirror app/payload_parsers.py — keep the two in sync.

Usage:
    .venv/bin/python tools/fake_radio.py
    # then, in another terminal:
    FLARE_SERIAL_PORT=/tmp/ttyUSB0 .venv/bin/python -m app.gui
"""

import math
import os
import pty
import struct
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.radio_parser import crc16_ccitt_false, cobs_encode, DELIMITER
from app.payload_parsers import (
    GPS_PACKET_ID,
    ID_KILL_SWITCH, ID_REAR_VCU_STATUS, ID_SUPP_BATTERY,
    ID_BMS_STATUS, ID_BATTERY_VOLTAGE, ID_BATTERY_TEMP, ID_BATTERY_CURRENT,
    ID_STEERING_REQUESTS, ID_STEERING_REQUESTS2, ID_FRONT_VCU_DRIVE, ID_SPEED,
    ID_MITSUBA_FRAME0, ID_MITSUBA_FRAME1, ID_MITSUBA_FRAME2,
    MPPT_BASES,
)

LINK = os.environ.get("FAKE_PORT_LINK", "/tmp/ttyUSB0")
RATE_HZ = 10.0
UNKNOWN_CAN_ID = 0x00000042


def build_frame(can_id: int, payload: bytes) -> bytes:
    body = struct.pack("<I", can_id) + struct.pack("<H", len(payload)) + payload
    return cobs_encode(body + struct.pack("<H", crc16_ccitt_false(body))) + bytes([DELIMITER])


def write_all(fd: int, data: bytes):
    """Write every byte; os.write() may only take part of a large bundle."""
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        view = view[written:]


def _bits_pack(fields) -> int:
    """fields: list of (value, start_bit, length) -> packed little-endian int."""
    v = 0
    for value, start, length in fields:
        v |= (int(value) & ((1 << length) - 1)) << start
    return v


# ---------------------------------------------------------------------------
# Per-message payload builders (t = seconds since start)
# ---------------------------------------------------------------------------

def gps_payload(t):
    # Drift a little around UF's Reitz Union so the numbers visibly move.
    lat = 29.6436 + 0.0010 * math.sin(t / 5.0)
    lon = -82.3549 + 0.0010 * math.cos(t / 5.0)
    speed = 25.0 + 10.0 * math.sin(t / 3.0)
    return GPS_PACKET_ID, struct.pack("<ddfB", lat, lon, speed, 9)


def kill_payload(t):
    return ID_KILL_SWITCH, bytes([0])          # 0 = running normally


def rear_vcu_payload(t):
    return ID_REAR_VCU_STATUS, bytes([1, 1, 1, 2, 0, 0, 0, 0])  # enabled, fwd, power, main closed


def supp_batt_payload(t):
    mv = int(13400 + 300 * math.sin(t / 4.0))
    current = int(2000 + 500 * math.sin(t / 2.0))
    return ID_SUPP_BATTERY, struct.pack("<HH", mv, current)


def bms_status_payload(t):
    return ID_BMS_STATUS, struct.pack(">H", 0x0000) + bytes([1, 0b000111, 0, 0, 0, 0])


def battery_voltage_payload(t):
    total = int((90.0 + 5.0 * math.sin(t / 6.0)) * 100)   # centi-volts
    high_mv = int(4050 + 30 * math.sin(t / 3.0))
    low_mv = int(3950 + 30 * math.cos(t / 3.0))
    return ID_BATTERY_VOLTAGE, struct.pack(">HHBHB", total, high_mv, 12, low_mv, 27)


def battery_temp_payload(t):
    high = int((35.0 + 5.0 * math.sin(t / 7.0)) * 10)     # deci-degrees
    avg = int((30.0 + 3.0 * math.sin(t / 7.0)) * 10)
    return ID_BATTERY_TEMP, struct.pack(">HBH", high, 5, avg) + b"\x00\x00\x00"


def battery_current_payload(t):
    amps = 20.0 * math.sin(t / 3.0)
    return ID_BATTERY_CURRENT, struct.pack(">f", amps) + b"\x00\x00\x00\x00"


def steering_payload(t):
    turn = int((t / 3) % 4)                                # cycle off/left/right/hazard
    regen = int(50 + 50 * math.sin(t / 4.0))
    return ID_STEERING_REQUESTS, bytes([turn, 1, 1, 0, 1, regen, 1, 0])


def steering2_payload(t):
    return ID_STEERING_REQUESTS2, bytes([1, 0, 0, 0, 0, 0, 0, 0])   # cruise on


def front_vcu_payload(t):
    throttle = int(32000 + 32000 * math.sin(t / 2.0))     # 0..~64000
    return ID_FRONT_VCU_DRIVE, struct.pack("<HHH", throttle, 120, 300) + bytes([0, 0])


def speed_payload(t):
    knots = int(abs(22.0 + 10.0 * math.sin(t / 3.0)))
    return ID_SPEED, bytes([knots & 0xFF])


def mitsuba0_payload(t):
    volt_raw = int((90.0 + 5.0 * math.sin(t / 6.0)) / 0.5)
    fields = [
        (volt_raw, 0, 10),          # battery voltage 0.5 V/LSB
        (int(40 + 20 * math.sin(t / 3.0)), 10, 9),   # battery current 1 A/LSB
        (0, 19, 1),                 # current direction (plus)
        (int(60 + 30 * math.sin(t / 2.0)), 20, 10),  # motor current
        (int(30 / 5.0), 30, 5),     # fet temp 5 C/LSB -> ~30C
        (int(2500 + 800 * math.sin(t / 3.0)), 35, 12),  # motor rpm
        (int(80 / 0.5), 47, 10),    # pwm duty 0.5%/LSB
        (int(10 / 0.5), 57, 7),     # lead angle 0.5 deg/LSB
    ]
    return ID_MITSUBA_FRAME0, _bits_pack(fields).to_bytes(8, "little")


def mitsuba1_payload(t):
    fields = [
        (1, 0, 1),                  # power mode
        (0, 1, 1),                  # current mode
        (int(70 / 0.5), 2, 10),     # accelerator 0.5%/LSB
        (0, 12, 10),                # regen VR
        (0, 22, 4),                 # digit SW
        (int(50 / 0.5), 26, 10),    # output target
        (2, 36, 2),                 # drive action = forward
        (0, 38, 1),                 # regen status = drive
    ]
    return ID_MITSUBA_FRAME1, _bits_pack(fields).to_bytes(5, "little")


def mitsuba2_payload(t):
    return ID_MITSUBA_FRAME2, (0).to_bytes(5, "little")   # no errors


def mppt_payloads(t):
    """One input, output, temperature, and power frame per MPPT base."""
    frames = []
    for base, idx in MPPT_BASES.items():
        sun = 0.5 + 0.5 * math.sin(t / 8.0 + idx)         # 0..1 irradiance-ish
        in_v = 80.0 + 10.0 * math.sin(t / 6.0 + idx)
        in_a = 5.0 * sun
        out_v = 110.0 + 5.0 * math.sin(t / 6.0)
        out_a = (in_v * in_a) / max(out_v, 1.0)
        frames.append((base + 0, struct.pack("<ff", in_a, in_v)))    # input: current, voltage
        frames.append((base + 1, struct.pack("<ff", out_a, out_v)))  # output: current, voltage
        frames.append((base + 2, struct.pack("<ff", 40.0, 55.0)))    # temp: controller, mosfet
        frames.append((base + 6, struct.pack("<ff", out_v, 45.0)))   # power: out_v, conn temp
    return frames


# Fixed-cadence producers sent every tick.
PRODUCERS = [
    gps_payload, kill_payload, rear_vcu_payload, supp_batt_payload,
    bms_status_payload, battery_voltage_payload, battery_temp_payload,
    battery_current_payload, steering_payload, steering2_payload,
    front_vcu_payload, speed_payload,
    mitsuba0_payload, mitsuba1_payload, mitsuba2_payload,
]


def main():
    master, slave = pty.openpty()
    slave_name = os.ttyname(slave)

    if os.path.islink(LINK) or os.path.exists(LINK):
        try:
            os.remove(LINK)
        except OSError:
            pass
    os.symlink(slave_name, LINK)

    print(f"fake radio streaming on {slave_name}")
    print(f"  symlink: {LINK}")
    print(f"  connect: FLARE_SERIAL_PORT={LINK} .venv/bin/python -m app.gui")
    print("  Ctrl-C to stop")

    period = 1.0 / RATE_HZ
    t0 = time.time()
    n = 0
    try:
        while True:
            t = time.time() - t0

            # Assemble this tick's frames from every producer.
            frames = bytearray()
            for producer in PRODUCERS:
                can_id, payload = producer(t)
                frames += build_frame(can_id, payload)
            for can_id, payload in mppt_payloads(t):
                frames += build_frame(can_id, payload)

            # Periodically exercise the decode-failure logging paths.
            if n % 50 == 49:
                # Corrupt frame: valid COBS/structure but wrong CRC -> [crc] log.
                _, gp = gps_payload(t)
                body = struct.pack("<I", GPS_PACKET_ID) + struct.pack("<H", len(gp)) + gp
                frames += cobs_encode(body + struct.pack("<H", 0x0000)) + bytes([DELIMITER])
            elif n % 20 == 19:
                # Valid but unknown CAN ID: decodes OK, GUI ignores it.
                frames += build_frame(UNKNOWN_CAN_ID, bytes([n & 0xFF, 0xAB, 0xCD]))

            write_all(master, bytes(frames))
            n += 1
            time.sleep(period)
    except KeyboardInterrupt:
        print("\nstopping")
    finally:
        try:
            os.remove(LINK)
        except OSError:
            pass
        os.close(master)
        os.close(slave)


if __name__ == "__main__":
    main()
