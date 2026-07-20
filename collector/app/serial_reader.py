# serial_reader.py

import threading
import time

import serial
from serial.tools import list_ports

from app.radio_parser import RadioFrameParser

DEFAULT_BAUD = 57600


def find_port(preferred: str | None = None) -> str | None:
    """Pick a serial port to read from.

    Returns `preferred` if it is currently present, otherwise the first
    USB/ACM serial adapter found, otherwise any available port, else None.
    """
    ports = list_ports.comports()

    if preferred:
        for p in ports:
            if p.device == preferred:
                return preferred

    for p in ports:
        if "USB" in p.device or "ACM" in p.device:
            return p.device

    return ports[0].device if ports else None


class SerialReader(threading.Thread):
    """Reads framed telemetry off a serial port on a background thread.

    Each decoded message is delivered via `on_message(msg_id, payload)`, which
    runs on this thread — keep it quick and thread-safe (don't touch Qt widgets
    from it). The reader keeps retrying if the port is absent or disconnects.
    """

    def __init__(
        self,
        on_message,
        port: str | None = None,
        baud: int = DEFAULT_BAUD,
        reconnect_delay: float = 2.0,
    ):
        super().__init__(daemon=True)
        self._on_message = on_message
        self._port = port          # None => auto-detect each connect attempt
        self._baud = baud
        self._reconnect_delay = reconnect_delay
        self._stop_event = threading.Event()
        self._connected = threading.Event()

    def stop(self):
        self._stop_event.set()

    def is_connected(self) -> bool:
        """True while a serial port is currently open (not searching)."""
        return self._connected.is_set()

    def run(self):
        parser = RadioFrameParser(on_message=self._on_message)

        while not self._stop_event.is_set():
            port = self._port or find_port()
            if not port:
                self._connected.clear()
                self._stop_event.wait(self._reconnect_delay)
                continue

            try:
                ser = serial.Serial(port, self._baud, timeout=0.5)
            except serial.SerialException:
                self._connected.clear()
                self._stop_event.wait(self._reconnect_delay)
                continue

            self._connected.set()
            try:
                while not self._stop_event.is_set():
                    # Drain whatever is buffered; block up to `timeout` for at
                    # least one byte so we can still notice stop requests.
                    data = ser.read(ser.in_waiting or 1)
                    if data:
                        parser.feed(data)
            except serial.SerialException:
                # Port went away (unplugged) — fall through to reconnect.
                pass
            finally:
                self._connected.clear()
                try:
                    ser.close()
                except Exception:
                    pass
