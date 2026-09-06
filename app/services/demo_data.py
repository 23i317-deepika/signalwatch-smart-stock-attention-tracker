"""
Synthetic market data for live demos (DEMO_MODE=true — see config.py).

Real yfinance calls can and do fail from shared/datacenter IPs (confirmed
during development: Yahoo Finance returned HTTP 429 consistently from this
project's dev sandbox). Rather than hope a live demo's network cooperates,
demo mode swaps the market data source for a small, deterministic fixture
set — same MarketData shape, same TTL cache, same Attention Engine; only
the provider underneath changes. This is off by default and clearly
labeled; it never substitutes fake data for real data unasked.

Two tickers exist purely to demonstrate resilience on command, live:
  - FAILDEMO always fails. No cache is ever established for it, so it
    always renders as "unavailable" — the per-ticker isolation story.
  - FLAKY succeeds exactly once per TTL-cache lifetime, then fails on every
    subsequent fetch until the process restarts — reload the dashboard
    after the demo TTL expires (default 15s) to see it flip to `stale:
    true`, still showing its last good price — the stale-fallback story.

Every other fixture ticker "moves" (per its `move_pct`) starting on its
second fetch (i.e. after the demo TTL first expires), so a second dashboard
load after that point demonstrates a real "changed since your last visit"
story instead of a flat, unchanging number.
"""

import threading
from datetime import date, datetime, timezone

from app.services.market_data import MarketData, ProviderError, TickerNotFoundError

ALWAYS_FAILS_TICKER = "FAILDEMO"
FLAKY_TICKER = "FLAKY"

# base_price/prev_close: today's and yesterday's price on the *first* fetch.
# move_pct: how much current_price jumps (once) starting on the second
# fetch — the "since your last visit" story a reload-after-TTL reveals.
DEMO_FIXTURES: dict[str, dict] = {
    "STEADY": dict(
        base_price=50.00, prev_close=49.85, move_pct=0.3,
        volatility_20d_pct=1.2, latest_volume=1_000_000, avg_volume_20d=1_050_000,
        week52_high=60.00, week52_low=40.00,
    ),
    "MOVER": dict(
        base_price=100.00, prev_close=99.00, move_pct=8.0,
        volatility_20d_pct=1.5, latest_volume=5_000_000, avg_volume_20d=1_500_000,
        week52_high=140.00, week52_low=70.00,
    ),
    "BREAKOUT": dict(
        base_price=90.00, prev_close=89.50, move_pct=2.0,
        volatility_20d_pct=2.0, latest_volume=1_200_000, avg_volume_20d=1_100_000,
        week52_high=92.00, week52_low=60.00,
    ),
    FLAKY_TICKER: dict(
        base_price=75.00, prev_close=74.50, move_pct=0.0,
        volatility_20d_pct=1.3, latest_volume=900_000, avg_volume_20d=950_000,
        week52_high=85.00, week52_low=65.00,
    ),
}

DEMO_TICKERS = sorted(DEMO_FIXTURES.keys() | {ALWAYS_FAILS_TICKER})

_lock = threading.Lock()
_fetch_counts: dict[str, int] = {}


def reset_demo_state() -> None:
    """Clear per-ticker fetch-count memory (FLAKY's "already succeeded
    once" state, and every fixture's "already moved" state) without
    restarting the process. Used by tests; harmless to call anytime."""
    with _lock:
        _fetch_counts.clear()


def fetch_demo_market_data(ticker: str) -> MarketData:
    """The demo-mode stand-in for `market_data.fetch_market_data`.

    Same exceptions, same return type — the TTL cache and everything
    downstream can't tell the difference.
    """
    ticker = ticker.strip().upper()

    if ticker == ALWAYS_FAILS_TICKER:
        raise ProviderError(f"[demo] Simulated provider outage for '{ticker}'.")

    if ticker not in DEMO_FIXTURES:
        raise TickerNotFoundError(
            f"'{ticker}' is not a recognized demo ticker. Demo mode only serves: "
            f"{', '.join(DEMO_TICKERS)}."
        )

    with _lock:
        fetch_count = _fetch_counts.get(ticker, 0) + 1
        _fetch_counts[ticker] = fetch_count

    if ticker == FLAKY_TICKER and fetch_count > 1:
        raise ProviderError(f"[demo] Simulated provider outage for '{ticker}' (was working a moment ago).")

    fixture = DEMO_FIXTURES[ticker]
    current_price = fixture["base_price"]
    if fetch_count > 1:
        current_price = fixture["base_price"] * (1 + fixture["move_pct"] / 100)

    return MarketData(
        ticker=ticker,
        current_price=round(current_price, 2),
        prev_close=fixture["prev_close"],
        latest_volume=fixture["latest_volume"],
        avg_volume_20d=fixture["avg_volume_20d"],
        volatility_20d_pct=fixture["volatility_20d_pct"],
        week52_high=fixture["week52_high"],
        week52_low=fixture["week52_low"],
        trading_date=date.today(),
        fetched_at=datetime.now(timezone.utc),
        partial_history=False,
    )
