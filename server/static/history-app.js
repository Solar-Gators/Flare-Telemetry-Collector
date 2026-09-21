/* Lightweight /map and /history controller. */
const HISTORY_STATS = [
  ["bms_battery_voltage", "pack_voltage", "Pack Voltage", "V"],
  ["bms_battery_voltage", "high_cell_mv", "High Cell Voltage", "V"],
  ["bms_battery_voltage", "low_cell_mv", "Low Cell Voltage", "V"],
  ["bms_battery_current", "current", "Pack Current", "A"],
  ["bms_battery_temperature", "high_temp", "High Cell Temperature", "C"],
  ["bms_battery_temperature", "avg_temp", "Average Cell Temperature", "C"],
  ["mitsuba_frame0", "motor_rpm", "Motor RPM", "RPM"],
  ["mitsuba_frame0", "battery_voltage", "Motor Battery Voltage", "V"],
  ["mitsuba_frame0", "battery_current", "Motor Battery Current", "A"],
  ["GpsPacket", "speed", "Speed", "knots"],
  ["MpptPacket", "output_voltage", "MPPT Output Voltage", "V"],
  ["MpptPacket", "output_current", "MPPT Output Current", "A"],
];
const HISTORY = {
  chart: null,
  map: null,
  run: null,
  runs: [],
  points: [],
  selected: null,
};
const h$ = (id) => document.getElementById(id);

async function historyRuns() {
  const res = await fetch("/api/sessions");
  const data = res.ok ? await res.json() : {};
  HISTORY.runs =
    data.runs ||
    (data.sessions || []).map((s) => ({
      id: s.session_uuid,
      first_seen: s.first_seen,
      last_seen: s.last_seen,
      sessions: [s.session_uuid],
      session_count: 1,
    }));
  HISTORY.run = HISTORY.runs[0] || null;
  for (const id of ["hist-session", "map-session"]) {
    const select = h$(id);
    if (!select) continue;
    select.replaceChildren(
      ...HISTORY.runs.map((run) => {
        const option = document.createElement("option");
        option.value = run.id;
        option.textContent = new Date(run.first_seen * 1000).toLocaleString();
        return option;
      }),
    );
  }
}

function runParam() {
  return HISTORY.run ? HISTORY.run.sessions.join(",") : "";
}

function statOptions() {
  const select = h$("statistic");
  if (!select) return;
  select.replaceChildren(
    ...HISTORY_STATS.map(([type, field, label, unit]) => {
      const option = document.createElement("option");
      option.value = `${type}|${field}`;
      option.textContent = `${label} (${unit})`;
      return option;
    }),
  );
}

function currentWindow() {
  const start = Number(h$("history-since")?.value);
  const end = Number(h$("history-until")?.value);
  return {
    since: Number.isFinite(start) && start ? start : null,
    until: Number.isFinite(end) && end ? end : null,
  };
}

function applyFilter(value) {
  const kind = h$("filter-kind")?.value || "none";
  const a = Number(h$("filter-a")?.value);
  const b = Number(h$("filter-b")?.value);
  if (kind === "above") return value >= a;
  if (kind === "below") return value <= a;
  if (kind === "between")
    return value >= Math.min(a, b) && value <= Math.max(a, b);
  return true;
}

function summarize(points) {
  if (!points.length) return null;
  const values = points.map((p) => p.value);
  let integral = 0;
  for (let i = 1; i < points.length; i += 1) {
    const dt = points[i].time - points[i - 1].time;
    if (dt <= 300)
      integral += ((points[i].value + points[i - 1].value) / 2) * dt;
  }
  const min = points.reduce((x, y) => (y.value < x.value ? y : x));
  const max = points.reduce((x, y) => (y.value > x.value ? y : x));
  return {
    count: points.length,
    min: min.value,
    max: max.value,
    average: values.reduce((sum, value) => sum + value, 0) / values.length,
    integral,
  };
}

function renderSummary(summary, unit) {
  const host = h$("history-summary");
  if (!host) return;
  host.textContent = summary
    ? `Selected ${summary.count} · min ${summary.min.toFixed(3)} ${unit} · max ${summary.max.toFixed(3)} ${unit} · avg ${summary.average.toFixed(3)} ${unit} · integral ${summary.integral.toFixed(3)} ${unit}s`
    : "No values match the current selection.";
}

function renderTable(points, unit) {
  const table = h$("history-table");
  if (!table) return;
  table.replaceChildren();
  const head = document.createElement("tr");
  head.innerHTML = `<th>Time</th><th>Value (${unit})</th>`;
  table.appendChild(head);
  for (const point of points.slice(-2000)) {
    const row = document.createElement("tr");
    row.innerHTML = `<td>${new Date(point.time * 1000).toISOString()}</td><td>${point.value}</td>`;
    table.appendChild(row);
  }
}

function downloadSelected() {
  const rows = [
    "time_utc,value",
    ...HISTORY.selected.map(
      (p) => `${new Date(p.time * 1000).toISOString()},${p.value}`,
    ),
  ];
  const url = URL.createObjectURL(
    new Blob([rows.join("\n")], { type: "text/csv" }),
  );
  const link = document.createElement("a");
  link.href = url;
  link.download = "flare-selected.csv";
  link.click();
  URL.revokeObjectURL(url);
}

function drawHistory() {
  const [type, field, label, unit] = h$("statistic").value.split("|");
  const included = HISTORY.points.filter((p) => applyFilter(p.value));
  HISTORY.selected = included;
  const excluded = HISTORY.points.map((p) =>
    applyFilter(p.value) ? { x: p.time, y: null } : { x: p.time, y: p.value },
  );
  HISTORY.chart.data.datasets = [
    {
      label,
      data: included.map((p) => ({ x: p.time, y: p.value })),
      borderColor: "#4fc3f7",
      pointRadius: 0,
      tension: 0.15,
    },
    {
      label: "Filtered out",
      data: excluded,
      borderColor: "#ff9800",
      pointRadius: 0,
      borderDash: [4, 4],
      tension: 0.15,
    },
  ];
  HISTORY.chart.options.scales.y.title.text = unit;
  HISTORY.chart.update("none");
  renderSummary(summarize(included), unit);
  renderTable(included, unit);
}

async function loadHistorySeries() {
  const [type, field] = h$("statistic").value.split("|");
  const q = new URLSearchParams({ type, max_points: "1500" });
  if (runParam()) q.set("sessions", runParam());
  const window = currentWindow();
  if (window.since != null) q.set("since", window.since);
  if (window.until != null) q.set("until", window.until);
  const res = await fetch(`/api/history?${q}`);
  const data = res.ok ? await res.json() : { rows: [] };
  HISTORY.points = (data.rows || [])
    .map((row) => ({ time: row.ts_utc, value: row.fields?.[field] }))
    .filter((p) => typeof p.value === "number" && Number.isFinite(p.value));
  drawHistory();
}

function initHistoryPage() {
  if (HISTORY.chart) return;
  statOptions();
  HISTORY.chart = new Chart(h$("history-canvas"), {
    type: "line",
    data: { datasets: [] },
    options: {
      animation: false,
      parsing: false,
      normalized: true,
      maintainAspectRatio: false,
      interaction: { mode: "nearest", intersect: false },
      scales: {
        x: {
          type: "linear",
          ticks: { callback: (v) => new Date(v * 1000).toLocaleTimeString() },
        },
        y: { title: { display: true, text: "" } },
      },
    },
  });
  for (const id of [
    "statistic",
    "filter-kind",
    "filter-a",
    "filter-b",
    "history-since",
    "history-until",
  ])
    h$(id)?.addEventListener("change", loadHistorySeries);
  h$("download-selected")?.addEventListener("click", downloadSelected);
  h$("toggle-table")?.addEventListener("click", () => {
    h$("history-table-wrap").hidden = !h$("history-table-wrap").hidden;
  });
  loadHistorySeries().catch(console.error);
}

async function initMapPage() {
  if (!HISTORY.map) HISTORY.map = makeMap("map-page-map", { follow: false });
  const q = runParam() ? `?sessions=${encodeURIComponent(runParam())}` : "";
  const res = await fetch(`/api/track${q}`);
  if (res.ok) HISTORY.map.setTrack((await res.json()).points || [], true);
  HISTORY.map.map.invalidateSize();
}

async function enterDataPage(view) {
  if (!HISTORY.runs.length) await historyRuns();
  if (view === "history") {
    initHistoryPage();
    await loadHistorySeries();
  }
  if (view === "map") await initMapPage();
}

function bindDataControls() {
  for (const id of ["hist-session", "map-session"])
    h$(id)?.addEventListener("change", (event) => {
      HISTORY.run =
        HISTORY.runs.find((run) => run.id === event.target.value) ||
        HISTORY.run;
      enterDataPage(location.pathname.slice(1)).catch(console.error);
    });
  h$("filter-kind")?.addEventListener("change", (event) => {
    h$("filter-b").hidden = event.target.value !== "between";
    loadHistorySeries();
  });
}
