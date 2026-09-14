// SPDX-FileCopyrightText: 2026 Maurice Casey
//
// SPDX-License-Identifier: AGPL-3.0-or-later

/* bind.js — DOM binding helpers and fetch wrapper for the Artifice Suite.
 *
 * Ships from packages/shared-ui alongside toast.js, and follows its
 * packaging and loading pattern exactly: same ES5-only dialect (IIFE,
 * "use strict", var/function rather than let/const/arrow, .then()/.catch()
 * rather than async/await), because artifice-graph's static/app.js and
 * pipeline.js are both plain ES5 and any shared script must run there too.
 *
 * Exposes a single global, `window.ArtificeBind`, matching toast.js's own
 * `window.ArtificeToast`.
 *
 *   window.ArtificeBind.onReady(function () {
 *     window.ArtificeBind.bindIfPresent("my-btn", "click", function () { ... });
 *   });
 *
 *   // apiFetch: thin fetch wrapper that validates response and throws
 *   // a descriptive error on failure. Callers are expected to catch
 *   // and call window.ArtificeToast.error(...) — this function checks,
 *   // the call site decides what to show the user.
 *
 *   window.ArtificeBind.apiFetch("/api/some-endpoint", { method: "GET" })
 *     .then(function (data) { ... })
 *     .catch(function (err) { window.ArtificeToast.error(err.message); });
 *
 * Error-message extraction checks for detail, error, message, and hint
 * fields — covering all four error-body shapes this suite's APIs return
 * (HTTPException uses "detail", byom.py JSONResponse uses "error" or
 * "hint", and ad-hoc routes may use "message").
 */

(function () {
  "use strict";

  // ── onReady ─────────────────────────────────────────────────────────
  // Standard pattern: run immediately if already past "loading", otherwise
  // wait for DOMContentLoaded.  Called with document.readyState === "loading"
  // is the only case where attachment is deferred.

  function onReady(fn) {
    if (document.readyState !== "loading") {
      fn();
    } else {
      document.addEventListener("DOMContentLoaded", fn);
    }
  }

  // ── bindIfPresent ───────────────────────────────────────────────────
  // Looks up document.getElementById(id).  If the element exists, attaches
  // the event handler and returns the element.  If not found, returns null.
  // A missing element on the current page is a normal, expected case (different
  // pages have different controls) — this function intentionally does
  // nothing and produces no console noise when an ID is absent.

  function bindIfPresent(id, event, handler) {
    var el = document.getElementById(id);
    if (el) {
      el.addEventListener(event, handler);
    }
    return el;
  }

  // ── apiFetch ────────────────────────────────────────────────────────
  // Thin wrapper around fetch.  Checks response.ok; throws an Error whose
  // message includes the HTTP status and, when the response body is JSON,
  // the value of any detail / error / message / hint field found therein.
  // On network failure (fetch itself rejects) re-throws with a clear
  // message so callers can distinguish network errors from HTTP errors.
  // Does NOT swallow errors — the caller catches and decides what to show.

  function apiFetch(url, opts) {
    opts = opts || {};
    return fetch(url, opts)
      .then(function (r) {
        if (r.ok) {
          var contentType = r.headers.get("content-type") || "";
          if (contentType.toLowerCase().indexOf("application/json") !== -1) {
            return r.json().catch(function () {
              var parseErr = new Error("Server returned a non-JSON response (status " + r.status + ")");
              parseErr.status = r.status;
              throw parseErr;
            });
          }
          return r;
        }
        // Non-ok response — extract a human-readable message.
        // Two-argument .then(onFulfilled, onRejected) is deliberate here: an
        // onRejected chained via a separate .catch() after this .then() would
        // also catch onFulfilled's own `throw err` below, discarding the
        // extracted message and replacing it with the generic fallback. This
        // form scopes onRejected to ONLY a genuine r.json() parse failure.
        return r.json().then(
          function (body) {
            var msg = body && (body.detail || body.error || body.message || body.hint);
            if (msg && typeof msg === "object") {
              msg = msg.msg || JSON.stringify(msg);
            }
            if (!msg) { msg = r.statusText || String(r.status); }
            var err = new Error(String(msg));
            err.status = r.status;
            throw err;
          },
          function () {
            // Body was not JSON — this handler now only ever fires for a
            // genuine parse failure, not for the onFulfilled throw above.
            var err = new Error(r.statusText || String(r.status));
            err.status = r.status;
            throw err;
          }
        );
      })
      .catch(function (err) {
        // Distinguish network errors (fetch itself rejected) from HTTP/parse
        // errors (already thrown above, both of which set err.status).
        if (err.status === undefined) {
          var netErr = new Error("Network error: could not reach the server");
          netErr.network = true;
          throw netErr;
        }
        throw err;
      });
  }

  window.ArtificeBind = {
    onReady: onReady,
    bindIfPresent: bindIfPresent,
    apiFetch: apiFetch
  };
})();
