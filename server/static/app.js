/* Flare Telemetry dashboard.
 *
 * Everything about "what a message looks like" comes from /api/schema (the vendored
 * shared/can_messages.toml) — field units, labels, and fault enums are not hardcoded
 * here. Live values arrive over /ws; history + GPS track come from the REST API.
 */

const MAX_POINTS = 600;               // per chart series in live mode
const STALE_MS = 5000;                // no data for this long => "stale"

let SCHEMA = { messages: {}, enums: {} };
let mode = "live";                    // "live" | "history"
let liveSession = null;               // session_uuid backing the live map track
const latest = {};                    // channel -> {msg_type, ts_utc, fields}
let lastRx = 0;

// ---- curated trend charts. Series reference catalog message + field names. ----
const METRICS = [
  { id: "pack", title: "Pack voltage (V)", series: [
    { type: "bms_battery_voltage", field: "pack_voltage", label: "pack", color: "#ffb020" } ] },
  { id: "current", title: "Pack current (A)", series: [
    { type: "bms_battery_current", field: "current", label: "pack", color: "#58a6ff" } ] },
  { id: "rpm", title: "Motor RPM", series: [
    { type: "mitsuba_frame0", field: "motor_rpm", label: "rpm", color: "#3fb950" } ] },
  { id: "motI", title: "Motor current (A)", series: [
    { type: "mitsuba_frame0", field: "motor_current", label: "motor", color: "#f778ba" } ] },
  { id: "mppt", title: "MPPT input voltage (V)", series: [
    { type: "MpptPacket", field: "input_voltage", label: "MPPT 1", color: "#ffb020", mppt: 1 },
    { type: "MpptPacket", field: "input_voltage", label: "MPPT 2", color: "#58a6ff", mppt: 2 },
    { type: "MpptPacket", field: "input_voltage", label: "MPPT 3", color: "#3fb950", mppt: 3 } ] },
  { id: "speed", title: "Speed", series: [
    { type: "GpsPacket", field: "speed", label: "gps", color: "#58a6ff" } ] },
];
const charts = {};                    // metric id -> Chart

// Fields whose numeric value is a bitfield decoded via a catalog enum.
const FAULT_FIELDS = { bms_faults: "BmsFaults", active_faults: "BmsFaults", error_flags: "MitsubaError" };

// --------------------------------------------------------------------------- utils
const $ = (sel) => document.querySelector(sel);

function pretty(name) {
  return name.replace(/_/g, " ").replace(/\b\w/g, (c) => c.toUpperCase());
}

function fieldMeta(msgType, field) {
  const m = SCHEMA.messages[msgType];
  return (m && m.fields && m.fields[field]) || {};
}

function fmtValue(v, meta) {
  if (typeof v === "boolean") return v ? "yes" : "no";
  if (typeof v === "number") return Number.isInteger(v) ? String(v) : v.toFixed(3).replace(/\.?0+$/, "");
  return String(v);
}

function channelFor(msgType, fields) {
  if (msgType === "MpptPacket" && fields && fields.mppt_index != null)
    return `MpptPacket:${fields.mppt_index}`;
  return msgType;
}

function timeTick(v) {
  return new Date(v * 1000).toLocaleTimeString([], { hour12: false });
}

// ----------------------------------------------------------------------------- map
let map, marker, path;
function initMap() {
  map = L.map("map", { zoomControl: true }).setView([29.6436, -82.3549], 13); // UF-ish
  L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxZoom: 19, attribution: "&copy; OpenStreetMap",
  }).addTo(map);
  path = L.polyline([], { color: "#ffb020", weight: 3 }).addTo(map);
  marker = L.circleMarker([29.6436, -82.3549], {
    radius: 7, color: "#fff", weight: 2, fillColor: "#3fb950", fillOpacity: 1,
  }).addTo(map);
}

function setTrack(points, recenter) {
  const latlngs = points.map((p) => [p.lat, p.lon]);
  path.setLatLngs(latlngs);
  if (latlngs.length) {
    marker.setLatLng(latlngs[latlngs.length - 1]);
    if (recenter) map.fitBounds(path.getBounds(), { padding: [30, 30], maxZoom: 16 });
  }
}

function pushGps(fields) {
  if (fields.latitude == null || fields.longitude == null) return;
  const ll = [fields.latitude, fields.longitude];
  path.addLatLng(ll);
  marker.setLatLng(ll);
}

// -------------------------------------------------------------------------- charts
function initCharts() {
  const host = $("#charts");
  for (const metric of METRICS) {
    const card = document.createElement("div");
    card.className = "chart-card";
    card.innerHTML = `<h3>${metric.title}</h3><canvas id="c_${metric.id}"></canvas>`;
    host.appendChild(card);
    const ctx = card.querySelector("canvas").getContext("2d");
    charts[metric.id] = new Chart(ctx, {
      type: "line",
      data: { datasets: metric.series.map((s) => ({
        label: s.label, data: [], borderColor: s.color, backgroundColor: s.color,
        borderWidth: 1.5, pointRadius: 0, tension: 0.25,
      })) },
      options: {
        animation: false, parsing: false, normalized: true,
        scales: {
          x: { type: "linear", ticks: { callback: timeTick, maxTicksLimit: 5, color: "#8b98a5" },
               grid: { color: "#222a34" } },
          y: { ticks: { color: "#8b98a5" }, grid: { color: "#222a34" } },
        },
        plugins: { legend: { display: metric.series.length > 1, labels: { color: "#8b98a5", boxWidth: 10 } } },
      },
    });
  }
}

function seriesMatches(s, msgType, fields) {
  if (s.type !== msgType) return false;
  if (s.mppt != null && (!fields || fields.mppt_index !== s.mppt)) return false;
  return true;
}

function liveAppendChart(msgType, ts, fields) {
  for (const metric of METRICS) {
    const chart = charts[metric.id];
    let touched = false;
    metric.series.forEach((s, i) => {
      if (!seriesMatches(s, msgType, fields)) return;
      const v = fields[s.field];
      if (v == null || typeof v !== "number") return;
      const ds = chart.data.datasets[i].data;
      ds.push({ x: ts, y: v });
      if (ds.length > MAX_POINTS) ds.shift();
      touched = true;
    });
    if (touched) chart.update("none");
  }
}

// ---------------------------------------------------------------------------- tiles
function renderTiles() {
  const host = $("#tiles");
  host.innerHTML = "";
  const channels = Object.keys(latest).sort();
  for (const ch of channels) {
    const item = latest[ch];
    const fields = item.fields || {};
    for (const [field, value] of Object.entries(fields)) {
      if (field === "mppt_index") continue;
      host.appendChild(tileFor(item.msg_type, ch, field, value));
    }
  }
  if (!channels.length) host.innerHTML = '<p class="muted" style="padding:8px">No data yet.</p>';
}

function tileFor(msgType, channel, field, value) {
  const el = document.createElement("div");
  el.className = "tile";
  const meta = fieldMeta(msgType, field);
  const suffix = channel.includes(":") ? " " + channel.split(":")[1] : "";
  const label = pretty(field) + suffix;

  if (FAULT_FIELDS[field] != null && typeof value === "number") {
    const names = decodeFaults(value, FAULT_FIELDS[field]);
    el.classList.add(names.length ? "danger" : "");
    el.innerHTML = `<div class="label">${label}</div>` +
      (names.length
        ? `<div class="faults">${names.map((n) => `<span class="badge">${n}</span>`).join("")}</div>`
        : `<div class="faults"><span class="badge ok">OK</span></div>`);
    return el;
  }

  const unit = meta.unit ? `<small>${meta.unit}</small>` : "";
  el.innerHTML = `<div class="label">${label}</div><div class="value">${fmtValue(value, meta)}${unit}</div>`;
  return el;
}

function decodeFaults(bits, enumName) {
  const en = SCHEMA.enums[enumName] || {};
  const out = [];
  for (const [name, bit] of Object.entries(en)) {
    if (name === "NONE" || bit === 0) continue;
    // BmsFaults values are masks (1,2,4…); MitsubaError values are bit indices.
    const mask = enumName === "MitsubaError" ? (1 << bit) : bit;
    if ((bits & mask) === mask) out.push(name);
  }
  return out;
}

// ------------------------------------------------------------------------ live wire
let ws, lastSeen = 0;
function connectWs() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  ws = new WebSocket(`${proto}://${location.host}/ws`);
  ws.onmessage = (e) => {
    const msg = JSON.parse(e.data);
    if (msg.type === "latest") {
      Object.assign(latest, msg.channels || {});
      renderTiles();
    } else if (msg.type === "telemetry") {
      for (const ev of msg.events) applyEvent(ev);
    }
  };
  ws.onclose = () => setTimeout(connectWs, 2000);
}

function applyEvent(ev) {
  const ch = ev.channel || channelFor(ev.msg_type, ev.fields);
  const prev = latest[ch];
  if (!prev || ev.ts_utc >= prev.ts_utc) latest[ch] = { ...ev, channel: ch };
  lastSeen = Date.now();
  if (mode !== "live") return;                 // frozen while viewing history
  renderTiles();
  if (ev.msg_type === "GpsPacket") pushGps(ev.fields || {});
  liveAppendChart(ev.msg_type, ev.ts_utc, ev.fields || {});
}

function tickLive() {
  const pill = $("#live");
  const txt = $("#live-text");
  if (mode === "history") { pill.className = "live-pill"; txt.textContent = "history"; return; }
  const age = Date.now() - lastSeen;
  if (!lastSeen) { pill.className = "live-pill"; txt.textContent = "waiting…"; }
  else if (age > STALE_MS) { pill.className = "live-pill stale"; txt.textContent = "stale"; }
  else { pill.className = "live-pill on"; txt.textContent = "live"; }
}

// --------------------------------------------------------------------------- history
async function loadHistory(session) {
  // reset charts
  for (const metric of METRICS) charts[metric.id].data.datasets.forEach((d) => (d.data = []));
  const types = [...new Set(METRICS.flatMap((m) => m.series.map((s) => s.type)))];
  const byType = {};
  await Promise.all(types.map(async (t) => {
    const q = new URLSearchParams({ type: t, limit: "20000" });
    if (session) q.set("session", session);
    const res = await fetch(`/api/history?${q}`);
    byType[t] = res.ok ? (await res.json()).rows : [];
  }));
  for (const metric of METRICS) {
    metric.series.forEach((s, i) => {
      const rows = byType[s.type] || [];
      charts[metric.id].data.datasets[i].data = rows
        .filter((r) => r.fields && (s.mppt == null || r.fields.mppt_index === s.mppt))
        .map((r) => ({ x: r.ts_utc, y: r.fields[s.field] }))
        .filter((p) => typeof p.y === "number");
    });
    charts[metric.id].update("none");
  }
}

async function loadTrack(session, recenter) {
  const q = session ? `?session=${encodeURIComponent(session)}` : "";
  const res = await fetch(`/api/track${q}`);
  if (res.ok) setTrack((await res.json()).points, recenter);
}

async function loadLatest() {
  const res = await fetch("/api/latest");
  if (res.ok) { Object.assign(latest, (await res.json()).channels || {}); renderTiles(); }
}

// ------------------------------------------------------------------------- sessions
async function loadSessions() {
  const res = await fetch("/api/sessions");
  const sessions = res.ok ? (await res.json()).sessions : [];
  const sel = $("#session");
  liveSession = sessions.length ? sessions[0].session_uuid : null;
  for (const s of sessions) {
    const opt = document.createElement("option");
    const when = new Date(s.first_seen * 1000).toLocaleString();
    opt.value = s.session_uuid;
    opt.textContent = `${when} — ${s.session_uuid.slice(0, 8)}`;
    sel.appendChild(opt);
  }
}

async function onSessionChange(e) {
  const val = e.target.value;
  if (!val) {
    mode = "live";
    await loadLatest();
    await loadTrack(liveSession, true);
    // rebuild live charts from the live session's history so trends aren't empty
    await loadHistory(liveSession);
  } else {
    mode = "history";
    await Promise.all([loadHistory(val), loadTrack(val, true)]);
  }
}

// ------------------------------------------------------------------------------ boot
async function boot() {
  const sres = await fetch("/api/schema");
  if (sres.status === 401) { location.reload(); return; }
  SCHEMA = await sres.json();

  initMap();
  initCharts();
  await loadSessions();
  await loadLatest();
  await loadTrack(liveSession, true);
  await loadHistory(liveSession);
  connectWs();

  $("#session").addEventListener("change", onSessionChange);
  $("#logout").addEventListener("click", async () => {
    await fetch("/api/logout", { method: "POST" });
    location.reload();
  });
  setInterval(tickLive, 1000);
}

boot();
