// SPDX-FileCopyrightText: 2026 Maurice Casey
//
// SPDX-License-Identifier: AGPL-3.0-or-later

/* byom.js — Bring-Your-Own-Model onboarding screen.
 *
 * Ships from packages/shared-ui and is loaded by all four apps, which is
 * why this file is written in ES5: artifice-graph's static/app.js (the
 * house style this follows — IIFE, "use strict", a namespace object on
 * `window`) is the only prior shared-JS convention in the suite, but the
 * other three apps run modern JS. ES5 is the floor that runs in all four.
 *
 * Namespacing precedent: this is the first JavaScript packages/shared-ui
 * has ever shipped, so the choice here sets the pattern for anything shared
 * that follows it. graph's own app.js exposes `window.Callopp` — a name
 * that does not derive from "artifice" or "graph" and appears to be a
 * leftover from an unrelated codebase (see the maintainer's environment
 * notes). That name is not repeated here. This file exposes a
 * single global, `window.ArtificeByom`, prefixed with the suite name so it
 * cannot collide with any app-local global (app.js, palette.js, etc. in
 * each app already define their own page-scoped identifiers).
 *
 * Unlike app.js's house style, this file does NOT self-initialise on
 * DOMContentLoaded. graph's app.js runs its init() unconditionally because
 * it owns one fixed page. byom.js is a component invoked on demand by four
 * different host pages with four different DOMs — it has nothing to
 * initialise until a caller asks for it. The public surface is a factory:
 *
 *   var byom = window.ArtificeByom.create({
 *     appName: "OCR Pipeline",
 *     onConfigured: function (result) { ... }   // called after a
 *                                                // successful Test connection
 *   });
 *   byom.open();
 *
 * `create()` builds nothing until `open()` is called, and `open()` can be
 * called again after `close()` — each call re-fetches state so the screen
 * never shows stale detection results.
 *
 * Talks to the /api/byom/* contract defined in the phase6 brief (not yet
 * implemented — see server-side TODO for Step 4). A caller may override the
 * transport via `opts.fetchImpl`, which is how the dev-only preview route
 * in artifice-ocr exercises every state with fixture data and no backend:
 * `fetchImpl(url, opts)` must return a Promise that resolves to the
 * *parsed* JSON body (matching packages/model-harness's dataclasses field
 * for field), not a Response object. The default implementation wraps
 * window.fetch to satisfy that contract.
 *
 * KNOWN CONTRACT MISMATCH, deliberately not resolved silently: the brief's
 * GET /api/byom/state example shows each recommendation as
 * `{ name, why, size_bytes }`. model_harness.registry.ModelRecommendation
 * (registry.py:167-181) has no `why` or `size_bytes` field at all, and
 * calls the model identifier `model_name`, not `name` — only
 * AsrModelInfo (a different dataclass, for artifice-transcribe) carries
 * size_bytes. This file reads `model_name` and `min_vram_gb`, the fields
 * the dataclass actually has, rather than the brief's example shape, per
 * the brief's own instruction to mirror model_harness rather than invent a
 * shape the backend cannot produce. Step 4's /api/byom/state handler must
 * serialise ModelRecommendation with these field names (or the route can
 * rename them in its response — either way, `why`/`size_bytes` cannot
 * come from this dataclass as it stands today).
 */

(function () {
  "use strict";

  // ── Motion ────────────────────────────────────────────────────────────
  // Design_Philosophy.md §7 line 501: JS-driven animation must check
  // reduced-motion before triggering, not rely solely on the CSS media
  // query catching it.

  function prefersReducedMotion() {
    return !!(window.matchMedia && window.matchMedia("(prefers-reduced-motion: reduce)").matches);
  }

  // ── Small helpers ────────────────────────────────────────────────────

  function escapeHtml(s) {
    return String(s == null ? "" : s).replace(/[&<>"']/g, function (c) {
      var map = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
      return map[c];
    });
  }

  function qs(root, sel) { return root.querySelector(sel); }
  function qsa(root, sel) { return Array.prototype.slice.call(root.querySelectorAll(sel)); }

  function elFromHtml(html) {
    var wrap = document.createElement("div");
    wrap.innerHTML = html;
    return wrap.firstElementChild;
  }

  // Default transport: real fetch, parsed to JSON, contract-uniform with
  // the fixture-backed override the preview route supplies (see file
  // header). Throws on non-2xx with the server's own `hint`/`error` field
  // as the message, so callers never have to branch on status separately.
  function defaultFetchImpl(url, opts) {
    opts = opts || {};
    var headers = opts.headers || {};
    var body = opts.body;
    if (body && typeof body !== "string") {
      headers["Content-Type"] = "application/json";
      body = JSON.stringify(body);
    }
    return window.fetch(url, {
      method: opts.method || "GET",
      headers: headers,
      body: body
    }).then(function (r) {
      return r.json()["catch"](function () { return {}; }).then(function (data) {
        if (!r.ok) {
          var message = (data && (data.hint || data.error)) || ("HTTP " + r.status);
          var err = new Error(message);
          err.status = r.status;
          err.data = data;
          throw err;
        }
        return data;
      });
    });
  }

  function copyToClipboard(text) {
    if (window.navigator && window.navigator.clipboard && window.navigator.clipboard.writeText) {
      return window.navigator.clipboard.writeText(text);
    }
    return new Promise(function (resolve, reject) {
      try {
        var ta = document.createElement("textarea");
        ta.value = text;
        ta.setAttribute("readonly", "");
        ta.style.position = "fixed";
        ta.style.left = "-9999px";
        document.body.appendChild(ta);
        ta.select();
        var ok = document.execCommand("copy");
        document.body.removeChild(ta);
        if (ok) { resolve(); } else { reject(new Error("copy command failed")); }
      } catch (e) {
        reject(e);
      }
    });
  }

  // ── Static content: hardware tiers ──────────────────────────────────
  // Values match model_harness.registry.HardwareTier exactly (laptop,
  // desktop, mac_unified) — see packages/model-harness/src/model_harness/
  // registry.py. Icons are line-drawn per Design_Philosophy.md §8.8 (viewBox
  // 0 0 24 24, stroke=currentColor, stroke-width 2, round caps, aria-hidden).
  // No product screenshots or trademarked logos — see the brief's
  // "no screenshots" decision; the Apple Silicon tier is represented as a
  // generic chip-with-pins glyph, not an Apple mark.

  var ICON_LAPTOP =
    '<svg viewBox="0 0 24 24" width="26" height="26" fill="none" stroke="currentColor" stroke-width="2" ' +
    'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
    '<rect x="4" y="4" width="16" height="10" rx="1"></rect>' +
    '<path d="M2 18h20l-2 2H4l-2-2z"></path>' +
    "</svg>";

  var ICON_DESKTOP =
    '<svg viewBox="0 0 24 24" width="26" height="26" fill="none" stroke="currentColor" stroke-width="2" ' +
    'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
    '<rect x="6" y="3" width="12" height="18" rx="1"></rect>' +
    '<line x1="9" y1="7" x2="15" y2="7"></line>' +
    '<line x1="9" y1="11" x2="15" y2="11"></line>' +
    '<circle cx="12" cy="17" r="1"></circle>' +
    "</svg>";

  var ICON_CHIP =
    '<svg viewBox="0 0 24 24" width="26" height="26" fill="none" stroke="currentColor" stroke-width="2" ' +
    'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
    '<rect x="6" y="6" width="12" height="12" rx="1"></rect>' +
    '<line x1="9" y1="2" x2="9" y2="6"></line><line x1="15" y1="2" x2="15" y2="6"></line>' +
    '<line x1="9" y1="18" x2="9" y2="22"></line><line x1="15" y1="18" x2="15" y2="22"></line>' +
    '<line x1="2" y1="9" x2="6" y2="9"></line><line x1="2" y1="15" x2="6" y2="15"></line>' +
    '<line x1="18" y1="9" x2="22" y2="9"></line><line x1="18" y1="15" x2="22" y2="15"></line>' +
    "</svg>";

  var TIERS = [
    { value: "laptop", label: "Laptop", hint: "8–16GB memory, built-in graphics", icon: ICON_LAPTOP },
    { value: "desktop", label: "Desktop", hint: "A dedicated graphics card (12GB+ VRAM)", icon: ICON_DESKTOP },
    { value: "mac_unified", label: "Apple Silicon Mac", hint: "M-series chip, unified memory", icon: ICON_CHIP }
  ];

  var ICON_CLOSE =
    '<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2" ' +
    'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
    '<line x1="4" y1="4" x2="20" y2="20"></line><line x1="20" y1="4" x2="4" y2="20"></line></svg>';

  var ICON_COPY =
    '<svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor" stroke-width="2" ' +
    'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
    '<rect x="9" y="9" width="11" height="11" rx="1"></rect>' +
    '<path d="M5 15H4a1 1 0 01-1-1V4a1 1 0 011-1h10a1 1 0 011 1v1"></path></svg>';

  var ICON_CHECK =
    '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" ' +
    'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><polyline points="20 6 9 17 4 12"></polyline></svg>';

  var ICON_FAIL =
    '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="2" ' +
    'stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">' +
    '<circle cx="12" cy="12" r="9"></circle><line x1="9" y1="9" x2="15" y2="15"></line><line x1="15" y1="9" x2="9" y2="15"></line></svg>';

  // No icon glyph for the detect callout's states — an earlier version had
  // one (a "seeking" magnifying glass, a plain dot, a warning triangle) but
  // at rendered size it read as a stray bullet or broken glyph rather than
  // an intentional mark. See the matching comment on .byom-detect in
  // byom.css: the callout now uses a left accent rule instead, and the
  // state's full meaning lives in the text (role="status" aria-live).

  var TIER_STORAGE_KEY = "artifice_byom_hardware_tier";

  function getStoredTier() {
    try { return window.localStorage.getItem(TIER_STORAGE_KEY); } catch (e) { return null; }
  }
  function setStoredTier(tier) {
    try { window.localStorage.setItem(TIER_STORAGE_KEY, tier); } catch (e) { /* ignore */ }
  }

  // ── Per-app copy ─────────────────────────────────────────────────────
  // One shared component, four different framings: ocr requires a
  // vision-capable model, graph needs two separate endpoints (an LLM and
  // a separate embedding server), and transcribe's endpoint is genuinely
  // optional — it powers only the post-transcription summarise/cleanup
  // actions (api/v1/routes.py:614, :652), never transcription itself,
  // which runs Whisper/pyannote downloaded separately and needs none of
  // this. Slugs below match GET /api/byom/state's `app` field exactly.

  var APP_OCR = "artifice-ocr";
  var APP_DRAFT = "artifice-draft";
  var APP_GRAPH = "artifice-graph";
  var APP_TRANSCRIBE = "artifice-transcribe";

  // titleFor/ledeFor are called only from _renderIdentity(), i.e. only
  // after GET /api/byom/state has resolved and appSlug is known — see the
  // timing note on _build()'s static placeholder lede below for why
  // nothing app-specific can be written any earlier than that.

  function titleFor(appSlug) {
    if (appSlug === APP_TRANSCRIBE) { return "Connect a model for summaries (optional)"; }
    if (appSlug === APP_GRAPH) { return "Connect local models"; }
    return "Connect a local model";
  }

  function ledeFor(appSlug, appName) {
    var name = escapeHtml(appName);
    if (appSlug === APP_TRANSCRIBE) {
      // Deliberately does not use "transcribe(s)" as the verb here — with
      // an appName that is itself some form of "Transcribe", "X
      // transcribes audio" reads as a stumble (repeats the app's own name
      // as its verb). This is also the *only* place optionality is
      // stated — the numbered steps below no longer repeat it as their
      // own step (see _transcribeSteps and D4/D5 in the review notes).
      return name + " turns audio into text using Whisper and pyannote, downloaded separately the " +
        "first time you run a job &mdash; that works fully offline and needs none of this. This " +
        "screen only configures an optional endpoint used to summarise and clean up a transcript " +
        "once it is finished, if you choose to use those features.";
    }
    if (appSlug === APP_OCR) {
      return name + " reads page images and needs a model that can <strong>see</strong> the " +
        "page &mdash; a text-only model will not work here. Connect one below: a small program " +
        "that runs on this computer, so nothing you scan is ever sent anywhere else.";
    }
    if (appSlug === APP_GRAPH) {
      return name + " needs two local models to build a knowledge graph: a language model to " +
        "extract entities and relations from your text, and a separate embedding model to match " +
        "entities that refer to the same thing. Connect both below &mdash; small programs that " +
        "run on this computer, so nothing you process here is ever sent anywhere else.";
    }
    // artifice-draft, and any app slug this file does not yet special-case.
    return name + " runs entirely on your machine. To do its work it needs a language model " +
      "&mdash; but instead of shipping one, you connect your own: a small program that runs on " +
      "this computer, so nothing you process here is ever sent anywhere else.";
  }

  // ── Badges helper (§8.17 Source Badge) ────────────────────────────────

  // Renders ethos_badges (array of strings, possibly empty/undefined) into
  // badge markup, or the empty string when the array is absent or empty.
  // Every string goes through escapeHtml() — defence in depth even though
  // the vocabulary is server-validated (PERMITTED_BADGES, registry.py:184).
  function _badgesHtml(badges) {
    if (!badges || !badges.length) { return ""; }
    var inner = badges.map(function (b) {
      return '<span class="byom-badge">' + escapeHtml(b) + "</span>";
    }).join("");
    return '<div class="byom-badges">' + inner + "</div>";
  }

  // ── Byom ─────────────────────────────────────────────────────────────

  function Byom(opts) {
    opts = opts || {};
    this.appName = opts.appName || document.title || "this app";
    this.fetchImpl = opts.fetchImpl || defaultFetchImpl;
    this.onConfigured = typeof opts.onConfigured === "function" ? opts.onConfigured : function () {};
    // Whether Escape / overlay-click / the close button may dismiss the
    // screen. Step 4's first-run interception may want this false until a
    // model is configured; defaults true so the screen is dismissable
    // during this preview/implementation phase.
    this.dismissable = opts.dismissable !== false;

    this.overlay = null;
    this.modal = null;
    this.lastFocused = null;
    this.state = null;
    this.selectedTier = getStoredTier() || "laptop";

    this._onKeydown = this._onKeydown.bind(this);
    this._onOverlayClick = this._onOverlayClick.bind(this);
  }

  // -- lifecycle -----------------------------------------------------------

  Byom.prototype.open = function (openOpts) {
    openOpts = openOpts || {};
    if (this.overlay) { this.close(); }

    // Remember what had focus so close() can return it there. Only a real,
    // pre-existing focus target counts: on the first-load auto-open (see
    // autostart()) the screen opens with no user interaction at all and
    // document.activeElement is document.body — that is not a trigger to
    // restore focus to, it is the platform default, and restoring "to
    // body" would force focus somewhere deliberately rather than leaving
    // it alone as the first-load case always has.
    var active = document.activeElement;
    this.lastFocused = (active && active !== document.body) ? active : null;
    this._build();
    document.body.appendChild(this.overlay);

    if (prefersReducedMotion()) {
      this.modal.style.animation = "none";
    }

    document.addEventListener("keydown", this._onKeydown, true);
    this.overlay.addEventListener("mousedown", this._onOverlayClick);

    if (openOpts.initialTab === "advanced") {
      this._activateTab("advanced");
    }

    // Focus the close button: always present, gives a predictable first
    // stop for both keyboard and screen-reader users, and is announced
    // alongside the dialog's role/label.
    this.closeBtn.focus();

    this._refresh(openOpts.autoTest);
  };

  Byom.prototype.close = function () {
    if (!this.overlay) return;
    document.removeEventListener("keydown", this._onKeydown, true);
    this.overlay.removeEventListener("mousedown", this._onOverlayClick);
    if (this.overlay.parentNode) { this.overlay.parentNode.removeChild(this.overlay); }
    this.overlay = null;
    this.modal = null;
    // Restore focus to whichever element opened the dialog (e.g. the
    // masthead trigger) — but only if it is still connected to the
    // document. A trigger that was itself removed while the dialog was
    // open (or the null captured for the no-trigger auto-open case above)
    // must not force focus anywhere; leaving activeElement wherever the
    // browser puts it on node removal is correct in that case, not a gap.
    var target = this.lastFocused;
    this.lastFocused = null;
    if (target && typeof target.focus === "function" && document.contains(target)) {
      target.focus();
    }
  };

  // -- keyboard / focus trap ------------------------------------------------

  Byom.prototype._onOverlayClick = function (e) {
    if (e.target === this.overlay && this.dismissable) { this.close(); }
  };

  Byom.prototype._focusableEls = function () {
    var sel = 'a[href], button:not([disabled]), input:not([disabled]):not([type="hidden"]), ' +
      'select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';
    return qsa(this.modal, sel).filter(function (el) {
      return el.offsetWidth > 0 || el.offsetHeight > 0 || el === document.activeElement;
    });
  };

  Byom.prototype._onKeydown = function (e) {
    if (!this.overlay) return;
    var key = e.key || (e.keyCode === 27 ? "Escape" : (e.keyCode === 9 ? "Tab" : ""));
    if (key === "Escape") {
      if (this.dismissable) { this.close(); }
      return;
    }
    if (key === "Tab") {
      var items = this._focusableEls();
      if (items.length === 0) { e.preventDefault(); return; }
      var first = items[0];
      var last = items[items.length - 1];
      if (e.shiftKey && document.activeElement === first) {
        e.preventDefault(); last.focus();
      } else if (!e.shiftKey && document.activeElement === last) {
        e.preventDefault(); first.focus();
      }
    }
  };

  // -- markup ---------------------------------------------------------------

  Byom.prototype._build = function () {
    var self = this;

    var tierCards = TIERS.map(function (t) {
      return (
        '<label class="byom-tier-card" data-tier="' + t.value + '">' +
        '<input type="radio" name="byom-tier" class="byom-tier-radio" value="' + t.value + '">' +
        // Non-colour selection indicator (§9) — a filled check badge,
        // opacity-toggled by .is-checked / :has() in byom.css. Never
        // display:none, so it costs no layout when it appears.
        '<span class="byom-tier-check" aria-hidden="true">' + ICON_CHECK + "</span>" +
        '<span class="byom-tier-figure">' + t.icon + "</span>" +
        '<span class="byom-tier-name">' + escapeHtml(t.label) + "</span>" +
        '<span class="byom-tier-hint">' + escapeHtml(t.hint) + "</span>" +
        "</label>"
      );
    }).join("");

    var html =
      '<div class="byom-overlay" role="presentation">' +
      '<div class="byom-modal" role="dialog" aria-modal="true" aria-labelledby="byomTitle" aria-describedby="byomLede">' +
      '<div class="byom-modal-inner">' +
      // Visually-hidden live region for state changes that a visible label
      // alone would not announce — the copy buttons below change their
      // *visible* text ("Copy" -> "Copied") but keep a static aria-label
      // ("Copy command"), and aria-label wins the accessible-name
      // computation over content, so a screen-reader user would otherwise
      // never hear the confirmation.
      '<div class="byom-sr-only" id="byomSrStatus" role="status" aria-live="polite"></div>' +
      '<div class="byom-head">' +
      '<h2 class="byom-title" id="byomTitle">Connect a local model</h2>' +
      '<button type="button" class="byom-close" id="byomClose" aria-label="Close">' + ICON_CLOSE + "</button>" +
      "</div>" +
      // Neutral-until-known placeholder. this.state is null here — _build()
      // runs at open() time, before _refresh()'s GET /api/byom/state fetch
      // resolves, so which app this is (and therefore whether it actually
      // *needs* a model, which is false for transcribe) is not yet known.
      // This sentence is true for all four apps regardless of that, so a
      // slow first paint shows something accurate rather than something
      // later corrected. _renderIdentity() overwrites it once state loads.
      '<p class="byom-lede" id="byomLede">' + escapeHtml(this.appName) +
      " can connect to a model server that runs on your own machine, so nothing you process here " +
      "is ever sent anywhere else.</p>" +
      '<div class="byom-detect" id="byomDetect" data-state="detecting" role="status" aria-live="polite">' +
      '<div class="byom-detect-inner">' +
      '<span class="byom-detect-text" id="byomDetectText">Looking for a model server on your machine&hellip;</span>' +
      "</div>" +
      '<div class="byom-detect-actions" id="byomDetectActions"></div>' +
      "</div>" +
      '<div class="byom-tabs" role="tablist" aria-label="Setup path">' +
      '<button type="button" class="byom-tab" id="byomTabBeginner" role="tab" aria-selected="true" ' +
      'aria-controls="byomPanelBeginner">New to this</button>' +
      '<button type="button" class="byom-tab" id="byomTabAdvanced" role="tab" aria-selected="false" ' +
      'aria-controls="byomPanelAdvanced" tabindex="-1">Done this before</button>' +
      "</div>" +
      '<div class="byom-panel" id="byomPanelBeginner" role="tabpanel" aria-labelledby="byomTabBeginner">' +
      '<fieldset class="byom-tier">' +
      '<legend class="byom-tier-legend">What machine are you using?</legend>' +
      '<div class="byom-tier-grid" id="byomTierGrid">' + tierCards + "</div>" +
      "</fieldset>" +
      '<ol class="byom-steps" id="byomSteps"></ol>' +
      "</div>" +
      '<div class="byom-panel" id="byomPanelAdvanced" role="tabpanel" aria-labelledby="byomTabAdvanced" hidden>' +
      '<p class="byom-advanced-lede">Commands are grouped by hardware &mdash; use the block that matches yours.</p>' +
      // Stated once: Ollama/LM Studio's ports don't vary by hardware tier,
      // so this used to repeat verbatim under all three groups below.
      '<p class="byom-endpoints-note">Whichever block you use, Ollama listens on ' +
      "<code>http://localhost:11434</code> and LM Studio on <code>http://localhost:1234/v1</code>.</p>" +
      '<div id="byomAdvancedGroups"></div>' +
      "</div>" +
      '<div class="byom-connect">' +
      // Text is neutral ("Connect") at build time and stays that for
      // draft/ocr/transcribe — only graph relabels this in
      // _renderIdentity() to "Language model", so its two endpoint
      // sections read as a named pair rather than identical twins (D2).
      '<h3 class="byom-connect-title" id="byomConnectTitle">Connect</h3>' +
      '<div class="byom-field">' +
      '<label for="byomUrl">Server URL</label>' +
      '<input type="text" id="byomUrl" class="byom-input" placeholder="http://localhost:11434" autocomplete="off">' +
      "</div>" +
      '<details class="byom-apikey"><summary>API key (optional &mdash; only needed for some servers)</summary>' +
      '<div class="byom-field"><label for="byomApiKey">API key</label>' +
      '<input type="password" id="byomApiKey" class="byom-input" autocomplete="off"></div>' +
      "</details>" +
      '<div class="byom-connect-actions">' +
      '<button type="button" class="byom-btn-primary" id="byomTestBtn">Test connection</button>' +
      '<div class="byom-result" id="byomResult" role="status" aria-live="polite" data-state="idle">Not tested yet.</div>' +
      "</div>" +
      // Model choice. Hidden until a connection test succeeds, because the
      // options are the models that endpoint actually reports — offering a
      // picker before we know what is installed is how the suite ended up
      // shipping model names nobody had. One row per role: most apps have a
      // single "chat" role, ocr has three and graph has two.
      '<div class="byom-modelpick" id="byomModelPick" hidden>' +
      '<div class="byom-modelpick-rows" id="byomModelRows"></div>' +
      '<button type="button" class="byom-btn-primary" id="byomModelSave">Use these models</button>' +
      '<div class="byom-result" id="byomModelResult" role="status" aria-live="polite" data-state="idle"></div>' +
      "</div>" +
      "</div>" +
      // Second, independent endpoint — graph only. Present in the DOM for
      // every app (this markup is built before state is known) but hidden
      // by default; _renderIdentity() unhides it only when state.app is
      // artifice-graph and state carries an `embedding` key, so the other
      // three apps' layout is byte-for-byte what it was before this block
      // existed. #byomEmbedResult is its own aria-live region, deliberately
      // separate from #byomResult — two endpoints must never share one
      // live region, or a screen-reader user cannot tell which endpoint a
      // status message is about.
      '<div class="byom-connect" id="byomEmbedConnect" hidden>' +
      '<h3 class="byom-connect-title">Embedding server</h3>' +
      '<p class="byom-advanced-lede" id="byomEmbedHint"></p>' +
      '<div class="byom-field">' +
      '<label for="byomEmbedUrl">Server URL</label>' +
      '<input type="text" id="byomEmbedUrl" class="byom-input" placeholder="http://localhost:11434" autocomplete="off">' +
      "</div>" +
      '<details class="byom-apikey"><summary>API key (optional &mdash; only needed for some servers)</summary>' +
      '<div class="byom-field"><label for="byomEmbedApiKey">API key</label>' +
      '<input type="password" id="byomEmbedApiKey" class="byom-input" autocomplete="off"></div>' +
      "</details>" +
      '<div class="byom-connect-actions">' +
      '<button type="button" class="byom-btn-primary" id="byomEmbedTestBtn">Test connection</button>' +
      '<div class="byom-result" id="byomEmbedResult" role="status" aria-live="polite" data-state="idle">Not tested yet.</div>' +
      "</div>" +
      "</div>" +
      "</div></div></div>";

    this.overlay = elFromHtml(html);
    this.modal = qs(this.overlay, ".byom-modal");
    this.closeBtn = qs(this.overlay, "#byomClose");
    this.detectEl = qs(this.overlay, "#byomDetect");
    this.detectText = qs(this.overlay, "#byomDetectText");
    this.detectActions = qs(this.overlay, "#byomDetectActions");
    this.tabBeginner = qs(this.overlay, "#byomTabBeginner");
    this.tabAdvanced = qs(this.overlay, "#byomTabAdvanced");
    this.panelBeginner = qs(this.overlay, "#byomPanelBeginner");
    this.panelAdvanced = qs(this.overlay, "#byomPanelAdvanced");
    this.tierGrid = qs(this.overlay, "#byomTierGrid");
    this.stepsEl = qs(this.overlay, "#byomSteps");
    this.advancedGroupsEl = qs(this.overlay, "#byomAdvancedGroups");
    this.urlInput = qs(this.overlay, "#byomUrl");
    this.apiKeyInput = qs(this.overlay, "#byomApiKey");
    this.testBtn = qs(this.overlay, "#byomTestBtn");
    this.resultEl = qs(this.overlay, "#byomResult");
    this.srStatusEl = qs(this.overlay, "#byomSrStatus");
    this.titleEl = qs(this.overlay, "#byomTitle");
    this.ledeEl = qs(this.overlay, "#byomLede");
    this.connectTitleEl = qs(this.overlay, "#byomConnectTitle");
    this.embedConnectEl = qs(this.overlay, "#byomEmbedConnect");
    this.embedHintEl = qs(this.overlay, "#byomEmbedHint");
    this.embedUrlInput = qs(this.overlay, "#byomEmbedUrl");
    this.embedApiKeyInput = qs(this.overlay, "#byomEmbedApiKey");
    this.embedTestBtn = qs(this.overlay, "#byomEmbedTestBtn");
    this.embedResultEl = qs(this.overlay, "#byomEmbedResult");

    this.closeBtn.addEventListener("click", function () { if (self.dismissable) { self.close(); } });
    this.tabBeginner.addEventListener("click", function () { self._activateTab("beginner"); });
    this.tabAdvanced.addEventListener("click", function () { self._activateTab("advanced"); });
    this.tabBeginner.addEventListener("keydown", function (e) { self._onTabKeydown(e); });
    this.tabAdvanced.addEventListener("keydown", function (e) { self._onTabKeydown(e); });

    this.tierGrid.addEventListener("change", function (e) {
      if (e.target && e.target.name === "byom-tier") {
        self.selectedTier = e.target.value;
        setStoredTier(self.selectedTier);
        self._markSelectedTier();
        self._renderSteps();
      }
    });

    this.testBtn.addEventListener("click", function () { self._testConnection(); });
    this.embedTestBtn.addEventListener("click", function () { self._testEmbeddingConnection(); });

    this.overlay.addEventListener("click", function (e) {
      var copyBtn = e.target.closest ? e.target.closest(".byom-copy") : null;
      if (copyBtn) { self._handleCopy(copyBtn); }
    });
  };

  Byom.prototype._onTabKeydown = function (e) {
    var key = e.key;
    if (key !== "ArrowLeft" && key !== "ArrowRight") return;
    e.preventDefault();
    this._activateTab(document.activeElement === this.tabBeginner ? "advanced" : "beginner");
    (document.activeElement === this.tabBeginner ? this.tabAdvanced : this.tabBeginner).focus();
  };

  Byom.prototype._activateTab = function (name) {
    var beginner = name === "beginner";
    this.tabBeginner.setAttribute("aria-selected", beginner ? "true" : "false");
    this.tabBeginner.setAttribute("tabindex", beginner ? "0" : "-1");
    this.tabAdvanced.setAttribute("aria-selected", beginner ? "false" : "true");
    this.tabAdvanced.setAttribute("tabindex", beginner ? "-1" : "0");
    if (beginner) {
      this.panelBeginner.removeAttribute("hidden");
      this.panelAdvanced.setAttribute("hidden", "");
    } else {
      this.panelAdvanced.removeAttribute("hidden");
      this.panelBeginner.setAttribute("hidden", "");
    }
  };

  // -- data / rendering -------------------------------------------------

  Byom.prototype._refresh = function (autoTest) {
    var self = this;
    this._setDetect("detecting", "Looking for a model server on your machine…");

    this.fetchImpl("/api/byom/state")
      .then(function (state) {
        self.state = state;
        self._renderIdentity();
        self._renderSteps();
        self._renderAdvancedGroups();
        self._markSelectedTier();
        return self.fetchImpl("/api/byom/detect");
      })
      .then(function (detect) {
        self._renderDetect(detect);
        if (autoTest) { self._testConnection(autoTest.url, autoTest.apiKey); }
      })
      ["catch"](function () {
        self._setDetect("error",
          "Could not check for a local model server automatically. You can still connect one below.");
      });
  };

  Byom.prototype._setDetect = function (state, text) {
    this.detectEl.setAttribute("data-state", state);
    this.detectText.textContent = text;
  };

  Byom.prototype._renderDetect = function (detect) {
    var self = this;
    var endpoints = (detect && detect.endpoints) || [];
    var found = null;
    for (var i = 0; i < endpoints.length; i++) {
      if (endpoints[i].reachable) { found = endpoints[i]; break; }
    }
    this.detectActions.innerHTML = "";
    if (found) {
      var modelCount = (found.models || []).length;
      var modelsText = modelCount
        ? modelCount + " model" + (modelCount === 1 ? "" : "s") + " available"
        : "no models pulled yet";
      this._setDetect("found",
        "Found " + (found.name || found.provider || "a server") + " running locally — " + modelsText + ".");
      var useBtn = elFromHtml('<button type="button" class="byom-btn-primary">Use this endpoint</button>');
      useBtn.addEventListener("click", function () {
        self.urlInput.value = found.url;
        self._testConnection(found.url, "");
      });
      this.detectActions.appendChild(useBtn);
    } else {
      this._setDetect("not-found",
        "No local model server found yet — that's normal on a first run. Follow the steps below.");
    }
  };

  Byom.prototype._markSelectedTier = function () {
    var cards = qsa(this.tierGrid, ".byom-tier-card");
    for (var i = 0; i < cards.length; i++) {
      var isSel = cards[i].getAttribute("data-tier") === this.selectedTier;
      cards[i].classList.toggle("is-checked", isSel);
      var radio = qs(cards[i], ".byom-tier-radio");
      radio.checked = isSel;
    }
  };

  // Sets the title/lede and the embedding-server section from state.app —
  // the one method allowed to write app-specific copy, because it only
  // runs after GET /api/byom/state has resolved (see the timing note atop
  // titleFor/ledeFor and on the _build() placeholder lede).
  Byom.prototype._renderIdentity = function () {
    var appSlug = this.state && this.state.app;
    this.titleEl.textContent = titleFor(appSlug);
    this.ledeEl.innerHTML = ledeFor(appSlug, this.appName);
    // "Connect" says nothing about *what* it connects — indistinguishable
    // from "Embedding server" below it when graph has two endpoint
    // sections stacked with the same field labels and button text. Only
    // graph relabels; the other three apps keep the plain "Connect" they
    // had before this existed (D2).
    this.connectTitleEl.textContent = appSlug === APP_GRAPH ? "Language model" : "Connect";

    var embedding = (appSlug === APP_GRAPH && this.state) ? this.state.embedding : null;
    if (embedding) {
      this.embedConnectEl.removeAttribute("hidden");
      // Prefill is conditional on `embedding.configured`, not merely on
      // `embedding.endpoint` being present — the LLM field's placeholder
      // vs. real-value distinction is exactly what tells a user whether
      // an endpoint is actually set up, and this field must carry the
      // same signal. Before this fix, an unconfigured embedder rendered
      // its default URL as a real value indistinguishable from a saved
      // one, so a user would see a "filled in" field, assume it was
      // configured, and skip it — the Run All stage would then fail on
      // an embedder nobody had actually set up, which is the exact
      // failure this graph task exists to prevent. `configured: false`
      // shows the suggested URL as a grey placeholder instead (matching
      // #byomUrl's own untouched treatment); `configured: true` shows it
      // as a real prefilled value, because it then reflects a genuine
      // saved setting rather than a suggestion.
      if (embedding.endpoint) {
        if (embedding.configured) {
          this.embedUrlInput.value = embedding.endpoint;
        } else {
          this.embedUrlInput.placeholder = embedding.endpoint;
        }
      }
      this.embedHintEl.textContent = embedding.model ? "Recommended model: " + embedding.model : "";
    } else {
      this.embedConnectEl.setAttribute("hidden", "");
    }
  };

  Byom.prototype._recommendationsFor = function (tier) {
    if (!this.state || !this.state.recommendations) return [];
    return this.state.recommendations[tier] || [];
  };

  // Shared Ollama-install steps (ocr, draft, and the fallback for any
  // unrecognised app), the pull step tailored per app: ocr must say the
  // model has to be vision-capable and *where* — the pull step, not only
  // the lede — because that is the point a user acts on it. `appSlug` is
  // only ever APP_OCR or something else here; APP_GRAPH and APP_TRANSCRIBE
  // build their own step lists below and call this one for the shared
  // first three steps.
  //
  // Field is `model_name`, not `name` — see the file-level note on the
  // /api/byom/state contract mismatch (model_harness.registry
  // .ModelRecommendation has no `name`, `why` or `size_bytes` field; this
  // reads the fields the dataclass actually has: model_name, vision,
  // min_vram_gb).
  Byom.prototype._defaultSteps = function (top, appSlug) {
    var isVisionApp = appSlug === APP_OCR;
    var pullCmd = top ? "ollama pull " + top.model_name : null;
    var whyText = top && top.min_vram_gb
      ? "recommended for machines with at least " + top.min_vram_gb + "GB VRAM"
      : "";
    var visionNote = isVisionApp
      ? " It must be a vision-capable model &mdash; a text-only model will not work here."
      : "";
    var pullDesc = pullCmd
      ? "Open a terminal and run the command below" + (whyText ? " — " + escapeHtml(whyText) : "") + "." + visionNote
      : "No recommendation is available for this hardware tier yet &mdash; pull any" +
        (isVisionApp ? " vision-capable" : "") + " model from ollama.com/library that fits your machine." + visionNote;

    return [
      {
        title: "Download Ollama",
        desc: "Get the installer for your operating system from ollama.com — it is free and open source. " +
          "(LM Studio is a good alternative if you prefer a graphical model browser.)"
      },
      {
        title: "Install and open it",
        desc: "Ollama runs quietly in the background once installed — there is no window to keep open."
      },
      {
        title: isVisionApp ? "Pull a vision-capable model" : "Pull a model sized for your machine",
        desc: pullDesc,
        code: pullCmd || undefined,
        badges: top && top.ethos_badges
      },
      {
        title: "Test the connection",
        desc: "Use the Test connection button below once the model has finished downloading."
      }
    ];
  };

  // Inserts *step* immediately before the list's terminal "Test the
  // connection" step, found by title rather than by index — so this stays
  // correct even if the shared step list this is spliced into ever grows
  // another step after the pull step. A plain .push() would put the new
  // step after "Test the connection", which is a real ordering bug (D1):
  // it told a user to test the connection before pulling a model the app
  // requires. Falls back to appending only if no such step exists, which
  // should not happen given _defaultSteps always ends with it.
  Byom.prototype._insertBeforeTestStep = function (steps, step) {
    var idx = steps.length;
    for (var i = 0; i < steps.length; i++) {
      if (steps[i].title === "Test the connection") { idx = i; break; }
    }
    steps.splice(idx, 0, step);
    return steps;
  };

  // Graph needs a second, separate model — see the lede in ledeFor(). The
  // embedding pull command reads state.embedding.model directly rather
  // than the recommendations registry: model_harness.registry has no
  // embedding-model table (only _RECOMMENDATIONS, which is LLM/vision
  // guidance), so state.embedding is the only source for this figure.
  Byom.prototype._graphSteps = function (top) {
    var steps = this._defaultSteps(top, APP_GRAPH);
    var embedding = this.state && this.state.embedding;
    var embedModel = embedding && embedding.model;
    var embedCmd = embedModel ? "ollama pull " + embedModel : null;
    return this._insertBeforeTestStep(steps, {
      title: "Pull the embedding model too",
      desc: embedCmd
        ? "This app also needs a separate, smaller embedding model to match entities that refer to " +
          "the same thing across your notes. Run the command below."
        : "This app also needs a separate embedding model to match entities that refer to the same " +
          "thing across your notes &mdash; pull one (bge-m3 is a good default) and set it in the " +
          "Embedding server field below.",
      code: embedCmd || undefined
    });
  };

  // Transcribe's endpoint is optional — stated once, in the lede (see
  // ledeFor() and the KNOWN CONTRACT MISMATCH note atop this file). It
  // used to repeat as this list's own step 1 ("This step is optional"),
  // which was wrong twice over: it duplicated the lede almost verbatim
  // within one viewport, and a statement *about* the list is not itself
  // one of its actions — it also pushed the four real actions to steps
  // 2-5, out of step with the other three apps' 1-4. Removed; the actions
  // below now renumber to 1-4 on their own (D4, D5).
  Byom.prototype._transcribeSteps = function (top) {
    var pullCmd = top ? "ollama pull " + top.model_name : null;
    var whyText = top && top.min_vram_gb
      ? "recommended for machines with at least " + top.min_vram_gb + "GB VRAM"
      : "";
    return [
      {
        title: "Download Ollama",
        desc: "If you want those two actions, get the installer for your operating system from " +
          "ollama.com &mdash; it is free and open source. (LM Studio is a good alternative if you " +
          "prefer a graphical model browser.)"
      },
      {
        title: "Install and open it",
        desc: "Ollama runs quietly in the background once installed &mdash; there is no window to keep open."
      },
      {
        title: "Pull a text model sized for your machine",
        desc: pullCmd
          ? "Open a terminal and run the command below" + (whyText ? " — " + escapeHtml(whyText) : "") + "."
          : "No recommendation is available for this hardware tier yet &mdash; pull any small " +
            "instruct model from ollama.com/library.",
        code: pullCmd || undefined,
        badges: top && top.ethos_badges
      },
      {
        title: "Test the connection",
        desc: "Use the Test connection button below once the model has finished downloading."
      }
    ];
  };

  Byom.prototype._stepsHtml = function (steps) {
    return steps.map(function (s, idx) {
      var codeHtml = s.code
        ? '<div class="byom-code-row"><pre class="byom-code"><code>' + escapeHtml(s.code) + "</code></pre>" +
          '<button type="button" class="byom-copy" data-copy="' + escapeHtml(s.code) + '" aria-label="Copy command">' +
          ICON_COPY + "<span>Copy</span></button></div>"
        : "";
      var badgesHtml = _badgesHtml(s.badges);
      return (
        '<li class="byom-step">' +
        '<span class="byom-step-num" aria-hidden="true">' + (idx + 1) + "</span>" +
        '<div class="byom-step-body">' +
        '<p class="byom-step-title">' + escapeHtml(s.title) + "</p>" +
        '<p class="byom-step-desc">' + s.desc + "</p>" +
        codeHtml +
        badgesHtml +
        "</div></li>"
      );
    }).join("");
  };

  Byom.prototype._renderSteps = function () {
    var appSlug = this.state && this.state.app;
    var recs = this._recommendationsFor(this.selectedTier);
    var top = recs.length ? recs[0] : null;
    var steps;
    if (appSlug === APP_TRANSCRIBE) {
      steps = this._transcribeSteps(top);
    } else if (appSlug === APP_GRAPH) {
      steps = this._graphSteps(top);
    } else {
      steps = this._defaultSteps(top, appSlug);
    }
    this.stepsEl.innerHTML = this._stepsHtml(steps);
  };

  Byom.prototype._renderAdvancedGroups = function () {
    var self = this;
    var appSlug = this.state && this.state.app;
    var isVisionApp = appSlug === APP_OCR;
    // Stated once for the whole tab, not once per row (D3): every real
    // ocr recommendation is vision-capable, so a label repeated on all six
    // rows never varies and carries no signal — it read as noise breaking
    // up the command blocks' rhythm. The per-row marker below now appears
    // only on a row that *departs* from this norm (the exception is the
    // information; the constant case isn't).
    var visionOnceNote = isVisionApp
      ? '<p class="byom-advanced-lede">Every recommendation below is vision-capable &mdash; ' +
        escapeHtml(this.appName) + " needs a model that can read images, not just text.</p>"
      : "";
    var html = TIERS.map(function (t) {
      var recs = self._recommendationsFor(t.value);
      var rowsHtml;
      if (!recs.length) {
        // No hardcoded model-name fallback (there used to be one,
        // "ollama pull llama3.2:3b") — the registry now has data for
        // every app (see model_harness.registry._RECOMMENDATIONS), so an
        // empty list here means "genuinely nothing known for this tier",
        // and inventing a model name would misrepresent that as guidance.
        rowsHtml = '<p class="byom-code-note">No recommendation is available for this tier yet ' +
          "&mdash; browse ollama.com/library for a model that fits your hardware" +
          (isVisionApp ? " and supports vision" : "") + ".</p>";
      } else if (isVisionApp) {
        // ocr only: surface the vision flag per-model, since this tab is
        // exactly where a user substitutes their own model and a
        // text-only substitution fails with no explanation (Task B) — but
        // only when it's false. A model that is vision-capable, the
        // expected case, gets the same plain row as every other app.
        rowsHtml = recs.map(function (r) {
          var cmd = "ollama pull " + r.model_name;
          var badges = _badgesHtml(r.ethos_badges);
          if (r.vision) {
            return (
              '<div class="byom-code-row"><pre class="byom-code"><code>' + escapeHtml(cmd) + "</code></pre>" +
              '<button type="button" class="byom-copy" data-copy="' + escapeHtml(cmd) + '" aria-label="Copy command">' +
              ICON_COPY + "<span>Copy</span></button></div>" +
              badges
            );
          }
          return (
            '<div class="byom-code-item">' +
            '<div class="byom-code-row"><pre class="byom-code"><code>' + escapeHtml(cmd) + "</code></pre>" +
            '<button type="button" class="byom-copy" data-copy="' + escapeHtml(cmd) + '" aria-label="Copy command">' +
            ICON_COPY + "<span>Copy</span></button></div>" +
            '<p class="byom-code-note">Text only &mdash; will not work for ' + escapeHtml(self.appName) + ".</p>" +
            badges +
            "</div>"
          );
        }).join("");
      } else {
        rowsHtml = recs.map(function (r) {
          var cmd = "ollama pull " + r.model_name;
          var badges = _badgesHtml(r.ethos_badges);
          return (
            '<div class="byom-code-row"><pre class="byom-code"><code>' + escapeHtml(cmd) + "</code></pre>" +
            '<button type="button" class="byom-copy" data-copy="' + escapeHtml(cmd) + '" aria-label="Copy command">' +
            ICON_COPY + "<span>Copy</span></button></div>" +
            badges
          );
        }).join("");
      }
      return (
        '<div class="byom-code-group" data-tier="' + t.value + '">' +
        '<h4 class="byom-code-group-title">' + escapeHtml(t.label) +
        '<span class="byom-code-group-hint">' + escapeHtml(t.hint) + "</span></h4>" +
        rowsHtml +
        "</div>"
      );
    }).join("");
    this.advancedGroupsEl.innerHTML = visionOnceNote + html;
  };

  Byom.prototype._handleCopy = function (btn) {
    var self = this;
    var text = btn.getAttribute("data-copy") || "";
    var label = qs(btn, "span");
    copyToClipboard(text).then(function () {
      btn.setAttribute("data-copied", "true");
      if (label) { label.textContent = "Copied"; }
      // The visible label change above is not itself announced — see the
      // comment on #byomSrStatus in _build for why aria-label wins over
      // content for this button's accessible name.
      if (self.srStatusEl) { self.srStatusEl.textContent = "Command copied to clipboard."; }
      window.setTimeout(function () {
        btn.removeAttribute("data-copied");
        if (label) { label.textContent = "Copy"; }
      }, 1500);
    })["catch"](function () {
      if (label) { label.textContent = "Copy failed"; }
      if (self.srStatusEl) { self.srStatusEl.textContent = "Could not copy the command — copy it manually."; }
      window.setTimeout(function () { if (label) { label.textContent = "Copy"; } }, 1500);
    });
  };

  // `resultEl` is a parameter rather than always `this.resultEl` because
  // graph has two independent result regions (#byomResult and
  // #byomEmbedResult) that must never share one aria-live announcement —
  // see the comment on #byomEmbedResult in _build().
  Byom.prototype._setResultOn = function (resultEl, state, text) {
    resultEl.setAttribute("data-state", state);
    var icon = state === "ok" ? ICON_CHECK : (state === "fail" ? ICON_FAIL : "");
    resultEl.innerHTML = icon ? icon + " " + escapeHtml(text) : escapeHtml(text);
  };

  Byom.prototype._setResult = function (state, text) {
    this._setResultOn(this.resultEl, state, text);
  };

  // Shared by both Test connection buttons — the LLM one (always present)
  // and the embedding one (graph only, hidden for the other three apps).
  // `cfg.onSuccess`, when given, is called with the same
  // {url, apiKey, provider, models} shape onConfigured always received;
  // only the LLM endpoint wires it to `this.onConfigured` today (see the
  // ambiguity noted in the report — there is no separate
  // onEmbeddingConfigured in the frozen contract to wire the embedding
  // test to).
  Byom.prototype._runTest = function (cfg) {
    var self = this;
    var url = cfg.url != null ? cfg.url : cfg.urlInput.value.trim();
    var apiKey = cfg.apiKey != null ? cfg.apiKey : (cfg.apiKeyInput.value || "");
    if (url && cfg.urlInput.value !== url) { cfg.urlInput.value = url; }

    if (!url) {
      self._setResultOn(cfg.resultEl, "idle", "Enter a server URL first.");
      return;
    }

    self._setResultOn(cfg.resultEl, "pending", "Testing connection…");
    cfg.testBtn.disabled = true;

    this.fetchImpl(cfg.endpoint, { method: "POST", body: { url: url, api_key: apiKey } })
      .then(function (result) {
        cfg.testBtn.disabled = false;
        if (result.reachable) {
          var count = (result.models || []).length;
          var modelsText = count
            ? count + " model" + (count === 1 ? "" : "s") + " available"
            : "connected, but no models are pulled yet";
          self._setResultOn(cfg.resultEl, "ok", "Connected — " + modelsText + ".");
          if (cfg.onSuccess) {
            cfg.onSuccess({ url: url, apiKey: apiKey, provider: result.provider, models: result.models });
          }
        } else {
          self._setResultOn(cfg.resultEl, "fail", result.hint || "Could not reach that server.");
        }
      })
      ["catch"](function (err) {
        cfg.testBtn.disabled = false;
        self._setResultOn(cfg.resultEl, "fail", (err && err.message) || "Could not reach that server.");
      });
  };

  Byom.prototype._testConnection = function (url, apiKey) {
    var self = this;
    this._runTest({
      url: url,
      apiKey: apiKey,
      urlInput: this.urlInput,
      apiKeyInput: this.apiKeyInput,
      resultEl: this.resultEl,
      testBtn: this.testBtn,
      endpoint: "/api/byom/test",
      onSuccess: function (info) {
        self._renderModelPicker(info.models || []);
        if (self.onConfigured) { self.onConfigured(info); }
      }
    });
  };

  // ── Model choice ─────────────────────────────────────────────────────────
  //
  // The BYOM screen used to configure an *endpoint* and nothing else: its only
  // POST was the connection test, and no app exposed a route to record which
  // model to use. Apps therefore fell back to a shipped literal — gemma4:12b in
  // draft, gemma2:27b in graph — which most users do not have. These methods
  // and POST /api/byom/model close that.

  // Collects the distinct, truthy `.role` strings from `list` (an array of
  // recommendation objects), appending into `acc` and returning it. Used by
  // _rolesFromState's recommendation-shaped fallback.
  function _collectRoles(list, acc) {
    if (!Array.isArray(list)) { return acc; }
    for (var i = 0; i < list.length; i++) {
      var r = list[i] && list[i].role;
      if (r && acc.indexOf(r) === -1) { acc.push(r); }
    }
    return acc;
  }

  // Returns the distinct, truthy strings in `list`, preserving order.
  function _uniqueRoles(list) {
    var acc = [];
    for (var i = 0; i < list.length; i++) {
      if (list[i] && acc.indexOf(list[i]) === -1) { acc.push(list[i]); }
    }
    return acc;
  }

  // A readable label for a role. ROLE_LABELS covers the four roles the suite
  // knows today; any role without an entry still renders as a title-cased
  // token rather than "undefined".
  function roleLabel(role) {
    if (ROLE_LABELS[role]) { return ROLE_LABELS[role]; }
    var s = String(role == null ? "" : role);
    return s.charAt(0).toUpperCase() + s.slice(1);
  }

  // Distinct roles this app uses, in a stable order.
  //
  // GET /api/byom/state now publishes `roles`, derived server-side from the
  // app's _ROLE_SETTING — the mapping POST /api/byom/model actually honours.
  // That list is authoritative: recommendations are only *suggestions* and
  // may omit a role the app supports (ocr's recommendations carry vision and
  // translation but no chat, so building the picker from them would make
  // cleanup_model unreachable — the exact bug this fixes, moved). The
  // recommendation-shaped fallback below exists only so this shared file keeps
  // working against a server that has not yet been updated to publish `roles`,
  // and it now handles the tier-keyed dict every server actually sends
  // ({laptop: [...], desktop: [...], mac_unified: [...]}) rather than the
  // `recs.models` array no server produces.
  Byom.prototype._rolesFromState = function () {
    var state = this.state || {};
    var roles;

    // 1. The server's own list wins.
    if (Array.isArray(state.roles)) {
      roles = _uniqueRoles(state.roles);
      if (roles.length) { return roles; }
    }

    // 2. Fall back to deriving roles from recommendations, handling both the
    //    flat-array shape the original code assumed and the tier-keyed dict.
    var recs = state.recommendations;
    if (recs) {
      if (Array.isArray(recs)) {
        roles = _collectRoles(recs, []);
      } else {
        roles = [];
        var tiers = Object.keys(recs);
        for (var i = 0; i < tiers.length; i++) {
          _collectRoles(recs[tiers[i]], roles);
        }
      }
      if (roles.length) { return roles; }
    }

    // 3. Nothing known: a single chat role (correct for draft and transcribe).
    return ["chat"];
  };

  var ROLE_LABELS = {
    vision: "Vision / OCR model",
    chat: "Text model",
    translation: "Translation model",
    embedding: "Embedding model"
  };

  Byom.prototype._renderModelPicker = function (models) {
    var wrap = qs(this.overlay, "#byomModelPick");
    var rows = qs(this.overlay, "#byomModelRows");
    if (!wrap || !rows) { return; }

    if (!models.length) {
      // Connected but nothing pulled. Say so rather than showing an empty
      // dropdown, which reads as a broken control.
      wrap.hidden = true;
      return;
    }

    rows.innerHTML = "";
    var roles = this._rolesFromState();
    for (var i = 0; i < roles.length; i++) {
      var role = roles[i];
      var field = document.createElement("div");
      field.className = "byom-field";

      var id = "byomModel_" + role;
      var label = document.createElement("label");
      label.setAttribute("for", id);
      label.textContent = roleLabel(role);
      field.appendChild(label);

      var select = document.createElement("select");
      select.className = "byom-input";
      select.id = id;
      select.setAttribute("data-role", role);

      // "Choose automatically" is first and is the default, because it is the
      // shipped behaviour: an empty choice means the app resolves a model per
      // run from what the endpoint serves. It is a real option, not a null.
      var auto = document.createElement("option");
      auto.value = "";
      auto.textContent = "Choose automatically";
      select.appendChild(auto);

      for (var m = 0; m < models.length; m++) {
        var opt = document.createElement("option");
        opt.value = models[m];
        opt.textContent = models[m];
        select.appendChild(opt);
      }
      field.appendChild(select);
      rows.appendChild(field);
    }

    wrap.hidden = false;
    this._wireModelSave();
  };

  Byom.prototype._wireModelSave = function () {
    var self = this;
    var btn = qs(this.overlay, "#byomModelSave");
    if (!btn || btn.getAttribute("data-wired") === "true") { return; }
    btn.setAttribute("data-wired", "true");

    btn.addEventListener("click", function () {
      var rows = qs(self.overlay, "#byomModelRows");
      var resultEl = qs(self.overlay, "#byomModelResult");
      var selects = rows ? rows.querySelectorAll("select") : [];
      if (!selects.length) { return; }

      btn.disabled = true;
      self._setResultOn(resultEl, "pending", "Saving…");

      var pending = [];
      for (var i = 0; i < selects.length; i++) {
        pending.push(self.fetchImpl("/api/byom/model", {
          method: "POST",
          body: { model: selects[i].value, role: selects[i].getAttribute("data-role") }
        }));
      }

      Promise.all(pending)
        .then(function () {
          btn.disabled = false;
          self._setResultOn(resultEl, "ok", "Saved.");
          // The masthead dot reads `configured`, which a model choice now
          // affects, so re-read state rather than leaving it stale.
          return self._refresh ? self._refresh() : null;
        })
        ["catch"](function (err) {
          btn.disabled = false;
          self._setResultOn(resultEl, "fail", (err && err.message) || "Could not save.");
        });
    });
  };

  // Graph only — see #byomEmbedConnect in _build(), hidden for the other
  // three apps. Posts to POST /api/byom/test-embedding per the frozen
  // contract, response shape identical to /api/byom/test.
  Byom.prototype._testEmbeddingConnection = function (url, apiKey) {
    this._runTest({
      url: url,
      apiKey: apiKey,
      urlInput: this.embedUrlInput,
      apiKeyInput: this.embedApiKeyInput,
      resultEl: this.embedResultEl,
      testBtn: this.embedTestBtn,
      endpoint: "/api/byom/test-embedding"
    });
  };

  // ── Public namespace ─────────────────────────────────────────────────
  //
  // create() (above) has been the only export since this file's first
  // version. autostart()/open() are added for Step 9 item 1 ("make the
  // BYOM screen re-openable") to replace four byte-identical bootstrap
  // IIFEs that each app's base.html carried — see the four base.html
  // files' single `ArtificeByom.autostart({...})` call. A `var byom`
  // inside an IIFE cannot be reached once the closure returns, so once a
  // user dismissed the screen nothing on the page — in particular, no
  // later-added masthead control — had any way back in. These two
  // functions share one module-level singleton so a masthead trigger and
  // the bootstrap's own auto-open logic operate on the same instance.

  var _instance = null;

  // The legacy masthead uses #navByom; the simplified application shell uses
  // [data-shell-action=model]. Support both while apps migrate so the visible
  // shell control cannot become a dead button.
  var NAV_TRIGGER_ID = "navByom";

  function _navTriggers() {
    var result = [];
    var legacy = document.getElementById(NAV_TRIGGER_ID);
    var shell = document.querySelector("[data-shell-action=model]");
    if (legacy) result.push(legacy);
    if (shell && shell !== legacy) result.push(shell);
    return result;
  }

  // Sets the masthead control's accessible name and its non-colour state
  // signal (filled vs. hollow dot, driven by [data-state] in
  // masthead.css) from the *configured* flag only. Never says
  // "Connected"/"Online" — GET /api/byom/state reports only whether
  // settings differ from defaults, nothing about whether the endpoint
  // answers right now, so a liveness claim here would be false the
  // moment a configured endpoint goes down, which is exactly the
  // scenario this control exists to recover from. Design_Philosophy.md
  // §9 ("Color is never the sole indicator of state") is why the dot's
  // meaning is also carried in the aria-label text, not by colour alone.
  function _setNavTriggerState(configured) {
    _navTriggers().forEach(function (btn) {
      btn.setAttribute("data-state", configured ? "configured" : "unconfigured");
      // "Connection", not "Model". GET /api/byom/state derives `configured`
      // from durable configuration; it does not make a liveness claim.
      btn.setAttribute(
        "aria-label",
        configured
          ? "Connection configured. Open connection settings."
          : "Connection not configured. Open connection settings."
      );
      var label = btn.querySelector("[data-model-label]");
      if (label) label.textContent = configured ? "Connection configured" : "Set up model";
      var dot = btn.querySelector(".status-dot");
      if (dot) dot.setAttribute("data-state", configured ? "configured" : "unconfigured");
    });
  }

  function _wireNavTrigger(instance) {
    _navTriggers().forEach(function (btn) {
      if (btn.getAttribute("data-byom-wired") === "true") return;
      btn.setAttribute("data-byom-wired", "true");
      btn.addEventListener("click", function () { instance.open(); });
    });
  }

  // Creates the singleton (once) and performs exactly the auto-open check
  // every base.html used to inline: fetch state, open only if not
  // configured. Deliberately uses window.fetch directly rather than
  // opts.fetchImpl — that override exists for byom-preview.html to feed
  // fixture data to the *modal's own* state fetch inside open()/_refresh(),
  // and the original four bootstraps never routed their own check through
  // it either, so first-load behaviour stays byte-for-byte what it was.
  function autostart(opts) {
    if (!_instance) { _instance = new Byom(opts); }
    _wireNavTrigger(_instance);
    window.fetch("/api/byom/state")
      .then(function (r) { return r.json(); })
      .then(function (state) {
        _setNavTriggerState(!!(state && state.configured));
        if (!state.configured) { _instance.open(); }
      })
      ["catch"](function () { /* server not ready yet — fine */ });
    return _instance;
  }

  // Opens the singleton, creating it with no options if autostart() has
  // not run yet (defensive only — every host page calls autostart() from
  // base.html before any control that could call open() is reachable).
  function open() {
    if (!_instance) { _instance = new Byom(); }
    _instance.open();
  }

  window.ArtificeByom = {
    create: function (opts) { return new Byom(opts); },
    autostart: autostart,
    open: open
  };
})();
