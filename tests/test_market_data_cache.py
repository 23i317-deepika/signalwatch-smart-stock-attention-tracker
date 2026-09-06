"""
Tests for the in-memory TTL cache (backend/app/services/market_data.py:
MarketDataCache). No real yfinance calls — the provider is a fake, and the
clock is fully controlled, so nothing here sleeps or touches the network.
"""

from datetime import date, datetime, timedelta, timezone

import pytest

from app.services.market_data import MarketData, MarketDataCache, ProviderError


def make_market_data(price: float = 100.0) -> MarketData:
    return MarketData(
        ticker="AAPL",
        current_price=price,
        prev_close=price,
        latest_volume=1_000_000,
        avg_volume_20d=1_000_000,
        volatility_20d_pct=1.5,
        week52_high=110.0,
        week52_low=90.0,
        trading_date=date(2026, 9, 4),
        fetched_at=datetime(2026, 9, 4, tzinfo=timezone.utc),
        partial_history=False,
    )


class FakeClock:
    """A controllable clock — tests advance it explicitly, never sleep."""

    def __init__(self, start: datetime):
        self._now = start

    def __call__(self) -> datetime:
        return self._now

    def advance(self, seconds: float) -> None:
        self._now += timedelta(seconds=seconds)


class FakeProvider:
    """A fetch_fn stand-in: returns/raises queued responses in order, counts calls."""

    def __init__(self):
        self.calls = 0
        self._queue: list = []

    def queue(self, response) -> None:
        self._queue.append(response)

    def __call__(self, ticker: str) -> MarketData:
        self.calls += 1
        if not self._queue:
            raise AssertionError("FakeProvider called with nothing queued")
        response = self._queue.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def test_fresh_cache_hit_does_not_call_provider_again():
    clock = FakeClock(datetime(2026, 1, 1, tzinfo=timezone.utc))
    provider = FakeProvider()
    provider.queue(make_market_data())
    cache = MarketDataCache(ttl_seconds=60, fetch_fn=provider, clock=clock)

    first = cache.get("AAPL")
    clock.advance(10)  # well within the 60s TTL
    second = cache.get("AAPL")

    assert provider.calls == 1
    assert first.stale is False
    assert second.stale is False
    assert second.data_age_seconds == pytest.approx(10)


def test_expired_cache_fetches_fresh_data():
    clock = FakeClock(datetime(2026, 1, 1, tzinfo=timezone.utc))
    provider = FakeProvider()
    provider.queue(make_market_data(price=100.0))
    provider.queue(make_market_data(price=105.0))
    cache = MarketDataCache(ttl_seconds=60, fetch_fn=provider, clock=clock)

    first = cache.get("AAPL")
    clock.advance(61)  # past the TTL
    second = cache.get("AAPL")

    assert provider.calls == 2
    assert first.data.current_price == 100.0
    assert second.data.current_price == 105.0
    assert second.stale is False


def test_provider_failure_with_cached_data_returns_stale():
    clock = FakeClock(datetime(2026, 1, 1, tzinfo=timezone.utc))
    provider = FakeProvider()
    provider.queue(make_market_data(price=100.0))
    provider.queue(ProviderError("provider is down"))
    cache = MarketDataCache(ttl_seconds=60, fetch_fn=provider, clock=clock)

    cache.get("AAPL")
    clock.advance(61)
    result = cache.get("AAPL")

    assert result.stale is True
    assert result.stale_reason == "provider_error"
    assert result.data.current_price == 100.0  # last good data, not an error


def test_provider_failure_without_cached_data_raises():
    clock = FakeClock(datetime(2026, 1, 1, tzinfo=timezone.utc))
    provider = FakeProvider()
    provider.queue(ProviderError("provider is down"))
    cache = MarketDataCache(ttl_seconds=60, fetch_fn=provider, clock=clock)

    with pytest.raises(ProviderError):
        cache.get("AAPL")


def test_ticker_lookup_is_case_insensitive():
    clock = FakeClock(datetime(2026, 1, 1, tzinfo=timezone.utc))
    provider = FakeProvider()
    provider.queue(make_market_data())
    cache = MarketDataCache(ttl_seconds=60, fetch_fn=provider, clock=clock)

    cache.get("aapl")
    result = cache.get("AAPL")

    assert provider.calls == 1
    assert result.stale is False
