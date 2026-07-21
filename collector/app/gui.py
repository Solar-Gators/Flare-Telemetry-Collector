import os
import sys
import json
import math
import time
import logging
import threading
from collections import deque
from PySide6.QtWidgets import (
    QApplication, QWidget, QVBoxLayout, QHBoxLayout,
    QLabel, QFrame, QSizePolicy, QStackedWidget
)
from PySide6.QtCore import Qt, QTimer, QPointF, QRectF, QEvent
from PySide6.QtGui import QColor, QPalette, QPainter, QPen, QBrush, QImage

from app.mercator import lonlat_to_world_px

from app.serial_reader import SerialReader
from app.payload_parsers import (
    parse_payload, GpsData, KillSwitch, RearVcuStatus, SupplementalBattery,
    BmsStatus, BatteryVoltage, BatteryTemperature, BatteryCurrent,
    SteeringRequests, SteeringRequests2, FrontVcuDrive,
    MpptData, MitsubaFrame0, RadioStats, BMS_FAULTS,
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


def fmt_duration(sec: float) -> str:
    """Elapsed seconds -> "45s" / "5m 03s" / "1h 05m 11s".

    Keeps long gaps readable instead of printing one huge seconds count.
    """
    s = max(0, int(sec))
    h, m, r = s // 3600, (s % 3600) // 60, s % 60
    if h:
        return f"{h}h {m:02d}m {r:02d}s"
    if m:
        return f"{m}m {r:02d}s"
    return f"{r}s"

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
STALE_AFTER   = 5.0                         # s without any packet -> "no signal"
LOSS_WINDOW_S = 60.0                        # rolling window for the radio drop rate
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


class TrackMap(QWidget):
    """GPS track — plots the car's path + current position.

    If a baked OpenStreetMap background is present (app/assets/track_map.*,
    produced by tools/fetch_map.py), the trail is drawn over it in the map's
    fixed Web-Mercator frame so the line lines up with the streets. If no map
    asset exists, it falls back to auto-fitting the trail into the widget
    (longitude aspect-corrected by cos(lat), y flipped so north is up).
    """

    MAX_POINTS = 5000               # rolling window; keeps memory/paint bounded
    MIN_MOVE   = 1e-6               # ° — ignore fixes that didn't meaningfully move
    MARGIN     = 12                 # px padding inside the widget (auto-fit mode)
    MIN_SPAN   = 1e-4               # ° — fallback half-span for a lone/still fix
    PAN_UP_FRAC = 0.10              # bias the map crop upward by up to 10% of height

    ASSET_DIR  = os.path.join(os.path.dirname(__file__), "assets")

    def __init__(self):
        super().__init__()
        self._points: deque[tuple[float, float]] = deque(maxlen=self.MAX_POINTS)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        self.setMinimumHeight(80)

        # Baked map background (optional — falls back to auto-fit if absent).
        self._map_img: QImage | None = None
        self._map_zoom: int | None = None
        self._map_origin: tuple[float, float] | None = None   # world-px of img top-left
        self._load_map()

        # Position marker (optional — falls back to a dot if absent).
        marker_path = os.path.join(self.ASSET_DIR, "gator.png")
        m = QImage(marker_path) if os.path.exists(marker_path) else None
        self._marker: QImage | None = m if (m is not None and not m.isNull()) else None

    def _load_map(self):
        meta_path = os.path.join(self.ASSET_DIR, "track_map.json")
        if not os.path.exists(meta_path):
            return
        try:
            with open(meta_path) as f:
                meta = json.load(f)
            img = QImage(os.path.join(self.ASSET_DIR, meta["image"]))
            if img.isNull():
                logging.warning("track map image failed to load: %s", meta["image"])
                return
            self._map_img = img
            self._map_zoom = int(meta["zoom"])
            self._map_origin = (float(meta["origin_px"][0]), float(meta["origin_px"][1]))
        except Exception:
            logging.exception("failed to load track map background")

    def add_point(self, lat: float, lon: float):
        # Reject implausible / no-fix data: out-of-range, or the near-origin box
        # (both coords within ±20°) where GPS parks 0,0-ish values before a lock.
        if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
            return
        if -20.0 <= lat <= 20.0 and -20.0 <= lon <= 20.0:
            return
        if self._points:
            plat, plon = self._points[-1]
            if abs(lat - plat) < self.MIN_MOVE and abs(lon - plon) < self.MIN_MOVE:
                return                      # same spot — don't pile up identical fixes
        self._points.append((lat, lon))
        self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setRenderHint(QPainter.Antialiasing)
        w, h = self.width(), self.height()
        p.fillRect(0, 0, w, h, QColor(BG_PANEL))

        if self._map_img is not None:
            self._paint_with_map(p, w, h)
        else:
            self._paint_autofit(p, w, h)

        # Thin border so the cell still reads as part of the grid.
        p.setBrush(Qt.NoBrush)
        p.setPen(QColor(BORDER))
        p.drawRect(0, 0, w - 1, h - 1)

    def _draw_trail(self, p: QPainter, screen_pts: list[QPointF], line_color: str):
        """Draw the polyline trail + current-position marker for either mode."""
        pen = QPen(QColor(line_color))
        pen.setWidthF(2.5)
        pen.setJoinStyle(Qt.RoundJoin)
        pen.setCapStyle(Qt.RoundCap)
        p.setPen(pen)
        if len(screen_pts) > 1:
            for a, b in zip(screen_pts, screen_pts[1:]):
                p.drawLine(a, b)

        self._draw_marker(p, screen_pts[-1])

    # Marker height scales with the widget width (small on the dash cell, capped
    # on the big full-screen map).
    MARKER_FRAC = 0.05
    MARKER_MIN  = 14.0
    MARKER_MAX  = 46.0

    def _draw_marker(self, p: QPainter, pt: QPointF):
        """Current position: the Albert marker if available, else a dot + ring."""
        if self._marker is not None and not self._marker.isNull():
            mh = min(self.MARKER_MAX, max(self.MARKER_MIN, self.width() * self.MARKER_FRAC))
            mw = mh * self._marker.width() / self._marker.height()
            # Centre Albert on the fix so the trail runs through him.
            p.drawImage(QRectF(pt.x() - mw / 2.0, pt.y() - mh / 2.0, mw, mh), self._marker)
            return
        p.setPen(Qt.NoPen)
        p.setBrush(QBrush(QColor(C_BLUE)))
        p.drawEllipse(pt, 5.0, 5.0)
        p.setBrush(Qt.NoBrush)
        ring = QPen(QColor(C_TEXT))
        ring.setWidthF(1.5)
        p.setPen(ring)
        p.drawEllipse(pt, 8.0, 8.0)

    def _paint_with_map(self, p: QPainter, w: int, h: int):
        img = self._map_img
        iw, ih = img.width(), img.height()
        scale = max(w / iw, h / ih)                 # cover: fill the whole cell
        dw, dh = iw * scale, ih * scale
        ox = (w - dw) / 2.0
        # Bias the vertical crop upward so the track sits a little higher, but
        # never past the cropped margin (so no blank edge appears).
        margin_y = max((dh - h) / 2.0, 0.0)
        pan = min(self.PAN_UP_FRAC * h, margin_y)
        oy = (h - dh) / 2.0 - pan
        p.drawImage(QRectF(ox, oy, dw, dh), img)

        x0px, y0px = self._map_origin

        def to_screen(lat, lon):
            wx, wy = lonlat_to_world_px(lat, lon, self._map_zoom)
            return QPointF(ox + (wx - x0px) * scale, oy + (wy - y0px) * scale)

        p.setClipRect(0, 0, w, h)                   # keep the trail inside the cell
        if self._points:
            # Red reads well over light OSM tiles (purple would wash out).
            self._draw_trail(p, [to_screen(lat, lon) for lat, lon in self._points], C_RED)
        p.setClipping(False)

        if not self._points:
            p.setPen(QColor(C_TEXT))
            p.drawText(self.rect(), Qt.AlignCenter, "ACQUIRING GPS…")

    def _paint_autofit(self, p: QPainter, w: int, h: int):
        if not self._points:
            p.setPen(QColor(C_LABEL))
            p.drawText(self.rect(), Qt.AlignCenter, "ACQUIRING GPS…")
            return

        lats = [lat for lat, _ in self._points]
        lons = [lon for _, lon in self._points]
        min_lat, max_lat = min(lats), max(lats)
        min_lon, max_lon = min(lons), max(lons)
        mean_lat = (min_lat + max_lat) / 2.0

        # Aspect-correct longitude: a degree of longitude is cos(lat)× a degree
        # of latitude. Work in a local planar space (x = lon·cos(lat), y = lat).
        cos_lat = max(math.cos(math.radians(mean_lat)), 1e-6)

        def to_xy(lat, lon):
            return (lon * cos_lat, lat)

        pts_xy = [to_xy(lat, lon) for lat, lon in self._points]
        xs = [x for x, _ in pts_xy]
        ys = [y for _, y in pts_xy]
        min_x, max_x = min(xs), max(xs)
        min_y, max_y = min(ys), max(ys)

        span_x = max(max_x - min_x, self.MIN_SPAN * cos_lat)
        span_y = max(max_y - min_y, self.MIN_SPAN)
        cx = (min_x + max_x) / 2.0
        cy = (min_y + max_y) / 2.0

        avail_w = max(w - 2 * self.MARGIN, 1)
        avail_h = max(h - 2 * self.MARGIN, 1)
        # Single uniform scale preserves aspect ratio; centered in the widget.
        scale = min(avail_w / span_x, avail_h / span_y)

        def to_screen(x, y):
            sx = w / 2.0 + (x - cx) * scale
            sy = h / 2.0 - (y - cy) * scale        # flip y so north is up
            return QPointF(sx, sy)

        self._draw_trail(p, [to_screen(x, y) for x, y in pts_xy], C_PURPLE)


# ---------------------------------------------------------------------------
# Telemetry data store  — populate from your CAN/UDP/serial thread
# ---------------------------------------------------------------------------

class TelemetryData:
    # Every telemetry field defaults to None meaning "no data received yet";
    # the GUI renders None as "N/A" rather than a misleading zero.

    # Main battery
    main_batt_voltage: float | None = None
    main_batt_current: float | None = None
    main_batt_high_cell_voltage: float | None = None
    main_batt_low_cell_voltage: float | None  = None
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

    # Radio link diagnostics (telemetry board's CAN->radio bridge)
    radio_queue_used:     int | None = None
    radio_queue_capacity: int | None = None
    radio_queue_high:     int | None = None
    radio_dropped:        int | None = None
    radio_sent:           int | None = None
    radio_interval_ms:    int | None = None

    # Frames we've successfully decoded locally (any ID), since GUI start.
    packets_decoded:      int | None = None

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
        # Rolling history of (monotonic_ts, radio_sent, packets_decoded) samples,
        # one per fresh RadioStats snapshot, used to compute the drop rate over the
        # last LOSS_WINDOW_S. The counters' different epochs (radio boot vs GUI
        # start) cancel because loss is a delta between two samples.
        self._loss_samples = deque()
        # monotonic() when the link last became NOMINAL; None whenever it isn't.
        self._nominal_since: float | None = None
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

        # Page 0 — the dashboard grid.
        dash = QWidget()
        body = QVBoxLayout(dash)
        body.setContentsMargins(8, 8, 8, 8)
        body.setSpacing(7)
        # Stretch weights: row1 and row2 share space equally; row3 is shorter
        body.addLayout(self._make_row1(), 38)
        body.addLayout(self._make_row2(), 38)
        body.addLayout(self._make_row3(), 24)

        # Page 1 — a full-screen live map (its own TrackMap, same trail).
        map_page = QWidget()
        map_lay = QVBoxLayout(map_page)
        map_lay.setContentsMargins(0, 0, 0, 0)
        map_lay.setSpacing(0)
        self._track_map_full = TrackMap()
        map_lay.addWidget(self._track_map_full)

        # Both maps are fed the same fixes in _refresh; Tab swaps the page.
        self._maps = [self._track_map, self._track_map_full]
        self._stack = QStackedWidget()
        self._stack.addWidget(dash)
        self._stack.addWidget(map_page)
        root.addWidget(self._stack, 1)

    # Header bar
    def _make_header(self) -> QWidget:
        bar = QWidget()
        bar.setFixedHeight(42)
        bar.setStyleSheet(
            f"background:{BG_HEADER}; border-bottom:1px solid {BORDER};"
        )
        lay = QHBoxLayout(bar)
        lay.setContentsMargins(16, 0, 16, 0)


        self._lbl_title = QLabel("FLARE LIVE TELEMETRY")
        self._lbl_title.setStyleSheet(
            f"color:{C_BLUE}; font-size:{SZ_HEADER}px; font-weight:700;"
            f" letter-spacing:4px; background:transparent;"
        )
        lay.addWidget(self._lbl_title)

        self._lbl_hint = QLabel("TAB ⇄ MAP")
        self._lbl_hint.setStyleSheet(
            f"color:{C_TITLE}; font-size:12px; font-weight:700;"
            f" letter-spacing:2px; background:transparent; padding-left:14px;"
        )
        lay.addWidget(self._lbl_hint)
        lay.addStretch()

        # Continuous-nominal uptime, resets whenever the link drops from NOMINAL.
        self.lbl_uptime = QLabel("UP --:--")
        self.lbl_uptime.setStyleSheet(
            f"color:{C_LABEL}; font-size:14px; font-weight:700;"
            f" letter-spacing:2px; background:transparent; padding-right:18px;"
        )
        lay.addWidget(self.lbl_uptime)

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

        # --- Main battery (now also carries cell voltages + supplemental) ---
        c = Card("Main Battery", "blue")
        self.v_mb_v   = ValLabel(C_BLUE,  "V")
        self.v_mb_a   = ValLabel(C_BLUE,  "A")
        self.v_mb_w   = ValLabel(C_BLUE,  "W")
        self.v_mb_hcv = ValLabel(C_GREEN, "V")
        self.v_mb_lcv = ValLabel(C_AMBER, "V")
        self.v_mb_ht  = ValLabel(C_AMBER, "°C")
        self.v_mb_at  = ValLabel(C_TEXT,  "°C")
        self.v_sb_v   = ValLabel(C_TEAL,  "V")
        self.v_sb_a   = ValLabel(C_TEAL,  "A")
        self.v_sb_w   = ValLabel(C_TEAL,  "W")
        c.add_row("Voltage",             self.v_mb_v)
        c.add_row("Current",             self.v_mb_a)
        c.add_row("Power",               self.v_mb_w)
        c.add_hline()
        c.add_row("High Cell Voltage",   self.v_mb_hcv)
        c.add_row("Low Cell Voltage",    self.v_mb_lcv)
        c.add_row("High Cell Temp",      self.v_mb_ht)
        c.add_row("Average Cell Temp",   self.v_mb_at)
        c.add_hline()
        c.add_row("Supp Voltage",        self.v_sb_v)
        c.add_row("Supp Current",        self.v_sb_a)
        c.add_row("Supp Power",          self.v_sb_w)
        c.add_stretch()
        row.addWidget(c, 5)

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

    # Row 2 — MPPT Front | MPPT Middle | MPPT Back | Total Solar
    # mppt_index 1/2/3 map to physical positions front/back/middle.
    MPPT_NAMES = {1: "Front", 2: "Back", 3: "Middle"}

    def _make_row2(self) -> QHBoxLayout:
        row = QHBoxLayout()
        row.setSpacing(7)

        # Display order Front, Middle, Back (data indices 1, 3, 2).
        for n in (1, 3, 2):
            c = Card(f"MPPT {self.MPPT_NAMES[n]}", "yellow")
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

        # --- GPS track map (fills the whole cell, no card chrome/labels) ---
        self._track_map = TrackMap()
        row.addWidget(self._track_map, 3)

        # --- Contactors, fault status + BMS fault code (combined) ---
        c_cont = Card("Contactors & Status", "green")
        self.v_arr_cont = ValLabel(C_RED, "")
        self.v_bms_cont = ValLabel(C_RED, "")
        self.v_kill     = ValLabel(C_OK,  "")
        c_cont.add_row("Array Contactor", self.v_arr_cont)
        c_cont.add_row("BMS Contactor",   self.v_bms_cont)
        c_cont.add_row("Fault Status",    self.v_kill)
        c_cont.add_hline()

        # BMS fault code (merged in from its own card).
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
        c_cont.add_row("BMS Fault Code", self.v_bms_hex)
        c_cont.add_widget(self.v_bms_name)
        c_cont.add_widget(ref)
        c_cont.add_stretch()
        row.addWidget(c_cont, 4)

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

        # --- Radio link diagnostics ---
        c_radio = Card("Radio Link", "cyan")
        self.v_radio_queue = ValLabel(C_TEAL, "")
        self.v_radio_peak  = ValLabel(C_TEAL, "")
        self.v_radio_drop  = ValLabel(C_OK,   "")
        self.v_radio_loss  = ValLabel(C_OK,   "%")
        self.v_radio_sent  = ValLabel(C_TEXT, "")
        self.v_radio_int   = ValLabel(C_TEAL, "ms")
        c_radio.add_row("TX Queue",   self.v_radio_queue)
        c_radio.add_row("Peak Queue", self.v_radio_peak)
        c_radio.add_row("Dropped",    self.v_radio_drop)
        c_radio.add_hline()
        c_radio.add_row("Drop (1m)",  self.v_radio_loss)
        c_radio.add_row("Sent",       self.v_radio_sent)
        c_radio.add_row("Send Gap",   self.v_radio_int)
        c_radio.add_stretch()
        row.addWidget(c_radio, 3)

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
            s.packets_decoded += 1

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
            elif isinstance(parsed, RadioStats):
                s.radio_stats = parsed
                s.last_frame_radio = now
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
            radio    = s.radio_stats
            mppt = dict(s.mppt)

            d.last_packet         = s.last_packet
            d.last_frame_gps      = s.last_frame_gps
            d.last_frame_bms      = s.last_frame_bms
            d.last_frame_steering = s.last_frame_steering
            d.last_frame_front_vcu = s.last_frame_front_vcu
            d.last_frame_rear_vcu  = s.last_frame_rear_vcu
            d.packets_decoded      = s.packets_decoded

        d.link_connected = self._reader.is_connected()

        if gps is not None:
            d.gps_lat   = gps.latitude
            d.gps_lon   = gps.longitude
            d.gps_sats  = gps.satellites
            d.speed_mph = gps.speed * KNOTS_TO_MPH

        # ── Main battery (BMS) ─────────────────────────────────────────
        if batt_v is not None:
            d.main_batt_voltage = batt_v.total_voltage
            d.main_batt_high_cell_voltage = batt_v.high_cell_voltage
            d.main_batt_low_cell_voltage  = batt_v.low_cell_voltage
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

        # ── Radio link diagnostics ─────────────────────────────────────
        if radio is not None:
            d.radio_queue_used     = radio.queue_used
            d.radio_queue_capacity = radio.queue_capacity
            d.radio_queue_high     = radio.queue_high_water
            d.radio_dropped        = radio.dropped
            d.radio_sent           = radio.sent
            d.radio_interval_ms    = radio.mean_interval_ms

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

    def _toggle_view(self):
        idx = 1 - self._stack.currentIndex()
        self._stack.setCurrentIndex(idx)
        on_map = idx == 1
        self._lbl_title.setText("FLARE LIVE MAP" if on_map else "FLARE LIVE TELEMETRY")
        self._lbl_hint.setText("TAB ⇄ DASH" if on_map else "TAB ⇄ MAP")

    def event(self, e):
        # Intercept Tab (and Shift+Tab) before Qt uses it for focus traversal.
        if e.type() == QEvent.KeyPress and e.key() in (Qt.Key_Tab, Qt.Key_Backtab):
            self._toggle_view()
            return True
        return super().event(e)

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

        # Nominal-uptime: count up while NOMINAL, reset the instant we leave it.
        if text == ST_NOMINAL[0]:
            if self._nominal_since is None:
                self._nominal_since = now
            secs = int(now - self._nominal_since)
            h, m, s = secs // 3600, (secs % 3600) // 60, secs % 60
            up = f"{h}:{m:02d}:{s:02d}" if h else f"{m:02d}:{s:02d}"
            self.lbl_uptime.setText(f"UP {up}")
            self.lbl_uptime.setStyleSheet(
                f"color:{C_OK}; font-size:14px; font-weight:700;"
                f" letter-spacing:2px; background:transparent; padding-right:18px;"
            )
        else:
            self._nominal_since = None
            self.lbl_uptime.setText("UP --:--")
            self.lbl_uptime.setStyleSheet(
                f"color:{C_LABEL}; font-size:14px; font-weight:700;"
                f" letter-spacing:2px; background:transparent; padding-right:18px;"
            )

        # ── Main battery ───────────────────────────────────────────────
        f3 = lambda v: "N/A" if v is None else f"{v:.3f}"   # cell volts, mV-ish
        mb_pwr = prod(d.main_batt_voltage, d.main_batt_current)
        self.v_mb_v.update_val(f1(d.main_batt_voltage))
        self.v_mb_a.update_val(f1(d.main_batt_current))
        self.v_mb_w.update_val(f0(mb_pwr))
        self.v_mb_hcv.update_val(f3(d.main_batt_high_cell_voltage))
        self.v_mb_lcv.update_val(f3(d.main_batt_low_cell_voltage))
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

        # ── GPS track map ──────────────────────────────────────────────
        if d.gps_lat is not None and d.gps_lon is not None:
            for m in self._maps:
                m.add_point(d.gps_lat, d.gps_lon)

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

        # ── Radio link diagnostics ─────────────────────────────────────
        cap = d.radio_queue_capacity
        qu  = d.radio_queue_used
        self.v_radio_queue.update_val(
            "N/A" if qu is None else (f"{qu} / {cap}" if cap is not None else str(qu))
        )
        self.v_radio_peak.update_val(
            "N/A" if d.radio_queue_high is None else
            (f"{d.radio_queue_high} / {cap}" if cap is not None else str(d.radio_queue_high))
        )

        # Dropped frames: green at zero, red once the radio starts shedding frames.
        if d.radio_dropped is None:
            self.v_radio_drop._color = C_LABEL
            self.v_radio_drop.update_val("N/A")
        else:
            self.v_radio_drop._color = C_FAULT if d.radio_dropped > 0 else C_OK
            self.v_radio_drop.update_val(f"{d.radio_dropped:,}")

        # End-to-end drop rate over the last LOSS_WINDOW_S: of the frames the
        # radio reports having sent in that window, how many never reached a
        # successful local decode. Sampled once per fresh RadioStats snapshot.
        sent, dec = d.radio_sent, d.packets_decoded
        loss_pct = None
        if sent is not None and dec is not None:
            samples = self._loss_samples
            if samples and sent < samples[-1][1]:
                samples.clear()                      # radio rebooted (sent reset)
            if not samples or sent != samples[-1][1]:
                samples.append((now, sent, dec))     # only on a new sent snapshot
            # Drop samples that have aged out of the window (always keep newest).
            while len(samples) > 1 and samples[0][0] < now - LOSS_WINDOW_S:
                samples.popleft()
            if len(samples) >= 2:
                sent_delta = samples[-1][1] - samples[0][1]
                dec_delta  = samples[-1][2] - samples[0][2]
                if sent_delta > 0:
                    loss_pct = max(0.0, min(100.0, 100.0 * (sent_delta - dec_delta) / sent_delta))

        if loss_pct is None:
            self.v_radio_loss._color = C_LABEL
            self.v_radio_loss.update_val("N/A")
        else:
            self.v_radio_loss._color = (
                C_OK if loss_pct < 1.0 else C_STALE if loss_pct < 5.0 else C_FAULT
            )
            self.v_radio_loss.update_val(f"{loss_pct:.1f}")

        self.v_radio_sent.update_val(
            "N/A" if d.radio_sent is None else f"{d.radio_sent:,}"
        )

        # Mean send gap: 0 ms means the link has stalled.
        gap = d.radio_interval_ms
        if gap is None:
            self.v_radio_int._color = C_TEAL
            self.v_radio_int.update_val("N/A")
        else:
            self.v_radio_int._color = C_FAULT if gap == 0 else C_TEAL
            self.v_radio_int.update_val(str(gap))

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
                    text  = f"{age:.1f}s ago"
                    color = C_OK
                elif age < 30.0:
                    text  = f"{age:.1f}s ago"
                    color = C_STALE    # getting old — orange warning
                else:
                    text  = f"{fmt_duration(age)} ago"   # 1h 05m 11s ago
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