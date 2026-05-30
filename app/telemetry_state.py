# app/telemetry_state.py

from dataclasses import dataclass, field
import threading
import time

from app.payload_parsers import GpsData, SuppBattInfo, RearVcuStatus

@dataclass
class TelemetryState:
    lock: threading.Lock = field(default_factory=threading.Lock)

    gps: GpsData | None = None
    supp_batt: SuppBattInfo | None = None
    rear_vcu_status: RearVcuStatus | None = None

    last_frame_rear_vcu: float | None = None

    def update_gps(self, gps: GpsData):
        with self.lock:
            self.gps = gps
            self.last_frame_rear_vcu = time.monotonic()

    def update_supp_batt(self, supp_batt: SuppBattInfo):
        with self.lock:
            self.supp_batt = supp_batt
            self.last_frame_rear_vcu = time.monotonic()

    def update_rear_vcu_status(self, status: RearVcuStatus):
        with self.lock:
            self.rear_vcu_status = status
            self.last_frame_rear_vcu = time.monotonic()