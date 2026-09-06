"""Tests for the watchlist management endpoints (Phase 2)."""


def test_add_valid_ticker(client):
    response = client.post(
        "/api/watchlist", json={"ticker": "AAPL"}, headers={"X-Device-ID": "device-1"}
    )

    assert response.status_code == 201
    body = response.json()
    assert body["ticker"] == "AAPL"
    assert "added_at" in body
    # Internal DB fields must not leak into the response.
    assert "id" not in body
    assert "device_id" not in body


def test_ticker_is_normalized_to_uppercase_and_trimmed(client):
    response = client.post(
        "/api/watchlist", json={"ticker": "  aapl  "}, headers={"X-Device-ID": "device-1"}
    )

    assert response.status_code == 201
    assert response.json()["ticker"] == "AAPL"


def test_duplicate_ticker_is_rejected(client):
    headers = {"X-Device-ID": "device-1"}
    first = client.post("/api/watchlist", json={"ticker": "AAPL"}, headers=headers)
    assert first.status_code == 201

    second = client.post("/api/watchlist", json={"ticker": "aapl"}, headers=headers)

    assert second.status_code == 409
    assert "already" in second.json()["detail"].lower()


def test_same_ticker_allowed_on_separate_devices(client):
    client.post("/api/watchlist", json={"ticker": "AAPL"}, headers={"X-Device-ID": "device-1"})

    response = client.post(
        "/api/watchlist", json={"ticker": "AAPL"}, headers={"X-Device-ID": "device-2"}
    )

    assert response.status_code == 201


def test_get_returns_only_current_devices_items(client):
    client.post("/api/watchlist", json={"ticker": "AAPL"}, headers={"X-Device-ID": "device-1"})
    client.post("/api/watchlist", json={"ticker": "MSFT"}, headers={"X-Device-ID": "device-2"})

    response = client.get("/api/watchlist", headers={"X-Device-ID": "device-1"})

    assert response.status_code == 200
    tickers = [item["ticker"] for item in response.json()]
    assert tickers == ["AAPL"]


def test_get_orders_items_by_creation_time(client):
    headers = {"X-Device-ID": "device-1"}
    client.post("/api/watchlist", json={"ticker": "MSFT"}, headers=headers)
    client.post("/api/watchlist", json={"ticker": "AAPL"}, headers=headers)

    response = client.get("/api/watchlist", headers=headers)

    tickers = [item["ticker"] for item in response.json()]
    assert tickers == ["MSFT", "AAPL"]


def test_get_empty_watchlist_returns_empty_list(client):
    response = client.get("/api/watchlist", headers={"X-Device-ID": "device-1"})

    assert response.status_code == 200
    assert response.json() == []


def test_delete_removes_the_correct_ticker(client):
    headers = {"X-Device-ID": "device-1"}
    client.post("/api/watchlist", json={"ticker": "AAPL"}, headers=headers)
    client.post("/api/watchlist", json={"ticker": "MSFT"}, headers=headers)

    response = client.delete("/api/watchlist/AAPL", headers=headers)

    assert response.status_code == 204
    remaining = [item["ticker"] for item in client.get("/api/watchlist", headers=headers).json()]
    assert remaining == ["MSFT"]


def test_delete_normalizes_ticker_case(client):
    headers = {"X-Device-ID": "device-1"}
    client.post("/api/watchlist", json={"ticker": "AAPL"}, headers=headers)

    response = client.delete("/api/watchlist/aapl", headers=headers)

    assert response.status_code == 204


def test_delete_does_not_affect_another_devices_ticker(client):
    client.post("/api/watchlist", json={"ticker": "AAPL"}, headers={"X-Device-ID": "device-1"})
    client.post("/api/watchlist", json={"ticker": "AAPL"}, headers={"X-Device-ID": "device-2"})

    response = client.delete("/api/watchlist/AAPL", headers={"X-Device-ID": "device-1"})

    assert response.status_code == 204
    other_device_items = client.get(
        "/api/watchlist", headers={"X-Device-ID": "device-2"}
    ).json()
    assert [item["ticker"] for item in other_device_items] == ["AAPL"]


def test_delete_nonexistent_ticker_returns_404(client):
    response = client.delete("/api/watchlist/AAPL", headers={"X-Device-ID": "device-1"})

    assert response.status_code == 404


def test_missing_device_id_header_returns_400(client):
    response = client.get("/api/watchlist")

    assert response.status_code == 400


def test_blank_device_id_header_returns_400(client):
    response = client.get("/api/watchlist", headers={"X-Device-ID": "   "})

    assert response.status_code == 400


def test_missing_device_id_on_post_returns_400(client):
    response = client.post("/api/watchlist", json={"ticker": "AAPL"})

    assert response.status_code == 400


def test_empty_ticker_returns_422(client):
    response = client.post(
        "/api/watchlist", json={"ticker": "   "}, headers={"X-Device-ID": "device-1"}
    )

    assert response.status_code == 422


# --- Reliability / error-handling phase --------------------------------------


def test_malformed_ticker_with_spaces_returns_422(client):
    response = client.post(
        "/api/watchlist", json={"ticker": "AB CD"}, headers={"X-Device-ID": "device-1"}
    )

    assert response.status_code == 422


def test_malformed_ticker_with_symbols_returns_422(client):
    response = client.post(
        "/api/watchlist", json={"ticker": "AB$%CD"}, headers={"X-Device-ID": "device-1"}
    )

    assert response.status_code == 422


def test_overly_long_ticker_returns_422(client):
    response = client.post(
        "/api/watchlist", json={"ticker": "A" * 11}, headers={"X-Device-ID": "device-1"}
    )

    assert response.status_code == 422


def test_ticker_with_dot_and_hyphen_is_accepted(client, fake_cache):
    # Real-world formats like BRK.B / BF-B must still pass the format check.
    response = client.post(
        "/api/watchlist", json={"ticker": "BRK.B"}, headers={"X-Device-ID": "device-1"}
    )

    assert response.status_code == 201
    assert response.json()["ticker"] == "BRK.B"


def test_unknown_ticker_is_rejected_with_422(client, fake_cache):
    from app.services.market_data import TickerNotFoundError

    fake_cache.set_error("ZZZINVALID", TickerNotFoundError("No market data found for ticker 'ZZZINVALID'"))

    response = client.post(
        "/api/watchlist", json={"ticker": "ZZZINVALID"}, headers={"X-Device-ID": "device-1"}
    )

    assert response.status_code == 422
    # A friendly, generic message — never a raw exception class name or trace.
    assert "TickerNotFoundError" not in response.json()["detail"]
    assert "check the symbol" in response.json()["detail"].lower()


def test_unknown_ticker_is_not_added_to_watchlist(client, fake_cache):
    from app.services.market_data import TickerNotFoundError

    fake_cache.set_error("ZZZINVALID", TickerNotFoundError("no data"))
    client.post("/api/watchlist", json={"ticker": "ZZZINVALID"}, headers={"X-Device-ID": "device-1"})

    response = client.get("/api/watchlist", headers={"X-Device-ID": "device-1"})

    assert response.json() == []


def test_add_ticker_fails_open_when_provider_is_down(client, fake_cache):
    """A transient provider outage says nothing about whether the ticker is
    valid — adding (pure local watchlist state) must not be blocked by it."""
    from app.services.market_data import ProviderError

    fake_cache.set_error("AAPL", ProviderError("connection reset by peer"))

    response = client.post(
        "/api/watchlist", json={"ticker": "AAPL"}, headers={"X-Device-ID": "device-1"}
    )

    assert response.status_code == 201
