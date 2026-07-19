# radio_parser.py

import logging
import struct

logger = logging.getLogger(__name__)

HEADER_LEN = 6          # uint32 canID + uint16 payload size
CRC_LEN = 2             # uint16 CRC-16
DELIMITER = 0x00        # COBS frame delimiter
MAX_FRAME_BYTES = 4096  # drop the buffer if a delimiter never arrives (garbage stream)

# ----- STX/ETX framing constants (used by the commented-out `main` parser) --
FRAME_START = 0x02
FRAME_END = 0x03
FRAME_ESCAPE = 0x1B


def _hex(b: bytes, limit: int = 64) -> str:
    """Space-separated hex, truncated so log lines stay bounded."""
    if len(b) <= limit:
        return b.hex(" ")
    return b[:limit].hex(" ") + f" ...(+{len(b) - limit}B)"


def crc16_ccitt_false(data: bytes) -> int:
    """CRC-16/CCITT-FALSE: poly 0x1021, init 0xFFFF, no reflection."""
    crc = 0xFFFF
    for b in data:
        crc ^= b << 8
        for _ in range(8):
            if crc & 0x8000:
                crc = ((crc << 1) ^ 0x1021) & 0xFFFF
            else:
                crc = (crc << 1) & 0xFFFF
    return crc


def cobs_encode(data: bytes) -> bytes:
    """Consistent Overhead Byte Stuffing encode (does not append the delimiter)."""
    out = bytearray()
    final_zero = True
    start = 0
    idx = 0
    for ch in data:
        if ch == 0:
            final_zero = True
            out.append(idx - start + 1)
            out += data[start:idx]
            start = idx + 1
        elif idx - start == 0xFD:
            final_zero = False
            out.append(0xFF)
            out += data[start:idx + 1]
            start = idx + 1
        idx += 1
    if idx != start or final_zero:
        out.append(idx - start + 1)
        out += data[start:idx]
    return bytes(out)


def cobs_decode(data: bytes) -> bytes:
    """Consistent Overhead Byte Stuffing decode of one frame (no delimiter)."""
    out = bytearray()
    idx = 0
    n = len(data)
    while idx < n:
        code = data[idx]
        if code == 0:
            raise ValueError("zero byte in COBS frame")
        idx += 1
        end = idx + code - 1
        if end > n:
            raise ValueError("COBS block overruns frame")
        out += data[idx:end]
        idx = end
        if code < 0xFF and idx < n:
            out.append(0)
    return bytes(out)


class RadioFrameParser:
    """Parses a COBS-framed byte stream of radio messages.

    Frames are COBS-encoded and separated by 0x00 delimiters. Each decoded
    frame is (all little-endian):
        uint32 canID | uint16 size | payload[size] | uint16 crc16

    Because 0x00 never appears inside a COBS frame, the delimiter gives clean
    framing even when attaching mid-stream (the first partial frame is simply
    dropped). CRC-16/CCITT-FALSE over header+payload guards each frame.

    NOTE: the STX/ETX-framed `main` variant (no CRC) is kept commented at the
    bottom of this file so we can switch back quickly.
    """

    def __init__(self, on_message, crc_big_endian: bool = False):
        self.on_message = on_message
        self._buf = bytearray()
        self._crc_fmt = ">H" if crc_big_endian else "<H"

    def feed_byte(self, byte: int):
        self.feed(bytes((byte,)))

    def feed(self, data: bytes):
        self._buf.extend(data)

        while True:
            idx = self._buf.find(DELIMITER)
            if idx == -1:
                if len(self._buf) > MAX_FRAME_BYTES:
                    logger.warning(
                        "decode fail [nosync]: %dB without a delimiter, buffer reset",
                        len(self._buf),
                    )
                    self._buf.clear()   # no delimiter in sight -> junk, reset
                return

            frame = bytes(self._buf[:idx])
            del self._buf[:idx + 1]     # consume frame + delimiter
            if frame:                   # ignore empty frames (back-to-back 0x00)
                self._handle_frame(frame)

    def _handle_frame(self, frame: bytes):
        try:
            msg = cobs_decode(frame)
        except ValueError as err:
            logger.warning("decode fail [cobs]: %s (%dB: %s)",
                           err, len(frame), _hex(frame))
            return

        if len(msg) < HEADER_LEN + CRC_LEN:
            logger.warning("decode fail [short]: %dB frame, need >=%dB: %s",
                           len(msg), HEADER_LEN + CRC_LEN, _hex(msg))
            return

        size = struct.unpack_from("<H", msg, 4)[0]
        if len(msg) != HEADER_LEN + size + CRC_LEN:
            logger.warning(
                "decode fail [size]: header size=%d implies %dB, frame is %dB: %s",
                size, HEADER_LEN + size + CRC_LEN, len(msg), _hex(msg),
            )
            return

        can_id = struct.unpack_from("<I", msg, 0)[0]
        payload = msg[HEADER_LEN:HEADER_LEN + size]
        rx_crc = struct.unpack_from(self._crc_fmt, msg, HEADER_LEN + size)[0]
        calc_crc = crc16_ccitt_false(msg[:HEADER_LEN + size])

        if rx_crc != calc_crc:
            logger.warning(
                "decode fail [crc]: canID=0x%08X rx=0x%04X calc=0x%04X payload=%s",
                can_id, rx_crc, calc_crc, _hex(payload),
            )
            return

        logger.debug("decode ok: canID=0x%08X size=%dB", can_id, size)
        self.on_message(can_id, payload)


# ----- STX/ETX framing variant, matches `main` (inactive; swap the class above
#       for this to restore). Body layout: uint8 size | uint32 msg_id | payload,
#       no CRC. Escapes 0x02/0x03/0x1B in the body with FRAME_ESCAPE (0x1B).
# class RadioFrameParser:
#     def __init__(self, on_message):
#         self.on_message = on_message
#         self.in_frame = False
#         self.escaped = False
#         self.frame = bytearray()
#
#     def feed(self, data: bytes):
#         """Convenience for byte-chunk callers (e.g. SerialReader)."""
#         for b in data:
#             self.feed_byte(b)
#
#     def feed_byte(self, byte: int):
#         if not self.in_frame:
#             if byte == FRAME_START:
#                 self.in_frame = True
#                 self.escaped = False
#                 self.frame.clear()
#             return
#
#         if self.escaped:
#             self.frame.append(byte)
#             self.escaped = False
#             return
#
#         if byte == FRAME_ESCAPE:
#             self.escaped = True
#             return
#
#         if byte == FRAME_END:
#             self._parse_frame(bytes(self.frame))
#             self.in_frame = False
#             self.escaped = False
#             self.frame.clear()
#             return
#
#         if byte == FRAME_START:
#             self.frame.clear()
#             self.escaped = False
#             return
#
#         self.frame.append(byte)
#
#     def _parse_frame(self, body: bytes):
#         if len(body) < 5:
#             logger.warning("decode fail [short]: %dB frame, need >=5B", len(body))
#             return
#
#         size = body[0]
#         msg_id = struct.unpack_from("<I", body, 1)[0]
#         payload = body[5:]
#
#         if len(payload) != size:
#             logger.warning(
#                 "decode fail [size]: header size=%d, got %dB payload",
#                 size, len(payload),
#             )
#             return
#
#         logger.debug("decode ok: msgID=0x%08X size=%dB", msg_id, size)
#         self.on_message(msg_id, payload)
