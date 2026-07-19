import os
import sys
import time
import logging
import threading
from PySide6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QFrame, QSizePolicy
)
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor, QPalette

from app.serial_reader import SerialReader
from app.payload_parsers import (
    parse_payload, GpsData, KillSwitch, RearVcuStatus, SupplementalBattery,
    BmsStatus, BatteryVoltage, BatteryTemperature, BatteryCurrent,
    SteeringRequests, SteeringRequests2, FrontVcuDrive,
    MpptData, MitsubaFrame0, BMS_FAULTS,
)
from app.telemetry_state import TelemetryState
from app.logging_setup import setup_logging, get_log_buffer
from app.storage import TelemetryStore, storage_enabled
from app.uploader import UploaderThread, make_backend


def decode_bms_faults(code: int) -> str:
    if code == 0:
        return "NONE"
    active = [name for mask, name in BMS_FAULTS.items() if code & mask]
    return " | ".join(active) if active else f"UNKNOWN (0x{code:04X})"

# ---------------------------------------------------------------------------
# Colors  — brighter label text so grey is actually readable on dark bg
# ---------------------------------------------------------------------------
BG_WINDOW  = "#0a0c0f"
BG_PANEL   = "#0f1318"
BG_HEADER  = "#0d1117"
BORDER     = "#243040"

C_BLUE     = "#4fc3f7"
C_GREEN    = "#69f0ae"
C_AMBER    = "#ffcc02"
C_YELLOW   = "#ffe57f"
C_PURPLE   = "#ce93d8"
C_RED      = "#ff5252"
C_TEAL     = "#26c6da"
C_TEXT     = "#e8edf2"      # bright white-ish for values
C_LABEL    = "#8faabb"      # muted but readable label colour (was too dark before)
C_TITLE    = "#5a7a90"      # card title
C_OK       = "#00e676"
C_FAULT    = "#ff1744"
C_STALE    = "#ff9800"      # last-frame age warning colour

KNOTS_TO_MPH = 1.15078                       # GPS reports speed in knots

# Header link-status states: (label, colour). Priority top-to-bottom.
STALE_AFTER  = 5.0                          # s without any packet -> "no signal"
ST_KILLED    = ("● KILLED",        C_FAULT) # car reported a fault kill
ST_NO_CONN   = ("● NO CONNECTION", C_LABEL) # no USB radio port found
ST_NO_SIGNAL = ("● NO SIGNAL",     C_STALE) # port open but no packets arriving
ST_NOMINAL   = ("● NOMINAL",       C_OK)    # receiving packets

ACCENT = {
    "blue":   "#1565c0",
    "teal":   "#00695c",
    "amber":  "#bf5000",
    "yellow": "#b58c00",
    "green":  "#2e7d32",
    "red":    "#b71c1c",
    "purple": "#4a148c",
    "cyan":   "#006064",
    "gray":   "#263040",
}

# Global font sizes — tweak one place to rescale everything
SZ_CARD_TITLE = 11   # card heading
SZ_LABEL      = 15   # row label text
SZ_VALUE      = 19   # row value
SZ_UNIT       = 13   # unit suffix
SZ_BIG        = 80   # speed / solar big number
SZ_HEADER     = 17   # top bar

QSS = f"""
QWidget {{
    background-color: {BG_WINDOW};
    color: {C_TEXT};
    font-family: "Courier New", monospace;
}}
QLabel {{
    background: transparent;
    padding: 0px;
    margin: 0px;
}}
"""

# ---------------------------------------------------------------------------
# Reusable widgets
# ---------------------------------------------------------------------------

def hline(color=BORDER) -> QFrame:
    f = QFrame()
    f.setFrameShape(QFrame.HLine)
    f.setFrameShadow(QFrame.Plain)
    f.setStyleSheet(f"background:{color}; border:none;")
    f.setFixedHeight(1)
    return f


class ValLabel(QLabel):
    """Right-aligned coloured value + small unit suffix."""

    def __init__(self, color: str = C_TEXT, unit: str = ""):
        super().__init__()
        self._color = color
        self._unit  = unit
        self.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
        self.setTextFormat(Qt.RichText)
        self.setStyleSheet("background:transparent;")
        self.update_val("—")

    def update_val(self, val: str):
        unit_html = (
            f' <span style="font-size:{SZ_UNIT}px; color:{C_LABEL}; font-weight:400;">'
            f'{self._unit}</span>'
            if self._unit else ""
        )
        self.setText(
            f'<span style="font-size:{SZ_VALUE}px; color:{self._color}; font-weight:700;">'
            f'{val}</span>{unit_html}'
        )


class Card(QFrame):
    """Dark panel with coloured accent stripe on top."""

    def __init__(self, title: str, accent: str):
        super().__init__()
        self.setObjectName("Card")
        stripe = ACCENT.get(accent, accent)
        self.setStyleSheet(
            f"#Card {{"
            f"  background:{BG_PANEL};"
            f"  border:1px solid {BORDER};"
            f"  border-top:3px solid {stripe};"
            f"  border-radius:4px;"
            f"}}"
        )
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)

        self._vbox = QVBoxLayout(self)
        self._vbox.setContentsMargins(14, 8, 14, 10)
        self._vbox.setSpacing(0)

        t = QLabel(title.upper())
        t.setStyleSheet(
            f"color:{C_TITLE}; font-size:{SZ_CARD_TITLE}px; font-weight:700;"
            f" letter-spacing:1px; background:transparent; padding-bottom:6px;"
        )
        self._vbox.addWidget(t)

    def add_row(self, label_text: str, val: QLabel, pad_top: int = 2):
        row = QHBoxLayout()
        row.setSpacing(8)
        row.setContentsMargins(0, pad_top, 0, pad_top)

        lbl = QLabel(label_text)
        lbl.setStyleSheet(
            f"color:{C_LABEL}; font-size:{SZ_LABEL}px; background:transparent;"
        )
        lbl.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Preferred)

        row.addWidget(lbl, 0)
        row.addWidget(val, 1)
        self._vbox.addLayout(row)

    def add_hline(self, margin: int = 4):
        sp = QLabel(); sp.setFixedHeight(margin)
        self._vbox.addWidget(sp)
        self._vbox.addWidget(hline())
        sp2 = QLabel(); sp2.setFixedHeight(margin)
        self._vbox.addWidget(sp2)

    def add_widget(self, w: QWidget, stretch: int = 0):
        self._vbox.addWidget(w, stretch)

    def add_stretch(self):
        self._vbox.addStretch(1)


# ---------------------------------------------------------------------------
# Telemetry data store  — populate from your CAN/UDP/serial thread
# ---------------------------------------------------------------------------

class TelemetryData:
    # Every telemetry field defaults to None meaning "no data received yet";
    # the GUI renders None as "N/A" rather than a misleading zero.

    # Main battery
    main_batt_voltage: float | None = None
    main_batt_current: float | None = None
    main_batt_high_cell_temp: float | None = None
    main_batt_avg_cell_temp: float | None  = None

    # Supplemental battery
    supp_batt_voltage: float | None = None
    supp_batt_current: float | None = None

    # MPPT 1
    mppt1_input_voltage:  float | None = None
    mppt1_input_current:  float | None = None
    mppt1_output_voltage: float | None = None
    mppt1_output_current: float | None = None

    # MPPT 2
    mppt2_input_voltage:  float | None = None
    mppt2_input_current:  float | None = None
    mppt2_output_voltage: float | None = None
    mppt2_output_current: float | None = None

    # MPPT 3
    mppt3_input_voltage:  float | None = None
    mppt3_input_current:  float | None = None
    mppt3_output_voltage: float | None = None
    mppt3_output_current: float | None = None

    # Motor controller
    motor_voltage: float | None = None
    motor_current: float | None = None

    # Speed / GPS
    speed_mph: float | None = None
    gps_lat:   float | None = None
    gps_lon:   float | None = None
    gps_sats:  int   | None = None

    # Fault / contactor status
    fault_killed:         bool | None = None
    array_contactor_open: bool | None = None
    bms_contactor_open:   bool | None = None
    bms_fault_code:       int  | None = None

    # Telemetry link status (populated from the serial reader)
    link_connected: bool         = False  # USB radio port currently open
    last_packet:    float | None = None   # time.monotonic() of last valid frame

    # Last-frame timestamps (time.monotonic(), or None if never received)
    last_frame_bms:      float | None = None
    last_frame_steering: float | None = None
    last_frame_front_vcu: float | None = None
    last_frame_rear_vcu:  float | None = None
    last_frame_gps:      float | None = None


# ---------------------------------------------------------------------------
# Main window
# ---------------------------------------------------------------------------

class TelemetryWindow(QWidget):

    LOG_LINES = 10          # how many recent log lines the overlay shows

    def __init__(self):
        super().__init__()
        self.data = TelemetryData()
        self.setWindowTitle("Flare Telemetry Dashboard")
        self.setStyleSheet(QSS)
        self._build_ui()
        self._refresh()

        # Log tail overlay, toggled with the 'L' key.
        self._log_buffer = get_log_buffer()
        self._log_visible = False
        self._log_overlay = self._make_log_overlay()

        # Local store (offline-first) + optional background uploader.
        self._store = TelemetryStore() if storage_enabled() else None
        self._uploader = None
        if self._store is not None:
            self._store.start()
            backend = make_backend()
            if backend is not None and self._store.enabled:
                self._uploader = UploaderThread(self._store, backend)
                self._uploader.start()

        # Shared state populated by the serial thread, read by the GUI timer.
        self._state = TelemetryState()
        self._state_lock = threading.Lock()
        # FLARE_SERIAL_PORT overrides auto-detect (e.g. a fake PTY for testing).
        port = os.environ.get("FLARE_SERIAL_PORT") or None
        self._reader = SerialReader(on_message=self._handle_message, port=port)
        self._reader.start()

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._on_tick)
        self._timer.start(100)

    # ------------------------------------------------------------------ UI

    def _build_ui(self):
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        root.addWidget(self._make_header())

        body = QVBoxLayout()
        body.setContentsMargins(8, 8, 8, 8)
        body.setSpacing(7)
        # Stretch weights: row1 and row2 share space equally; row3 is shorter
        body.addLayout(self._make_row1(), 38)
        body.addLayout(self._make_row2(), 38)
        body.addLayout(self._make_row3(), 24)
        root.addLayout(body, 1)

    # Header bar
    def _make_header(self) -> QWidget:
        bar = QWidget()
        bar.setFixedHeight(42)
        bar.setStyleSheet(
            f"background:{BG_HEADER}; border-bottom:1px solid {BORDER};"
        )
        lay = QHBoxLayout(bar)
        lay.setContentsMargins(16, 0, 16, 0)


        ttl = QLabel("FLARE LIVE TELEMETRY")
        ttl.setStyleSheet(
            f"color:{C_BLUE}; font-size:{SZ_HEADER}px; font-weight:700;"
            f" letter-spacing:4px; background:transparent;"
        )
        lay.addWidget(ttl)
        lay.addStretch()

        self.lbl_status = QLabel("● NOMINAL")
        self.lbl_status.setStyleSheet(
            f"color:{C_OK}; font-size:{SZ_HEADER}px; font-weight:700;"
            f" letter-spacing:2px; background:transparent;"
        )
        lay.addWidget(self.lbl_status)
        return bar

    # Row 1 — Main Battery | Supp Battery | Motor Controller | Speed
    def _make_row1(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(7)

        # --- Main battery ---
        c = Card("Main Battery", "blue")
        self.v_mb_v  = ValLabel(C_BLUE,  "V")
        self.v_mb_a  = ValLabel(C_BLUE,  "A")
        self.v_mb_w  = ValLabel(C_BLUE,  "W")
        self.v_mb_ht = ValLabel(C_AMBER, "°C")
        self.v_mb_at = ValLabel(C_TEXT,  "°C")
        c.add_row("Voltage",             self.v_mb_v)
        c.add_row("Current",             self.v_mb_a)
        c.add_row("Power",               self.v_mb_w)
        c.add_hline()
        c.add_row("High Cell Temp",      self.v_mb_ht)
        c.add_row("Average Cell Temp",   self.v_mb_at)
        c.add_stretch()
        row.addWidget(c, 5)

        # --- Supplemental battery ---
        c2 = Card("Supplemental Battery", "teal")
        self.v_sb_v = ValLabel(C_TEAL, "V")
        self.v_sb_a = ValLabel(C_TEAL, "A")
        self.v_sb_w = ValLabel(C_TEAL, "W")
        c2.add_row("Voltage", self.v_sb_v)
        c2.add_row("Current", self.v_sb_a)
        c2.add_row("Power",   self.v_sb_w)
        c2.add_stretch()
        row.addWidget(c2, 4)

        # --- Motor controller ---
        c3 = Card("Motor Controller", "amber")
        self.v_mc_v = ValLabel(C_AMBER, "V")
        self.v_mc_a = ValLabel(C_AMBER, "A")
        self.v_mc_w = ValLabel(C_AMBER, "W")
        c3.add_row("Voltage", self.v_mc_v)
        c3.add_row("Current", self.v_mc_a)
        c3.add_row("Power",   self.v_mc_w)
        c3.add_stretch()
        row.addWidget(c3, 4)

        # --- Speed (big centred numeral) ---
        c4 = Card("Speed", "cyan")
        self.v_speed = QLabel("0")
        self.v_speed.setAlignment(Qt.AlignCenter)
        self.v_speed.setStyleSheet(
            f"color:{C_BLUE}; font-size:{SZ_BIG}px; font-weight:700; background:transparent;"
        )
        self.v_speed.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        mph_lbl = QLabel("MPH")
        mph_lbl.setAlignment(Qt.AlignCenter)
        mph_lbl.setStyleSheet(
            f"color:{C_LABEL}; font-size:15px; letter-spacing:4px; background:transparent;"
        )
        c4.add_widget(self.v_speed, 1)
        c4.add_widget(mph_lbl)
        row.addWidget(c4, 3)

        return row

    # Row 2 — MPPT 1 | MPPT 2 | MPPT 3 | Total Solar
    def _make_row2(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(7)

        for n in (1, 2, 3):
            c = Card(f"MPPT {n}", "yellow")
            iv  = ValLabel(C_YELLOW, "V")
            ic  = ValLabel(C_YELLOW, "A")
            ov  = ValLabel(C_YELLOW, "V")
            oc  = ValLabel(C_YELLOW, "A")
            op  = ValLabel(C_YELLOW, "W")
            c.add_row("Input Voltage",  iv)
            c.add_row("Input Current",  ic)
            c.add_hline()
            c.add_row("Output Voltage", ov)
            c.add_row("Output Current", oc)
            c.add_row("Output Power",   op)
            c.add_stretch()
            row.addWidget(c, 4)
            setattr(self, f"v_m{n}_iv", iv)
            setattr(self, f"v_m{n}_ic", ic)
            setattr(self, f"v_m{n}_ov", ov)
            setattr(self, f"v_m{n}_oc", oc)
            setattr(self, f"v_m{n}_op", op)

        # --- Total solar (big number) ---
        c_s = Card("Total Solar Array", "green")
        self.v_solar = QLabel("0")
        self.v_solar.setAlignment(Qt.AlignCenter)
        self.v_solar.setStyleSheet(
            f"color:{C_GREEN}; font-size:{SZ_BIG - 10}px; font-weight:700; background:transparent;"
        )
        self.v_solar.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        w_lbl = QLabel("WATTS")
        w_lbl.setAlignment(Qt.AlignCenter)
        w_lbl.setStyleSheet(
            f"color:{C_LABEL}; font-size:14px; letter-spacing:3px; background:transparent;"
        )
        c_s.add_widget(self.v_solar, 1)
        c_s.add_widget(w_lbl)
        row.addWidget(c_s, 3)

        return row

    # Row 3 — GPS | Contactors + Status | BMS Fault | Last Frame Times
    def _make_row3(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(7)

        # --- GPS ---
        c_gps = Card("GPS / Position", "purple")
        self.v_lat  = ValLabel(C_PURPLE, "° N")
        self.v_lon  = ValLabel(C_PURPLE, "° W")
        self.v_sats = ValLabel(C_AMBER,  "sats")
        c_gps.add_row("Latitude",   self.v_lat)
        c_gps.add_row("Longitude",  self.v_lon)
        c_gps.add_hline()
        c_gps.add_row("Satellites", self.v_sats)
        c_gps.add_stretch()
        row.addWidget(c_gps, 3)

        # --- Contactors + fault status ---
        c_cont = Card("Contactors & Status", "green")
        self.v_arr_cont = ValLabel(C_RED, "")
        self.v_bms_cont = ValLabel(C_RED, "")
        self.v_kill     = ValLabel(C_OK,  "")
        c_cont.add_row("Array Contactor", self.v_arr_cont)
        c_cont.add_row("BMS Contactor",   self.v_bms_cont)
        c_cont.add_hline()
        c_cont.add_row("Fault Status",    self.v_kill)
        c_cont.add_stretch()
        row.addWidget(c_cont, 3)

        # --- BMS fault code ---
        c_bms = Card("BMS Fault Code", "red")
        self.v_bms_hex  = ValLabel(C_RED, "")
        self.v_bms_name = QLabel("NONE")
        self.v_bms_name.setAlignment(Qt.AlignCenter)
        self.v_bms_name.setWordWrap(True)
        self.v_bms_name.setStyleSheet(
            f"color:{C_OK}; font-size:16px; font-weight:700;"
            f" background:transparent; padding:4px 0;"
        )
        # Reference key — dimmer, smaller, still readable
        ref_text = (
            "0x0001 OVERVOLTAGE    0x0002 UNDERVOLTAGE\n"
            "0x0004 CELL_IMBALANCE 0x0008 OVERTEMPERATURE\n"
            "0x0010 BATT_OVERCURRENT 0x0020 AUX_OVERCURRENT\n"
            "0x0040 DATA_STALE 0x0080 EMERGENCY_SHUTDOWN\n"
        )
        ref = QLabel(ref_text)
        ref.setStyleSheet(
            f"color:{C_TITLE}; font-size:11px; background:transparent; padding-top:2px;"
        )
        c_bms.add_row("Code", self.v_bms_hex)
        c_bms.add_widget(self.v_bms_name)
        c_bms.add_hline()
        c_bms.add_widget(ref)
        c_bms.add_stretch()
        row.addWidget(c_bms, 4)

        # --- Last frame received times ---
        c_frames = Card("Last Frame Received", "gray")

        self._frame_labels: dict[str, QLabel] = {}
        for node in ("BMS", "Steering Wheel", "Front VCU", "Rear VCU", "GPS"):
            lbl = QLabel("never")
            lbl.setAlignment(Qt.AlignRight | Qt.AlignVCenter)
            lbl.setStyleSheet(
                f"color:{C_STALE}; font-size:{SZ_VALUE}px; font-weight:700;"
                f" background:transparent;"
            )
            c_frames.add_row(node, lbl)
            self._frame_labels[node] = lbl

        c_frames.add_stretch()
        row.addWidget(c_frames, 3)

        return row

    # ------------------------------------------------------------------ Loop

    def _on_tick(self):
        self._fetch_data()
        self._refresh()
        if self._log_visible:
            self._update_log_overlay()

    def _handle_message(self, msg_id: int, payload: bytes):
        """Runs on the serial thread — decode a frame into shared state.

        Keep this thread-safe and Qt-free; the GUI thread reads the result in
        _fetch_data().
        """
        now = time.monotonic()
        parsed = parse_payload(msg_id, payload)

        # Persist every CRC-valid frame to the local store (never blocks here).
        if self._store is not None:
            self._store.record(msg_id, payload, time.time(), now, parsed)

        with self._state_lock:
            s = self._state
            # Any CRC-valid frame counts as "receiving", even unknown IDs.
            s.last_packet = now

            if isinstance(parsed, GpsData):
                s.gps = parsed
                s.last_frame_gps = now
            elif isinstance(parsed, KillSwitch):
                s.kill = parsed
            elif isinstance(parsed, RearVcuStatus):
                s.rear_vcu = parsed
                s.last_frame_rear_vcu = now
            elif isinstance(parsed, SupplementalBattery):
                s.supp_battery = parsed
                s.last_frame_rear_vcu = now      # supp battery is sent by the rear VCU
            elif isinstance(parsed, BmsStatus):
                s.bms_status = parsed
                s.last_frame_bms = now
            elif isinstance(parsed, BatteryVoltage):
                s.battery_voltage = parsed
                s.last_frame_bms = now
            elif isinstance(parsed, BatteryTemperature):
                s.battery_temp = parsed
                s.last_frame_bms = now
            elif isinstance(parsed, BatteryCurrent):
                s.battery_current = parsed
                s.last_frame_bms = now
            elif isinstance(parsed, SteeringRequests):
                s.steering = parsed
                s.last_frame_steering = now
            elif isinstance(parsed, SteeringRequests2):
                s.steering2 = parsed
                s.last_frame_steering = now
            elif isinstance(parsed, FrontVcuDrive):
                s.front_vcu = parsed
                s.last_frame_front_vcu = now
            elif isinstance(parsed, MitsubaFrame0):
                s.mitsuba0 = parsed
            elif isinstance(parsed, MpptData):
                s.mppt[parsed.mppt_index] = parsed

    def _fetch_data(self):
        """Copy the latest serial-thread state into self.data (GUI thread)."""
        d = self.data
        with self._state_lock:
            s = self._state
            gps      = s.gps
            kill     = s.kill
            rear     = s.rear_vcu
            supp     = s.supp_battery
            bms      = s.bms_status
            batt_v   = s.battery_voltage
            batt_t   = s.battery_temp
            batt_c   = s.battery_current
            mitsuba0 = s.mitsuba0
            mppt = dict(s.mppt)

            d.last_packet         = s.last_packet
            d.last_frame_gps      = s.last_frame_gps
            d.last_frame_bms      = s.last_frame_bms
            d.last_frame_steering = s.last_frame_steering
            d.last_frame_front_vcu = s.last_frame_front_vcu
            d.last_frame_rear_vcu  = s.last_frame_rear_vcu

        d.link_connected = self._reader.is_connected()

        if gps is not None:
            d.gps_lat   = gps.latitude
            d.gps_lon   = gps.longitude
            d.gps_sats  = gps.satellites
            d.speed_mph = gps.speed * KNOTS_TO_MPH

        # ── Main battery (BMS) ─────────────────────────────────────────
        if batt_v is not None:
            d.main_batt_voltage = batt_v.total_voltage
        if batt_c is not None:
            d.main_batt_current = batt_c.current
        if batt_t is not None:
            d.main_batt_high_cell_temp = batt_t.high_temp
            d.main_batt_avg_cell_temp  = batt_t.avg_temp

        # ── Supplemental battery ───────────────────────────────────────
        if supp is not None:
            d.supp_batt_voltage = supp.voltage
            # supp_batt_current scaling is TBD in the CAN map — left as N/A.

        # ── Motor controller (Mitsuba battery-side V/A) ────────────────
        if mitsuba0 is not None:
            d.motor_voltage = mitsuba0.battery_voltage
            d.motor_current = mitsuba0.battery_current

        # ── MPPTs (mppt_index 1/2/3 -> cards 1/2/3) ────────────────────
        for n, m in mppt.items():
            setattr(d, f"mppt{n}_input_voltage", m.input_voltage)
            setattr(d, f"mppt{n}_input_current", m.input_current)
            setattr(d, f"mppt{n}_output_voltage", m.output_voltage)
            setattr(d, f"mppt{n}_output_current", m.output_current)

        # ── Contactors / fault status ──────────────────────────────────
        if kill is not None:
            d.fault_killed = kill.car_killed
        if rear is not None:
            # array contactors: 0 both open, 1 precharge closed, 2 main closed
            d.array_contactor_open = (rear.array_contactors == 0)
        if bms is not None:
            d.bms_contactor_open = not bms.contactor_closed
            d.bms_fault_code     = bms.fault_code

    def closeEvent(self, event):
        self._reader.stop()
        if self._uploader is not None:
            self._uploader.stop()
        if self._store is not None:
            self._store.stop()
        super().closeEvent(event)

    # ------------------------------------------------------------- Log overlay

    def _make_log_overlay(self) -> QWidget:
        panel = QFrame(self)
        panel.setStyleSheet(
            f"background:rgba(5,7,10,238); border-top:2px solid {C_BLUE};"
        )
        lay = QVBoxLayout(panel)
        lay.setContentsMargins(14, 8, 14, 10)
        lay.setSpacing(2)

        title = QLabel(f"RECENT LOGS — last {self.LOG_LINES}   (press L to hide)")
        title.setStyleSheet(
            f"color:{C_TITLE}; font-size:{SZ_CARD_TITLE}px; font-weight:700;"
            f" letter-spacing:1px; background:transparent;"
        )
        lay.addWidget(title)

        self._log_text = QLabel("")
        self._log_text.setTextFormat(Qt.RichText)
        self._log_text.setWordWrap(False)
        self._log_text.setAlignment(Qt.AlignTop | Qt.AlignLeft)
        self._log_text.setStyleSheet(
            "font-family:'Courier New', monospace; font-size:13px;"
            " background:transparent;"
        )
        lay.addWidget(self._log_text)
        lay.addStretch()

        panel.hide()
        return panel

    def _position_log_overlay(self):
        h = 44 + self.LOG_LINES * 19
        self._log_overlay.setGeometry(0, self.height() - h, self.width(), h)

    def _update_log_overlay(self):
        if not self._log_buffer:
            self._log_text.setText(
                f'<span style="color:{C_LABEL};">(no log records yet)</span>'
            )
            return

        rows = list(self._log_buffer)[-self.LOG_LINES:]
        html = []
        for levelno, text in rows:
            if levelno >= logging.ERROR:
                color = C_FAULT
            elif levelno >= logging.WARNING:
                color = C_STALE
            elif levelno <= logging.DEBUG:
                color = C_LABEL
            else:
                color = C_TEXT
            safe = (text.replace("&", "&amp;")
                        .replace("<", "&lt;")
                        .replace(">", "&gt;"))
            html.append(f'<span style="color:{color};">{safe}</span>')
        self._log_text.setText("<br>".join(html))

    def _toggle_logs(self):
        self._log_visible = not self._log_visible
        if self._log_visible:
            self._position_log_overlay()
            self._update_log_overlay()
            self._log_overlay.show()
            self._log_overlay.raise_()
        else:
            self._log_overlay.hide()

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_L:
            self._toggle_logs()
        else:
            super().keyPressEvent(event)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if self._log_visible:
            self._position_log_overlay()

    def _refresh(self):
        d   = self.data
        now = time.monotonic()
        f1  = lambda v: "N/A" if v is None else f"{v:.1f}"
        f0  = lambda v: "N/A" if v is None else f"{v:.0f}"
        # Product of two readings, or None if either is missing.
        prod = lambda a, b: None if a is None or b is None else a * b

        # ── Header status ──────────────────────────────────────────────
        if d.fault_killed:
            text, color = ST_KILLED
        elif not d.link_connected:
            text, color = ST_NO_CONN
        elif d.last_packet is None or (now - d.last_packet) > STALE_AFTER:
            text, color = ST_NO_SIGNAL
        else:
            text, color = ST_NOMINAL

        self.lbl_status.setText(text)
        self.lbl_status.setStyleSheet(
            f"color:{color}; font-size:{SZ_HEADER}px; font-weight:700;"
            f" letter-spacing:2px; background:transparent;"
        )

        # ── Main battery ───────────────────────────────────────────────
        mb_pwr = prod(d.main_batt_voltage, d.main_batt_current)
        self.v_mb_v.update_val(f1(d.main_batt_voltage))
        self.v_mb_a.update_val(f1(d.main_batt_current))
        self.v_mb_w.update_val(f0(mb_pwr))
        self.v_mb_ht.update_val(f1(d.main_batt_high_cell_temp))
        self.v_mb_at.update_val(f1(d.main_batt_avg_cell_temp))

        # ── Supplemental battery ───────────────────────────────────────
        sb_pwr = prod(d.supp_batt_voltage, d.supp_batt_current)
        self.v_sb_v.update_val(f1(d.supp_batt_voltage))
        self.v_sb_a.update_val(f1(d.supp_batt_current))
        self.v_sb_w.update_val(f1(sb_pwr))

        # ── Motor controller ───────────────────────────────────────────
        mc_pwr = prod(d.motor_voltage, d.motor_current)
        self.v_mc_v.update_val(f1(d.motor_voltage))
        self.v_mc_a.update_val(f1(d.motor_current))
        self.v_mc_w.update_val(f0(mc_pwr))

        # ── Speed ──────────────────────────────────────────────────────
        self.v_speed.setText(f0(d.speed_mph))

        # ── MPPTs + total solar ────────────────────────────────────────
        total_solar = 0.0
        have_solar  = False        # stays False until at least one MPPT reports
        for n in (1, 2, 3):
            iv = getattr(d, f"mppt{n}_input_voltage")
            ic = getattr(d, f"mppt{n}_input_current")
            ov = getattr(d, f"mppt{n}_output_voltage")
            oc = getattr(d, f"mppt{n}_output_current")
            op = prod(ov, oc)
            if op is not None:
                total_solar += op
                have_solar = True
            getattr(self, f"v_m{n}_iv").update_val(f1(iv))
            getattr(self, f"v_m{n}_ic").update_val(f1(ic))
            getattr(self, f"v_m{n}_ov").update_val(f1(ov))
            getattr(self, f"v_m{n}_oc").update_val(f1(oc))
            getattr(self, f"v_m{n}_op").update_val(f0(op))
        self.v_solar.setText(f"{total_solar:.0f}" if have_solar else "N/A")

        # ── GPS ────────────────────────────────────────────────────────
        self.v_lat.update_val("N/A" if d.gps_lat is None else f"{abs(d.gps_lat):.4f}")
        self.v_lon.update_val("N/A" if d.gps_lon is None else f"{abs(d.gps_lon):.4f}")
        self.v_sats.update_val("N/A" if d.gps_sats is None else str(d.gps_sats))

        # ── Contactors ─────────────────────────────────────────────────
        def cont(is_open):
            if is_open is None:
                return ("N/A", C_LABEL)
            return ("OPEN", C_RED) if is_open else ("CLOSED", C_OK)

        at, ac = cont(d.array_contactor_open)
        bt, bc = cont(d.bms_contactor_open)
        self.v_arr_cont._color = ac;  self.v_arr_cont.update_val(at)
        self.v_bms_cont._color = bc;  self.v_bms_cont.update_val(bt)

        if d.fault_killed is None:
            kt, kc = ("N/A", C_LABEL)
        else:
            kt, kc = ("KILLED", C_FAULT) if d.fault_killed else ("OKAY", C_OK)
        self.v_kill._color = kc;      self.v_kill.update_val(kt)

        # ── BMS fault code ─────────────────────────────────────────────
        code = d.bms_fault_code
        if code is None:
            self.v_bms_hex._color = C_LABEL
            self.v_bms_hex.update_val("N/A")
            self.v_bms_name.setText("N/A")
            name_color = C_LABEL
        else:
            self.v_bms_hex._color = C_FAULT if code else C_GREEN
            self.v_bms_hex.update_val(f"0x{code:04X}")
            self.v_bms_name.setText(decode_bms_faults(code))
            name_color = C_FAULT if code else C_OK
        self.v_bms_name.setStyleSheet(
            f"color:{name_color}; font-size:16px; font-weight:700;"
            f" background:transparent; padding:4px 0;"
        )

        # ── Last frame received ────────────────────────────────────────
        # Map node name → TelemetryData attribute
        frame_map = {
            "BMS":           d.last_frame_bms,
            "Steering Wheel":d.last_frame_steering,
            "Front VCU":     d.last_frame_front_vcu,
            "Rear VCU":      d.last_frame_rear_vcu,
            "GPS":           d.last_frame_gps,
        }
        for node, ts in frame_map.items():
            lbl = self._frame_labels[node]
            if ts is None:
                text  = "never"
                color = C_FAULT
            else:
                age = now - ts
                if age < 1.0:
                    text  = "live"
                    color = C_OK
                elif age < 5.0:
                    text  = f"{age:.1f} s ago"
                    color = C_OK
                elif age < 30.0:
                    text  = f"{age:.1f} s ago"
                    color = C_STALE    # getting old — orange warning
                else:
                    text  = f"{age:.0f} s ago"
                    color = C_FAULT    # stale — red

            lbl.setText(
                f'<span style="font-size:{SZ_VALUE}px; color:{color}; font-weight:700;">'
                f'{text}</span>'
            )
            lbl.setTextFormat(Qt.RichText)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    setup_logging()

    app = QApplication(sys.argv)
    app.setStyle("Fusion")

    pal = QPalette()
    pal.setColor(QPalette.Window,        QColor(BG_WINDOW))
    pal.setColor(QPalette.WindowText,    QColor(C_TEXT))
    pal.setColor(QPalette.Base,          QColor(BG_PANEL))
    pal.setColor(QPalette.AlternateBase, QColor(BG_WINDOW))
    pal.setColor(QPalette.Text,          QColor(C_TEXT))
    pal.setColor(QPalette.ButtonText,    QColor(C_TEXT))
    pal.setColor(QPalette.Button,        QColor(BG_PANEL))
    pal.setColor(QPalette.Highlight,     QColor(ACCENT["blue"]))
    app.setPalette(pal)

    win = TelemetryWindow()
    win.showFullScreen()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()