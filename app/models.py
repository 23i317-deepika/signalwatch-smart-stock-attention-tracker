"""
ORM models for SignalWatch's persisted user state.

Only two tables, per PROJECT_PLAN.md §4:
- WatchlistItem: which tickers a device is watching.
- VisitSnapshot: the price baseline from a device's last *completed* visit
  to a ticker (written only by POST /api/visit/complete — see §6 of the
  plan; never as a side effect of a GET).

Market data itself is not modeled here — it lives in an in-memory cache
(see PROJECT_PLAN.md §2/§9), not in SQLite.
"""

from datetime import date, datetime, timezone

from sqlalchemy import Date, DateTime, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class WatchlistItem(Base):
    """One ticker on one device's watchlist."""

    __tablename__ = "watchlist_items"
    __table_args__ = (
        UniqueConstraint("device_id", "ticker", name="uq_watchlist_device_ticker"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    device_id: Mapped[str] = mapped_column(String, index=True, nullable=False)
    ticker: Mapped[str] = mapped_column(String, nullable=False)
    added_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )


class VisitSnapshot(Base):
    """The baseline price captured at a device's last completed visit to a ticker."""

    __tablename__ = "visit_snapshots"
    __table_args__ = (
        UniqueConstraint("device_id", "ticker", name="uq_snapshot_device_ticker"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    device_id: Mapped[str] = mapped_column(String, index=True, nullable=False)
    ticker: Mapped[str] = mapped_column(String, nullable=False)
    snapshot_price: Mapped[float] = mapped_column(nullable=False)
    # The *exchange* trading date this snapshot represents (not server/client
    # wall-clock date) — see PROJECT_PLAN.md §6/§8 edge case 13.
    snapshot_trading_date: Mapped[date] = mapped_column(Date, nullable=False)
    captured_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )


class MarketSnapshot(Base):
    """The most recently seen market data for a device's view of a ticker.

    Distinct from VisitSnapshot above: VisitSnapshot only advances on an
    explicit `POST /api/visit/complete` (at most once per trading day) and
    backs the Attention Score baseline. MarketSnapshot backs the separate
    "meaningful change" feature (see services/change_detection.py) — simple
    rule-based signals (>=2% move, direction reversal) computed by comparing
    each dashboard read's market data against the *previous read's*, so it
    is deliberately updated on every `GET /api/dashboard` call, not just on
    visit-complete.
    """

    __tablename__ = "market_snapshots"
    __table_args__ = (
        UniqueConstraint("device_id", "ticker", name="uq_market_snapshot_device_ticker"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    device_id: Mapped[str] = mapped_column(String, index=True, nullable=False)
    ticker: Mapped[str] = mapped_column(String, nullable=False)
    price: Mapped[float] = mapped_column(nullable=False)
    # The percent change that produced this snapshot (vs. the snapshot
    # before it) — None for a ticker's very first snapshot, when there was
    # nothing to compare against yet. Kept so the *next* read can detect a
    # direction reversal without an extra history table.
    percent_change: Mapped[float | None] = mapped_column(nullable=True)
    captured_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=_utcnow, nullable=False
    )
