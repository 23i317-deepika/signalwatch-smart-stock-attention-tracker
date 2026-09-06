"use strict";

/**
 * SignalWatch frontend — plain vanilla JS, no build step, no framework.
 *
 * Talks to the FastAPI backend via three endpoints:
 *   POST   /api/watchlist        add a ticker
 *   DELETE /api/watchlist/{t}    remove a ticker
 *   GET    /api/dashboard        the scored, ranked, explained watchlist —
 *                                  everything this file renders comes from
 *                                  a single call to this endpoint
 *   POST   /api/visit/complete   moves the Attention Score's "since last
 *                                  visit" baseline forward — called only
 *                                  after a dashboard render succeeds
 *                                  (see PROJECT_PLAN.md §6)
 *
 * PRODUCT DECISION (see conversation): the primary UI signal is the
 * Attention Score (attention_score / score_components / reasons /
 * comparison_available / baseline_price / change_since_baseline_pct) — the
 * backend's own novel, volatility-aware scoring. The separate fixed-2%
 * change-detection fields the API also returns (change_message, signals,
 * percent_change, change_count, first_visit) still exist and are still
 * computed server-side, but are intentionally NOT read or rendered here.
 * They aren't deleted from the backend — they're just not this screen's
 * story.
 *
 * Pure formatting/parsing helpers (error extraction, price/pct formatting,
 * score category grouping) live in utils.js (loaded before this file) so
 * they can be unit-tested with plain `node --test`, no DOM involved.
 */

// Change this if the backend isn't running on the default local port.
const API_BASE_URL =
  "https://signalwatch-smart-stock-attention-tracker.onrender.com";

const DEVICE_ID_STORAGE_KEY = "signalwatch_device_id";

const {
  extractErrorMessage,
  formatPrice,
  formatPct,
  changeDirectionClass,
  categoryFor,
  categoryLabel,
  friendlyNetworkErrorMessage,
  normalizeTickerDisplay,
} = window.SignalWatchUtils;

class ApiError extends Error {
  constructor(message, status) {
    super(message);
    this.status = status;
  }
}

// --- Device identity (no auth — see PROJECT_PLAN.md §9) ---------------------

function getDeviceId() {
  try {
    let id = localStorage.getItem(DEVICE_ID_STORAGE_KEY);
    if (!id) {
      id = crypto.randomUUID();
      localStorage.setItem(DEVICE_ID_STORAGE_KEY, id);
    }
    return id;
  } catch {
    // localStorage can throw in private-browsing/locked-down contexts.
    // Fall back to a session-only id so the app still works, just without
    // persistence across reloads.
    if (!getDeviceId._fallback) {
      getDeviceId._fallback = crypto.randomUUID();
    }
    return getDeviceId._fallback;
  }
}

const deviceId = getDeviceId();

// --- API helper ---------------------------------------------------------

async function apiFetch(path, options = {}) {
  let response;
  try {
    response = await fetch(`${API_BASE_URL}${path}`, {
      ...options,
      headers: {
        "Content-Type": "application/json",
        "X-Device-ID": deviceId,
        ...(options.headers || {}),
      },
    });
  } catch {
    // fetch() itself rejected — the server is unreachable, offline, CORS
    // blocked, etc. This is the "provider/network failure" case at the
    // transport level (before any HTTP status even exists), so it gets its
    // own friendly message rather than surfacing the raw browser error
    // text (e.g. "Failed to fetch", "NetworkError when attempting to fetch
    // resource") verbatim to the user.
    throw new ApiError(friendlyNetworkErrorMessage(), 0);
  }

  let body = null;
  try {
    body = await response.json();
  } catch {
    // No JSON body (e.g. a 204) — fine.
  }

  if (!response.ok) {
    throw new ApiError(extractErrorMessage(body, response.status), response.status);
  }

  return body;
}

// --- DOM references -------------------------------------------------------

const addForm = document.getElementById("add-form");
const tickerInput = document.getElementById("ticker-input");
const addButton = document.getElementById("add-button");
const addButtonLabel = document.getElementById("add-button-label");
const addError = document.getElementById("add-error");
const loadError = document.getElementById("load-error");
const loadingIndicator = document.getElementById("loading-indicator");
const emptyState = document.getElementById("empty-state");
const emptyStateCta = document.getElementById("empty-state-cta");
const summaryGrid = document.getElementById("summary-grid");
const stockList = document.getElementById("stock-list");
const statusIndicator = document.getElementById("status-indicator");
const statusDot = document.getElementById("status-dot");
const statusText = document.getElementById("status-text");
const toastRegion = document.getElementById("toast-region");

const summaryEls = {
  total: document.getElementById("summary-total"),
  high: document.getElementById("summary-high"),
  moderate: document.getElementById("summary-moderate"),
  quiet: document.getElementById("summary-quiet"),
};

const stockCardTemplate = document.getElementById("stock-card-template");
const unavailableCardTemplate = document.getElementById("unavailable-card-template");

// --- Toasts ---------------------------------------------------------------
// Lightweight, dependency-free success/error confirmations (ticker added,
// ticker removed, ...) — separate from the persistent inline error banners
// (#add-error, #load-error), which stay for errors the user needs to act
// on. Toasts announce themselves via aria-live so they reach screen readers
// without stealing focus, and they never carry raw backend objects/stack
// traces — only the same sanitized messages already used elsewhere.
let toastCounter = 0;
const MAX_VISIBLE_TOASTS = 3;

function showToast(message, variant = "success") {
  // Cap how many toasts can pile up at once (e.g. adding several tickers in
  // quick succession) — drop the oldest immediately rather than letting the
  // stack grow and cover the dashboard underneath.
  const existing = toastRegion.querySelectorAll(".toast");
  if (existing.length >= MAX_VISIBLE_TOASTS) {
    existing[0].remove();
  }

  const toast = document.createElement("div");
  toast.className = `toast toast-${variant}`;
  toast.textContent = message;
  toast.id = `toast-${++toastCounter}`;
  toastRegion.appendChild(toast);

  // Give the browser a frame to paint the pre-transition state, then
  // animate in — same double-rAF trick used for the score bar.
  requestAnimationFrame(() => {
    requestAnimationFrame(() => {
      toast.classList.add("toast-visible");
    });
  });

  const remove = () => {
    toast.classList.remove("toast-visible");
    toast.addEventListener("transitionend", () => toast.remove(), { once: true });
    // Fallback in case transitionend never fires (e.g. reduced-motion).
    setTimeout(() => toast.remove(), 400);
  };
  setTimeout(remove, 4000);
}

// --- Rendering: one stock card -----------------------------------------------

function buildStockCard(item) {
  const fragment = stockCardTemplate.content.cloneNode(true);
  const card = fragment.querySelector(".stock-card");
  const category = categoryFor(item.attention_score);

  card.classList.add(`card-${category}`);
  card.querySelector(".card-ticker").textContent = item.ticker;

  const badge = card.querySelector(".category-badge");
  badge.textContent = categoryLabel(category);
  badge.classList.add(`badge-${category}`);

  const removeBtn = card.querySelector(".remove-btn");
  removeBtn.addEventListener("click", () => handleRemove(item.ticker, removeBtn));

  card.querySelector(".price-value").textContent = formatPrice(item.price);

  renderPriceChange(card, item);
  renderAttentionScore(card, item.attention_score, category);
  renderReasons(card, item.reasons);
  renderBreakdown(card, item.score_components);
  renderDataStatus(card, item);

  return card;
}

function renderPriceChange(card, item) {
  const labelEl = card.querySelector(".price-change-label");
  const valueEl = card.querySelector(".price-change-value");
  const contextEl = card.querySelector(".market-context-value");

  if (item.comparison_available) {
    labelEl.textContent = "Since Your Last Visit";
    valueEl.textContent = formatPct(item.change_since_baseline_pct);
    const cls = changeDirectionClass(item.change_since_baseline_pct);
    if (cls) valueEl.classList.add(cls);
    contextEl.hidden = true;
  } else {
    // Never pretend a "since last visit" comparison exists when it doesn't.
    labelEl.textContent = "";
    valueEl.textContent = "First visit — building your baseline";
    valueEl.classList.add("first-visit-value");

    const contextPct = item.market_context && item.market_context.change_vs_prev_close_pct;
    if (contextPct != null) {
      contextEl.hidden = false;
      contextEl.textContent = `vs. previous close: ${formatPct(contextPct)}`;
      const cls = changeDirectionClass(contextPct);
      if (cls) contextEl.classList.add(cls);
    } else {
      contextEl.hidden = true;
    }
  }
}

function renderAttentionScore(card, score, category) {
  card.querySelector(".attention-value").textContent = `${score} / 100`;

  const track = card.querySelector(".score-track");
  track.setAttribute("role", "meter");
  track.setAttribute("aria-valuemin", "0");
  track.setAttribute("aria-valuemax", "100");
  track.setAttribute("aria-valuenow", String(score));
  track.setAttribute("aria-label", `Attention Score ${score} out of 100, ${categoryLabel(category)}`);

  const fill = card.querySelector(".score-fill");
  fill.classList.add(`fill-${category}`);
  fill.style.width = "0%";
  // Double rAF so the browser paints the 0% state before transitioning to
  // the real width — this is what makes the bar visibly animate in,
  // instead of just appearing already full.
  requestAnimationFrame(() => {
    requestAnimationFrame(() => {
      fill.style.width = `${score}%`;
    });
  });
}

function renderReasons(card, reasons) {
  const list = card.querySelector(".reasons-list");
  const items = reasons && reasons.length > 0 ? reasons : ["No unusual market context detected."];
  items.forEach((reasonText) => {
    const li = document.createElement("li");
    li.textContent = reasonText;
    list.appendChild(li);
  });
}

function renderBreakdown(card, components) {
  const safeComponents = components || {};
  card.querySelectorAll("[data-component]").forEach((el) => {
    const key = el.dataset.component;
    el.textContent = safeComponents[key] != null ? safeComponents[key] : "0";
  });
}

function renderDataStatus(card, item) {
  const statusEl = card.querySelector(".data-status");
  if (item.stale) {
    statusEl.textContent = "⚠ Showing cached market data";
    statusEl.classList.add("status-stale");
  } else if (item.partial_history) {
    statusEl.textContent = "ℹ Limited historical data available";
    statusEl.classList.add("status-partial");
  } else {
    statusEl.textContent = "Updated recently";
  }
}

function buildUnavailableCard(item) {
  const fragment = unavailableCardTemplate.content.cloneNode(true);
  const card = fragment.querySelector(".stock-card");

  card.querySelector(".card-ticker").textContent = item.ticker;
  card.querySelector(".unavailable-error-text").textContent = item.error
    ? item.error
    : "No further details available.";

  const removeBtn = card.querySelector(".remove-btn");
  removeBtn.addEventListener("click", () => handleRemove(item.ticker, removeBtn));

  return card;
}

// --- Rendering: summary + status indicator + full list -----------------------

function renderSummary(items) {
  const okItems = items.filter((item) => item.status === "ok");
  const counts = { high: 0, moderate: 0, quiet: 0 };
  okItems.forEach((item) => {
    counts[categoryFor(item.attention_score)] += 1;
  });

  summaryEls.total.textContent = items.length;
  summaryEls.high.textContent = counts.high;
  summaryEls.moderate.textContent = counts.moderate;
  summaryEls.quiet.textContent = counts.quiet;
}

function updateStatusIndicator(items) {
  if (items.length === 0) {
    statusIndicator.hidden = true;
    return;
  }
  const anyStale = items.some((item) => item.stale);
  statusIndicator.hidden = false;
  statusDot.classList.toggle("dot-stale", anyStale);
  statusText.textContent = anyStale ? "Some data is cached" : "Market data live";
}

function render(items) {
  stockList.innerHTML = "";

  if (items.length === 0) {
    emptyState.hidden = false;
    summaryGrid.hidden = true;
    updateStatusIndicator(items);
    return;
  }

  emptyState.hidden = true;
  summaryGrid.hidden = false;
  renderSummary(items);
  updateStatusIndicator(items);

  // Items already arrive ranked by the backend (highest Attention Score
  // first, unavailable tickers last) — render in that order, no client-side
  // re-sorting or grouping.
  items.forEach((item) => {
    const card = item.status === "ok" ? buildStockCard(item) : buildUnavailableCard(item);
    stockList.appendChild(card);
  });
}

// --- Actions ----------------------------------------------------------------

async function loadDashboard() {
  loadError.hidden = true;
  loadingIndicator.hidden = false;

  try {
    const items = await apiFetch("/api/dashboard");
    render(items);
    loadingIndicator.hidden = true;

    // Only after a successful render — never as a side effect of merely
    // reading the dashboard. Fire-and-forget: if this fails (e.g. a
    // dropped connection right after load), the baseline just doesn't
    // advance this time, and the next successful visit retries it. It
    // must never surface as a scary error to the user.
    apiFetch("/api/visit/complete", { method: "POST" }).catch((err) => {
      console.warn("visit/complete failed (non-fatal):", err);
    });
  } catch (err) {
    loadingIndicator.hidden = true;
    loadError.hidden = false;
    loadError.textContent = `Couldn't load your watchlist: ${err.message}`;
  }
}

let addInFlight = false;

async function handleAddTicker(event) {
  event.preventDefault();
  if (addInFlight) return; // guard against a double-click/double-submit race
  addError.hidden = true;

  const rawValue = tickerInput.value.trim();
  if (!rawValue) {
    addError.hidden = false;
    addError.textContent = "Enter a ticker symbol first.";
    return;
  }

  const displayTicker = normalizeTickerDisplay(rawValue);
  addInFlight = true;
  addButton.disabled = true;
  addButtonLabel.textContent = "Adding…";
  try {
    await apiFetch("/api/watchlist", {
      method: "POST",
      body: JSON.stringify({ ticker: rawValue }),
    });
    tickerInput.value = "";
    await loadDashboard();
    showToast(`${displayTicker} added to your watchlist.`, "success");
  } catch (err) {
    addError.hidden = false;
    addError.textContent = err.status === 409
      ? `${displayTicker} is already on your watchlist.`
      : err.message;
  } finally {
    addInFlight = false;
    addButton.disabled = false;
    addButtonLabel.textContent = "+ Add to Watchlist";
  }
}

async function handleRemove(ticker, removeBtn) {
  if (removeBtn.disabled) return; // guard against a double-click race
  removeBtn.disabled = true;
  removeBtn.classList.add("removing");
  removeBtn.setAttribute("aria-label", `Removing ${ticker}…`);
  try {
    await apiFetch(`/api/watchlist/${encodeURIComponent(ticker)}`, { method: "DELETE" });
    await loadDashboard();
    showToast(`${ticker} removed from your watchlist.`, "success");
  } catch (err) {
    loadError.hidden = false;
    loadError.textContent = `Couldn't remove ${ticker}: ${err.message}`;
    // Only restore the button if the card is still around to restore it on
    // — a successful removal already replaced this DOM via loadDashboard().
    removeBtn.disabled = false;
    removeBtn.classList.remove("removing");
    removeBtn.setAttribute("aria-label", `Remove ${ticker} from watchlist`);
  }
}

// --- Init ---------------------------------------------------------------

addForm.addEventListener("submit", handleAddTicker);
emptyStateCta.addEventListener("click", () => tickerInput.focus());
loadDashboard();
