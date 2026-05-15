# telemetry_state.py

from dataclasses import dataclass
from typing import Optional
from app.payload_parsers import GpsData


@dataclass
class TelemetryState:
    gps: Optional[GpsData] = None