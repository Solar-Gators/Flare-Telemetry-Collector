# payload_parsers.py

import struct
from dataclasses import dataclass


GPS_PACKET_ID = 0xFFFFFFFF


@dataclass
class GpsData:
    latitude: float
    longitude: float
    speed: float
    satellites: int


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


def parse_payload(msg_id: int, payload: bytes):
    if msg_id == GPS_PACKET_ID:
        return parse_gps_payload(payload)

    return None