"""
Visit-snapshot persistence.

Reads and writes the "last completed visit" baseline per (device_id,
ticker). Snapshots are written ONLY by the explicit visit-complete flow —
never as a side effect of reading the dashboard. See PROJECT_PLAN.md §6:
reading data should not unexpectedly mutate the user's comparison baseline.
"""

from datetime import date, datetime, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import VisitSnapshot


def get_snapshot(db: Session, device_id: str, ticker: str) -> VisitSnapshot | None:
    """Return the current baseline for (device_id, ticker), or None if there isn't one yet."""
    return db.execute(
        select(VisitSnapshot).where(
            VisitSnapshot.device_id == device_id,
            VisitSnapshot.ticker == ticker,
        )
    ).scalar_one_or_none()


def complete_visit(db: Session, device_id: str, ticker: str, price: float, trading_date: date) -> bool:
    """Upsert the visit baseline for (device_id, ticker).

    Idempotent within a trading day: if a snapshot already exists for this
    exact `trading_date`, nothing is written — safe to call repeatedly (e.g.
    extra page loads, a retried request) without moving the baseline more
    than once per trading day.

    Returns True if a write actually happened (new baseline established, or
    moved forward to a new trading day), False if it was a no-op.
    """
    existing = get_snapshot(db, device_id, ticker)

    if existing is not None and existing.snapshot_trading_date == trading_date:
        return False

    if existing is not None:
        existing.snapshot_price = price
        existing.snapshot_trading_date = trading_date
        existing.captured_at = datetime.now(timezone.utc)
    else:
        db.add(
            VisitSnapshot(
                device_id=device_id,
                ticker=ticker,
                snapshot_price=price,
                snapshot_trading_date=trading_date,
            )
        )

    try:
        db.commit()
    except IntegrityError:
        # Two concurrent "complete visit" calls raced past the check above on
        # the very first snapshot for this (device_id, ticker) — the unique
        # constraint is the real guard. Treat it the same as "already up to
        # date": a safe, idempotent no-op rather than a 500.
        db.rollback()
        return False

    return True
