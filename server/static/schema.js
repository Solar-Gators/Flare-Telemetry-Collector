/* schema.js — shared state, catalog schema, and pure helpers.
 *
 * Loaded first. Everything about "what a message looks like" comes from
 * /api/schema (the vendored shared/can_messages.toml) — field units, labels, and
 * fault enums are not hardcoded. Cross-module state (SCHEMA, latest, mode) lives
 * here as top-level bindings shared across the classic <script> files.
 */

// ------------------------------------------------------------------ tunables
const MAX_POINTS = 600;               // per chart series in live mode
const LIVE_MAX_AGE_S = 15;            // newest data older than this (vs now) => not "live"
const STALE_DIM_S = 60;               // a channel older than this => grey out (keep last value)
const STALE_DATA_S = 300;             // a channel older than this (vs now) => N/A
const TILE_REFRESH_MS = 500;          // how often live cards re-render (values + staleness)
const GPS_MIN_SATS = 4;               // trust a fix only with at least this many sats

// ------------------------------------------------------------------ shared state
let SCHEMA = { messages: {}, enums: {} };
let mode = "live";                    // "live" | "history"
let liveSession = null;               // session_uuid backing the live map track
const latest = {};                    // channel -> {channel, msg_type, ts_utc, fields}

// Fields whose numeric value is a bitfield decoded via a catalog enum.
const FAULT_FIELDS = {
  bms_faults: "BmsFaults", active_faults: "BmsFaults", error_flags: "MitsubaError",
};

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

// Live-view key. MPPT packets split by controller index; everything else is
// keyed by its catalog message name. Mirrors the server's channel_for().
function channelFor(msgType, fields) {
  if (msgType === "MpptPacket" && fields && fields.mppt_index != null)
    return `MpptPacket:${fields.mppt_index}`;
  return msgType;
}

// A GPS fix is trusted only with enough satellites, in-range, and not near
// null-island (no-fix readings report 0 sats and garbage coords).
function validGps(f) {
  if (!f) return false;
  const { latitude: lat, longitude: lon, num_satellites: sats } = f;
  if (lat == null || lon == null) return false;
  if (sats != null && sats < GPS_MIN_SATS) return false;
  if (lat < -90 || lat > 90 || lon < -180 || lon > 180) return false;
  if (Math.abs(lat) < 1 && Math.abs(lon) < 3) return false;
  return true;
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

function timeTick(v) {
  return new Date(v * 1000).toLocaleTimeString([], { hour12: false });
}

function fmtAge(s) {
  if (s < 90) return Math.round(s) + "s ago";
  if (s < 5400) return Math.round(s / 60) + "m ago";
  return (s / 3600).toFixed(1) + "h ago";
}

// Elapsed seconds -> "45s" / "5m 03s" / "1h 05m 11s". Keeps long gaps readable
// instead of printing a single huge seconds count.
function fmtDur(sec) {
  const s = Math.max(0, Math.floor(sec));
  const h = Math.floor(s / 3600);
  const m = Math.floor((s % 3600) / 60);
  const r = s % 60;
  const pad = (n) => String(n).padStart(2, "0");
  if (h) return `${h}h ${pad(m)}m ${pad(r)}s`;
  if (m) return `${m}m ${pad(r)}s`;
  return `${r}s`;
}

async function loadSchema() {
  const res = await fetch("/api/schema");
  if (res.status === 401) { location.reload(); return false; }
  SCHEMA = await res.json();
  return true;
}
