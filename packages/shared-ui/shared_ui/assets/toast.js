// SPDX-FileCopyrightText: 2026 Maurice Casey
//
// SPDX-License-Identifier: AGPL-3.0-or-later

/* toast.js — transient status notices for a completed or failed action.
 *
 * Ships from packages/shared-ui alongside byom.js, and follows its
 * packaging and loading pattern exactly: same ES5-only dialect (IIFE,
 * "use strict", var/function rather than let/const/arrow, .then()/.catch()
 * rather than async/await), because artifice-graph's static/app.js and
 * pipeline.js are both plain ES5 and any shared script must run there too
 * even though graph is not adopting this file today (see the ui-ux brief
 * this was written against, and the header comment in toast.css for why).
 * graph's own equivalent today is showNotification()/.notification in
 * pipeline.js:222-236 and pipeline.css:283-300 — found by re-measuring the
 * brief's survey, which had said no implementation existed in graph or
 * draft; draft genuinely had none, graph's exists under a different name.
 *
 * Exposes a single global, `window.ArtificeToast`, matching byom.js's own
 * `window.ArtificeByom` — prefixed with the suite name so it cannot
 * collide with any app-local global.
 *
 *   window.ArtificeToast.success("Saved.");
 *   window.ArtificeToast.error("Could not reach the server.");
 *   var handle = window.ArtificeToast.show("Exporting…", "info", { duration: 0 });
 *   // ...later, once the export actually finishes:
 *   handle.close();
 *
 * Promoted from two near-identical per-app implementations — see toast.css
 * for the full comparison and what changed. The two behavioural additions
 * this file makes over both of them:
 *
 * 1. Accessible announcement. Neither prior implementation set any ARIA at
 *    all, so a screen-reader user had no way to learn that a background job
 *    had finished or failed short of noticing the on-screen change. Every
 *    toast node here carries role="status" (aria-live="polite") or, for the
 *    error tone, role="alert" (aria-live="assertive") — chosen per node,
 *    not once on the container, because a container-level aria-live cannot
 *    express that an error should interrupt while a success confirmation
 *    should not (screen readers do not reliably escalate an inner
 *    role="alert" nested in an outer polite region). The element already
 *    carries its role and text at the moment it lands in the DOM, the same
 *    "build populated, then insert" order byom.js uses for its own
 *    role="status" regions — inserting a fully-formed live-region node is
 *    picked up by every screen reader this suite targets without a
 *    separate mutation step.
 *
 * 2. An error does not vanish unread. Both prior implementations
 *    auto-dismissed every tone, including error (ocr's default was even
 *    longer for error — 6000ms vs 3500ms — but still a fixed timeout with
 *    no way to extend it and no way to close it early). Neither is
 *    sufficient on its own: a purely longer timeout can still expire before
 *    a slow reader finishes, and no manual close means a user who *has*
 *    read it must wait out the timer to declutter the stack. This file
 *    does both instead: the error tone defaults to no auto-dismiss at all
 *    (DEFAULT_DURATIONS.error = 0, meaning "stays until dismissed"), and
 *    every toast — regardless of tone — gets a manual close button sized to
 *    --control-height. A caller that genuinely wants an auto-dismissing
 *    error can still pass an explicit `duration`; the default is simply the
 *    side that cannot silently lose information. Non-error tones also pause
 *    their countdown on hover/focus and resume on mouseleave/blur, so a
 *    cursor resting on the stack to read one message does not cost it
 *    disappearing mid-read.
 */

(function () {
  "use strict";

  // ── Motion ────────────────────────────────────────────────────────────
  // Design_Philosophy.md §7 line 501: JS-driven animation must check
  // reduced-motion before triggering, not rely solely on the CSS media
  // query catching it. Identical to byom.js's helper of the same name.

  function prefersReducedMotion() {
    return !!(window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches);
  }

  function qs(root, sel) { return root.querySelector(sel); }

  function elFromHtml(html) {
    var wrap = document.createElement("div");
    wrap.innerHTML = html;
    return wrap.firstElementChild;
  }

  // ── Icons ─────────────────────────────────────────────────────────────
  // Line-drawn per Design_Philosophy.md §8.18: viewBox 0 0 24 24,
  // fill="none", stroke="currentColor", stroke-width 2, round caps,
  // aria-hidden="true". A "dot" (the info/warning glyphs' point) is drawn
  // as a zero-length round-capped line rather than a filled circle, so
  // every icon here still honours fill="none" with no per-element
  // exception.

  var ICON_SUCCESS =
    '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" ' +
    'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
    '<circle cx="12" cy="12" r="9"></circle><polyline points="8 12 11 15 16 9"></polyline></svg>';

  var ICON_WARNING =
    '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" ' +
    'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
    '<path d="M10.29 3.86L1.82 18a2 2 0 001.71 3h16.94a2 2 0 001.71-3L13.71 3.86a2 2 0 00-3.42 0z"></path>' +
    '<line x1="12" y1="9" x2="12" y2="13"></line><line x1="12" y1="17" x2="12.01" y2="17"></line></svg>';

  var ICON_ERROR =
    '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" ' +
    'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
    '<circle cx="12" cy="12" r="9"></circle><line x1="9" y1="9" x2="15" y2="15"></line>' +
    '<line x1="15" y1="9" x2="9" y2="15"></line></svg>';

  var ICON_INFO =
    '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" ' +
    'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
    '<circle cx="12" cy="12" r="9"></circle><line x1="12" y1="11" x2="12" y2="16"></line>' +
    '<line x1="12" y1="7.5" x2="12.01" y2="7.5"></line></svg>';

  var ICON_CLOSE =
    '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" ' +
    'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
    '<line x1="5" y1="5" x2="19" y2="19"></line><line x1="19" y1="5" x2="5" y2="19"></line></svg>';

  var ICONS = { success: ICON_SUCCESS, warning: ICON_WARNING, error: ICON_ERROR, info: ICON_INFO };

  // ── Defaults ──────────────────────────────────────────────────────────
  // 0 means "does not auto-dismiss" — see the file header for why error
  // defaults that way. Callers may override per-call via opts.duration.
  var DEFAULT_DURATIONS = { success: 4000, warning: 6000, error: 0, info: 4000 };

  // ── Container ─────────────────────────────────────────────────────────
  // Lazily created and appended to <body> on first use, matching
  // artifice-ocr's toast.js precedent (the self-contained one of the two
  // prior implementations — artifice-transcribe instead required a static
  // #toast-container div in its own index.html). Self-building means
  // adopting this file costs a host page only the <link>/<script> tags,
  // no template markup, matching byom.js's own self-building overlay.

  var _container = null;

  function ensureContainer() {
    if (!_container || !document.body.contains(_container)) {
      _container = document.createElement("div");
      _container.className = "toast-container";
      document.body.appendChild(_container);
    }
    return _container;
  }

  // ── Toast ─────────────────────────────────────────────────────────────

  function buildToastEl(tone, message, dismissible) {
    var icon = ICONS[tone] || ICONS.info;
    var role = tone === "error" ? "alert" : "status";
    var live = tone === "error" ? "assertive" : "polite";
    var html =
      '<div class="toast" data-tone="' + tone + '" role="' + role + '" aria-live="' + live + '">' +
      '<span class="toast-icon" aria-hidden="true">' + icon + "</span>" +
      '<p class="toast-message"></p>' +
      (dismissible
        ? '<button type="button" class="toast-close" aria-label="Dismiss notification">' + ICON_CLOSE + "</button>"
        : "") +
      "</div>";
    var el = elFromHtml(html);
    // Set via textContent, not interpolated into the HTML string above, so
    // the caller's message never needs escaping and can never be
    // interpreted as markup.
    qs(el, ".toast-message").textContent = message;
    return el;
  }

  function removeToastEl(el) {
    if (el && el.parentNode) { el.parentNode.removeChild(el); }
  }

  function dismissToastEl(el) {
    if (!el || el.getAttribute("data-dismissing") === "true") return;
    el.setAttribute("data-dismissing", "true");
    if (prefersReducedMotion()) {
      // toast.css also zeroes the animation under this same media query;
      // this is the JS-side half of Design_Philosophy.md §7's rule that a
      // reduced-motion check must gate triggering, not rely on the CSS
      // alone catching it. Remove immediately rather than waiting on an
      // "animationend" that a zeroed-duration animation may fire before
      // this handler has even attached its listener.
      removeToastEl(el);
      return;
    }
    el.classList.add("toast-out");
    el.addEventListener("animationend", function () { removeToastEl(el); });
  }

  /**
   * Show a toast.
   * @param {string} message
   * @param {string} [tone] - "success" | "warning" | "error" | "info" (default "info")
   * @param {Object} [opts]
   * @param {number} [opts.duration] - ms before auto-dismiss; 0 (or omitted
   *   for tone "error") means it stays until closed manually.
   * @param {boolean} [opts.dismissible] - show a manual close control.
   *   Defaults true; only set false for a toast a caller is certain it will
   *   dismiss itself via the returned handle (removing the only way to
   *   close it manually is an accessibility regression otherwise).
   * @returns {{close: function(): void}}
   */
  function show(message, tone, opts) {
    opts = opts || {};
    tone = ICONS[tone] ? tone : "info";
    var dismissible = opts.dismissible !== false;
    var duration = typeof opts.duration === "number" ? opts.duration : DEFAULT_DURATIONS[tone];

    var container = ensureContainer();
    var el = buildToastEl(tone, message, dismissible);
    container.appendChild(el);

    var closeBtn = dismissible ? qs(el, ".toast-close") : null;
    if (closeBtn) {
      closeBtn.addEventListener("click", function () { dismissToastEl(el); });
    }

    // Pause-on-hover/focus: a countdown that keeps running while a user's
    // pointer or keyboard focus is on the toast can dismiss it out from
    // under them mid-read. Tracked by wall-clock remaining time (not a
    // fixed re-arm) so pausing and resuming any number of times still adds
    // up to the original duration, not a fresh one each time.
    var timer = null;
    var remaining = duration;
    var startedAt = 0;

    function arm() {
      if (remaining <= 0) return;
      startedAt = Date.now();
      timer = window.setTimeout(function () { dismissToastEl(el); }, remaining);
    }
    function pause() {
      if (!timer) return;
      window.clearTimeout(timer);
      timer = null;
      remaining -= (Date.now() - startedAt);
    }

    if (duration > 0) {
      arm();
      el.addEventListener("mouseenter", pause);
      el.addEventListener("mouseleave", arm);
      el.addEventListener("focusin", pause);
      el.addEventListener("focusout", arm);
    }

    return {
      close: function () {
        if (timer) { window.clearTimeout(timer); timer = null; }
        dismissToastEl(el);
      }
    };
  }

  window.ArtificeToast = {
    show: show,
    success: function (message, opts) { return show(message, "success", opts); },
    warning: function (message, opts) { return show(message, "warning", opts); },
    error: function (message, opts) { return show(message, "error", opts); },
    info: function (message, opts) { return show(message, "info", opts); }
  };
})();
