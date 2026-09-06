# SignalWatch — Project Plan

**Challenge:** Build a Smart Market Watchlist ("Code, by Groww" hackathon)
**Core idea:** A stock movement is "meaningful" when it's unusual *relative to that stock's own
history*, not when it crosses some arbitrary fixed percentage. SignalWatch remembers a visitor's
last visit, compares against it, and surfaces a ranked, explainable **Attention Score** per stock.

> **Revision note:** this plan was revised after initial approval to (1) simplify market-data
> caching to in-memory only, (2) rework first-time-user messaging, (3) replace the implicit
> read-triggers-a-write snapshot flow with an explicit `GET /api/dashboard` +
> `POST /api/visit/complete` pair, and (4) trim the folder structure. Rationale for each is
> documented inline below.

---

## 1. Problem interpretation

Three explicit product moments to support:

1. **Manage a watchlist** — add/remove tickers, no duplicates, no auth.
2. **See current market state** — price, volume, 52-week range, freshness of the data.
3. **Return later and understand what changed** — this is the differentiator. The system must
   remember a device's last visit per ticker and answer, in plain English, "why should I care
   about this one today?"

The Attention Score is explicitly **not** a prediction or a buy/sell signal — it's a triage tool:
rank the watchlist so the user reads the 2–3 stocks that deserve attention first, and can safely
ignore the rest. Explainability is a first-class requirement: every score decomposes into
human-readable reasons.

Two ideas the interpretation deliberately keeps separate:
- **"Since last visit"** (episodic, per-device, per-ticker — only advances when the user
  explicitly finishes a visit, see §6)
- **"Unusual for this stock"** (statistical, market-derived, same for every viewer of that ticker)

The Attention Score is the product of these two: *did something happen, and is it out of
character for this stock.*

---

## 2. Architecture overview

```
┌─────────────┐      HTTP/JSON       ┌────────────────────────────────────────────────┐
│  Frontend    │ ───────────────────▶│  FastAPI app                                    │
│  (vanilla    │                      │  ┌────────────┐   ┌──────────────────────────┐│
│  HTML/CSS/JS)│◀─────────────────── │  │  Routers    │──▶│  market_data.py          ││
│  device_id in│                      │  │ /watchlist  │   │  yfinance call +         ││
│  localStorage│                      │  │ /dashboard  │   │  in-memory TTL cache     ││
└─────────────┘                      │  │ /stocks     │   │  (dict, keyed by ticker) ││
                                      │  └────┬───────┘   └────────────┬─────────────┘│
                                      │       │                        │              │
                                      │       ▼                        ▼              │
                                      │  ┌────────────┐    (plain numbers passed in)  │
                                      │  │ Attention   │◀───────────────────────────  │
                                      │  │ Engine      │                              │
                                      │  │ (pure,      │───▶ score + explanation      │
                                      │  │  no I/O)    │                              │
                                      │  └────────────┘                              │
                                      │       │                                       │
                                      │       ▼                                       │
                                      │  snapshot_service.py ──▶ SQLite (WAL mode)     │
                                      │  (watchlist_items, visit_snapshots only)       │
                                      └────────────────────────────────────────────────┘
```

**What persists vs. what doesn't:**
- **SQLite** holds only durable *user state*: which tickers a device is watching, and the price
  baseline from that device's last completed visit. This is small, low-write-volume, and worth
  persisting — losing it would mean losing the product's core memory.
- **Market data (price, volume, volatility, 52-week range) lives in an in-memory dict**, keyed by
  ticker, shared across all devices, with a `fetched_at` timestamp and TTL. It is derived,
  re-fetchable data, not user state — persisting it in SQLite would add a table and migration
  surface for no real benefit at hackathon scale. Trade-off: the cache is empty after a process
  restart, so stale-fallback protection only applies once a ticker has been fetched at least once
  in the current process's lifetime (documented in §8).

**Attention Engine stays pure**, per the brief: `services/attention.py` takes plain numbers
(prices, volumes, volatility, an optional baseline) in and returns a score + structured breakdown
out. It never imports FastAPI, SQLAlchemy, or yfinance — it can be constructed and tested with
plain dataclasses, no mocking.

---

## 3. Folder structure

Kept intentionally flat — no Alembic, no Redis, no microservices, no auth, no extra abstraction
layers beyond what's needed to keep the engine pure and the code readable.

```
signalwatch/
├── PROJECT_PLAN.md
├── README.md
├── .gitignore
├── backend/
│   ├── requirements.txt
│   ├── signalwatch.db                # SQLite file (gitignored) — watchlist + snapshots only
│   └── app/
│       ├── __init__.py
│       ├── main.py                   # FastAPI app, CORS, startup (DB init), /api/health
│       ├── config.py                 # env-driven settings: cache TTL, DB path, CORS origins
│       ├── database.py               # SQLAlchemy engine/session, WAL pragma
│       ├── models.py                 # ORM models: WatchlistItem, VisitSnapshot
│       ├── schemas.py                # Pydantic request/response models
│       ├── deps.py                   # get_db(), get_device_id()
│       ├── routers/
│       │   ├── __init__.py
│       │   ├── watchlist.py          # GET/POST /api/watchlist, DELETE /api/watchlist/{ticker}
│       │   ├── dashboard.py          # GET /api/dashboard, POST /api/visit/complete
│       │   └── stocks.py             # GET /api/stocks/{ticker} (detail/chart)
│       └── services/
│           ├── __init__.py
│           ├── market_data.py        # yfinance wrapper + in-memory TTL cache + per-ticker lock
│           ├── attention.py          # compute_attention_score() + reasons — pure, no I/O
│           └── snapshot_service.py   # read previous snapshot / complete-visit (idempotent) logic
│   └── tests/
│       ├── test_attention_engine.py  # table-driven, no mocks — the highest-value test target
│       ├── test_market_data.py       # provider normalization, mocked yfinance
│       ├── test_market_data_cache.py # TTL expiry + stale-fallback-on-failure behavior
│       └── test_dashboard_api.py     # integration tests against a temp SQLite DB (later phase)
└── frontend/
    ├── index.html
    ├── styles.css
    └── app.js                        # device_id bootstrap, dashboard load, visit/complete call
```

---

## 4. Database schema (SQLite via SQLAlchemy)

Only two tables. No `users`/auth tables — `device_id` is an opaque client-generated UUID string,
trusted as-is (documented in §8). No market-data table — that's in-memory only (§2).

### `watchlist_items`
| column      | type              | notes                                      |
|-------------|-------------------|---------------------------------------------|
| id          | INTEGER PK        | autoincrement                              |
| device_id   | TEXT, indexed     | from `X-Device-Id` header                  |
| ticker      | TEXT              | normalized uppercase, e.g. `AAPL`          |
| added_at    | DATETIME          | UTC                                        |

`UNIQUE(device_id, ticker)` — enforces no duplicates at the DB level.

### `visit_snapshots`
The "memory" of the last **completed** visit — one row per (device, ticker), written only by
`POST /api/visit/complete` (see §6), never by a `GET`.

| column                 | type       | notes                                                  |
|------------------------|------------|---------------------------------------------------------|
| id                     | INTEGER PK |                                                         |
| device_id              | TEXT, indexed |                                                      |
| ticker                 | TEXT       |                                                         |
| snapshot_price         | REAL       | price captured as the new baseline                      |
| snapshot_trading_date  | DATE       | the *exchange* trading date the snapshot represents (not server wall-clock date) |
| captured_at            | DATETIME   | UTC timestamp, for display ("baseline set on...")       |

`UNIQUE(device_id, ticker)` — one current baseline per pair; writes are upserts.
`snapshot_trading_date` is what makes `POST /api/visit/complete` idempotent within a trading day
(see §6) — no separate "last write time" bookkeeping needed.

---

## 5. REST API

All endpoints require an `X-Device-Id: <uuid>` header (except `/api/health`). Missing/malformed
header → `400`. The frontend generates the UUID once via `crypto.randomUUID()` and persists it in
`localStorage`.

| Method | Path                          | Purpose |
|--------|-------------------------------|---------|
| `GET`  | `/api/health`                 | Liveness + in-memory cache diagnostics (entries cached, oldest entry age) — useful to demo resilience live. |
| `GET`  | `/api/watchlist`               | Plain list of the device's watchlist items (ticker, added_at). No market data, no scoring — pure DB read, used for a simple manage view. |
| `POST` | `/api/watchlist`               | Body `{ "ticker": "AAPL" }`. Validates the ticker resolves against the market data provider, normalizes to uppercase, prevents duplicates. `201` on create; `200` idempotent-return if it already exists (see §7 #6); `422` if the ticker doesn't resolve. |
| `DELETE` | `/api/watchlist/{ticker}`    | Remove one ticker (and, for cleanliness, its snapshot row) from the device's watchlist. `404` if not present. |
| `GET`  | `/api/dashboard`               | **The main "smart" view.** For each watchlist ticker: reads the existing snapshot (if any), fetches current market data (via the in-memory cache), computes the Attention Score, and returns the ranked, explained list. **Read-only — never writes a snapshot.** |
| `POST` | `/api/visit/complete`          | Called by the frontend only after the dashboard has successfully rendered. Writes the *current* prices as the new baseline for every ticker on the watchlist. Idempotent within a trading day (see §6). Returns `200` with a JSON breakdown of which tickers were updated/skipped/unavailable. |
| `GET`  | `/api/stocks/{ticker}`         | Detail view for one ticker: full score breakdown, ~6 months of daily history for an optional Chart.js sparkline. Works even if the ticker isn't on the watchlist. |

Response shape for a dashboard item (illustrative):
```jsonc
{
  "ticker": "AAPL",
  "price": 231.4,
  "currency": "USD",
  "as_of": "2026-09-05T20:00:00Z",
  "stale": false,
  "stale_reason": null,
  "data_age_seconds": 120,

  "comparison_available": true,
  "baseline_price": 223.0,
  "baseline_captured_at": "2026-09-03T14:02:00Z",
  "change_since_baseline_pct": 3.8,

  "market_context": {
    "change_vs_prev_close_pct": 0.9
  },

  "attention_score": 71,
  "score_breakdown": {
    "volatility_adjusted_move": 0.62,
    "volume_anomaly": 0.81,
    "week52_context": 0.10,
    "raw_change_magnitude": 0.38
  },
  "explanations": [
    "Up 3.8% since your last visit (Sep 3) — about 2.1x this stock's typical move over that span.",
    "Volume today is 3.1x its 20-day average."
  ]
}
```

For a first-time ticker (`comparison_available: false`), `baseline_price`/`change_since_baseline_pct`
are `null`, `score_breakdown.volatility_adjusted_move` and `raw_change_magnitude` are `0`, and
`explanations` opens with the baseline-establishing message (see §6).

---

## 6. Visit snapshot flow (revised — explicit, not a GET side effect)

**Problem with the original design:** having `GET /api/watchlist` silently write a new baseline
made *reading* data mutate state. That's surprising, breaks safe polling/retries, and means a
page refresh could silently move the goalposts before the user even saw the previous comparison.

**Final flow:**

1. **`GET /api/dashboard`** — purely a read:
   - For each ticker, load the existing `visit_snapshots` row for `(device_id, ticker)`, if any.
   - Fetch current market data (in-memory cache, falling back to stale cache on provider failure).
   - Call the Attention Engine with `baseline = snapshot.price if snapshot else None`.
   - Return the full ranked/explained dashboard. **No database write happens here.**

2. **Frontend renders the dashboard.** The user sees "since your last visit" (or the first-time
   message) based on the *old* baseline — exactly what they'd expect.

3. **`POST /api/visit/complete`** — fired by the frontend immediately after a successful render
   (not before, and not on a failed/partial load):
   - For each ticker on the watchlist, upsert `visit_snapshots` with the *current* price and
     today's exchange trading date.
   - **Idempotency / no duplicate writes:** if a snapshot row already has
     `snapshot_trading_date == current_trading_date` for that ticker, the write is skipped
     entirely — calling the endpoint again later the same trading day (extra page loads,
     accidental double-fire, retried requests) is a safe no-op. The baseline only actually moves
     the *first* time a visit is completed on a new trading day.
   - Returns `200` with a JSON body categorizing every ticker on the watchlist as `"updated"`
     (baseline moved forward), `"skipped"` (already had today's baseline — no-op), or
     `"unavailable"` (no usable market data right now, so no baseline could be written). The
     frontend isn't required to inspect this — firing the call is enough — but it's there for
     visibility/debugging rather than a bare `204`.

**Why this is the right shape, not just a workaround:** reading data should never have a side
effect a user didn't ask for. Separating "look at the dashboard" from "I've seen this, move my
baseline forward" also naturally handles a user who loads the dashboard but closes the tab before
it finishes rendering (e.g., a slow network) — their baseline correctly stays where it was, so
nothing is silently "consumed" without being seen. It also makes the endpoint trivially safe to
retry, which matters given the resilience emphasis of this challenge.

---

## 6a. Meaningful-change detection (additive, post-MVP)

A second, deliberately simpler signal layered on top of the Attention Score, added after the
initial build: a single configurable percent-change threshold — implemented in
`services/change_detection.py` — computed by comparing each `GET /api/dashboard` read's price
against the *previous read's*, stored in its own `market_snapshots` table (`device_id`, `ticker`,
`price`, `percent_change`, `captured_at`; `UNIQUE(device_id, ticker)`, one row per pair, upserted).
The threshold defaults to 2% and is configurable via `MEANINGFUL_CHANGE_THRESHOLD_PCT` (see
`config.py`) — no code change needed to tune it.

Every ticker is in exactly one of three states per read, each with exactly one message
(`DashboardItem.change_message`) — **never shown combined**, which is the whole point:
- **First visit** — no previous snapshot yet. The current price is stored as the baseline;
  `change_count` stays `0`, no change is ever reported.
  *"First visit — baseline saved for future comparison."*
- **No meaningful change** — a previous snapshot exists, but `abs(percent_change)` is below the
  threshold. *"No unusually significant movement since your last visit."*
- **Meaningful change** — `abs(percent_change) >= threshold`. `change_count` becomes `1` and the
  ticker moves into the frontend's "Meaningful changes" section.
  *"Price increased 2.8% since your last visit."* (or "decreased")

`DashboardItem` also carries `percent_change`, `change_count` (`0` or `1`), `signals` (0- or
1-element list containing the same message as `change_message`, kept for API shape continuity),
and `first_visit`.

**Deliberate exception to §6's read/write split:** unlike `VisitSnapshot` (which only advances on
an explicit `POST /api/visit/complete`), `MarketSnapshot` is compared against *and advanced* on
every `GET /api/dashboard` read. This is intentional for this feature specifically — "since your
last visit" here means since the last time the dashboard was loaded, not since the last completed
visit — and doesn't reopen the original problem §6 fixed (the Attention Score baseline is still
never touched by a GET). The Attention Score (`attention_score`/`reasons`, §7) is still computed
and still drives ranking, but its `reasons` text is deliberately **not** rendered by the frontend
— only `change_message` is — so a card never shows two different "first visit"/"quiet" messages
at once.

---

## 7. Attention Score algorithm

Lives entirely in `services/attention.py` as a pure function:

```python
def compute_attention_score(inputs: AttentionInput) -> AttentionResult
```

`AttentionInput` is a plain frozen dataclass — no DB/HTTP objects, no yfinance types, no
DataFrames — so tests construct it by hand with plain numbers. `historical_volatility_pct`,
`latest_volume`, `avg_volume_20d`, `week52_high`, `week52_low` are already-normalized numbers
computed upstream by `services/market_data.py`; the engine itself never touches raw price history
or does its own statistics — that keeps it trivially pure and keeps "how do we derive volatility
from OHLCV" isolated to the one module that also owns yfinance.

### Inputs
- `current_price`
- `previous_visit_price: float | None` — `None` means `comparison_available` is false (first-time view)
- `historical_volatility_pct: float | None` — 20-day daily-return stdev, as a percent (e.g. `1.8`)
- `latest_volume`, `avg_volume_20d`
- `week52_high`, `week52_low`

Each of the four components below is computed directly in **points** (its own 0–max range), not
as a 0–1 fraction of an overall weight — simpler to reason about and to unit test in isolation.
The four maxima (40 + 25 + 20 + 15) sum to exactly 100.

### A. Volatility-adjusted move (0–40) — **requires a baseline + known volatility**
```
if previous_visit_price is None: price_change_pct = None
else: price_change_pct = (current_price - previous_visit_price) / previous_visit_price * 100

if price_change_pct is None or historical_volatility_pct is None or historical_volatility_pct ~= 0:
    volatility_component = 0                          # guarded — never divides by zero
else:
    z_score = abs(price_change_pct) / historical_volatility_pct
    volatility_component = min(z_score / 3.0, 1) * 40  # a move >= 3x typical daily volatility maxes out
```

### B. Volume anomaly (0–25) — **market-derived, no baseline needed**
```
if latest_volume is None or avg_volume_20d is None or avg_volume_20d ~= 0:
    volume_component = 0                                        # never claims a volume signal it can't support
else:
    volume_ratio = latest_volume / avg_volume_20d
    volume_component = min(max(volume_ratio - 1, 0) / 2.0, 1) * 25   # ratio >= 3x maxes out; <=1x contributes 0
```

### C. 52-week range context (0–20) — **market-derived, no baseline needed; no direction judgment**
```
if week52_high is None or week52_low is None or week52_high <= week52_low:
    range_component = 0
else:
    dist_to_high = max(0, (week52_high - current_price) / current_price)   # 0 at/above the high
    dist_to_low  = max(0, (current_price - week52_low) / current_price)    # 0 at/below the low
    nearer_pct   = min(dist_to_high, dist_to_low) * 100
    range_component = max(0, 1 - nearer_pct / 5.0) * 20    # within 5% of either bound maxes out
```
Being near a 52-week high vs. low is never labeled "good" or "bad" — only "unusual for this
stock's yearly range."

### D. Raw move backstop (0–15) — **requires a baseline; guards against A under-scoring**
```
if price_change_pct is None: raw_move_component = 0
else: raw_move_component = min(abs(price_change_pct) / 10.0, 1) * 15   # a 10%+ move maxes out
```
Ensures a genuinely large move is never scored near-zero just because component A's volatility
data is missing, or the stock is normally this jumpy.

### Final score
```
score = round(volatility_component) + round(volume_component) + round(range_component) + round(raw_move_component)
score = clamp(score, 0, 100)
```
Weights are **not** redistributed when `previous_visit_price is None` — a first-time view can only
score on volume anomaly + 52-week context (components B and C). This is intentional and simple:
first-time scores skew lower, which is correct (there's genuinely less to compare yet), while
still surfacing a real signal if, say, a stock is added on the day it hits a 52-week high.

### Reasons (same module — `_generate_reasons` and helpers)
Rule-based templates, not LLM-based — deterministic, testable, fast. Never says "buy", "sell",
"bullish", "bearish", or predicts anything.
- **First-time ticker:** opens with *"First visit — "* followed by whatever market context (volume/
  range) is actually supported by data, e.g. *"First visit — currently trading at 2.8× average
  volume."* Never phrased as a comparison, since there isn't one yet.
- **Score < 15 ("quiet"):** *"No unusually significant movement since your last visit."*
- **15 ≤ score < 60 ("moderate"):** *"Price movement is larger than usual."* — a deliberately
  generic statement; at this range no single signal is strong enough to lead with specific numbers.
- **Score ≥ 60 ("high") with at least one supported signal:** names the strongest 1–2 signals with
  real interpolated numbers, e.g. *"Moved 2.3× more than its typical daily volatility, within 0.8%
  of its 52-week high."* Candidates are ranked by their component's point contribution; only a
  component whose underlying data was actually available is ever mentioned — a missing
  volume/volatility/range input is silently skipped, never guessed at.

### Ranking (future dashboard concern, not part of this module)
Once wired into `GET /api/dashboard` (a later phase): sort by `score` descending, tie-break by
`abs(price_change_pct)` descending. Items below the quiet threshold are still returned but grouped
under "no meaningful change" rather than mixed into the ranked list.

---

## 8. Edge cases and handling

| # | Edge case | Handling |
|---|-----------|----------|
| 1 | First-time user / no snapshot for a ticker | `comparison_available: false`; Steps 1 & 4 contribute 0; explanation opens with the baseline-establishing message, never "since your last visit"; optional `market_context.change_vs_prev_close_pct` shown separately, clearly labeled. |
| 2 | Dashboard has no meaningful changes | All items still returned with real scores; frontend shows a calm "nothing meaningfully different" state instead of forcing a noisy top-N. |
| 3 | Market data provider fails (timeout, exception, empty response) | `market_data.py` catches the error and serves the last cached in-memory entry for that ticker with `stale: true`, `stale_reason: "provider_error"`, `data_age_seconds` from the cache's `fetched_at`. |
| 4 | Provider fails **and** nothing is cached yet for that ticker (cold cache + outage) | That ticker is returned with an `"unavailable"` status and a friendly message; it does **not** fail the rest of `/api/dashboard` — each ticker is fetched/scored independently. |
| 5 | Invalid/unknown ticker on add | Provider lookup attempted synchronously on `POST /api/watchlist`; no resolvable data → `422` before any DB write. |
| 6 | Duplicate ticker add (including a same-device double-click race) | Ticker normalized to uppercase; DB `UNIQUE(device_id, ticker)` is the real guard. On a unique-constraint violation, the API returns the existing row (`200`) rather than erroring. |
| 7 | Empty watchlist | `GET /api/dashboard` returns `[]`, `200`; frontend shows an empty-state prompting the user to add a ticker; `POST /api/visit/complete` is a harmless no-op. |
| 8 | A watchlisted ticker later becomes invalid/delisted | Treated as a per-ticker provider failure (cases 3/4) — shown with an error badge, still removable, never crashes the dashboard. |
| 9 | New/recently-listed stock with < 20 trading days of history (or < 1 year for 52-week range) | Volatility/avg-volume computed from whatever history exists (minimum 5 sessions required, else that component contributes 0); 52-week high/low computed from all available history; response includes `partial_history: true`. |
| 10 | Near-zero volatility (very stable stock) | Guarded divide-by-zero in Step 1 — `f1` falls back to 0; the raw-magnitude backstop (`f4`) still catches a real move. |
| 11 | Multiple `POST /api/visit/complete` calls the same trading day | No-op after the first — guarded by `snapshot_trading_date` equality check (see §6). Safe to retry. |
| 12 | Dashboard loaded but `visit/complete` never fires (tab closed, network drop mid-render) | Baseline correctly stays at its old value; next dashboard load compares against the same, still-valid baseline — nothing was silently consumed. |
| 13 | Timezone inconsistency for trading-day comparisons | Trading dates are taken from the market data provider's own bar dates, not server/client wall-clock dates — avoids spurious day-boundary mismatches across timezones. |
| 14 | Missing/malformed `X-Device-Id` header | `400 Bad Request`; frontend always ensures a UUID exists in `localStorage` before any API call. |
| 15 | Concurrent requests for the same uncached/expired ticker (thundering herd) | An in-process per-ticker lock in `market_data.py` ensures only one upstream fetch happens per ticker at a time; concurrent callers await the same in-flight fetch. Single-process limitation, documented in §9. |
| 16 | Process restart | In-memory cache is empty; stale-fallback only protects tickers that have had at least one successful fetch since the restart. Documented trade-off of not persisting market data (§9). |
| 17 | SQLite write contention | WAL mode enabled at startup; write volume (watchlist + snapshot upserts) is far below where this matters at hackathon scale. |
| 18 | Large watchlist | Per-ticker fetches run concurrently (`asyncio.gather`) with a per-request overall timeout; each ticker isolated so one slow/failed fetch doesn't block the rest. Not paginated — acceptable at hackathon scale, called out as a scaling limit. |

---

## 9. Deliberate trade-offs / explicitly NOT building

- **No authentication.** `device_id` is a client-generated UUID in `localStorage`, unauthenticated
  and unverified — a deliberate MVP scope decision. Trade-off: clearing browser storage or
  switching devices loses the watchlist/history; no cross-device sync.
- **Market data cache is in-memory only, not persisted.** Simpler than a DB-backed cache table,
  no schema/migration surface for purely derived data, and correct for the single-process
  demo this is built for. Trade-off: cold on every process restart — a provider outage
  immediately after a restart has no stale data to fall back to for tickers not yet fetched in
  that process's lifetime (edge case #16). Acceptable for a hackathon demo; a follow-up could
  swap in Redis behind the same `market_data.py` interface without touching callers.
- **In-process TTL cache + lock, not Redis.** Not multi-instance-safe, by design — avoiding
  infra complexity with no hackathon payoff.
- **No real-time push (WebSockets/SSE).** The frontend loads the dashboard on demand; "since last
  visit" is inherently a check-in model, not a live feed, so push would imply a live-ness the
  data doesn't back up.
- **No alerting/notifications.** Pull-based ("come back and see what changed"), not push-based
  ("ping me at a threshold") — a different, larger product surface.
- **SQLite, single process, no Alembic.** Two small tables created via `create_all` at startup is
  sufficient for this scope; a real migration tool would be overhead with nothing to migrate yet.
- **No ML / predictive modeling.** The Attention Score is a transparent, deterministic statistical
  formula (z-scores, ratios, band proximity) — not a black box. Matches the stated goal ("not to
  predict prices") and keeps the engine unit-testable and explainable.
- **Daily OHLCV granularity, not intraday ticks.** Matches what a free data source reliably
  offers and matches the "check in occasionally" product framing.
- **yfinance as-is, no premium data vendor.** Acknowledged as unofficial/rate-limited; isolated
  entirely behind `market_data.py` so it's swappable, and the resilience story (TTL cache +
  stale fallback + per-ticker isolation) exists specifically because this dependency is expected
  to be unreliable.
- **No rate limiting / abuse protection on the API itself.** Out of scope for a judged demo
  running locally or on a single instance.
- **Minimal automated frontend testing.** Test investment concentrated on the Attention Engine
  (pure logic, highest-value target) and dashboard/API integration tests; the frontend is simple
  enough to verify manually within the hackathon timebox.

---

## 10. Implementation plan (phases)

1. ✅ **Scaffolding** — repo layout, `requirements.txt`, FastAPI app skeleton, `.gitignore`, DB init
   with WAL mode, two tables via `create_all`.
2. ✅ **Data layer** — SQLAlchemy models, CRUD for watchlist items and snapshots, including the
   idempotent "complete visit" upsert logic (`snapshot_service.py`).
3. ✅ **Market data service** — yfinance wrapper, in-memory TTL cache dict + per-ticker lock,
   stale-fallback logic. Testable in isolation with a fake/mocked provider call.
4. ✅ **Attention Engine** — pure scoring functions + explanation generator, built and unit-tested
   *before* wiring to the API (table-driven tests: no baseline, zero volatility, thin history,
   breakout, quiet stock, etc.).
5. ✅ **API routes** — `watchlist.py` (CRUD), `dashboard.py` (`GET /api/dashboard`,
   `POST /api/visit/complete`); integration tests against a temp SQLite DB.
   **Not built:** `stocks.py` (a `GET /api/stocks/{ticker}` detail/chart view) — nothing in the
   approved MVP scope requires it; deferred as optional future work, not an oversight.
6. ✅ **Frontend** — device-id bootstrap, watchlist management UI, dashboard load →
   render → fire `visit/complete`, stale/first-time/quiet/error states. No Chart.js — the
   optional sparkline depends on the detail endpoint above, which wasn't built.
7. ✅ **Demo readiness** — a real, confirmed risk (yfinance rate-limits this project's dev sandbox
   with `HTTP 429`) is mitigated by `DEMO_MODE` (`app/services/demo_data.py`): a small synthetic,
   deterministic fixture set standing in for yfinance behind the same cache/engine, including two
   tickers that demonstrate the stale-fallback and provider-failure stories live, on command. The
   full walkthrough — verified against the live server, not just designed on paper — is in
   [README.md](README.md). *(Visual/UI polish beyond what Phase 6 already built was explicitly
   deprioritized in favor of this — a demo that survives an unreliable network matters more than
   extra styling.)*
