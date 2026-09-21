function $(sel) {
  return document.querySelector(sel);
}

function setView(name, push = true) {
  if (!["live", "map", "history"].includes(name)) name = "live";
  if (push) window.history.pushState({}, "", `/${name}`);
  const live = name === "live";
  mode = live ? "live" : name;
  $("#view-live").hidden = !live;
  $("#view-map").hidden = name !== "map";
  $("#view-history").hidden = name !== "history";
  document
    .querySelectorAll(".viewnav button")
    .forEach((button) =>
      button.classList.toggle("active", button.dataset.view === name),
    );
  if (live) refreshLiveCards();
  else enterDataPage(name).catch((error) => console.error(name, error));
}

async function boot() {
  if (!(await loadSchema())) return;
  initLiveCards();
  initLiveMap();
  connectWs();
  setInterval(tickLive, 1000);
  setInterval(() => {
    if (mode === "live") refreshLiveCards();
  }, TILE_REFRESH_MS);
  document
    .querySelectorAll(".viewnav button")
    .forEach((button) =>
      button.addEventListener("click", () => setView(button.dataset.view)),
    );
  $("#toggle-details").addEventListener("click", (event) => {
    const details = $("#detail-cards");
    details.hidden = !details.hidden;
    event.target.textContent = details.hidden
      ? "Show detailed telemetry"
      : "Hide detailed telemetry";
  });
  $("#enable-live-chart").addEventListener("click", (event) => {
    enableLiveChart();
    event.target.hidden = true;
  });
  $("#logout").addEventListener("click", async () => {
    await fetch("/api/logout", { method: "POST" });
    location.reload();
  });
  bindDataControls();
  window.addEventListener("popstate", () =>
    setView(location.pathname.slice(1), false),
  );
  try {
    await loadLatest();
  } catch (error) {
    console.error("latest", error);
  }
  try {
    await historyRuns();
  } catch (error) {
    console.error("sessions", error);
  }
  seedLiveTrack().catch((error) => console.error("live track", error));
  setView(location.pathname.slice(1), false);
}

boot();
