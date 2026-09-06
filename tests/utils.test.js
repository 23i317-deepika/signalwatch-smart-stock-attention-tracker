"use strict";

/**
 * Unit tests for frontend/utils.js — plain Node, no framework, no build
 * step. Run with:
 *
 *   node --test frontend/tests
 *
 * (Node's built-in test runner, available since Node 18; this project's
 * Node is v24.) These exercise the same pure helpers app.js uses to render
 * the dashboard, in particular the two FastAPI error-response shapes the
 * backend actually returns:
 *   - a plain string `detail` (our own HTTPException calls — e.g. 409
 *     duplicate ticker, 422 unknown ticker, 404 remove-not-found)
 *   - a Pydantic validation-error *list* `detail` (e.g. a malformed ticker
 *     caught by schemas.py's field_validator before it reaches a route)
 */

const test = require("node:test");
const assert = require("node:assert/strict");

const {
  extractErrorMessage,
  formatPrice,
  formatPct,
  changeDirectionClass,
  categoryFor,
  categoryLabel,
  friendlyNetworkErrorMessage,
  normalizeTickerDisplay,
} = require("../utils.js");

// --- extractErrorMessage: FastAPI's two `detail` shapes ---------------------

test("extractErrorMessage: string detail (HTTPException, e.g. 409/422/404)", () => {
  assert.equal(
    extractErrorMessage({ detail: "AAPL is already on your watchlist" }, 409),
    "AAPL is already on your watchlist"
  );
});

test("extractErrorMessage: validation-error list detail (Pydantic 422)", () => {
  const body = {
    detail: [
      {
        type: "value_error",
        loc: ["body", "ticker"],
        msg: "Value error, ticker must contain only letters, numbers, '.' or '-' (max 10 characters)",
      },
    ],
  };
  assert.equal(
    extractErrorMessage(body, 422),
    "ticker must contain only letters, numbers, '.' or '-' (max 10 characters)"
  );
});

test("extractErrorMessage: strips the 'Value error, ' prefix Pydantic adds", () => {
  const body = { detail: [{ msg: "Value error, ticker must not be empty" }] };
  assert.equal(extractErrorMessage(body, 422), "ticker must not be empty");
});

test("extractErrorMessage: empty validation list falls back to a generic message", () => {
  assert.equal(extractErrorMessage({ detail: [] }, 422), "Request failed (HTTP 422)");
});

test("extractErrorMessage: validation entry missing msg falls back to 'Invalid request.'", () => {
  assert.equal(extractErrorMessage({ detail: [{}] }, 422), "Invalid request.");
});

test("extractErrorMessage: no body at all (e.g. a non-JSON error page)", () => {
  assert.equal(extractErrorMessage(null, 500), "Request failed (HTTP 500)");
});

test("extractErrorMessage: body with neither string nor list detail", () => {
  assert.equal(extractErrorMessage({ detail: { weird: true } }, 500), "Request failed (HTTP 500)");
});

// --- formatPrice / formatPct -------------------------------------------------

test("formatPrice: formats to two decimals with a dollar sign", () => {
  assert.equal(formatPrice(231.4), "$231.40");
  assert.equal(formatPrice(0), "$0.00");
});

test("formatPrice: null/undefined renders as an em dash, never as 'null'", () => {
  assert.equal(formatPrice(null), "—");
  assert.equal(formatPrice(undefined), "—");
});

test("formatPct: signs a positive change and formats two decimals", () => {
  assert.equal(formatPct(3.8), "+3.80%");
});

test("formatPct: a negative change keeps its own minus sign, no double sign", () => {
  assert.equal(formatPct(-2.15), "-2.15%");
});

test("formatPct: zero is not treated as a missing value", () => {
  assert.equal(formatPct(0), "0.00%");
});

test("formatPct: null is not rendered as '0.00%'", () => {
  assert.equal(formatPct(null), null);
});

// --- changeDirectionClass ---------------------------------------------------

test("changeDirectionClass: positive/negative/zero/null", () => {
  assert.equal(changeDirectionClass(1.2), "change-up");
  assert.equal(changeDirectionClass(-1.2), "change-down");
  assert.equal(changeDirectionClass(0), null);
  assert.equal(changeDirectionClass(null), null);
});

// --- categoryFor / categoryLabel: mirrors attention.py's own thresholds ----

test("categoryFor: matches backend QUIET_THRESHOLD=15 / HIGH_THRESHOLD=60 boundaries", () => {
  assert.equal(categoryFor(0), "quiet");
  assert.equal(categoryFor(14), "quiet");
  assert.equal(categoryFor(15), "moderate");
  assert.equal(categoryFor(59), "moderate");
  assert.equal(categoryFor(60), "high");
  assert.equal(categoryFor(100), "high");
});

test("categoryLabel: maps each category to its display label", () => {
  assert.equal(categoryLabel("quiet"), "QUIET");
  assert.equal(categoryLabel("moderate"), "MODERATE");
  assert.equal(categoryLabel("high"), "HIGH ATTENTION");
});

// --- friendlyNetworkErrorMessage / normalizeTickerDisplay -------------------

test("friendlyNetworkErrorMessage: never leaks a raw browser fetch error", () => {
  const message = friendlyNetworkErrorMessage();
  assert.match(message, /server/i);
  assert.doesNotMatch(message, /TypeError|stack|fetch\(\)/i);
});

test("normalizeTickerDisplay: uppercases for display only", () => {
  assert.equal(normalizeTickerDisplay("aapl"), "AAPL");
  assert.equal(normalizeTickerDisplay("Brk.b"), "BRK.B");
});
