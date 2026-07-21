# main.py

import time

import serial

from app.radio_parser import RadioFrameParser
from app.payload_parsers import parse_payload, GpsData
from app.telemetry_state import TelemetryState
from app.logging_setup import setup_logging
from app.storage import TelemetryStore, storage_enabled
from app.uploader import UploaderThread, make_backend


PORT = "COM4"
BAUD = 115200

state = TelemetryState()
store: TelemetryStore | None = None


def handle_message(msg_id: int, payload: bytes):
    now = time.monotonic()
    parsed = parse_payload(msg_id, payload)

    # Persist every decoded frame to the local store (works offline).
    if store is not None:
        store.record(msg_id, payload, time.time(), now, parsed)

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
    global store
    setup_logging()

    uploader = None
    if storage_enabled():
        store = TelemetryStore()
        store.start()
        backend = make_backend()
        if backend is not None and store.enabled:
            uploader = UploaderThread(store, backend)
            uploader.start()

    ser = serial.Serial(PORT, BAUD, timeout=1)
    parser = RadioFrameParser(on_message=handle_message)

    print(f"Listening on {PORT} at {BAUD} baud...")

    try:
        while True:
            data = ser.read(1)

            if not data:
                continue

            parser.feed_byte(data[0])
    except KeyboardInterrupt:
        pass
    finally:
        ser.close()
        if uploader is not None:
            uploader.stop()
        if store is not None:
            store.stop()


if __name__ == "__main__":
    main()