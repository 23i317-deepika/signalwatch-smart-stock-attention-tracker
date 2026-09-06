"""
Integration tests for GET /api/dashboard and POST /api/visit/complete.

The market data cache is swapped for a fake via dependency override (see
tests/conftest.py's autouse `fake_cache` fixture) — no real yfinance calls,
fully deterministic.
"""

from datetime import date

import pytest

from app.services.market_data import ProviderError
from tests.conftest import make_market_data


def test_empty_watchlist_returns_empty_dashboard(client, fake_cache):
    response = client.get("/api/dashboard", headers={"X-Device-ID": "device-1"})

    assert response.status_code == 200
    assert response.json() == []


def test_first_time_ticker_has_no_comparison(client, fake_cache):
    headers = {"X-Device-ID": "device-1"}
    client.post("/api/watchlist", json={"ticker": "AAPL"}, headers=headers)
    fake_cache.set("AAPL", make_market_data())

    response = client.get("/api/dashboard", headers=headers)

    assert response.status_code == 200
    item = response.json()[0]
    assert item["status"] == "ok"
    assert item["comparison_available"] is False
    assert item["baseline_price"] is None
    assert item["reasons"][0].startswith("First visit")


def test_returning_ticker_compares_against_snapshot(client, fake_cache):
    headers = {"X-Device-ID": "device-1"}
    client.post("/api/watchlist", json={"ticker": "AAPL"}, headers=headers)
    fake_cache.set("AAPL", make_market_data(price=110.0))

    client.post("/api/visit/complete", headers=headers)  # baseline = 110.0, 2026-09-04

    fake_cache.set("AAPL", make_market_data(price=121.0, trading_date=date(2026, 9, 5)))
    response = client.get("/api/dashboard", headers=headers)

    item = response.json()[0]
    assert item["comparison_available"] is True
    assert item["baseline_price"] == 110.0
    assert item["change_since_baseline_pct"] == pytest.approx(10.0)


def test_get_dashboard_does_not_write_a_snapshot(client, fake_cache):
    headers = {"X-Device-ID": "device-1"}
    client.post("/api/watchlist", json={"ticker": "AAPL"}, headers=headers)
    fake_cache.set("AAPL", make_market_data(price=100.0))

    client.get("/api/dashboard", headers=headers)  # read-only
    fake_cache.set("AAPL", make_market_data(price=999.0))
    response = client.get("/api/dashboard", headers=headers)

    # No snapshot was ever written, so there's still no baseline to compare to.
    item = response.json()[0]
    assert item["comparison_available"] is False


def test_one_failing_ticker_does_not_break_the_rest(client, fake_cache):
    headers = {"X-Device-ID": "device-1"}
    client.post("/api/watchlist", json={"ticker": "AAPL"}, headers=headers)
    client.post("/api/watchlist", json={"ticker": "BADTICKER"}, headers=headers)
    fake_cache.set("AAPL", make_market_data())
    fake_cache.set_error("BADTICKER", ProviderError("provider is down"))

    response = client.get("/api/dashboard", headers=headers)

    assert response.status_code == 200
    statuses = {item["ticker"]: item["status"] for item in response.json()}
    assert statuses == {"AAPL": "ok", "BADTICKER": "unavailable"}


def test_visit_complete_is_idempotent_within_a_trading_day(client, fake_cache):
    headers = {"X-Device-ID": "device-1"}
    client.post("/api/watchlist", json={"ticker": "AAPL"}, headers=headers)
    fake_cache.set("AAPL", make_market_data(price=100.0, trading_date=date(2026, 9, 4)))

    first = client.post("/api/visit/complete", headers=headers)
    assert first.status_code == 200
    assert first.json()["updated"] == ["AAPL"]
    assert first.json()["skipped"] == []

    # Same trading day, price "moved" — should NOT move the baseline.
    fake_cache.set("AAPL", make_market_data(price=150.0, trading_date=date(2026, 9, 4)))
    second = client.post("/api/visit/complete", headers=headers)
    assert second.status_code == 200
    assert second.json()["updated"] == []
    assert second.json()["skipped"] == ["AAPL"]

    dashboard = client.get("/api/dashboard", headers=headers).json()
    assert dashboard[0]["baseline_price"] == 100.0


def test_visit_complete_skips_tickers_with_no_market_data(client, fake_cache):
    headers = {"X-Device-ID": "device-1"}
    client.post("/api/watchlist", json={"ticker": "BADTICKER"}, headers=headers)
    fake_cache.set_error("BADTICKER", ProviderError("provider is down"))

    response = client.post("/api/visit/complete", headers=headers)

    assert response.status_code == 200  # does not fail the whole request
    body = response.json()
    assert body["unavailable"] == ["BADTICKER"]
    assert body["updated"] == []
    assert body["skipped"] == []
    assert body["items"] == [{"ticker": "BADTICKER", "status": "unavailable", "baseline_price": None}]


def test_visit_complete_reports_mixed_outcomes_across_tickers(client, fake_cache):
    headers = {"X-Device-ID": "device-1"}
    client.post("/api/watchlist", json={"ticker": "AAPL"}, headers=headers)
    client.post("/api/watchlist", json={"ticker": "MSFT"}, headers=headers)
    client.post("/api/watchlist", json={"ticker": "BADTICKER"}, headers=headers)

    fake_cache.set("AAPL", make_market_data(ticker="AAPL", price=100.0, trading_date=date(2026, 9, 4)))
    fake_cache.set("MSFT", make_market_data(ticker="MSFT", price=200.0, trading_date=date(2026, 9, 4)))
    fake_cache.set_error("BADTICKER", ProviderError("provider is down"))

    first = client.post("/api/visit/complete", headers=headers)
    assert first.status_code == 200
    assert sorted(first.json()["updated"]) == ["AAPL", "MSFT"]
    assert first.json()["unavailable"] == ["BADTICKER"]

    # Same trading day, second call: AAPL/MSFT already have today's
    # baseline (skipped), BADTICKER is still unavailable.
    second = client.post("/api/visit/complete", headers=headers)
    body = second.json()

    assert second.status_code == 200
    assert sorted(body["skipped"]) == ["AAPL", "MSFT"]
    assert body["updated"] == []
    assert body["unavailable"] == ["BADTICKER"]
    assert {item["ticker"] for item in body["items"]} == {"AAPL", "MSFT", "BADTICKER"}


def test_higher_score_ranks_first(client, fake_cache):
    headers = {"X-Device-ID": "device-1"}
    client.post("/api/watchlist", json={"ticker": "QUIET"}, headers=headers)
    client.post("/api/watchlist", json={"ticker": "LOUD"}, headers=headers)
    # QUIET: no volume/range signal at all.
    fake_cache.set(
        "QUIET",
        make_market_data(ticker="QUIET", price=100.0, volume=None, avg_volume=None, high=None, low=None),
    )
    # LOUD: a big volume spike -> should score higher.
    fake_cache.set(
        "LOUD",
        make_market_data(ticker="LOUD", price=100.0, volume=5_000_000, avg_volume=1_000_000),
    )

    response = client.get("/api/dashboard", headers=headers)
    tickers_in_order = [item["ticker"] for item in response.json()]

    assert tickers_in_order == ["LOUD", "QUIET"]


def test_missing_device_id_on_dashboard_returns_400(client, fake_cache):
    response = client.get("/api/dashboard")

    assert response.status_code == 400


def test_missing_device_id_on_visit_complete_returns_400(client, fake_cache):
    response = client.post("/api/visit/complete")

    assert response.status_code == 400


# --- Meaningful-change detection (services/change_detection.py) ------------


def test_first_dashboard_read_reports_first_visit_message_only(client, fake_cache):
    headers = {"X-Device-ID": "device-1"}
    client.post("/api/watchlist", json={"ticker": "AAPL"}, headers=headers)
    fake_cache.set("AAPL", make_market_data(price=100.0))

    item = client.get("/api/dashboard", headers=headers).json()[0]

    assert item["first_visit"] is True
    assert item["percent_change"] is None
    assert item["change_count"] == 0
    assert item["signals"] == []
    assert item["change_message"] == "First visit — baseline saved for future comparison."


def test_second_read_with_a_2_percent_move_is_a_meaningful_change(client, fake_cache):
    headers = {"X-Device-ID": "device-1"}
    client.post("/api/watchlist", json={"ticker": "AAPL"}, headers=headers)
    fake_cache.set("AAPL", make_market_data(price=100.0))
    client.get("/api/dashboard", headers=headers)  # establishes the first market snapshot

    fake_cache.set("AAPL", make_market_data(price=102.8))
    item = client.get("/api/dashboard", headers=headers).json()[0]

    assert item["first_visit"] is False
    assert item["percent_change"] == pytest.approx(2.8)
    assert item["change_count"] == 1
    assert item["change_message"] == "Price increased 2.8% since your last visit."
    assert item["signals"] == ["Price increased 2.8% since your last visit."]


def test_second_read_with_a_small_move_reports_no_meaningful_change(client, fake_cache):
    headers = {"X-Device-ID": "device-1"}
    client.post("/api/watchlist", json={"ticker": "AAPL"}, headers=headers)
    fake_cache.set("AAPL", make_market_data(price=100.0))
    client.get("/api/dashboard", headers=headers)

    fake_cache.set("AAPL", make_market_data(price=100.5))
    item = client.get("/api/dashboard", headers=headers).json()[0]

    assert item["change_count"] == 0
    assert item["signals"] == []
    assert item["change_message"] == "No unusually significant movement since your last visit."


def test_a_significant_drop_is_reported_as_decreased(client, fake_cache):
    headers = {"X-Device-ID": "device-1"}
    client.post("/api/watchlist", json={"ticker": "AAPL"}, headers=headers)
    fake_cache.set("AAPL", make_market_data(price=100.0))
    client.get("/api/dashboard", headers=headers)

    fake_cache.set("AAPL", make_market_data(price=94.0))  # -6%
    item = client.get("/api/dashboard", headers=headers).json()[0]

    assert item["change_count"] == 1
    assert item["change_message"] == "Price decreased 6.0% since your last visit."


def test_meaningful_change_snapshot_is_isolated_per_device(client, fake_cache):
    fake_cache.set("AAPL", make_market_data(price=100.0))
    client.post("/api/watchlist", json={"ticker": "AAPL"}, headers={"X-Device-ID": "device-1"})
    client.post("/api/watchlist", json={"ticker": "AAPL"}, headers={"X-Device-ID": "device-2"})

    client.get("/api/dashboard", headers={"X-Device-ID": "device-1"})  # device-1 has now seen AAPL

    item = client.get("/api/dashboard", headers={"X-Device-ID": "device-2"}).json()[0]
    assert item["first_visit"] is True  # device-2 is still on its first read


# --- Reliability / error-handling phase --------------------------------------


def test_unavailable_ticker_error_message_is_sanitized(client, fake_cache):
    """A ProviderError's raw text (which may embed network/provider
    internals) must never reach the API response — only the generic,
    safe message from `describe_error`."""
    headers = {"X-Device-ID": "device-1"}
    client.post("/api/watchlist", json={"ticker": "BADTICKER"}, headers=headers)
    fake_cache.set_error(
        "BADTICKER", ProviderError("Connection refused by upstream host 10.0.0.5:443 [errno 111]")
    )

    item = client.get("/api/dashboard", headers=headers).json()[0]

    assert item["status"] == "unavailable"
    assert "10.0.0.5" not in item["error"]
    assert "errno" not in item["error"]
    assert item["error"] == "Market data is temporarily unavailable. Please try again later."


def test_unknown_ticker_error_message_is_user_friendly(client, fake_cache):
    from app.services.market_data import TickerNotFoundError

    headers = {"X-Device-ID": "device-1"}
    client.post("/api/watchlist", json={"ticker": "BADTICKER"}, headers=headers)
    fake_cache.set_error("BADTICKER", TickerNotFoundError("No market data found for ticker 'BADTICKER'"))

    item = client.get("/api/dashboard", headers=headers).json()[0]

    assert item["status"] == "unavailable"
    assert item["error"] == "We couldn't find market data for this ticker. Please check the symbol."


def test_stale_market_data_flows_through_to_dashboard_response(client, fake_cache):
    headers = {"X-Device-ID": "device-1"}
    client.post("/api/watchlist", json={"ticker": "AAPL"}, headers=headers)
    fake_cache.set_stale("AAPL", make_market_data(price=100.0), data_age_seconds=930.0)

    item = client.get("/api/dashboard", headers=headers).json()[0]

    assert item["status"] == "ok"
    assert item["stale"] is True
    assert item["stale_reason"] == "provider_error"
    assert item["data_age_seconds"] == 930.0
    # Stale data is still usable data, not an error — a score is still computed.
    assert item["attention_score"] is not None


def test_partial_market_data_does_not_crash_dashboard(client, fake_cache):
    """Missing volume, volatility, and 52-week range simultaneously — the
    dashboard must still return 200 with a safe (low) score, never a 500."""
    headers = {"X-Device-ID": "device-1"}
    client.post("/api/watchlist", json={"ticker": "AAPL"}, headers=headers)
    fake_cache.set(
        "AAPL",
        make_market_data(
            price=100.0, volatility=None, volume=None, avg_volume=None, high=None, low=None,
            partial_history=True,
        ),
    )

    response = client.get("/api/dashboard", headers=headers)
    item = response.json()[0]

    assert response.status_code == 200
    assert item["status"] == "ok"
    assert item["partial_history"] is True
    assert item["attention_score"] == 0
    assert item["score_components"] == {"volatility": 0, "volume": 0, "range": 0, "raw_move": 0}
    assert item["reasons"] is not None


def test_add_ticker_still_succeeds_when_provider_times_out(client, fake_cache):
    """A network timeout while validating a new ticker must not block the
    add — see routers/watchlist.py's fail-open behavior for ProviderError."""
    fake_cache.set_error("AAPL", ProviderError("timed out after 10s"))

    response = client.post(
        "/api/watchlist", json={"ticker": "AAPL"}, headers={"X-Device-ID": "device-1"}
    )

    assert response.status_code == 201
