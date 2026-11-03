/* main.js — boot + view router. Loaded last.
 *
 * Boot order matters: schema first, then build the live cards + connect the WS
 * and status indicator BEFORE any history/track query, so a slow history load
 * never blocks the live view. History is lazy-initialised on first entry.
 */

function setView(name) {
  mode = name;
  $("#view-live").hidden = name !== "live";
  $("#view-map").hidden = name !== "map";
  $("#view-history").hidden = name !== "history";
  document.querySelectorAll(".viewnav button").forEach((b) =>
    b.classList.toggle("active", b.dataset.view === name));
  if (name === "live") {
    refreshLiveCards();
    if (liveMap) liveMap.map.invalidateSize();
  } else if (name === "map") {
    if (!liveMap) initLiveMap();
    if (liveMap) liveMap.map.invalidateSize();
  } else if (name === "history") {
    enterHistory();
  }
}

function viewPath(name) {
  return { live: "/live", map: "/map", history: "/history" }[name] || "/live";
}

async function boot() {
  if (!(await loadSchema())) return;

  initLiveCards();     // builds the live telemetry cards
  // Connect live + status FIRST.
  connectWs();
  setInterval(tickLive, 1000);
  setInterval(() => { if (mode === "live") refreshLiveCards(); }, TILE_REFRESH_MS);

  document.querySelectorAll(".viewnav button").forEach((button) =>
    button.addEventListener("click", () => {
      const name = button.dataset.view;
      history.pushState({ view: name }, "", viewPath(name));
      setView(name);
    }));
  window.addEventListener("popstate", () => {
    const name = { "/history": "history", "/map": "map", "/live": "live" }[location.pathname] || "live";
    setView(name);
  });

  $("#logout").addEventListener("click", async () => {
    await fetch("/api/logout", { method: "POST" });
    location.reload();
  });

  // Lightweight seeds.
  try { await loadLatest(); } catch (e) { console.error("latest", e); }
  try { await loadSessions(); } catch (e) { console.error("sessions", e); }

  const pathView = { "/history": "history", "/map": "map", "/live": "live" }[location.pathname] || "live";
  setView(pathView);
  // Seed the live position track after the map has been initialized (best-effort).
  seedLiveTrack().catch((e) => console.error("live track", e));
}

boot();
