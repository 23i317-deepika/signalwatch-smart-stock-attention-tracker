"""
Tests for the demo-mode synthetic market data
(backend/app/services/demo_data.py). No network, no yfinance.
"""

import pytest

from app.services.demo_data import (
    ALWAYS_FAILS_TICKER,
    DEMO_FIXTURES,
    FLAKY_TICKER,
    fetch_demo_market_data,
    reset_demo_state,
)
from app.services.market_data import ProviderError, TickerNotFoundError


@pytest.fixture(autouse=True)
def _reset():
    reset_demo_state()
    yield
    reset_demo_state()


def test_known_fixture_ticker_returns_base_price_on_first_fetch():
    data = fetch_demo_market_data("STEADY")

    assert data.ticker == "STEADY"
    assert data.current_price == DEMO_FIXTURES["STEADY"]["base_price"]


def test_fixture_ticker_moves_on_second_fetch():
    first = fetch_demo_market_data("MOVER")
    second = fetch_demo_market_data("MOVER")

    assert first.current_price == 100.00
    assert second.current_price == pytest.approx(108.00)  # +8% move_pct
    assert second.current_price != first.current_price


def test_fixture_ticker_stays_at_moved_price_on_further_fetches():
    fetch_demo_market_data("MOVER")
    fetch_demo_market_data("MOVER")
    third = fetch_demo_market_data("MOVER")

    assert third.current_price == pytest.approx(108.00)  # stable, not drifting further


def test_always_fails_ticker_always_raises_provider_error():
    with pytest.raises(ProviderError):
        fetch_demo_market_data(ALWAYS_FAILS_TICKER)
    with pytest.raises(ProviderError):
        fetch_demo_market_data(ALWAYS_FAILS_TICKER)


def test_flaky_ticker_succeeds_once_then_fails():
    first = fetch_demo_market_data(FLAKY_TICKER)
    assert first.current_price == DEMO_FIXTURES[FLAKY_TICKER]["base_price"]

    with pytest.raises(ProviderError):
        fetch_demo_market_data(FLAKY_TICKER)
    with pytest.raises(ProviderError):
        fetch_demo_market_data(FLAKY_TICKER)


def test_unknown_ticker_raises_ticker_not_found():
    with pytest.raises(TickerNotFoundError):
        fetch_demo_market_data("NOTADEMOTICKER")


def test_ticker_lookup_is_case_insensitive():
    data = fetch_demo_market_data("steady")

    assert data.ticker == "STEADY"


def test_reset_demo_state_clears_fetch_counts():
    fetch_demo_market_data("MOVER")  # fetch #1
    fetch_demo_market_data("MOVER")  # fetch #2 -> moved

    reset_demo_state()

    data = fetch_demo_market_data("MOVER")  # back to fetch #1 behavior
    assert data.current_price == 100.00
