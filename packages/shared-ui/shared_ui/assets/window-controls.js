/* SPDX-FileCopyrightText: 2026 Maurice Casey
 *
 * SPDX-License-Identifier: AGPL-3.0-or-later
 */

/* window-controls.js — Reveals and wires the frameless-PyWebView window
 * controls injected by _masthead.html.
 *
 * The buttons are rendered with display:none by masthead.css.  When PyWebView
 * fires the pywebviewready event, this IIFE adds the .pywebview-active class
 * to <html> (which the CSS keys off to show the controls) and wires the
 * Minimize / Maximize / Close buttons to the JS-Python bridge.
 *
 * Expected Python-side API (exposed via webview.create_window(...,
 * js_api=api_instance) — see packages/shared-ui/shared_ui/window.py's
 * WindowApi class, which all five apps now share):
 *
 *   class Api:
 *       def minimize(self):
 *           webview.windows[0].minimize()
 *
 *       def toggle_maximize(self):
 *           # WindowApi tracks maximized state internally and calls
 *           # maximize()/restore() as appropriate — no state passed from JS.
 *           ...
 *
 *       def destroy(self):
 *           webview.windows[0].destroy()
 *
 * In a standard browser pywebviewready never fires, so the buttons stay
 * hidden and the page is unchanged.
 *
 * ES5-compatible: no const/let, arrow functions, or template literals.
 */
(function () {
  "use strict";

  function callPywebview(name) {
    if (
      typeof window.pywebview !== "undefined" &&
      window.pywebview !== null &&
      typeof window.pywebview.api !== "undefined" &&
      window.pywebview.api !== null &&
      typeof window.pywebview.api[name] === "function"
    ) {
      window.pywebview.api[name]();
      return true;
    }
    return false;
  }

  /* The native window is frameless (see shared_ui/window.py) so the OS resize
   * border is gone; `resizable=True` alone does not give a frameless WinForms/
   * WebView2 window draggable edges. Re-implement resizing with a bottom-right
   * grip that drives the exposed WindowApi.resize(width, height). Anchored
   * top-left (which is all resize() offers), so only the right/bottom edges
   * move — the common case. Injected here, not in markup, so every app that
   * loads this script gets it without per-app template changes. */
  function installResizeGrip() {
    if (!document.body || document.getElementById("windowResizeGrip")) return;

    var grip = document.createElement("div");
    grip.id = "windowResizeGrip";
    grip.className = "window-resize-grip pywebview-drag-region-exclude";
    grip.setAttribute("aria-hidden", "true");
    document.body.appendChild(grip);

    var dragging = false;
    var startX = 0, startY = 0, startW = 0, startH = 0, lastW = 0, lastH = 0;
    var frame = null;

    function apiResize() {
      frame = null;
      if (
        window.pywebview && window.pywebview.api &&
        typeof window.pywebview.api.resize === "function"
      ) {
        window.pywebview.api.resize(Math.round(lastW), Math.round(lastH));
      }
    }

    function onMove(e) {
      if (!dragging) return;
      // min_size in window.py is (640, 480); keep the JS floor in step.
      lastW = Math.max(640, startW + (e.screenX - startX));
      lastH = Math.max(480, startH + (e.screenY - startY));
      e.preventDefault();
      if (frame === null) frame = window.requestAnimationFrame(apiResize);
    }

    function onUp(e) {
      dragging = false;
      if (frame !== null) { window.cancelAnimationFrame(frame); frame = null; }
      try { grip.releasePointerCapture(e.pointerId); } catch (err) {}
      window.removeEventListener("pointermove", onMove, true);
      window.removeEventListener("pointerup", onUp, true);
    }

    grip.addEventListener("pointerdown", function (e) {
      dragging = true;
      startX = e.screenX;
      startY = e.screenY;
      startW = window.innerWidth;   // frameless: inner size == window size
      startH = window.innerHeight;
      lastW = startW;
      lastH = startH;
      try { grip.setPointerCapture(e.pointerId); } catch (err) {}
      window.addEventListener("pointermove", onMove, true);
      window.addEventListener("pointerup", onUp, true);
      e.preventDefault();
      e.stopPropagation();
    });
  }

  function onPywebviewReady() {
    var root = document.documentElement;
    if (root) {
      var cls = " " + root.className + " ";
      if (cls.indexOf(" pywebview-active ") === -1) {
        root.className = root.className + " pywebview-active";
      }
    }

    installResizeGrip();

    function stopDrag(e) {
      if (e && typeof e.stopPropagation === "function") {
        e.stopPropagation();
      }
    }

    var minimizeBtn = document.getElementById("windowMinimize");
    var maximizeBtn = document.getElementById("windowMaximize");
    var closeBtn = document.getElementById("windowClose");

    if (minimizeBtn) {
      minimizeBtn.onmousedown = stopDrag;
      minimizeBtn.onclick = function () {
        callPywebview("minimize");
      };
    }

    if (maximizeBtn) {
      maximizeBtn.onmousedown = stopDrag;
      maximizeBtn.onclick = function () {
        callPywebview("toggle_maximize");
      };
    }

    if (closeBtn) {
      closeBtn.onmousedown = stopDrag;
      closeBtn.onclick = function () {
        if (!callPywebview("destroy")) {
          if (typeof window.close === "function") {
            window.close();
          }
        }
      };
    }
  }

  if (typeof window.addEventListener === "function") {
    window.addEventListener("pywebviewready", onPywebviewReady, false);
  } else if (typeof window.attachEvent === "function") {
    window.attachEvent("onpywebviewready", onPywebviewReady);
  }
})();
