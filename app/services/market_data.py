"""
Market data service.

Owns ALL yfinance-specific logic. Nothing outside this module should ever
import yfinance or touch a DataFrame — everything downstream (the Attention
Engine, and later the dashboard API) works only with the normalized
dataclasses defined here.

Also owns the in-memory TTL cache in front of the provider (kept in this
same file rather than a separate module — see PROJECT_PLAN.md §3/§9: one
file for "get me market data for a ticker, cheaply and resiliently" is
simpler than splitting provider and cache across files for no real gain).
"""

from __future__ import annotations

import logging
import statistics
import threading
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import Callable

import yfinance as yf

from app.config import settings

# TEMPORARY diagnostic logger — see main.py's logging.basicConfig(). Remove
# once market-data issues are no longer being actively diagnosed.
logger = logging.getLogger(__name__)

# How much daily history to pull in one call. One year comfortably covers
# the 52-week range and the 20-day volatility/volume windows without extra
# round trips per stat (PROJECT_PLAN.md: "use recent historical daily data
# ... instead of making many separate API calls").
HISTORY_PERIOD = "1y"

# Below this many trading days of history, 52-week/20-day stats are based on
# less than their full intended window (still computed from what's
# available — just flagged so callers can decide whether to caveat it).
FULL_HISTORY_MIN_ROWS = 200

# Minimum trading days needed to consider a 20-day rolling stat meaningful
# at all (PROJECT_PLAN.md edge case: brand-new listings).
MIN_ROWS_FOR_ROLLING_STATS = 5


class MarketDataError(Exception):
    """Base class for market-data failures the caller can catch generically."""


class TickerNotFoundError(MarketDataError):
    """The ticker doesn't resolve to any real market data (invalid symbol)."""


class ProviderError(MarketDataError):
    """The provider call itself failed (network/timeout/unexpected error)."""


def describe_error(exc: MarketDataError) -> str:
    """A safe, user-facing message for a market-data failure.

    Never returns the raw exception text — `ProviderError` in particular
    wraps whatever yfinance/requests/network exception actually occurred,
    which can embed internal detail (hostnames, raw HTTP errors, etc.) that
    has no business reaching a client. Callers should still log the real
    exception server-side (see market_data.py's own logging, and callers in
    routers/) — this is only what gets returned over the API.
    """
    if isinstance(exc, TickerNotFoundError):
        return "We couldn't find market data for this ticker. Please check the symbol."
    return "Market data is temporarily unavailable. Please try again later."


@dataclass(frozen=True)
class MarketData:
    """Normalized market data for one ticker, as of one fetch."""

    ticker: str
    current_price: float
    prev_close: float
    latest_volume: float | None
    avg_volume_20d: float | None
    volatility_20d_pct: float | None  # stdev of daily returns, in percent (e.g. 1.8 = 1.8%/day)
    week52_high: float | None
    week52_low: float | None
    trading_date: date
    fetched_at: datetime
    partial_history: bool  # True if less than ~a year of daily history was available


@dataclass(frozen=True)
class MarketDataResult:
    """What the cache returns to callers: data plus freshness metadata."""

    data: MarketData
    stale: bool
    stale_reason: str | None = None
    data_age_seconds: float = 0.0


def fetch_market_data(ticker: str) -> MarketData:
    """Fetch and normalize market data for one ticker via yfinance.

    Raises:
        TickerNotFoundError: the symbol doesn't resolve to any data.
        ProviderError: the provider call itself failed (network, timeout,
            unexpected exception) — distinct from an invalid ticker so
            callers (the TTL cache) can fall back to stale data for a
            *transient* failure, while an invalid ticker is a real 404.
    """
    try:
        history = yf.Ticker(ticker).history(period=HISTORY_PERIOD, interval="1d")
    except Exception as exc:  # yfinance/requests can raise all sorts of things
        # TEMPORARY: log the real underlying exception (network error, JSON
        # decode error from a stale/broken yfinance version, rate limit,
        # etc.) — the wrapped ProviderError message alone often isn't enough
        # to tell those apart.
        logger.warning("yfinance provider call failed for '%s': %r", ticker, exc)
        raise ProviderError(f"Failed to fetch market data for '{ticker}': {exc}") from exc

    if history is None or history.empty:
        logger.warning("yfinance returned no history rows for '%s' (invalid ticker or empty response)", ticker)
        raise TickerNotFoundError(f"No market data found for ticker '{ticker}'")

    closes = history["Close"].dropna()
    if closes.empty:
        logger.warning("yfinance history for '%s' has no usable Close prices", ticker)
        raise TickerNotFoundError(f"No price data found for ticker '{ticker}'")

    logger.info("Fetched market data for '%s': %d rows, latest close %.2f", ticker, len(history), float(closes.iloc[-1]))

    current_price = float(closes.iloc[-1])
    prev_close = float(closes.iloc[-2]) if len(closes) >= 2 else current_price
    trading_date = history.index[-1].to_pydatetime().date()

    latest_volume, avg_volume_20d = _volume_stats(history)
    volatility_20d_pct = _volatility_stats(closes)
    week52_high, week52_low = _range_stats(history)

    return MarketData(
        ticker=ticker,
        current_price=current_price,
        prev_close=prev_close,
        latest_volume=latest_volume,
        avg_volume_20d=avg_volume_20d,
        volatility_20d_pct=volatility_20d_pct,
        week52_high=week52_high,
        week52_low=week52_low,
        trading_date=trading_date,
        fetched_at=datetime.now(timezone.utc),
        partial_history=len(history) < FULL_HISTORY_MIN_ROWS,
    )


def _volume_stats(history) -> tuple[float | None, float | None]:
    """Latest completed volume + trailing 20-day average, excluding the latest bar."""
    if "Volume" not in history.columns:
        return None, None

    volumes = history["Volume"].dropna()
    if volumes.empty:
        return None, None

    latest_volume = float(volumes.iloc[-1])

    # Average over the window *before* the latest bar, so a still-accumulating
    # "today" is never compared against itself.
    prior_window = volumes.iloc[:-1].tail(20)
    if len(prior_window) < 1:
        return latest_volume, None

    return latest_volume, float(prior_window.mean())


def _volatility_stats(closes) -> float | None:
    """Stdev of daily returns over the trailing ~20 trading days, as a percent."""
    window = closes.tail(21)  # up to 21 closes -> up to 20 returns
    if len(window) < MIN_ROWS_FOR_ROLLING_STATS:
        return None

    returns = window.pct_change().dropna()
    if len(returns) < 2:
        # stdev is undefined (or meaningless) with fewer than 2 data points.
        return None

    return statistics.stdev(returns.tolist()) * 100


def _range_stats(history) -> tuple[float | None, float | None]:
    week52_high = None
    week52_low = None
    if "High" in history.columns:
        highs = history["High"].dropna()
        if not highs.empty:
            week52_high = float(highs.max())
    if "Low" in history.columns:
        lows = history["Low"].dropna()
        if not lows.empty:
            week52_low = float(lows.min())
    return week52_high, week52_low


@dataclass
class _CacheEntry:
    data: MarketData
    fetched_at: datetime
    expires_at: datetime


class MarketDataCache:
    """In-memory, per-ticker TTL cache in front of the market data provider.

    Not persisted (see PROJECT_PLAN.md §2/§9) — a plain dict, empty again on
    process restart. Behavior:

    1. Fresh cache hit -> return immediately, stale=False, no provider call.
    2. Missing/expired -> attempt a fetch.
    3. Fetch succeeds -> cache updated, fresh data returned.
    4. Fetch fails but a cached entry exists -> return it, stale=True.
    5. Fetch fails and nothing is cached -> re-raise for the caller to
       handle per-ticker (never let one bad ticker break a whole batch).

    `clock` is injectable so tests can control expiry deterministically
    without sleeping or patching global time.
    """

    def __init__(
        self,
        ttl_seconds: int,
        fetch_fn: Callable[[str], MarketData] | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._ttl_seconds = ttl_seconds
        self._fetch_fn = fetch_fn or fetch_market_data
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._entries: dict[str, _CacheEntry] = {}
        self._ticker_locks: dict[str, threading.Lock] = {}
        self._locks_guard = threading.Lock()

    def get(self, ticker: str) -> MarketDataResult:
        """Return normalized market data for `ticker`, per the behavior above."""
        ticker = ticker.strip().upper()

        cached = self._fresh_entry(ticker)
        if cached is not None:
            return cached

        # Cache miss/expired — one fetch per ticker at a time (avoids a
        # thundering herd of concurrent requests all hitting the provider
        # for the same expired ticker; single-process limitation, which is
        # fine at this scope — see PROJECT_PLAN.md).
        with self._lock_for(ticker):
            cached = self._fresh_entry(ticker)  # re-check post-lock
            if cached is not None:
                return cached

            stale_entry = self._entries.get(ticker)
            try:
                fresh = self._fetch_fn(ticker)
            except MarketDataError as exc:
                if stale_entry is not None:
                    logger.warning(
                        "Fetch failed for '%s' (%s); serving stale cached data from %s",
                        ticker, exc, stale_entry.fetched_at,
                    )
                    now = self._clock()
                    return MarketDataResult(
                        data=stale_entry.data,
                        stale=True,
                        stale_reason="provider_error",
                        data_age_seconds=(now - stale_entry.fetched_at).total_seconds(),
                    )
                logger.warning("Fetch failed for '%s' (%s) and nothing cached — returning unavailable", ticker, exc)
                raise

            now = self._clock()
            self._entries[ticker] = _CacheEntry(
                data=fresh,
                fetched_at=now,
                expires_at=now + timedelta(seconds=self._ttl_seconds),
            )
            return MarketDataResult(data=fresh, stale=False, data_age_seconds=0.0)

    def _fresh_entry(self, ticker: str) -> MarketDataResult | None:
        entry = self._entries.get(ticker)
        if entry is None:
            return None
        now = self._clock()
        if now >= entry.expires_at:
            return None
        return MarketDataResult(
            data=entry.data,
            stale=False,
            data_age_seconds=(now - entry.fetched_at).total_seconds(),
        )

    def _lock_for(self, ticker: str) -> threading.Lock:
        with self._locks_guard:
            return self._ticker_locks.setdefault(ticker, threading.Lock())


# Single shared cache for the whole process — market data is the same for
# every device watching a given ticker (see PROJECT_PLAN.md §2), so there's
# exactly one cache, not one per request. A plain module-level singleton is
# enough for a single-process app; exposed as a FastAPI dependency so tests
# can override it with a fake via `app.dependency_overrides`.
#
# In demo mode (see config.py / app/services/demo_data.py), the same cache
# class fronts a synthetic fetch function and a short TTL instead of
# yfinance — everything downstream is unaware of the difference.
if settings.demo_mode:
    from app.services.demo_data import fetch_demo_market_data

    _market_data_cache = MarketDataCache(
        ttl_seconds=settings.demo_cache_ttl_seconds, fetch_fn=fetch_demo_market_data
    )
else:
    _market_data_cache = MarketDataCache(ttl_seconds=settings.market_data_cache_ttl_seconds)


def get_market_data_cache() -> MarketDataCache:
    return _market_data_cache
