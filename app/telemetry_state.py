# telemetry_state.py

from dataclasses import dataclass
from typing import Optional
from app.payload_parsers import GpsData


@dataclass
class TelemetryState:
    gps: Optional[GpsData] = None
    last_frame_gps: Optional[float] = None  # time.monotonic() of last GPS frame
    last_packet: Optional[float] = None     # time.monotonic() of last valid frame (any ID)