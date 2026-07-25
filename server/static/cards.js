/* cards.js — declarative subsystem cards, mirroring the desktop GUI (gui.py).
 *
 * The layout is DATA: CARDS below describes each subsystem panel the way
 * gui.py's _make_rowN() do — grouped rows, derived values (Power = V×A, Total
 * Solar = Σ MPPT output power), and status coloring. buildCards(container)
 * builds the DOM skeleton ONCE and returns an updater; the updater is driven
 * every tick with a channel->item state map. The SAME renderer serves the live
 * view (state = `latest`) and history replay (state = /api/replay channels).
 *
 * Value semantics (confirmed against collector/app/upload_map.py): uploaded
 * values are already engineering units under catalog field NAMES — e.g.
 * `pack_voltage`/`high_cell_mv` are Volts (the "mv" is the wire encoding, not
 * the value), temps are °C, GPS `speed` is knots. So transforms are identity
 * except knots→mph. Do NOT apply catalog scale/unit here.
 */

const KNOTS_TO_MPH = 1.15078;
const TRANSFORMS = {
  identity: (v) => v,
  knotsToMph: (v) => v * KNOTS_TO_MPH,
  maToA: (v) => v / 1000,               // supplemental-battery current is reported in mA
};

// value formatters matching gui.py (_refresh: f1/f0/f3)
const FMT = {
  1: (v) => v.toFixed(1),
  0: (v) => v.toFixed(0),
  3: (v) => v.toFixed(3),
  int: (v) => String(Math.round(v)),
  thou: (v) => Math.round(v).toLocaleString(),   // thousands-separated counts
};

// MPPT index 1/2/3 -> physical position, displayed in order Front, Middle, Back
// (gui.py: MPPT_NAMES={1:Front,2:Back,3:Middle}, display order (1,3,2)).
const MPPT_NAMES = { 1: "Front", 2: "Back", 3: "Middle" };
const MPPT_ORDER = [1, 3, 2];

// -------------------------------------------------------------------- card model
const CARDS = [
  { id: "main_batt", title: "Main Battery", accent: "blue", rows: [
    { label: "Voltage", ch: "bms_battery_voltage", field: "pack_voltage", unit: "V", fmt: 1, color: "blue" },
    { label: "Current", ch: "bms_battery_current", field: "current", unit: "A", fmt: 1, color: "blue" },
    { label: "Power", derived: "power", a: ["bms_battery_voltage", "pack_voltage"],
      b: ["bms_battery_current", "current"], unit: "W", fmt: 0, color: "blue" },
    { hr: true },
    { label: "High Cell Voltage", ch: "bms_battery_voltage", field: "high_cell_mv", unit: "V", fmt: 3, color: "green" },
    { label: "Low Cell Voltage", ch: "bms_battery_voltage", field: "low_cell_mv", unit: "V", fmt: 3, color: "amber" },
    { label: "High Cell Temp", ch: "bms_battery_temperature", field: "high_temp", unit: "°C", fmt: 1, color: "amber" },
    { label: "Average Cell Temp", ch: "bms_battery_temperature", field: "avg_temp", unit: "°C", fmt: 1 },
    { hr: true },
    { label: "Supp Voltage", ch: "supp_batt_frame", field: "supp_batt_voltage_mv", unit: "V", fmt: 1, color: "teal" },
    { label: "Supp Current", ch: "supp_batt_frame", field: "supp_batt_current", unit: "A", fmt: 1, color: "teal", transform: "maToA" },
    { label: "Supp Power", derived: "power", a: ["supp_batt_frame", "supp_batt_voltage_mv"],
      b: ["supp_batt_frame", "supp_batt_current"], scale: 0.001, unit: "W", fmt: 1, color: "teal" },
  ]},

  { id: "motor", title: "Motor Controller", accent: "amber", rows: [
    { label: "Voltage", ch: "mitsuba_frame0", field: "battery_voltage", unit: "V", fmt: 1, color: "amber" },
    { label: "Current", ch: "mitsuba_frame0", field: "battery_current", unit: "A", fmt: 1, color: "amber",
      signFrom: "battery_current_minus" },
    { label: "Power", derived: "power", a: ["mitsuba_frame0", "battery_voltage"],
      b: ["mitsuba_frame0", "battery_current"], unit: "W", fmt: 0, color: "amber" },
    { hr: true },
    { label: "Motor RPM", ch: "mitsuba_frame0", field: "motor_rpm", unit: "", fmt: "thou" },
    { label: "Motor Current", ch: "mitsuba_frame0", field: "motor_current", unit: "A", fmt: 1 },
    { label: "FET Temp", ch: "mitsuba_frame0", field: "fet_temp", unit: "°C", fmt: 0 },
    { label: "Errors", ch: "mitsuba_frame2", field: "error_flags", faults: "MitsubaError" },
  ]},

  { id: "speed", title: "Speed", accent: "cyan", big: true, bigUnit: "MPH",
    value: { ch: "GpsPacket", field: "speed", transform: "knotsToMph", fmt: 0, color: "blue" } },

  // State of charge is INFERRED, not measured — the collector doesn't decode
  // bms_pack_status, so the BMS's own soc never reaches us. server/derived.py
  // fuses a coulomb count with an OCV lookup against the pack model; both the
  // fused answer and the raw OCV estimate are shown, because their divergence
  // is the health signal (a pack whose real capacity has aged below the
  // modelled 38.078 Ah drifts the two apart).
  { id: "soc", title: "State of Charge", accent: "green", big: true, bigUnit: "% CHARGE",
    value: { ch: "derived.pack", field: "soc_pct", fmt: 1, color: "green" }, rows: [
      { label: "OCV Estimate", ch: "derived.pack", field: "soc_ocv_pct", unit: "%", fmt: 1, color: "teal" },
      { label: "Energy Left", ch: "derived.pack", field: "wh_remaining", unit: "Wh", fmt: 0, color: "green" },
    ]},

  // MPPT Front / Middle / Back (indices 1,3,2)
  ...MPPT_ORDER.map((n) => ({
    id: `mppt${n}`, title: `MPPT ${MPPT_NAMES[n]}`, accent: "yellow", ch: `MpptPacket:${n}`, rows: [
      { label: "Input Voltage", field: "input_voltage", unit: "V", fmt: 1, color: "yellow" },
      { label: "Input Current", field: "input_current", unit: "A", fmt: 1, color: "yellow" },
      { hr: true },
      { label: "Output Voltage", field: "output_voltage", unit: "V", fmt: 1, color: "yellow" },
      { label: "Output Current", field: "output_current", unit: "A", fmt: 1, color: "yellow" },
      { label: "Output Power", derived: "power", a: "output_voltage", b: "output_current",
        unit: "W", fmt: 0, color: "yellow" },
    ],
  })),

  { id: "solar", title: "Total Solar Array", accent: "green", big: true, bigUnit: "WATTS",
    value: { derived: "total_solar", channels: MPPT_ORDER.map((n) => `MpptPacket:${n}`),
      product: ["output_voltage", "output_current"], fmt: 0, color: "green" } },

  { id: "map", title: "Position", accent: "teal", map: true },

  { id: "contactors", title: "Contactors & Status", accent: "green", rows: [
    { label: "Array Contactor", ch: "rearvcu_statuses_frame", field: "array_contactors",
      map: { 0: ["OPEN", "fault"], 1: ["PRECHARGE", "warn"], 2: ["CLOSED", "ok"] } },
    { label: "BMS Contactor", ch: "bms_status", field: "contactors_state",
      map: { 0: ["OPEN", "fault"], 1: ["CLOSED", "ok"] } },
    { label: "Fault Status", ch: "kill_frame", field: "killed_status",
      map: { 0: ["OKAY", "ok"], 1: ["KILLED", "fault"] } },
    { hr: true },
    { label: "BMS Fault", ch: "bms_status", field: "bms_faults", faults: "BmsFaults" },
  ]},

  { id: "frames", title: "Last Frame Received", accent: "gray", ages: [
    { node: "BMS", channels: ["bms_status", "bms_battery_voltage", "bms_battery_current", "bms_battery_temperature"] },
    { node: "Steering Wheel", channels: ["steering_requests_frame", "steering_requests_frame_2"] },
    { node: "Front VCU", channels: ["tb_frame"] },
    { node: "Rear VCU", channels: ["rearvcu_statuses_frame", "supp_batt_frame"] },
    { node: "GPS", channels: ["GpsPacket"] },
  ]},

  { id: "radio", title: "Radio Link", accent: "cyan", rows: [
    { label: "TX Queue", ch: "RadioStatsPacket", pair: ["queue_used", "queue_capacity"], color: "teal" },
    { label: "Peak Queue", ch: "RadioStatsPacket", pair: ["queue_high_water", "queue_capacity"], color: "teal" },
    { label: "Dropped", ch: "RadioStatsPacket", field: "dropped", fmt: "thou", colorRule: "zeroGood" },
    { hr: true },
    { label: "Sent", ch: "RadioStatsPacket", field: "sent", fmt: "thou" },
    { label: "Send Gap", ch: "RadioStatsPacket", field: "mean_interval_ms", unit: "ms", fmt: "int", colorRule: "zeroBad" },
  ]},
];

// ---------------------------------------------------------------- state readers
function itemOf(state, ch) { return state[ch] || null; }

function rawVal(state, ch, field) {
  const it = state[ch];
  if (!it || !it.fields) return null;
  const v = it.fields[field];
  return (v == null || (typeof v === "number" && Number.isNaN(v))) ? null : v;
}

// A channel is stale (=> N/A) when its newest sample is older than STALE_DATA_S
// (live only). In replay, values are "current as of t" — never wall-clock-stale.
function channelStale(state, ch, nowS, mode) {
  if (mode === "replay") return false;
  const it = state[ch];
  if (!it) return true;
  return nowS - (it.ts_utc || 0) > STALE_DATA_S;
}

// Channels a row/value descriptor depends on (for dimming).
function govChannels(card, d) {
  if (d.derived === "power") return [pairRef(d.a, card.ch)[0], pairRef(d.b, card.ch)[0]];
  if (d.derived === "total_solar") return d.channels.slice();
  return [d.ch || card.ch];
}

// True once a still-shown value is older than STALE_DIM_S (but not yet N/A) —
// the "grey out" band between 1 and 5 minutes. Never dims in replay.
function anyDim(state, channels, nowS, mode) {
  if (mode === "replay") return false;
  return channels.some((ch) => {
    const it = state[ch];
    if (!it) return false;
    const age = nowS - (it.ts_utc || 0);
    return age > STALE_DIM_S && age <= STALE_DATA_S;
  });
}

// ----------------------------------------------------------------- builders
function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text != null) e.textContent = text;
  return e;
}

function buildCard(card) {
  // Per-card CLASS (not id) so the same model can be instantiated more than once
  // — the live dashboard and the history replay panel both render these.
  const box = el("section", `card card--${card.accent} card-${card.id}`);
  box.appendChild(el("h2", null, card.title.toUpperCase()));

  if (card.map) {                       // the Leaflet map lives here, never re-rendered
    const m = el("div", "card-mapbox");
    m.id = "map";
    box.appendChild(m);
    return { box, update: () => {} };   // managed by live.js/history.js, not the tick
  }

  if (card.big) return buildBigCard(card, box);
  if (card.ages) return buildFramesCard(card, box);
  return buildRowsCard(card, box);
}

function buildRowsCard(card, box) {
  const updaters = appendRows(card, box);      // DOM built ONCE, at build time
  return { box, update: (s, n, m) => updaters.forEach((u) => u(s, n, m)) };
}

// Build `card.rows` into `box` and return their updaters. Shared by the plain
// rows card and by big-numeral cards that carry supporting rows underneath.
function appendRows(card, box) {
  const updaters = [];
  for (const row of card.rows) {
    if (row.hr) { box.appendChild(el("hr", "card-hr")); continue; }
    const line = el("div", "card-row");
    line.appendChild(el("span", "row-label", row.label));

    if (row.faults) {                   // fault-bit badges
      const wrap = el("div", "row-faults");
      line.appendChild(wrap);
      box.appendChild(line);
      updaters.push((state, nowS, mode) => updateFaults(wrap, card, row, state, nowS, mode));
      continue;
    }

    // value + unit share one right-aligned group so numbers line up in a column.
    // The unit slot is always present (fixed width) so the number's right edge is
    // the same across every row in the card, regardless of unit width.
    const wrap = el("span", "row-valwrap");
    const val = el("span", "row-value v-na", "N/A");
    const unit = el("small", "row-unit", row.unit || "");
    // Hidden until the first update, so a card that never receives one reads
    // "N/A" rather than a bare unit with an empty value beside it.
    unit.style.visibility = "hidden";
    wrap.appendChild(val);
    wrap.appendChild(unit);
    line.appendChild(wrap);
    box.appendChild(line);
    updaters.push((state, nowS, mode) => updateRow(val, unit, card, row, state, nowS, mode));
  }
  return updaters;
}

function buildBigCard(card, box) {
  const big = el("div", `card-big v-${card.value.color || "text"}`, "—");
  const unit = el("div", "card-big-unit", card.bigUnit);
  box.appendChild(big);
  box.appendChild(unit);
  // A big card may also carry supporting rows (State of Charge shows the
  // independent OCV estimate and remaining energy beneath the headline number).
  const rowUpdaters = card.rows ? appendRows(card, box) : [];
  return { box, update: (state, nowS, mode) => {
    const v = computeValue(card.value, card, state, nowS, mode);
    big.textContent = v == null ? "N/A" : (FMT[card.value.fmt] || FMT[0])(v);
    big.classList.toggle("v-na", v == null);
    big.classList.toggle("dim", v != null && anyDim(state, govChannels(card, card.value), nowS, mode));
    rowUpdaters.forEach((u) => u(state, nowS, mode));
  }};
}

function buildFramesCard(card, box) {
  const rows = card.ages.map((a) => {
    const line = el("div", "card-row");
    line.appendChild(el("span", "row-label", a.node));
    const val = el("span", "row-value", "never");
    line.appendChild(val);
    box.appendChild(line);
    return { a, val };
  });
  return { box, update: (state, nowS, mode) => {
    for (const { a, val } of rows) {
      let newest = null;
      for (const ch of a.channels) {
        const it = state[ch];
        if (it && (newest == null || it.ts_utc > newest)) newest = it.ts_utc;
      }
      applyAge(val, newest == null ? null : nowS - newest);
    }
  }};
}

// ----------------------------------------------------------------- value compute
// Resolve a row/value descriptor to a number (or null). Handles direct fields,
// per-subsystem power (a×b), total-solar sum, and cell delta.
function computeValue(desc, card, state, nowS, mode) {
  const cardCh = card.ch;
  if (desc.derived === "power") {
    const a = pairRef(desc.a, cardCh), b = pairRef(desc.b, cardCh);
    const av = rawVal(state, a[0], a[1]), bv = rawVal(state, b[0], b[1]);
    if (av == null || bv == null) return null;
    if (channelStale(state, a[0], nowS, mode) || channelStale(state, b[0], nowS, mode)) return null;
    return av * bv * (desc.scale || 1);
  }
  if (desc.derived === "total_solar") {
    let sum = 0, any = false;
    for (const ch of desc.channels) {
      const v = rawVal(state, ch, desc.product[0]), i = rawVal(state, ch, desc.product[1]);
      if (v == null || i == null || channelStale(state, ch, nowS, mode)) continue;
      sum += v * i; any = true;
    }
    return any ? sum : null;
  }
  if (desc.derived === "delta") {
    const ch = desc.ch || cardCh;
    const hi = rawVal(state, ch, desc.field), lo = rawVal(state, ch, desc.minus);
    return (hi == null || lo == null) ? null : hi - lo;
  }
  // direct field
  const ch = desc.ch || cardCh;
  if (channelStale(state, ch, nowS, mode)) return null;
  let v = rawVal(state, ch, desc.field);
  if (v == null) return null;
  if (desc.signFrom && rawVal(state, ch, desc.signFrom)) v = -Math.abs(v);
  const tf = TRANSFORMS[desc.transform || "identity"];
  return tf(v);
}

function pairRef(ref, cardCh) {
  return Array.isArray(ref) ? ref : [cardCh, ref];   // [channel, field] or just field
}

// ----------------------------------------------------------------- row updaters
function updateRow(valEl, unitEl, card, row, state, nowS, mode) {
  const dim = anyDim(state, govChannels(card, row), nowS, mode);
  const na = () => setVal(valEl, unitEl, "N/A", "na", false, false);
  const show = (text, kind) => setVal(valEl, unitEl, text, kind, true, dim);

  // enum map (e.g. contactor / kill status) — never has a unit
  if (row.map) {
    const ch = row.ch || card.ch;
    const raw = rawVal(state, ch, row.field);
    const num = typeof raw === "boolean" ? (raw ? 1 : 0) : raw;
    const entry = (!channelStale(state, ch, nowS, mode) && num != null) ? row.map[num] : null;
    return entry ? show(entry[0], entry[1]) : na();
  }

  // paired "used / capacity" display
  if (row.pair) {
    const ch = row.ch || card.ch;
    const a = rawVal(state, ch, row.pair[0]), b = rawVal(state, ch, row.pair[1]);
    if (channelStale(state, ch, nowS, mode) || a == null) return na();
    return show(b == null ? String(a) : `${a} / ${b}`, row.color || "text");
  }

  const v = computeValue(row, card, state, nowS, mode);
  if (v == null) return na();
  const text = (FMT[row.fmt] || FMT[1])(v);
  if (row.colorRule === "zeroGood") return show(text, v > 0 ? "fault" : "ok");
  if (row.colorRule === "zeroBad") return show(text, v === 0 ? "fault" : (row.color || "text"));
  return show(text, row.color || "text");
}

// Set a row's value text/color/unit and toggle the whole row's greyed-out state.
function setVal(valEl, unitEl, text, kind, showUnit, dim) {
  valEl.textContent = text;
  setValClass(valEl, kind);
  if (unitEl) unitEl.style.visibility = (showUnit && unitEl.textContent) ? "visible" : "hidden";
  const rowEl = valEl.closest(".card-row");
  if (rowEl) rowEl.classList.toggle("row-dim", !!dim);
}

function updateFaults(wrap, card, row, state, nowS, mode) {
  wrap.innerHTML = "";
  const ch = row.ch || card.ch;
  const stale = channelStale(state, ch, nowS, mode);
  const raw = rawVal(state, ch, row.field);
  const rowEl = wrap.closest(".card-row");
  if (stale || raw == null || typeof raw !== "number") {
    wrap.appendChild(badge("N/A", "na"));
    if (rowEl) rowEl.classList.toggle("row-dim", false);
    return;
  }
  const names = decodeFaults(raw, row.faults);
  if (!names.length) wrap.appendChild(badge("OK", "ok"));
  else for (const n of names) wrap.appendChild(badge(n, "fault"));
  if (rowEl) rowEl.classList.toggle("row-dim", anyDim(state, [ch], nowS, mode));
}

function badge(text, kind) {
  const b = el("span", `badge badge--${kind}`, text);
  return b;
}

function setValClass(valEl, kind) {
  valEl.className = "row-value v-" + kind;
}

// Last-frame age → text + color, matching gui.py's thresholds.
function applyAge(valEl, age) {
  if (age == null) { valEl.textContent = "never"; setValClass(valEl, "fault"); return; }
  let text, kind;
  if (age < 1.0) { text = "live"; kind = "ok"; }
  else if (age < 5.0) { text = age.toFixed(1) + "s ago"; kind = "ok"; }
  else if (age < 30.0) { text = age.toFixed(1) + "s ago"; kind = "stale"; }
  else { text = fmtDur(age) + " ago"; kind = "fault"; }   // 1h 05m 11s ago
  valEl.textContent = text;
  setValClass(valEl, kind);
}

// ----------------------------------------------------------------- public API
// Build all cards into `container` once; returns { update(state, nowS, mode) }.
// includeMap:false skips the GPS map card — the History replay panel reuses this
// model but already has its own map (#history-map), and only one element may
// own the id "map".
function buildCards(container, { includeMap = true } = {}) {
  container.innerHTML = "";
  const built = CARDS.filter((c) => includeMap || !c.map).map((c) => {
    const b = buildCard(c);
    container.appendChild(b.box);
    return b;
  });
  return { update: (state, nowS, mode) => built.forEach((b) => b.update(state, nowS, mode)) };
}
