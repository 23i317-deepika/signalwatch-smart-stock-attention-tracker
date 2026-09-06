# SignalWatch

A smart market watchlist built for the **Code, by Groww** hackathon.

SignalWatch doesn't flag a stock because it moved more than some fixed percentage. It flags a
stock because the move is **unusual for that stock** — bigger than its own recent volatility
would predict, on unusually heavy volume, or at an unusual point in its 52-week range — relative
to what you saw **the last time you looked**. The Attention Score exists to reduce noise, not to
predict prices or recommend trades.

Full design rationale, schema, algorithm derivation, and edge-case handling: **[PROJECT_PLAN.md](PROJECT_PLAN.md)**.
This README is the quick-start + live-demo guide.

---

## 1. Setup

Requires Python 3.10+.

```powershell
cd backend
python -m venv venv
.\venv\Scripts\python.exe -m pip install -r requirements.txt
```

## 2. Run it

**Backend** (from `backend/`):
```powershell
.\venv\Scripts\python.exe -m uvicorn app.main:app --reload
```
Serves on `http://127.0.0.1:8000`. `GET /api/health` → `{"status": "ok"}`. A SQLite file
(`backend/signalwatch.db`) is created automatically on first run.

**Frontend** (from `frontend/`, any static server — this one's in the backend's CORS allowlist):
```powershell
python -m http.server 5500
```
Open `http://127.0.0.1:5500`. No build step — plain HTML/CSS/vanilla JS.

## 3. Run the tests

```powershell
cd backend
.\venv\Scripts\python.exe -m pytest -q
```
65 tests, all offline (yfinance is fully mocked; the market-data cache uses a fake provider and
an injectable clock — nothing sleeps, nothing hits the network).

---

## 4. ⚠️ A known, real limitation — and how demo mode handles it

**yfinance calls can fail from shared/cloud/datacenter IP addresses.** This isn't hypothetical —
during development, Yahoo Finance consistently returned `HTTP 429 Too Many Requests` to the dev
sandbox's outbound IP. This is exactly the kind of "unreliable dependency" the app is designed to
tolerate (see the TTL cache + stale-fallback + per-ticker isolation in `services/market_data.py`),
but it means a live demo on an unfamiliar network is a real risk, not a hypothetical one.

**Mitigation: `DEMO_MODE`.** Set the environment variable before starting the backend:

```powershell
$env:DEMO_MODE = "true"
.\venv\Scripts\python.exe -m uvicorn app.main:app --reload
```

In demo mode, market data comes from a small synthetic, deterministic fixture set
(`app/services/demo_data.py`) instead of yfinance — same `MarketData` shape, same TTL cache, same
Attention Engine underneath; only the data source changes. It's off by default, and every fixture
ticker is a made-up symbol, never a real company's ticker with fake numbers under it — nothing
here could be mistaken for real market data.

If your demo network turns out to be fine, you don't need this — real tickers work normally.
`DEMO_MODE` is the fallback plan, not the primary path.

### Demo mode ticker cheat sheet

| Ticker | Story it tells |
|---|---|
| `MOVER` | A big, unusual move — high Attention Score with a combined "volatility × volume" explanation. |
| `BREAKOUT` | A modest move but very close to its 52-week high — a moderate score driven by range context. |
| `STEADY` | Barely moves — a near-zero score, demonstrating noise reduction. |
| `FLAKY` | Succeeds once, then fails every fetch after — demonstrates the **stale-data fallback** live. |
| `FAILDEMO` | Always fails, never has any cached data — demonstrates **per-ticker failure isolation** live. |

### Suggested live walkthrough (~2 minutes)

1. Start the backend with `DEMO_MODE=true` (demo mode also drops the market-data cache TTL to 15s,
   so "later" is a short, comfortable pause instead of a real wait).
2. Open the frontend. Add `MOVER`, `BREAKOUT`, `STEADY`, `FLAKY`, `FAILDEMO`.
3. **First load**: `FAILDEMO` immediately shows "unavailable" — one bad ticker, no crash, nothing
   else affected. Everything else shows a "First visit —" message (no comparison yet), and the
   *visit-complete* call fires in the background, quietly establishing today's prices as the
   baseline for next time.
4. **Wait ~15–20 seconds**, then reload the page (or re-trigger the dashboard load).
5. Now the story lands:
   - `MOVER` jumps into "Worth a look" with a real combined explanation
     (*"Moved 5.3× its typical volatility on 3.3× average volume."*).
   - `BREAKOUT` shows a moderate score from being near its 52-week high.
   - `STEADY` stays in "No meaningful change" — this is the noise-reduction story, working as
     intended, not a bug.
   - `FLAKY` now shows a **stale** badge — its last known price, clearly marked as stale, because
     its "provider" started failing. This is the resilience story, live, on command.
   - `FAILDEMO` is still "unavailable" — it never had data to fall back to.
6. Reloading again is safe and repeatable — the story doesn't drift or reset, so you can re-run
   step 4–5 as many times as you want on stage without losing the thread.

(This exact sequence was run against the live server during development to confirm the numbers
above — it's not a hoped-for outcome, it's what actually happens.)

---

## 5. API summary

| Method | Path | Notes |
|---|---|---|
| `GET` | `/api/health` | Liveness check. |
| `GET` | `/api/watchlist` | Raw watchlist (ticker, added_at) for the device in `X-Device-ID`. |
| `POST` | `/api/watchlist` | `{"ticker": "AAPL"}` — add one; `409` on duplicate. |
| `DELETE` | `/api/watchlist/{ticker}` | Remove one; `404` if absent. |
| `GET` | `/api/dashboard` | The scored, ranked, explained view. Read-only — never writes a baseline. |
| `POST` | `/api/visit/complete` | Advances the "since last visit" baseline. Idempotent within a trading day. Call only after a successful dashboard render. |

All endpoints except `/api/health` require an `X-Device-ID` header (a client-generated UUID —
see §9 below). Full request/response shapes: `backend/app/schemas.py`.

---

## 6. Architecture, in one paragraph

FastAPI + SQLAlchemy + SQLite persist only user state (which tickers a device watches, and its
last-visit price baseline). Market data (price, volatility, volume, 52-week range) lives in a
plain in-memory TTL cache, shared across devices, keyed by ticker — not persisted, since it's
derived data, not user state. The **Attention Engine** (`app/services/attention.py`) is a pure
function with no FastAPI/SQLAlchemy/yfinance imports: plain numbers in, a 0–100 score + a
human-readable explanation out. See [PROJECT_PLAN.md](PROJECT_PLAN.md) for the full schema, the
exact scoring formula, and the complete edge-case list.

## 7. Deliberate trade-offs (not oversights)

- **No authentication.** A device is identified by a UUID the frontend generates and stores in
  `localStorage` — not a real account. Clearing site data or switching devices starts a fresh
  watchlist. This is a deliberate MVP scope decision: it satisfies "remember me across visits"
  without the complexity of accounts, which isn't what this challenge evaluates.
- **In-memory market-data cache, not persisted, not Redis.** Correct for a single demo process;
  empty again on restart. A follow-up could swap in Redis behind the same interface.
- **No real-time push.** The dashboard is pull-based ("come back and see what changed"), matching
  the product's own framing — not a live trading feed.
- **No ML/prediction.** The score is a transparent, deterministic statistical formula
  (z-scores, ratios, band proximity) — not a black box, and never advice.
- **Daily OHLCV granularity**, not intraday ticks — matches what a free data source reliably
  offers and the "check in occasionally" product framing.

The full list, with rationale for each, is in [PROJECT_PLAN.md §9](PROJECT_PLAN.md).
