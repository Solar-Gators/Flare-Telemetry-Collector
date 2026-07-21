/* history.js — the History view: pick a past session, a time range, and review
 * decimated trend charts + the GPS track. Owns its own Leaflet map and Chart.js
 * instances (separate from the live view) and is lazily initialised the first
 * time the view is shown, so charts/map size correctly (not while hidden).
 *
 * Phase 3 adds a replay scrubber here; Phase 4 adds a data table + CSV export.
 */

// palette (matches the live cards)
const CH = { blue: "#4fc3f7", green: "#69f0ae", amber: "#ffcc02", teal: "#26c6da",
             red: "#ff5252", purple: "#ce93d8", pink: "#f778ba", orange: "#ffb020" };
const MPH = 1.15078;

// Curated trend charts. Series reference catalog message + field names; `compute`
// derives a value from a row's fields (same-message), and `derived` metrics join
// across message types (pack power) or aggregate a grouped type (total solar).
const METRICS = [
  { id: "packV", title: "Pack Voltage", unit: "V", series: [
    { type: "bms_battery_voltage", field: "pack_voltage", label: "pack", color: CH.blue, fill: true } ] },
  { id: "packA", title: "Pack Current", unit: "A", series: [
    { type: "bms_battery_current", field: "current", label: "pack", color: CH.green, fill: true } ] },
  { id: "packW", title: "Pack Power", unit: "W", derived: "crossProduct",
    inputs: { a: { type: "bms_battery_voltage", field: "pack_voltage" },
              b: { type: "bms_battery_current", field: "current" } },
    series: [{ label: "power", color: CH.orange, fill: true }] },
  { id: "cellV", title: "Cell Voltage", unit: "V", series: [
    { type: "bms_battery_voltage", field: "high_cell_mv", label: "high", color: CH.green },
    { type: "bms_battery_voltage", field: "low_cell_mv", label: "low", color: CH.amber } ] },
  { id: "cellDelta", title: "Cell Delta", unit: "mV", series: [
    { type: "bms_battery_voltage", label: "Δ", color: CH.purple, fill: true,
      compute: (f) => (f.high_cell_mv - f.low_cell_mv) * 1000 } ] },
  { id: "temp", title: "Cell Temperature", unit: "°C", series: [
    { type: "bms_battery_temperature", field: "high_temp", label: "high", color: CH.red },
    { type: "bms_battery_temperature", field: "avg_temp", label: "avg", color: CH.amber } ] },
  { id: "solarW", title: "Total Solar Power", unit: "W", derived: "totalSolar",
    series: [{ label: "array", color: CH.green, fill: true }] },
  { id: "mpptW", title: "MPPT Power", unit: "W", group: "mppt_index", series: [
    { type: "MpptPacket", label: "Front", color: CH.orange, mppt: 1, compute: (f) => f.output_voltage * f.output_current },
    { type: "MpptPacket", label: "Middle", color: CH.blue, mppt: 3, compute: (f) => f.output_voltage * f.output_current },
    { type: "MpptPacket", label: "Back", color: CH.green, mppt: 2, compute: (f) => f.output_voltage * f.output_current } ] },
  { id: "rpm", title: "Motor RPM", unit: "RPM", series: [
    { type: "mitsuba_frame0", field: "motor_rpm", label: "rpm", color: CH.teal, fill: true } ] },
  { id: "motW", title: "Motor Power", unit: "W", series: [
    { type: "mitsuba_frame0", label: "power", color: CH.amber, fill: true,
      compute: (f) => f.battery_voltage * f.battery_current * (f.battery_current_minus ? -1 : 1) } ] },
  { id: "speed", title: "Speed", unit: "mph", series: [
    { type: "GpsPacket", label: "gps", color: CH.blue, fill: true, compute: (f) => f.speed * MPH } ] },
];

function hexA(hex, a) {
  const n = parseInt(hex.slice(1), 16);
  return `rgba(${(n >> 16) & 255}, ${(n >> 8) & 255}, ${n & 255}, ${a})`;
}

const HIST = {
  inited: false,
  map: null,
  charts: {},
  runs: [],              // [{id, first_seen, last_seen, sessions:[uuid], gaps:[[a,b]]}]
  run: null,             // selected run (several sessions merged for display)
  bucket: null,          // current downsample bucket, drives gap detection
  range: "full",         // "full" | seconds (number) — trailing window off last_seen
  override: null,        // {since, until} from a drag-zoom, or null
  loading: false,        // guard so programmatic scale changes don't re-trigger zoom
  maxPoints: 1500,
  // replay
  trackPoints: [],       // loaded GPS track, used to move the marker while scrubbing
  replayCards: null,     // second cards.js instance, rendered in "replay" mode
  replayT: null,         // instant currently being replayed (epoch seconds)
  replayPending: false,  // a /api/replay request is in flight
  replayQueued: null,    // newest scrub position to fetch once the current one lands
  replayTimer: null,     // debounce handle
};

// Vertical dashed line marking the replayed instant on every chart, with the
// timestamp in a small chip riding on top of the cursor.
const timeCursorPlugin = {
  id: "timeCursor",
  afterDatasetsDraw(chart) {
    const t = HIST.replayT;
    if (t == null) return;
    const x = chart.scales.x;
    if (!x || t < x.min || t > x.max) return;
    const px = x.getPixelForValue(t);
    const { top, bottom, left, right } = chart.chartArea;
    const ctx = chart.ctx;

    ctx.save();
    // the cursor line
    ctx.beginPath();
    ctx.strokeStyle = "#e8edf2";
    ctx.lineWidth = 1;
    ctx.setLineDash([4, 3]);
    ctx.moveTo(px, top);
    ctx.lineTo(px, bottom);
    ctx.stroke();
    ctx.setLineDash([]);

    // timestamp chip, clamped to stay inside the plot area
    const label = new Date(t * 1000).toLocaleTimeString([], { hour12: false });
    ctx.font = '600 11px "Courier New", monospace';
    const w = ctx.measureText(label).width + 12;
    const h = 18;
    const bx = Math.min(Math.max(px - w / 2, left), right - w);
    const by = top + 2;
    ctx.fillStyle = "#0d1117";
    ctx.strokeStyle = "#4fc3f7";
    ctx.lineWidth = 1;
    if (ctx.roundRect) { ctx.beginPath(); ctx.roundRect(bx, by, w, h, 4); ctx.fill(); ctx.stroke(); }
    else { ctx.fillRect(bx, by, w, h); ctx.strokeRect(bx, by, w, h); }
    ctx.fillStyle = "#e8edf2";
    ctx.textAlign = "center";
    ctx.textBaseline = "middle";
    ctx.fillText(label, bx + w / 2, by + h / 2 + 0.5);
    ctx.restore();
  },
};

// Coalesce cursor redraws to one per animation frame — dragging the scrubber
// fires input events far faster than 11 canvases can usefully repaint.
let cursorRaf = null;
function renderCursors() {
  if (cursorRaf) return;
  cursorRaf = requestAnimationFrame(() => {
    cursorRaf = null;
    for (const id in HIST.charts) HIST.charts[id].render();
  });
}

// ------------------------------------------------------------------- sessions
async function loadSessions() {
  const res = await fetch("/api/sessions");
  const data = res.ok ? await res.json() : {};
  // Sessions are grouped server-side into "runs" — a collector restart starts a
  // new session_uuid, so one afternoon is many short sessions. Nothing in the DB
  // is merged; this is purely how they're presented.
  HIST.runs = data.runs || [];
  // Graceful degradation: if the server didn't group runs, treat each session as
  // its own run. Never leave HIST.run null — a null run means no session filter
  // and no time window, which drops queries onto the raw oldest-N path.
  if (!HIST.runs.length && (data.sessions || []).length) {
    HIST.runs = data.sessions.map((s) => ({
      id: s.session_uuid, first_seen: s.first_seen, last_seen: s.last_seen,
      sessions: [s.session_uuid], gaps: [], session_count: 1,
    }));
  }
  liveSession = HIST.runs.length ? HIST.runs[0].sessions : null;
  const sel = $("#hist-session");
  if (sel) {
    sel.innerHTML = "";
    for (const r of HIST.runs) {
      const opt = document.createElement("option");
      const when = new Date(r.first_seen * 1000).toLocaleString();
      const mins = Math.max(1, Math.round((r.last_seen - r.first_seen) / 60));
      const parts = r.session_count > 1 ? ` · ${r.session_count} sessions` : "";
      opt.value = r.id;
      opt.textContent = `${when} · ${mins} min${parts}`;
      sel.appendChild(opt);
    }
    HIST.run = HIST.runs.length ? HIST.runs[0] : null;
  }
}

function sessionBounds() {
  const r = HIST.run;
  return r ? { first: r.first_seen, last: r.last_seen } : null;
}

// The selected run's member sessions, for the API's `sessions=` param.
function runSessionsParam() {
  return HIST.run ? HIST.run.sessions.join(",") : "";
}

// Resolve the current [since, until] from a drag-zoom override, else the preset.
function currentWindow() {
  if (HIST.override) return HIST.override;
  const b = sessionBounds();
  if (!b) return { since: null, until: null };
  if (HIST.range === "full") return { since: b.first, until: b.last };
  return { since: Math.max(b.first, b.last - HIST.range), until: b.last };
}

// -------------------------------------------------------------------- charts
function initHistoryCharts() {
  const host = $("#history-charts");
  host.innerHTML = '<div class="section-title">Trends — drag to zoom a time range</div>';
  for (const metric of METRICS) {
    const card = document.createElement("div");
    card.className = "chart-card";
    card.innerHTML = `<h3>${metric.title} <span class="unit">${metric.unit}</span></h3><canvas id="hc_${metric.id}"></canvas>`;
    host.appendChild(card);
    wireCardClick(card, metric.id);
    const ctx = card.querySelector("canvas").getContext("2d");
    HIST.charts[metric.id] = new Chart(ctx, {
      type: "line",
      plugins: [timeCursorPlugin],
      data: { datasets: metric.series.map((s) => ({
        label: s.label, data: [],
        borderColor: s.color, backgroundColor: s.fill ? hexA(s.color, 0.14) : s.color,
        borderWidth: 1.6, pointRadius: 0, pointHoverRadius: 3, tension: 0.25,
        fill: s.fill ? "origin" : false,
        // Stretches with no recorded data (between merged sessions, or a radio
        // dropout) are drawn dashed and dimmed instead of as a solid line that
        // implies we measured something across the gap.
        segment: {
          borderDash: (ctx) => (isGapSegment(ctx) ? [6, 4] : undefined),
          borderColor: (ctx) => (isGapSegment(ctx) ? hexA(s.color, 0.35) : undefined),
        },
      })) },
      options: {
        animation: false, parsing: false, normalized: true, maintainAspectRatio: false,
        interaction: { mode: "index", intersect: false },
        scales: {
          x: { type: "linear", ticks: { callback: timeTick, maxTicksLimit: 6, color: "#8b98a5", font: { size: 10 } },
               grid: { color: "#1c242e" } },
          y: { ticks: { color: "#8b98a5", font: { size: 10 } }, grid: { color: "#1c242e" },
               title: { display: true, text: metric.unit, color: "#5a7a90", font: { size: 10 } } },
        },
        plugins: {
          legend: { display: metric.series.length > 1, labels: { color: "#8b98a5", boxWidth: 10, boxHeight: 2, font: { size: 11 } } },
          tooltip: {
            backgroundColor: "#0d1117", borderColor: "#243040", borderWidth: 1,
            titleColor: "#8faabb", bodyColor: "#e8edf2", padding: 8,
            callbacks: {
              title: (items) => items.length ? new Date(items[0].parsed.x * 1000).toLocaleTimeString([], { hour12: false }) : "",
              label: (item) => `${item.dataset.label}: ${fmtNum(item.parsed.y)} ${metric.unit}`,
            },
          },
          zoom: {
            zoom: { drag: { enabled: true, backgroundColor: "rgba(79,195,247,.15)", borderColor: "#4fc3f7", borderWidth: 1 },
                    mode: "x", onZoomComplete: ({ chart }) => onChartZoom(chart) },
          },
        },
      },
    });
  }
}

// ---------------------------------------------------------- expanded chart window
// The clicked card is MOVED into the modal (same Chart instance, so data and zoom
// state carry over) and moved back on close, with a placeholder holding its slot
// in the grid.
const MODAL = { open: false, card: null, ph: null, metricId: null };

function openChartModal(cardEl, metricId) {
  if (MODAL.open) return;
  const modal = $("#chart-modal");
  const body = modal.querySelector(".modal-body");
  const ph = document.createElement("div");
  ph.className = "chart-placeholder";
  cardEl.parentNode.insertBefore(ph, cardEl);
  body.appendChild(cardEl);
  modal.hidden = false;
  Object.assign(MODAL, { open: true, card: cardEl, ph, metricId });
  requestAnimationFrame(() => HIST.charts[metricId] && HIST.charts[metricId].resize());
}

function closeChartModal() {
  if (!MODAL.open) return;
  const { card, ph, metricId } = MODAL;
  ph.parentNode.replaceChild(card, ph);
  $("#chart-modal").hidden = true;
  Object.assign(MODAL, { open: false, card: null, ph: null, metricId: null });
  requestAnimationFrame(() => HIST.charts[metricId] && HIST.charts[metricId].resize());
}

function initChartModal() {
  const modal = $("#chart-modal");
  if (!modal) return;
  // click the dimmed backdrop (but not the window itself) to dismiss
  modal.addEventListener("click", (e) => { if (e.target === modal) closeChartModal(); });
  modal.querySelector(".modal-close").addEventListener("click", closeChartModal);
  document.addEventListener("keydown", (e) => { if (e.key === "Escape") closeChartModal(); });
}

// Open on a genuine click — not at the end of a drag-to-zoom gesture.
function wireCardClick(card, metricId) {
  let dx = 0, dy = 0;
  card.addEventListener("mousedown", (e) => { dx = e.clientX; dy = e.clientY; });
  card.addEventListener("click", (e) => {
    if (MODAL.open) return;
    if (Math.abs(e.clientX - dx) > 5 || Math.abs(e.clientY - dy) > 5) return;  // was a drag
    openChartModal(card, metricId);
  });
}

// A segment spans a data gap when its two points are much further apart than the
// normal sample spacing (the downsample bucket).
function gapThreshold() {
  return Math.max((HIST.bucket || 1) * 4, 10);
}
function isGapSegment(ctx) {
  return (ctx.p1.parsed.x - ctx.p0.parsed.x) > gapThreshold();
}

function fmtNum(v) {
  if (typeof v !== "number") return v;
  const a = Math.abs(v);
  return a >= 100 ? v.toFixed(0) : a >= 1 ? v.toFixed(1) : v.toFixed(3);
}

// Fetch every message type the charts need, once, over the current window.
// MpptPacket is fetched grouped by controller so all three survive decimation.
async function fetchHistoryData() {
  const { since, until } = currentWindow();
  const specs = {};   // type -> group|null
  for (const m of METRICS) {
    for (const s of (m.series || [])) if (s.type) specs[s.type] = m.group || specs[s.type] || null;
    if (m.derived === "crossProduct") { specs[m.inputs.a.type] ??= null; specs[m.inputs.b.type] ??= null; }
    if (m.derived === "totalSolar") specs["MpptPacket"] = "mppt_index";
  }
  const cache = {};   // type -> {rows, bucket}
  let downsampled = false, bucket = null, maxCount = 0;
  await Promise.all(Object.entries(specs).map(async ([t, group]) => {
    const q = new URLSearchParams({ type: t, max_points: String(HIST.maxPoints) });
    const ss = runSessionsParam();
    if (ss) q.set("sessions", ss);
    if (since != null) q.set("since", String(since));
    if (until != null) q.set("until", String(until));
    if (group) q.set("group", group);
    const res = await fetch(`/api/history?${q}`);
    const data = res.ok ? await res.json() : { rows: [] };
    cache[t] = { rows: data.rows || [], bucket: data.bucket };
    if (data.downsampled) { downsampled = true; bucket = data.bucket; }
    maxCount = Math.max(maxCount, cache[t].rows.length);
  }));
  return { cache, since, until, downsampled, bucket, maxCount };
}

function bucketKey(ts, bucket) { return bucket ? Math.floor(ts / bucket) : Math.round(ts * 2); }

// Build a metric's datasets from the fetched cache.
function buildDatasets(metric, cache) {
  if (metric.derived === "crossProduct") {
    const A = cache[metric.inputs.a.type] || { rows: [] }, B = cache[metric.inputs.b.type] || { rows: [] };
    const bk = A.bucket || B.bucket;
    const am = new Map(), bm = new Map();
    for (const r of A.rows) if (r.fields) am.set(bucketKey(r.ts_utc, bk), { t: r.ts_utc, v: r.fields[metric.inputs.a.field] });
    for (const r of B.rows) if (r.fields) bm.set(bucketKey(r.ts_utc, bk), r.fields[metric.inputs.b.field]);
    const pts = [];
    for (const [k, a] of am) {
      const b = bm.get(k);
      if (typeof a.v === "number" && typeof b === "number") pts.push({ x: a.t, y: a.v * b });
    }
    pts.sort((p, q) => p.x - q.x);
    return [pts];
  }
  if (metric.derived === "totalSolar") {
    const M = cache["MpptPacket"] || { rows: [], bucket: null };
    const groups = new Map();   // bucketKey -> {t, sum}
    for (const r of M.rows) {
      const f = r.fields; if (!f) continue;
      const p = f.output_voltage * f.output_current;
      if (typeof p !== "number" || Number.isNaN(p)) continue;
      const k = bucketKey(r.ts_utc, M.bucket);
      const g = groups.get(k) || { t: r.ts_utc, sum: 0 };
      g.sum += p; g.t = Math.max(g.t, r.ts_utc); groups.set(k, g);
    }
    const pts = [...groups.values()].map((g) => ({ x: g.t, y: g.sum })).sort((p, q) => p.x - q.x);
    return [pts];
  }
  // normal series (field or compute), one dataset each
  return metric.series.map((s) => {
    const rows = (cache[s.type] || { rows: [] }).rows;
    return rows
      .filter((r) => r.fields && (s.mppt == null || r.fields.mppt_index === s.mppt))
      .map((r) => ({ x: r.ts_utc, y: s.compute ? s.compute(r.fields) : r.fields[s.field] }))
      .filter((p) => typeof p.y === "number" && !Number.isNaN(p.y));
  });
}

async function loadHistoryCharts() {
  HIST.loading = true;
  try {
    const { cache, since, until, downsampled, bucket, maxCount } = await fetchHistoryData();
    HIST.bucket = bucket;          // drives gap detection for dashed segments
    for (const metric of METRICS) {
      const datasets = buildDatasets(metric, cache);
      const chart = HIST.charts[metric.id];
      datasets.forEach((d, i) => { if (chart.data.datasets[i]) chart.data.datasets[i].data = d; });
      if (since != null && until != null) { chart.options.scales.x.min = since; chart.options.scales.x.max = until; }
      chart.update("none");
    }
    const badge = $("#downsample-badge");
    if (badge) {
      badge.textContent = downsampled
        ? `↓ ~${maxCount} pts/series · ${bucket >= 1 ? bucket.toFixed(1) + "s" : (bucket * 1000).toFixed(0) + "ms"} buckets`
        : `${maxCount} raw pts/series`;
    }
    updateResetBtn();
    syncScrubber();      // every path that changes the window re-anchors the scrubber
  } finally {
    // release the zoom guard on the next frame, after Chart.js settles the scales
    setTimeout(() => { HIST.loading = false; }, 0);
  }
}

// Drag-zoom on any chart → adopt that time window for ALL charts (re-queried at
// finer resolution) so the whole review stays synchronized.
function onChartZoom(chart) {
  if (HIST.loading) return;
  const x = chart.scales.x;
  if (!(x.max - x.min > 0.5)) return;
  HIST.override = { since: x.min, until: x.max };
  document.querySelectorAll(".range-presets button").forEach((b) => b.classList.remove("active"));
  loadHistoryCharts();
}

function resetZoom() {
  HIST.override = null;
  const full = document.querySelector('.range-presets button[data-range="full"]');
  document.querySelectorAll(".range-presets button").forEach((b) => b.classList.remove("active"));
  if (full) { full.classList.add("active"); HIST.range = "full"; }
  loadHistoryCharts();
}

function updateResetBtn() {
  const btn = $("#reset-zoom");
  if (btn) btn.hidden = !HIST.override;
}

async function loadHistoryTrack() {
  if (!HIST.map) return;
  const ss = runSessionsParam();
  const q = ss ? `?sessions=${encodeURIComponent(ss)}` : "";
  const res = await fetch(`/api/track${q}`);
  if (!res.ok) return;
  const points = (await res.json()).points || [];
  HIST.trackPoints = points;      // kept so scrubbing can move the marker locally
  HIST.map.setTrack(points, true);
}

// Move the marker to the fix at-or-before `t` using the already-loaded track —
// instant, no network round-trip, so the position tracks the drag smoothly.
// /api/track returns points ordered by ts_utc, so a binary search works.
function markerAtTime(t) {
  const pts = HIST.trackPoints;
  if (!pts || !pts.length || !HIST.map || t == null) return;
  let lo = 0, hi = pts.length - 1, best = -1;
  while (lo <= hi) {
    const mid = (lo + hi) >> 1;
    if (pts[mid].ts_utc <= t) { best = mid; lo = mid + 1; } else { hi = mid - 1; }
  }
  const p = pts[best >= 0 ? best : 0];
  HIST.map.setMarker(p.lat, p.lon);
}

async function loadHistoryView() {
  await Promise.all([
    loadHistoryCharts().catch((e) => console.error("hist charts", e)),
    loadHistoryTrack().catch((e) => console.error("hist track", e)),
  ]);
}

// ------------------------------------------------------------------- replay
// Map the slider's 0..1000 position onto the current window.
function scrubberToTime(pos) {
  const { since, until } = currentWindow();
  if (since == null || until == null) return null;
  return since + (until - since) * (pos / 1000);
}

// Re-anchor the scrubber whenever the window changes (session/preset/zoom) and
// show the state at the end of that window.
function syncScrubber() {
  const sc = $("#scrubber");
  const { since, until } = currentWindow();
  if (!sc || since == null || until == null) return;
  sc.value = "1000";
  requestReplay(until);
}

// Debounced, single-in-flight: while a request is out, remember only the newest
// position and fetch it once the current one lands. Keeps dragging smooth.
function requestReplay(t) {
  if (t == null) return;
  HIST.replayT = t;
  const label = $("#replay-time");
  if (label) label.textContent = new Date(t * 1000).toLocaleString([], { hour12: false });
  markerAtTime(t);     // instant: marker + chart cursor follow the drag with no
  renderCursors();     // network wait; the debounced fetch below refreshes cards
  clearTimeout(HIST.replayTimer);
  HIST.replayTimer = setTimeout(() => fireReplay(t), 120);
}

async function fireReplay(t) {
  if (!HIST.run) return;
  if (HIST.replayPending) { HIST.replayQueued = t; return; }
  HIST.replayPending = true;
  try {
    const q = new URLSearchParams({ sessions: runSessionsParam(), t: String(t) });
    const res = await fetch(`/api/replay?${q}`);
    if (res.ok) applyReplay(await res.json());
  } catch (e) {
    console.error("replay", e);
  } finally {
    HIST.replayPending = false;
    const queued = HIST.replayQueued;
    HIST.replayQueued = null;
    if (queued != null && queued !== t) fireReplay(queued);
  }
}

function applyReplay(data) {
  const channels = data.channels || {};
  HIST.replayT = data.t;
  // Same renderer as the live dashboard — "replay" mode skips wall-clock
  // staleness so nothing greys out or reads N/A just for being old.
  if (HIST.replayCards) HIST.replayCards.update(channels, data.t, "replay");
  // The marker normally moves instantly from the local track (see markerAtTime);
  // fall back to the server's fix only when no track is loaded, so the two
  // sources never fight over the marker. The full trail stays drawn for context.
  const gps = channels["GpsPacket"];
  if (gps && gps.fields && HIST.map && !HIST.trackPoints.length) {
    HIST.map.setMarker(gps.fields.latitude, gps.fields.longitude);
  }
  renderCursors();
}

// ------------------------------------------------------------------- lifecycle
function initHistory() {
  HIST.map = makeMap("history-map");
  initHistoryCharts();
  initChartModal();
  // Second card instance — no map card here, the History view has its own.
  HIST.replayCards = buildCards($("#replay-cards"), { includeMap: false });

  const sc = $("#scrubber");
  if (sc) sc.addEventListener("input", () => requestReplay(scrubberToTime(Number(sc.value))));

  $("#hist-session").addEventListener("change", (e) => {
    HIST.run = HIST.runs.find((r) => r.id === e.target.value) || null;
    HIST.override = null;
    loadHistoryView();
  });
  document.querySelectorAll(".range-presets button").forEach((btn) => {
    btn.addEventListener("click", () => {
      const r = btn.dataset.range;
      HIST.range = r === "full" ? "full" : Number(r);
      HIST.override = null;
      document.querySelectorAll(".range-presets button").forEach((b) => b.classList.remove("active"));
      btn.classList.add("active");
      loadHistoryView();
    });
  });
  const reset = $("#reset-zoom");
  if (reset) reset.addEventListener("click", resetZoom);
  HIST.inited = true;
}

// Called by the router each time the History view is shown.
function enterHistory() {
  if (!HIST.inited) {
    initHistory();
    loadHistoryView();
    return;
  }
  // Re-show: Leaflet/Chart.js need a nudge after being hidden.
  if (HIST.map) HIST.map.map.invalidateSize();
  for (const id in HIST.charts) HIST.charts[id].resize();
}
