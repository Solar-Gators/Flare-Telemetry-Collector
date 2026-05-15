# radio_parser.py

import struct

FRAME_START = 0x02
FRAME_END = 0x03
FRAME_ESCAPE = 0x1B

class RadioFrameParser:
    def __init__(self, on_message):
        self.on_message = on_message
        self.in_frame = False
        self.escaped = False
        self.frame = bytearray()

    def feed_byte(self, byte: int):
        if not self.in_frame:
            if byte == FRAME_START:
                self.in_frame = True
                self.escaped = False
                self.frame.clear()
            return

        if self.escaped:
            self.frame.append(byte)
            self.escaped = False
            return

        if byte == FRAME_ESCAPE:
            self.escaped = True
            return

        if byte == FRAME_END:
            self._parse_frame(bytes(self.frame))
            self.in_frame = False
            self.escaped = False
            self.frame.clear()
            return

        if byte == FRAME_START:
            self.frame.clear()
            self.escaped = False
            return

        self.frame.append(byte)

    def _parse_frame(self, body: bytes):
        if len(body) < 5:
            print("Invalid frame: too short")
            return

        size = body[0]
        msg_id = struct.unpack_from("<I", body, 1)[0]
        payload = body[5:]

        if len(payload) != size:
            print(f"Invalid frame size: expected {size}, got {len(payload)}")
            return

        self.on_message(msg_id, payload)