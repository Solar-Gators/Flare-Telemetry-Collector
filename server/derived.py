# derived.py — second-order telemetry: state-of-charge estimation.
#
# The collector never decodes bms_pack_status, so the BMS's own `soc` never
# reaches this server. Every state-of-charge number the dashboard shows is
# inferred here from pack voltage and current alone, against the battery model
# in pack_model.py.
#
# Two independent estimates exist, and they fail in complementary ways:
#
#   OCV      — invert V = OCV(soc) + I*R(soc) for soc (pack_model.soc_from_loaded).
#              Absolutely referenced and never drifts, but the IR correction is
#              only characterised at 1C while the BMS permits 1.45C, so it gets
#              noisy and biased under load.
#   Coulomb  — integrate current. Tracks fast load changes exactly and does not
#              care about R at all, but it is an integral with no absolute
#              reference: sensor bias accumulates forever and a data gap is
#              unrecoverable.
#
# So: integrate current for the fast path, and continuously pull the result
# toward the OCV estimate — slowly enough that load transients do not leak in.
# That is a complementary filter (a Kalman filter with a hand-set gain). The
# trick that makes it work is that the correction time constant depends on load:
# at rest OCV is trustworthy so we correct hard, and under heavy current — where
# the R model is least reliable — we nearly ignore it.
#
# Both estimates are published. Their divergence is itself a health signal: as
# the pack ages its true capacity falls below the simulated 38.078 Ah, and the
# coulomb path will drift steadily against the OCV path.
#
# The same estimator serves the live dashboard (fed incrementally from ingest)
# and replay (re-run in batch over stored frames), so the two always agree.

import math

import db
import pack_model
import settings

# --- unit contract, verified against collector/app/payload_parsers.py ---------
# bms_battery_voltage.pack_voltage : Volts (uint16/100 on the wire)
# bms_battery_current.current      : Amps, NEGATIVE = discharge
#   (DistributedBMSPrimaryV2 BmsManager.cpp faults on current < -discharge_oc)
V_MSG, V_FIELD = "bms_battery_voltage", "pack_voltage"
I_MSG, I_FIELD = "bms_battery_current", "current"

CHANNEL = "derived.pack"

# Voltage and current arrive as separate 50 ms messages; they must be close in
# time before it is meaningful to pair them.
PAIR_FRESH_S = 2.0

# A gap longer than this means we lost frames (radio dropout, collector restart).
# Charge that moved during the gap is unknown, so the coulomb counter cannot be
# carried across it — re-anchor to OCV instead of silently inventing continuity.
MAX_GAP_S = 5.0

# Complementary-filter time constants for the OCV correction.
TAU_REST_S = 60.0        # at rest, pull toward OCV within about a minute
TAU_LOAD_S = 3600.0      # under full load, essentially ignore OCV
I_FULL_A = 40.0          # 1C — the current at which the R model stops being trusted


def _tau_for(current_a: float) -> float:
    """Correction time constant as a function of load.

    Linear in |I| between rest and 1C. Above 1C we are past the model's
    characterised range entirely, so the constant simply saturates.
    """
    load = min(1.0, abs(current_a or 0.0) / I_FULL_A)
    return TAU_REST_S + (TAU_LOAD_S - TAU_REST_S) * load


class SocEstimator:
    """Complementary filter over (coulomb count, OCV estimate).

    Feed it time-ordered (ts, pack_voltage, current) samples via step(). Out-of-
    order samples are ignored rather than integrated backwards — store-and-
    forward uploads can deliver a backlog after live data.
    """

    def __init__(self):
        self.soc: float | None = None
        self.last_ts: float | None = None
        self.ah_since_anchor = 0.0
        self.reanchors = 0

    def step(self, ts: float, pack_v: float, current: float) -> dict | None:
        soc_ocv = pack_model.soc_from_loaded(pack_v, current)
        if soc_ocv is None:
            return None

        lo, hi = pack_model.soc_bounds()

        if self.soc is None or self.last_ts is None:
            # First sample: nothing to integrate from, so OCV is all we have.
            # If the car happens to be under load right now this anchor is
            # biased, but the filter walks it back within a minute of rest.
            self.soc = soc_ocv
        else:
            dt = ts - self.last_ts
            if dt <= 0:
                return None                      # out-of-order backlog row
            if dt > MAX_GAP_S:
                self.soc = soc_ocv               # lost history — re-anchor
                self.ah_since_anchor = 0.0
                self.reanchors += 1
            else:
                d_ah = current * dt / 3600.0     # +charge / -discharge
                self.ah_since_anchor += d_ah
                self.soc += d_ah / pack_model.load()["pack"]["capacity_ah"] * 100.0
                alpha = 1.0 - math.exp(-dt / _tau_for(current))
                self.soc += alpha * (soc_ocv - self.soc)

        self.soc = max(lo, min(hi, self.soc))
        self.last_ts = ts
        return {
            "soc_pct": round(self.soc, 2),
            "soc_ocv_pct": round(soc_ocv, 2),
            "wh_remaining": round(pack_model.wh_remaining_at(self.soc), 1),
            "pack_power_w": round(pack_v * current, 1),
            "ah_since_anchor": round(self.ah_since_anchor, 3),
        }


class LiveSoc:
    """Drives a SocEstimator from the live ingest stream.

    Voltage and current are separate messages, so this holds the newest of each
    and steps whenever both are present and mutually fresh. State is per session:
    a new collector launch means the car may have been driven or charged in
    between, so the coulomb history is void and the estimator restarts.
    """

    def __init__(self):
        self.session: str | None = None
        self.est = SocEstimator()
        self._v: tuple[float, float] | None = None      # (ts, volts)
        self._i: tuple[float, float] | None = None      # (ts, amps)

    def observe(self, event: dict) -> dict | None:
        mt = event.get("msg_type")
        if mt not in (V_MSG, I_MSG):
            return None
        fields = event.get("fields") or {}
        ts = event.get("ts_utc")
        if ts is None:
            return None

        suid = event.get("session_uuid")
        if suid and suid != self.session:
            self.session, self.est = suid, SocEstimator()
            self._v = self._i = None

        if mt == V_MSG:
            v = fields.get(V_FIELD)
            if v is None:
                return None
            self._v = (ts, v)
        else:
            i = fields.get(I_FIELD)
            if i is None:
                return None
            self._i = (ts, i)

        if self._v is None or self._i is None:
            return None
        if abs(self._v[0] - self._i[0]) > PAIR_FRESH_S:
            return None
        return self.est.step(max(self._v[0], self._i[0]), self._v[1], self._i[1])


def _merged_samples(sessions) -> list[tuple[float, float, float]]:
    """Time-ordered (ts, pack_voltage, current) for a session/run.

    Voltage and current live in different rows, so both series are pulled and
    merged, carrying the most recent value of each forward. A sample is only
    emitted once both sides have been seen and are mutually fresh.
    """
    cap = settings.HISTORY_SCAN_CAP
    vs = db.history(sessions, V_MSG, None, None, cap)
    cs = db.history(sessions, I_MSG, None, None, cap)

    stream = ([(r["ts_utc"], "v", (r["fields"] or {}).get(V_FIELD)) for r in vs] +
              [(r["ts_utc"], "i", (r["fields"] or {}).get(I_FIELD)) for r in cs])
    stream.sort(key=lambda x: x[0])

    out: list[tuple[float, float, float]] = []
    v = i = None
    for ts, kind, val in stream:
        if val is None:
            continue
        if kind == "v":
            v = (ts, val)
        else:
            i = (ts, val)
        if v and i and abs(v[0] - i[0]) <= PAIR_FRESH_S:
            out.append((max(v[0], i[0]), v[1], i[1]))
    return out


# Replay scrubs backwards as freely as forwards, and the fused estimate is
# path-dependent, so it cannot be reconstructed from a lookback window the way
# raw channels can. Instead the whole session is run through the estimator once
# and cached as a track; replay then binary-searches it. Keyed by session scope
# and invalidated when the session grows.
_track_cache: dict[tuple, tuple[float, list, SocEstimator]] = {}
_TRACK_CACHE_MAX = 8


def _run(sessions) -> tuple[list[dict], SocEstimator]:
    """Estimator track + final state for a session/run, cached on last_seen."""
    key = tuple(sessions) if sessions else ("__all__",)
    stamp = db.time_bounds(sessions, None, None, None)[1] or 0.0

    hit = _track_cache.get(key)
    if hit and hit[0] == stamp:
        return hit[1], hit[2]

    est = SocEstimator()
    track = []
    for ts, v, i in _merged_samples(sessions):
        out = est.step(ts, v, i)
        if out is not None:
            track.append({"ts_utc": ts, "fields": out})

    if len(_track_cache) >= _TRACK_CACHE_MAX:
        _track_cache.pop(next(iter(_track_cache)))
    _track_cache[key] = (stamp, track, est)
    return track, est


def soc_track(sessions) -> list[dict]:
    """Run the estimator across a whole session/run: [{ts_utc, fields}, ...]."""
    return _run(sessions)[0]


def seed_live(live: "LiveSoc", session: str) -> dict | None:
    """Warm-start the live estimator from stored history for `session`.

    Without this a server restart would re-anchor state of charge to a single
    OCV reading — under load that reads low by several points and then takes a
    minute of rest to walk back. Replaying the session's own history instead
    means the live number picks up exactly where it left off, and matches what
    replay shows for the same instant.
    """
    track, est = _run([session])
    if not track:
        return None
    live.session, live.est = session, est
    return {"channel": CHANNEL, "msg_type": CHANNEL,
            "ts_utc": track[-1]["ts_utc"], "fields": track[-1]["fields"]}


def soc_at(sessions, t: float) -> dict | None:
    """The estimator's state at replay instant `t` (newest sample at or before)."""
    track = soc_track(sessions)
    if not track:
        return None
    lo, hi = 0, len(track)
    while lo < hi:
        mid = (lo + hi) // 2
        if track[mid]["ts_utc"] <= t:
            lo = mid + 1
        else:
            hi = mid
    if lo == 0:
        return None
    row = track[lo - 1]
    return {"channel": CHANNEL, "msg_type": CHANNEL,
            "ts_utc": row["ts_utc"], "fields": row["fields"]}
