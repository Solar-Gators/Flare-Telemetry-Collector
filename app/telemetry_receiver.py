# app/telemetry_receiver.py

import threading
import serial

from app.radio_parser import RadioFrameParser
from app.payload_parsers import parse_payload, GpsData, SuppBattInfo, RearVcuStatus
from app.telemetry_state import TelemetryState


class TelemetryReceiver:
    def __init__(self, port: str, baud: int, state: TelemetryState):
        self.port = port
        self.baud = baud
        self.state = state

        self._serial: serial.Serial | None = None
        self._thread: threading.Thread | None = None
        self._running = False

        self._parser = RadioFrameParser(on_message=self._handle_message)

    def start(self):
        if self._running:
            return

        self._serial = serial.Serial(self.port, self.baud, timeout=0.1)
        self._running = True

        self._thread = threading.Thread(
            target=self._read_loop,
            daemon=True,
        )
        self._thread.start()

    def stop(self):
        self._running = False

        if self._thread is not None:
            self._thread.join(timeout=1.0)

        if self._serial is not None and self._serial.is_open:
            self._serial.close()

    def _read_loop(self):
        print(f"Listening on {self.port} at {self.baud} baud...")

        while self._running:
            if self._serial is None:
                continue

            data = self._serial.read(1)

            if not data:
                continue

            self._parser.feed_byte(data[0])

    def _handle_message(self, msg_id: int, payload: bytes):
        try:
            parsed = parse_payload(msg_id, payload)
        except ValueError as e:
            print(f"Bad payload for ID 0x{msg_id:08X}: {e}")
            return

        if parsed is None:
            print(f"Unknown message ID: 0x{msg_id:08X}")
            print(f"Payload: {payload.hex(' ')}")
            return

        if isinstance(parsed, GpsData):
            self.state.update_gps(parsed)

        elif isinstance(parsed, SuppBattInfo):
            self.state.update_supp_batt(parsed)

        elif isinstance(parsed, RearVcuStatus):
            self.state.update_rear_vcu_status(parsed)