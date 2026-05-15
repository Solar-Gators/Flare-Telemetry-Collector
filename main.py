# main.py

import serial

from app.radio_parser import RadioFrameParser
from app.payload_parsers import parse_payload, GpsData
from app.telemetry_state import TelemetryState


PORT = "COM4"
BAUD = 57600

state = TelemetryState()


def handle_message(msg_id: int, payload: bytes):
    parsed = parse_payload(msg_id, payload)

    if parsed is None:
        print(f"Unknown message ID: 0x{msg_id:08X}")
        print(f"Payload: {payload.hex(' ')}")
        return

    if isinstance(parsed, GpsData):
        state.gps = parsed

        print("----- GPS -----")
        print(f"Latitude:   {parsed.latitude:.8f}")
        print(f"Longitude:  {parsed.longitude:.8f}")
        print(f"Speed:      {parsed.speed:.3f}")
        print(f"Satellites: {parsed.satellites}")
        print()


def main():
    ser = serial.Serial(PORT, BAUD, timeout=1)
    parser = RadioFrameParser(on_message=handle_message)

    print(f"Listening on {PORT} at {BAUD} baud...")

    while True:
        data = ser.read(1)

        if not data:
            continue

        parser.feed_byte(data[0])


if __name__ == "__main__":
    main()