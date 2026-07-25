# pack_model.py — the battery model behind the state-of-charge estimate.
#
# Reads shared/pack_model.json (distilled from the Voltt simulation by
# shared/build_pack_model.py) and exposes the two lookups derived.py needs:
# open-circuit voltage <-> SOC, and internal resistance vs SOC.
#
# Why a model at all: the collector does not decode bms_pack_status, so the BMS's
# own `soc` never reaches this server. Every state-of-charge number the dashboard
# shows is inferred here, from pack voltage and current alone.
#
# Read as data, exactly like the CAN catalog — this module imports nothing from
# collector/ and knows nothing about CAN.

import functools
import json

import settings


@functools.lru_cache(maxsize=1)
def load() -> dict:
    with open(settings.PACK_MODEL_PATH) as f:
        return json.load(f)


def _interp(x: float, xs: list, ys: list) -> float:
    """Linear interpolation on an ascending-x table, clamped at both ends."""
    if x <= xs[0]:
        return ys[0]
    if x >= xs[-1]:
        return ys[-1]
    lo, hi = 0, len(xs) - 1
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if xs[mid] <= x:
            lo = mid
        else:
            hi = mid
    span = xs[hi] - xs[lo]
    if span == 0:
        return ys[lo]
    return ys[lo] + (x - xs[lo]) / span * (ys[hi] - ys[lo])


def soc_bounds() -> tuple[float, float]:
    """The SOC range the model actually covers. The simulation stops at the BMS
    undervoltage cutoff, so the low end is ~4.8%, not 0 — below that we are
    extrapolating and say so rather than inventing a number."""
    grid = load()["soc_pct"]
    return grid[0], grid[-1]


def ocv_at(soc_pct: float) -> float:
    """Open-circuit voltage per cell at a given SOC."""
    m = load()
    return _interp(soc_pct, m["soc_pct"], m["ocv_cell_v"])


def resistance_at(soc_pct: float) -> float:
    """Internal resistance per cell (ohms) at a given SOC.

    Characterised at 1C only. The BMS permits 58 A discharge (1.45C), so real
    sag at high draw exceeds this table — which is exactly why derived.py leans
    on coulomb counting under load and only trusts OCV near rest.
    """
    m = load()
    return _interp(soc_pct, m["soc_pct"], m["r_cell_ohm"])


def wh_remaining_at(soc_pct: float) -> float:
    """Energy left to the BMS undervoltage cutoff, in Wh.

    Deliberately a curve rather than `soc x capacity`: energy remaining is
    strongly non-linear in SOC (at 20% SOC only ~13% of the pack's Wh are left,
    because voltage has sagged), so the linear form overpromises range badly.
    """
    m = load()
    return _interp(soc_pct, m["soc_pct"], m["wh_remaining"])


def soc_from_ocv(ocv_cell_v: float) -> float:
    """Invert the OCV curve: cell open-circuit voltage -> SOC percent.

    OCV is monotonic in SOC (asserted when the model is built), so the same
    table serves both directions — read with voltage as the independent axis.
    """
    m = load()
    return _interp(ocv_cell_v, m["ocv_cell_v"], m["soc_pct"])


_BISECT_STEPS = 40      # 95-point SOC range -> ~1e-10 %, i.e. exact for our purposes


def soc_from_loaded(pack_voltage_v: float, pack_current_a: float) -> float | None:
    """Instantaneous SOC from a pack voltage/current pair, corrected for IR sag.

    `pack_current_a` follows the BMS convention: NEGATIVE is discharge
    (DistributedBMSPrimaryV2 faults on `battery_current_A_ < -discharge_oc`).
    A cell is modelled as an ideal source OCV(soc) behind a resistance R(soc):

        V_terminal = OCV(soc) + I*R(soc)

    so on discharge (I<0) the terminal reading sits *below* the true OCV, and
    reading SOC straight off the sagged voltage understates charge.

    Solving for SOC means finding the root of

        h(soc) = OCV(soc) + I*R(soc) - V_terminal

    which is NOT safe to do by fixed-point iteration (`soc <- soc_from_ocv(V -
    I*R(soc))`), even though that reads more naturally. Below ~15% SOC the R
    curve turns sharply upward, and the iteration's contraction factor

        |dSOC/dOCV * I * dR/dSOC|

    exceeds 1 there — at 10% SOC and 58 A it is ~1.24, so the iteration
    oscillates and settles on the wrong answer (~7.4% for a true 10%). That is
    the worst possible place to be wrong by 2.6 points.

    Bisection instead: h is strictly increasing in SOC across the entire
    operating envelope (min dh/dsoc = +0.0028 over SOC x [-58 A, +24 A],
    because the OCV slope dominates the R slope everywhere), so a root is
    guaranteed and bisection always finds it. Costs ~40 interpolations at a few
    Hz — free at this rate.
    """
    m = load()
    series, parallel = m["pack"]["series"], m["pack"]["parallel"]
    if not pack_voltage_v or pack_voltage_v <= 0:
        return None

    v_cell = pack_voltage_v / series
    i_cell = (pack_current_a or 0.0) / parallel

    lo, hi = soc_bounds()
    for _ in range(_BISECT_STEPS):
        mid = (lo + hi) / 2
        if ocv_at(mid) + i_cell * resistance_at(mid) < v_cell:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2
