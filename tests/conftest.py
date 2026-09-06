"""
Shared test fixtures.

Points the app at an isolated SQLite file (backend/tests/test_signalwatch.db)
*before* app.config/app.database are imported anywhere in the test session,
so running the test suite never reads or writes the real backend/signalwatch.db.
"""

import os
from datetime import date, datetime, timezone
from pathlib import Path

_TEST_DB_PATH = Path(__file__).resolve().parent / "test_signalwatch.db"
os.environ["DATABASE_PATH"] = str(_TEST_DB_PATH)

import pytest  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.database import Base, engine  # noqa: E402
from app.main import app  # noqa: E402
from app.services.market_data import MarketData, MarketDataResult, get_market_data_cache  # noqa: E402


@pytest.fixture(autouse=True)
def _reset_database():
    """Fresh, empty tables before every test — deterministic, isolated state."""
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    yield


@pytest.fixture()
def client():
    with TestClient(app) as test_client:
        yield test_client


def make_market_data(
    ticker="AAPL",
    price=100.0,
    prev_close=99.0,
    volatility=1.5,
    volume=1_000_000,
    avg_volume=1_000_000,
    high=120.0,
    low=80.0,
    trading_date=date(2026, 9, 4),
    partial_history=False,
) -> MarketData:
    """A plausible, boring default fixture — for tests that only need a
    ticker to resolve successfully and don't care about the specific
    numbers driving its Attention Score."""
    return MarketData(
        ticker=ticker,
        current_price=price,
        prev_close=prev_close,
        latest_volume=volume,
        avg_volume_20d=avg_volume,
        volatility_20d_pct=volatility,
        week52_high=high,
        week52_low=low,
        trading_date=trading_date,
        fetched_at=datetime.now(timezone.utc),
        partial_history=partial_history,
    )


class FakeMarketDataCache:
    """Stands in for MarketDataCache — no TTL logic, just canned responses.

    A ticker not explicitly configured via `.set()`/`.set_error()`/
    `.set_stale()` still resolves successfully with a plausible default
    (see `make_market_data`) — most tests (watchlist CRUD, etc.) don't care
    about market data content at all, only that adding/reading a ticker
    never needs a real yfinance call. Tests that DO care about specific
    values, staleness, or failures configure them explicitly.
    """

    def __init__(self):
        self._data: dict[str, MarketData] = {}
        self._errors: dict[str, Exception] = {}
        self._stale: dict[str, float] = {}  # ticker -> data_age_seconds

    def set(self, ticker: str, data: MarketData) -> None:
        self._data[ticker.upper()] = data

    def set_error(self, ticker: str, exc: Exception) -> None:
        self._errors[ticker.upper()] = exc

    def set_stale(self, ticker: str, data: MarketData, *, data_age_seconds: float = 900.0) -> None:
        """Configure `ticker` to resolve as if a live fetch failed but a
        cached copy was served (see MarketDataCache's real stale-fallback)."""
        self._data[ticker.upper()] = data
        self._stale[ticker.upper()] = data_age_seconds

    def get(self, ticker: str) -> MarketDataResult:
        ticker = ticker.upper()
        if ticker in self._errors:
            raise self._errors[ticker]

        data = self._data.get(ticker) or make_market_data(ticker=ticker)
        if ticker in self._stale:
            return MarketDataResult(
                data=data, stale=True, stale_reason="provider_error", data_age_seconds=self._stale[ticker]
            )
        return MarketDataResult(data=data, stale=False, data_age_seconds=5.0)


@pytest.fixture(autouse=True)
def fake_cache():
    """Installed for every test — nothing in this suite makes a real
    network call. Tests that need specific market data, staleness, or
    failure behavior call `.set()`/`.set_stale()`/`.set_error()` on the
    returned object; everything else gets a harmless default automatically."""
    cache = FakeMarketDataCache()
    app.dependency_overrides[get_market_data_cache] = lambda: cache
    yield cache
    app.dependency_overrides.pop(get_market_data_cache, None)
