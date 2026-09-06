"""
Tests for the market data provider normalization logic
(backend/app/services/market_data.py).

yfinance is fully mocked — no live network, no real stock data.
"""

import pandas as pd
import pytest

from app.services import market_data


class _FakeTicker:
    """Stands in for yf.Ticker(symbol) — only .history() is exercised."""

    def __init__(self, df: pd.DataFrame | None = None, raises: Exception | None = None):
        self._df = df
        self._raises = raises

    def history(self, **kwargs):
        if self._raises is not None:
            raise self._raises
        return self._df


def _build_history(n: int, *, include_volume=True, include_range=True) -> pd.DataFrame:
    """A simple, deterministic n-day daily OHLCV frame, ascending by date."""
    dates = pd.bdate_range(end=pd.Timestamp("2026-09-04"), periods=n)
    closes = [100.0 * (1 + 0.01 * (i % 3 - 1)) for i in range(n)]  # small deterministic wiggle
    data = {"Close": closes}
    if include_range:
        data["High"] = [c * 1.02 for c in closes]
        data["Low"] = [c * 0.98 for c in closes]
    if include_volume:
        data["Volume"] = [1_000_000 + 1000 * i for i in range(n)]
    return pd.DataFrame(data, index=dates)


def _patch_ticker(monkeypatch, ticker_instance: _FakeTicker):
    monkeypatch.setattr(market_data.yf, "Ticker", lambda symbol: ticker_instance)


def test_invalid_ticker_raises_ticker_not_found(monkeypatch):
    _patch_ticker(monkeypatch, _FakeTicker(df=pd.DataFrame()))

    with pytest.raises(market_data.TickerNotFoundError):
        market_data.fetch_market_data("NOTAREALTICKER")


def test_provider_exception_raises_provider_error(monkeypatch):
    _patch_ticker(monkeypatch, _FakeTicker(raises=ConnectionError("network down")))

    with pytest.raises(market_data.ProviderError):
        market_data.fetch_market_data("AAPL")


def test_valid_history_is_normalized_correctly(monkeypatch):
    df = _build_history(30)
    _patch_ticker(monkeypatch, _FakeTicker(df=df))

    data = market_data.fetch_market_data("AAPL")

    assert data.ticker == "AAPL"
    assert data.current_price == df["Close"].iloc[-1]
    assert data.prev_close == df["Close"].iloc[-2]
    assert data.trading_date == df.index[-1].date()
    assert data.latest_volume == df["Volume"].iloc[-1]
    assert data.avg_volume_20d == pytest.approx(df["Volume"].iloc[:-1].tail(20).mean())
    assert data.week52_high == pytest.approx(df["High"].max())
    assert data.week52_low == pytest.approx(df["Low"].min())
    assert data.volatility_20d_pct is not None and data.volatility_20d_pct > 0
    assert data.partial_history is True  # 30 rows < FULL_HISTORY_MIN_ROWS


def test_full_year_history_is_not_marked_partial(monkeypatch):
    df = _build_history(252)
    _patch_ticker(monkeypatch, _FakeTicker(df=df))

    data = market_data.fetch_market_data("AAPL")

    assert data.partial_history is False


def test_missing_volume_column_does_not_crash(monkeypatch):
    df = _build_history(30, include_volume=False)
    _patch_ticker(monkeypatch, _FakeTicker(df=df))

    data = market_data.fetch_market_data("AAPL")

    assert data.latest_volume is None
    assert data.avg_volume_20d is None
    # Everything else still computed.
    assert data.current_price == df["Close"].iloc[-1]


def test_missing_52_week_range_does_not_crash(monkeypatch):
    df = _build_history(30, include_range=False)
    _patch_ticker(monkeypatch, _FakeTicker(df=df))

    data = market_data.fetch_market_data("AAPL")

    assert data.week52_high is None
    assert data.week52_low is None


def test_very_short_history_skips_volatility_but_does_not_crash(monkeypatch):
    df = _build_history(2)  # below MIN_ROWS_FOR_ROLLING_STATS
    _patch_ticker(monkeypatch, _FakeTicker(df=df))

    data = market_data.fetch_market_data("AAPL")

    assert data.volatility_20d_pct is None
    assert data.partial_history is True
    assert data.current_price == df["Close"].iloc[-1]


def test_single_row_history_uses_current_price_as_prev_close(monkeypatch):
    df = _build_history(1)
    _patch_ticker(monkeypatch, _FakeTicker(df=df))

    data = market_data.fetch_market_data("AAPL")

    assert data.prev_close == data.current_price


def test_network_timeout_raises_provider_error(monkeypatch):
    _patch_ticker(monkeypatch, _FakeTicker(raises=TimeoutError("request timed out after 10s")))

    with pytest.raises(market_data.ProviderError):
        market_data.fetch_market_data("AAPL")


def test_describe_error_never_leaks_raw_provider_exception_text():
    raw = market_data.ProviderError("Connection refused by upstream host 10.0.0.5:443 [errno 111]")

    message = market_data.describe_error(raw)

    assert "10.0.0.5" not in message
    assert "errno" not in message
    assert message == "Market data is temporarily unavailable. Please try again later."


def test_describe_error_for_ticker_not_found_is_user_friendly():
    message = market_data.describe_error(market_data.TickerNotFoundError("No market data found for ticker 'X'"))

    assert message == "We couldn't find market data for this ticker. Please check the symbol."
