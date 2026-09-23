// SPDX-FileCopyrightText: 2026 Maurice Casey
//
// SPDX-License-Identifier: AGPL-3.0-or-later

// ---------------------------------------------------------------------- SSE

let startTime = null;
let finishedCount = 0;

function updateProgress() {
  const total = items.size;
  const finished = [...items.values()]
    .filter(i => ["done", "failed", "skipped", "cancelled"].includes(i.state)).length;
  const pct = total ? Math.round((finished / total) * 100) : 0;
  els["progress-bar"].style.width = `${pct}%`;
  els["progress-bar"].classList.toggle("active", running && pct < 100 && pct > 0);
  const pv = els["progress-value"];
  if (pv) pv.textContent = running && pct < 100 ? `${pct}%` : pct === 100 ? "Done" : "0%";
  if (running) {
    let text = `Running — ${finished}/${total}`;
    if (startTime && finishedCount > 0) {
      const elapsed = (Date.now() - startTime) / 1000;
      const eta = elapsed / finishedCount * (total - finishedCount);
      if (eta > 60) {
        text += `  ~${Math.round(eta / 60)}m remaining`;
      } else {
        text += `  ~${Math.round(eta)}s remaining`;
      }
    }
    els["status-text"].textContent = text;
  } else {
    els["progress-bar"].classList.remove("active");
  }
}

function connectEvents() {
  const es = new EventSource("/api/events");
  es.onmessage = (e) => {
    const evt = JSON.parse(e.data);
    if (evt.message) log(evt.message, evt.tag);
    if (evt.item) updateRow(evt.item);

    switch (evt.kind) {
      case "run_started":
        startTime = Date.now();
        finishedCount = 0;
        setRunning(true);
        els["stage-text"].textContent = "";
        break;
      case "stage_started":
        els["stage-text"].textContent = evt.item ? `${evt.stage} · ${evt.item.name}` : "";
        updateProgress();
        break;
      case "item_finished":
        finishedCount++;
        updateProgress();
        // Mirrors the desktop build: if Preview is the open tab, follow
        // whichever item just finished rather than making the user click it.
        if (window.PreviewTab && document.getElementById("panel-preview").classList.contains("active")) {
          window.PreviewTab.open(evt.item.id);
        }
        break;
      case "paused":
        els["status-text"].textContent = "Paused";
        break;
      case "resumed":
        els["status-text"].textContent = "Running";
        break;
      case "run_finished": {
        const p = evt.payload || {};
        setRunning(false);
        els["stage-text"].textContent = "";
        els["status-text"].textContent =
          `Done — ${p.done ?? 0} ok` + (p.failed ? `, ${p.failed} failed` : "");
        const pv = els["progress-value"];
        if (pv) pv.textContent = "Done";
        refreshQueue();
        break;
      }
    }
  };
  es.onerror = () => { /* EventSource retries on its own */ };
}

// -------------------------------------------------------------------- tabs

document.querySelectorAll(".tab").forEach(tab => {
  tab.addEventListener("click", () => {
    document.querySelectorAll(".tab").forEach(t => t.classList.remove("active"));
    document.querySelectorAll(".panel").forEach(p => p.classList.remove("active"));
    tab.classList.add("active");
    document.getElementById(`panel-${tab.dataset.tab}`).classList.add("active");
    TAB_ACTIVATE[tab.dataset.tab]?.();
  });
});

// The simplified shell owns the visible navigation while the compact legacy
// tab buttons remain the panel controller. Bridge them in-page: a normal click
// must not reload the entire local app, discard queue selection, and restart
// endpoint detection merely to move from Source to Review.
document.querySelectorAll(".shell-nav a").forEach(link => {
  const url = new URL(link.href, window.location.href);
  const view = url.searchParams.get("view");
  const controller = view ? document.querySelector(`.tab[data-tab="${view}"]`) : null;
  if (!controller || url.pathname !== window.location.pathname) return;
  link.addEventListener("click", event => {
    event.preventDefault();
    controller.click();
    document.querySelectorAll(".shell-nav a").forEach(item => item.removeAttribute("aria-current"));
    link.setAttribute("aria-current", "page");
    window.history.replaceState(null, "", url.pathname + url.search);
  });
});
const requestedTabName = new URLSearchParams(window.location.search).get("view");
const requestedTab = document.querySelector(`.tab[data-tab="${requestedTabName}"]`);
if (requestedTab) requestedTab.click();

// ------------------------------------------------------------------- output

els["btn-browse-output"].onclick = async () => {
  const dir = await pickFolder("output");
  if (dir) els["output-dir"].value = dir;
};

// -------------------------------------------------------------------- init

(async function init() {
  try {
    const cfg = await api("GET", "/api/config");
    if (cfg.output_dir) els["output-dir"].value = cfg.output_dir;
  } catch { /* config is optional at startup */ }
  await refreshQueue();
  connectEvents();
})();
