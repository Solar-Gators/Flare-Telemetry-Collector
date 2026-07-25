/* live.js — the always-current view: WebSocket stream, live position map, and
 * the subsystem-card refresh loop (cards.js renderer over `latest`).
 *
 * Live stays WS-connected even while the History view is open; the `mode` guard
 * freezes the live map (but never the `latest` map) during history.
 *
 * makeMap() is a small Leaflet factory reused by both the live position map and
 * the History track/replay map.
 */

// -------------------------------------------------------------------- map factory
// follow:true keeps the view centred on the car as fixes arrive, with a toggle
// control. Panning by hand switches follow off (the user asked to look
// elsewhere); zooming does not, since zooming in on the car is a normal thing to
// want while following.
function makeMap(containerId, { follow = false } = {}) {
  const map = L.map(containerId, { zoomControl: true }).setView([29.6436, -82.3549], 13);
  L.tileLayer("https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png", {
    maxZoom: 19, attribution: "&copy; OpenStreetMap",
  }).addTo(map);
  const path = L.polyline([], { color: "#ffb020", weight: 3 }).addTo(map);
  const marker = L.circleMarker([29.6436, -82.3549], {
    radius: 7, color: "#fff", weight: 2, fillColor: "#3fb950", fillOpacity: 1,
  }).addTo(map);

  // ------------------------------------------------------------ auto-follow
  let following = follow;
  let btn = null;

  function setFollowing(on) {
    following = on;
    if (btn) btn.classList.toggle("on", on);
  }
  function centerOn(ll) {
    // panTo degrades to an instant jump when the target is off-screen, so a
    // reconnect after a long gap doesn't slide across the county.
    if (following) map.panTo(ll, { animate: true, duration: 0.5 });
  }
  if (follow) {
    map.on("dragstart", () => setFollowing(false));
    const FollowCtl = L.Control.extend({
      options: { position: "topright" },
      onAdd() {
        btn = L.DomUtil.create("button", "map-follow" + (following ? " on" : ""));
        btn.type = "button";
        btn.textContent = "◎ Follow";
        btn.title = "Keep the map centred on the car";
        L.DomEvent.disableClickPropagation(btn);
        L.DomEvent.on(btn, "click", () => {
          setFollowing(!following);
          centerOn(marker.getLatLng());   // re-centre immediately when switched on
        });
        return btn;
      },
    });
    map.addControl(new FollowCtl());
  }

  function setTrack(points, recenter) {
    // /api/track is already filtered server-side; this is a geo guard for
    // anything that slips through (track points carry no satellite count).
    const latlngs = points
      .filter((p) => p.lat != null && p.lon != null &&
                     Math.abs(p.lat) <= 90 && Math.abs(p.lon) <= 180 &&
                     !(Math.abs(p.lat) < 1 && Math.abs(p.lon) < 3))
      .map((p) => [p.lat, p.lon]);
    path.setLatLngs(latlngs);
    if (latlngs.length) {
      const last = latlngs[latlngs.length - 1];
      marker.setLatLng(last);
      // Following means "show me where the car is now", so seed on the car
      // rather than zooming out to the whole track.
      if (following) map.setView(last, Math.max(map.getZoom(), 16));
      else if (recenter) map.fitBounds(path.getBounds(), { padding: [30, 30], maxZoom: 16 });
    }
  }
  function pushGps(fields) {
    if (fields.latitude == null || fields.longitude == null) return;
    const ll = [fields.latitude, fields.longitude];
    path.addLatLng(ll);
    marker.setLatLng(ll);
    centerOn(ll);
  }
  function setMarker(lat, lon) { if (lat != null && lon != null) marker.setLatLng([lat, lon]); }

  return { map, marker, path, setTrack, pushGps, setMarker, setFollowing };
}

// ----------------------------------------------------------------------- cards
let liveCards = null;                  // { update(state, nowS, mode) } from cards.js
let liveMap = null;

function initLiveCards() { liveCards = buildCards($("#cards")); }
function initLiveMap()   { liveMap = makeMap("map", { follow: true }); }

function refreshLiveCards() {
  if (liveCards) liveCards.update(latest, Date.now() / 1000, "live");
}

// ------------------------------------------------------------------ live wire
let ws;
function connectWs() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  ws = new WebSocket(`${proto}://${location.host}/ws`);
  ws.onmessage = (e) => {
    const msg = JSON.parse(e.data);
    if (msg.type === "latest") {
      Object.assign(latest, msg.channels || {});
      refreshLiveCards();
    } else if (msg.type === "telemetry") {
      for (const ev of msg.events) applyEvent(ev);
    }
  };
  ws.onclose = () => setTimeout(connectWs, 2000);
}

function applyEvent(ev) {
  if (ev.msg_type === "GpsPacket" && !validGps(ev.fields)) return;   // drop bad fixes
  const ch = ev.channel || channelFor(ev.msg_type, ev.fields);
  const prev = latest[ch];
  if (!prev || ev.ts_utc >= prev.ts_utc) latest[ch] = { ...ev, channel: ch };
  if (mode !== "live") return;                 // map frozen while viewing history
  // Cards re-render on a timer (see boot) so staleness is re-evaluated even when
  // a channel simply stops arriving; here we only drive the live map.
  if (ev.msg_type === "GpsPacket" && liveMap) liveMap.pushGps(ev.fields || {});
}

// --------------------------------------------------------------- live status pill
function tickLive() {
  const pill = $("#live");
  const txt = $("#live-text");
  if (mode === "history") { pill.className = "live-pill"; txt.textContent = "history"; return; }
  const channels = Object.keys(latest);
  if (!channels.length) { pill.className = "live-pill"; txt.textContent = "waiting…"; return; }
  let newest = 0;
  for (const ch of channels) newest = Math.max(newest, latest[ch].ts_utc || 0);
  const age = Date.now() / 1000 - newest;
  if (age <= LIVE_MAX_AGE_S) {
    pill.className = "live-pill on"; txt.textContent = "live";
  } else {
    pill.className = "live-pill stale"; txt.textContent = "no data · " + fmtAge(age);
  }
}

async function loadLatest() {
  const res = await fetch("/api/latest");
  if (res.ok) { Object.assign(latest, (await res.json()).channels || {}); refreshLiveCards(); }
}

// Seed the live position map from the current run's stored track. `liveSession`
// holds the newest run's member session uuids (a collector restart mints a new
// session, so the current run may span several).
async function seedLiveTrack() {
  if (!liveMap) return;
  const ss = Array.isArray(liveSession) ? liveSession.join(",") : (liveSession || "");
  const q = ss ? `?sessions=${encodeURIComponent(ss)}` : "";
  const res = await fetch(`/api/track${q}`);
  if (res.ok) liveMap.setTrack((await res.json()).points, true);
}
