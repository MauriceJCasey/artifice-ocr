/* SPDX-FileCopyrightText: 2026 Maurice Casey */
/* SPDX-License-Identifier: AGPL-3.0-or-later */
(function () {
  "use strict";
  const fallback = { theme: "system", reduced_motion: false };
  const activities = new Map();
  const root = document.documentElement;
  const $ = (selector) => document.querySelector(selector);

  function applyPreferences(value) {
    const preferences = Object.assign({}, fallback, value || {});
    root.dataset.theme = preferences.theme;
    root.dataset.reducedMotion = String(preferences.reduced_motion);
    return preferences;
  }
  function stored() { try { return JSON.parse(localStorage.getItem("artifice.ui.preferences")) || fallback; } catch (_) { return fallback; } }
  async function getPreferences() {
    try { const response = await fetch("/api/ui/preferences"); if (!response.ok) throw new Error(); return applyPreferences(await response.json()); }
    catch (_) { return applyPreferences(stored()); }
  }
  async function setPreferences(patch) {
    const next = applyPreferences(Object.assign({}, stored(), patch));
    localStorage.setItem("artifice.ui.preferences", JSON.stringify(next));
    try { await fetch("/api/ui/preferences", { method:"PATCH", headers:{"Content-Type":"application/json"}, body:JSON.stringify(patch) }); } catch (_) { /* local fallback is intentional */ }
    return next;
  }
  function renderActivities() {
    const list = $("[data-activity-list]"); if (!list) return;
    list.innerHTML = Array.from(activities.values()).map((item) => `<div class="activity-item" data-state="${item.state}"><span>${item.label}</span><div class="activity-progress" aria-label="${item.progress || 0}% complete"><span style="width:${Math.max(0,Math.min(100,item.progress || 0))}%"></span></div><small>${item.detail || item.state}</small></div>`).join("");
    const count = $("[data-activity-count]"); if (count) count.textContent = String(activities.size);
  }
  function publishActivity(item) { if (!item || !item.id || !item.label || !item.state) throw new TypeError("Activity requires id, label, and state"); activities.set(item.id, item); renderActivities(); }
  function removeActivity(id) { activities.delete(id); renderActivities(); }
  function setModelStatus(status) { const label=$("[data-model-label]"); const dot=$(".status-dot"); if(label) label.textContent=status.label; if(dot) dot.dataset.state=status.state; }
  function syncNavigation() {
    const current = new URL(window.location.href);
    const currentView = current.searchParams.get("view");
    if (!currentView) return;
    document.querySelectorAll(".shell-nav a").forEach((link) => {
      const target = new URL(link.href, current);
      const samePath = target.pathname === current.pathname;
      const targetView = target.searchParams.get("view");
      const selected = samePath && (targetView ? targetView === currentView : !currentView);
      if (selected) link.setAttribute("aria-current", "page");
      else link.removeAttribute("aria-current");
    });
  }
  async function refreshSuiteApps() {
    let apps=[]; try { const response=await fetch("/api/suite/apps"); if(response.ok) apps=await response.json(); } catch (_) { /* popover retains an empty state */ }
    const host=$("[data-suite-apps]"); if(host) host.innerHTML=apps.length ? apps.map((app)=>`<a class="suite-app" href="${app.url || "/?manage="+encodeURIComponent(app.slug)}"><span class="suite-app-dot" style="background:${app.accent}"></span>${app.name}<small>${app.running ? "Running" : "Open in Hub"}</small></a>`).join("") : '<p class="suite-empty">Suite status is unavailable.</p>';
    return apps;
  }
  function init() {
    getPreferences();
    syncNavigation();
    document.addEventListener("click", async (event) => { const action=event.target.closest("[data-shell-action]")?.dataset.shellAction; if(action==="nav"){const nav=$("[data-shell-panel=nav]");nav.toggleAttribute("data-open");event.target.setAttribute("aria-expanded",String(nav.hasAttribute("data-open")));} if(action==="suite"){const pop=$("[data-suite-popover]");pop.hidden=!pop.hidden;event.target.setAttribute("aria-expanded",String(!pop.hidden));if(!pop.hidden) await refreshSuiteApps();} if(action==="theme"){const order=["system","light","dark"];const current=root.dataset.theme||"system";setPreferences({theme:order[(order.indexOf(current)+1)%order.length]});} if(action==="activity"){const list=$("[data-activity-list]");list.hidden=!list.hidden;event.target.setAttribute("aria-expanded",String(!list.hidden));} });
  }
  window.ArtificeShell={init,publishActivity,removeActivity,setModelStatus,getPreferences,setPreferences,refreshSuiteApps};
  if(document.readyState==="loading") document.addEventListener("DOMContentLoaded",init); else init();
}());
