# telemetry_state.py

from dataclasses import dataclass, field
from typing import Optional
from app.payload_parsers import (
    GpsData, KillSwitch, RearVcuStatus, SupplementalBattery,
    BmsStatus, BatteryVoltage, BatteryTemperature, BatteryCurrent,
    SteeringRequests, SteeringRequests2, FrontVcuDrive,
    MpptInput, MpptOutput, MitsubaFrame0,
)


@dataclass
class TelemetryState:
    """Latest decoded value of each CAN message, shared serial-thread -> GUI.

    Written under the GUI's state lock in _handle_message; read in _fetch_data.
    """
    # GPS (telemetry-internal)
    gps: Optional[GpsData] = None

    # Custom boards
    kill: Optional[KillSwitch] = None
    rear_vcu: Optional[RearVcuStatus] = None
    supp_battery: Optional[SupplementalBattery] = None
    bms_status: Optional[BmsStatus] = None
    battery_voltage: Optional[BatteryVoltage] = None
    battery_temp: Optional[BatteryTemperature] = None
    battery_current: Optional[BatteryCurrent] = None
    steering: Optional[SteeringRequests] = None
    steering2: Optional[SteeringRequests2] = None
    front_vcu: Optional[FrontVcuDrive] = None

    # Motor controller (Mitsuba primary status frame)
    mitsuba0: Optional[MitsubaFrame0] = None

    # MPPTs, keyed by mppt_index (0=front, 1=mid, 2=rear)
    mppt_input: dict = field(default_factory=dict)
    mppt_output: dict = field(default_factory=dict)

    # Last-frame timestamps per node (time.monotonic(), or None if never seen)
    last_frame_gps: Optional[float] = None
    last_frame_bms: Optional[float] = None
    last_frame_steering: Optional[float] = None
    last_frame_front_vcu: Optional[float] = None
    last_frame_rear_vcu: Optional[float] = None

    last_packet: Optional[float] = None     # time.monotonic() of last valid frame (any ID)
