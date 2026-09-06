"use strict";

/**
 * Pure, DOM-free helpers shared by app.js and the Node-based unit tests in
 * tests/utils.test.js. Nothing in this file touches `document`, `window`,
 * `fetch`, or `localStorage` — that's what keeps it testable with plain
 * `node --test`, no framework, no DOM shim.
 *
 * Mirrors the backend's own category thresholds (services/attention.py:
 * QUIET_THRESHOLD=15, HIGH_THRESHOLD=60) — display-only grouping on top of
 * a score the backend already computed. Never used to calculate a score.
 *
 * Everything below lives inside one IIFE deliberately: a top-level
 * `function` declaration in a classic (non-module) <script> becomes a
 * global binding shared across *every* <script> tag on the page, not just
 * this file — so without this wrapper, app.js's
 * `const { extractErrorMessage, ... } = window.SignalWatchUtils` would
 * collide with this file's own same-named global function declarations
 * and throw "Identifier 'extractErrorMessage' has already been declared"
 * (confirmed live while wiring these two files together). The IIFE keeps
 * every helper private to this file; only the single `SignalWatchUtils`
 * object is exposed.
 */

const SignalWatchUtils = (function () {
  const QUIET_MAX = 14;
  const MODERATE_MAX = 59;

  // FastAPI returns `detail` as a plain string for our own HTTPException
  // calls, but as a *list* of {msg, loc, type} objects for Pydantic request
  // validation errors (e.g. an empty or malformed ticker). Handle both, so
  // a validation error never renders as the literal text "[object Object]".
  function extractErrorMessage(body, status) {
    if (body && typeof body.detail === "string") {
      return body.detail;
    }
    if (body && Array.isArray(body.detail) && body.detail.length > 0) {
      const firstError = body.detail[0];
      const rawMessage = (firstError && firstError.msg) || "Invalid request.";
      return rawMessage.replace(/^Value error,\s*/i, "");
    }
    return `Request failed (HTTP ${status})`;
  }

  function formatPrice(value) {
    if (value == null) return "—";
    return `$${value.toFixed(2)}`;
  }

  function formatPct(value) {
    if (value == null) return null;
    const sign = value > 0 ? "+" : "";
    return `${sign}${value.toFixed(2)}%`;
  }

  function changeDirectionClass(value) {
    if (value == null || value === 0) return null;
    return value > 0 ? "change-up" : "change-down";
  }

  // Category is a *display* grouping over the backend's own 0-100 score —
  // the score itself, and where these boundaries sit, are entirely the
  // backend's (services/attention.py). Nothing here recomputes a score.
  function categoryFor(score) {
    if (score <= QUIET_MAX) return "quiet";
    if (score <= MODERATE_MAX) return "moderate";
    return "high";
  }

  function categoryLabel(category) {
    if (category === "high") return "HIGH ATTENTION";
    if (category === "moderate") return "MODERATE";
    return "QUIET";
  }

  // A visible, friendly stand-in for whatever the browser's fetch()
  // rejection message happens to be (e.g. "Failed to fetch", "NetworkError
  // when attempting to fetch resource", "Load failed") — those are
  // accurate but read like debug output, not a message meant for an end
  // user.
  function friendlyNetworkErrorMessage() {
    return "Can't reach the SignalWatch server. Check your connection and try again.";
  }

  // Trims and uppercases as the user types, purely for a WYSIWYG input —
  // the backend (schemas.py: WatchlistItemCreate.normalize_ticker) remains
  // the single source of truth for normalization/validation. This never
  // rejects anything; it only mirrors what the backend will do to
  // whitespace/case.
  function normalizeTickerDisplay(rawValue) {
    return rawValue.toUpperCase();
  }

  return {
    QUIET_MAX,
    MODERATE_MAX,
    extractErrorMessage,
    formatPrice,
    formatPct,
    changeDirectionClass,
    categoryFor,
    categoryLabel,
    friendlyNetworkErrorMessage,
    normalizeTickerDisplay,
  };
})();

// Dual-mode export: a plain `<script>` tag in index.html exposes this on
// `window`, while `node --test` picks it up as a CommonJS module. Neither
// environment sees the other's branch.
if (typeof module !== "undefined" && module.exports) {
  module.exports = SignalWatchUtils;
}
if (typeof window !== "undefined") {
  window.SignalWatchUtils = SignalWatchUtils;
}
