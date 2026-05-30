# app/payload_parsers.py

import struct
from dataclasses import dataclass


GPS_PACKET_ID = 0xFFFFFFFF
SUPP_BATT_INFO_ID = 0x021
REAR_VCU_STATUS_ID = 0x020


@dataclass
class GpsData:
    latitude: float
    longitude: float
    speed: float
    satellites: int


@dataclass
class SuppBattInfo:
    voltage_mv: int
    current_ma: int

    @property
    def voltage_v(self) -> float:
        return self.voltage_mv / 1000.0

    @property
    def current_a(self) -> float:
        return self.current_ma / 1000.0

@dataclass
class RearVcuStatus:
    mc_enabled: bool
    mc_forward: bool
    mc_power_mode: bool
    array_contactor_status: int


def parse_gps_payload(payload: bytes) -> GpsData:
    if len(payload) != 21:
        raise ValueError(f"GPS payload should be 21 bytes, got {len(payload)}")

    latitude, longitude, speed, satellites = struct.unpack("<ddfB", payload)

    return GpsData(
        latitude=latitude,
        longitude=longitude,
        speed=speed,
        satellites=satellites,
    )


def parse_supp_batt_payload(payload: bytes) -> SuppBattInfo:
    if len(payload) != 4:
        raise ValueError(f"Supp batt payload should be 4 bytes, got {len(payload)}")

    voltage_mv, current_ma = struct.unpack("<HH", payload)

    return SuppBattInfo(
        voltage_mv=voltage_mv,
        current_ma=current_ma,
    )


def parse_rear_vcu_status_payload(payload: bytes) -> RearVcuStatus:
    if len(payload) != 8:
        raise ValueError(f"Rear VCU status payload should be 8 bytes, got {len(payload)}")

    mc_status, mc_direction, mc_power_eco, array_contactor_status = struct.unpack(
        "<BBBB",
        payload[0:4],
    )

    return RearVcuStatus(
        mc_enabled=bool(mc_status),
        mc_forward=bool(mc_direction),
        mc_power_mode=bool(mc_power_eco),
        array_contactor_status=array_contactor_status,
    )


def parse_payload(msg_id: int, payload: bytes):
    if msg_id == GPS_PACKET_ID:
        print("gps")
        return parse_gps_payload(payload)

    if msg_id == SUPP_BATT_INFO_ID:
        print("sup bat")
        return parse_supp_batt_payload(payload)

    if msg_id == REAR_VCU_STATUS_ID:
        print("rear vcu")
        return parse_rear_vcu_status_payload(payload)

    return None