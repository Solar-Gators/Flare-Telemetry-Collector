/* main.js — boot + view router. Loaded last.
 *
 * Boot order matters: schema first, then build the live cards + connect the WS
 * and status indicator BEFORE any history/track query, so a slow history load
 * never blocks the live view. History is lazy-initialised on first entry.
 */

function setView(name) {
  const live = name !== "history";
  mode = live ? "live" : "history";
  $("#view-live").hidden = !live;
  $("#view-history").hidden = live;
  document.querySelectorAll(".viewnav button").forEach((b) =>
    b.classList.toggle("active", b.dataset.view === name));
  if (live) {
    refreshLiveCards();
    if (liveMap) liveMap.map.invalidateSize();
  } else {
    enterHistory();
  }
}

async function boot() {
  if (!(await loadSchema())) return;

  initLiveCards();     // builds the subsystem cards (and the live #map div)
  initLiveMap();       // Leaflet on the live position map

  // Connect live + status FIRST.
  connectWs();
  setInterval(tickLive, 1000);
  setInterval(() => { if (mode === "live") refreshLiveCards(); }, TILE_REFRESH_MS);

  document.querySelectorAll(".viewnav button").forEach((b) =>
    b.addEventListener("click", () => setView(b.dataset.view)));
  $("#logout").addEventListener("click", async () => {
    await fetch("/api/logout", { method: "POST" });
    location.reload();
  });

  // Lightweight seeds.
  try { await loadLatest(); } catch (e) { console.error("latest", e); }
  try { await loadSessions(); } catch (e) { console.error("sessions", e); }

  // Seed the live position track in the background (best-effort).
  seedLiveTrack().catch((e) => console.error("live track", e));
}

boot();
