"""
Meaningful-change detection.

A separate, deliberately simple signal from the Attention Score
(services/attention.py, unaffected by this module — still computed and
still drives ranking, just no longer shown as its own text in the UI to
avoid two different "first visit"/"quiet" messages appearing on one card):
a single configurable percent-change threshold, computed by comparing the
current dashboard read's price against the previous read's, stored in
`MarketSnapshot` (see models.py). Every `GET /api/dashboard` call both
compares against and then advances this snapshot; that's intentional here
(unlike the Attention Score's `VisitSnapshot` baseline, which only moves on
an explicit `POST /api/visit/complete`) — see MarketSnapshot's docstring
for why the two are kept separate.

Every ticker is in exactly one of three states, each with exactly one
message (`ChangeDetectionResult.message`) — never combined:
  - First visit: no previous snapshot. The current price is stored as the
    baseline; no change is ever reported.
  - No meaningful change: a previous snapshot exists, but the move since it
    is below the threshold.
  - Meaningful change: a previous snapshot exists and the move since it is
    at or beyond the threshold.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.config import settings
from app.models import MarketSnapshot

# A move at or beyond this magnitude (in percent) since the previous
# snapshot counts as "meaningful" on its own. Configurable via the
# MEANINGFUL_CHANGE_THRESHOLD_PCT env var (see app/config.py) — no code
# change needed to tune it.
SIGNIFICANT_MOVE_PCT = settings.meaningful_change_threshold_pct

FIRST_VISIT_MESSAGE = "First visit — baseline saved for future comparison."
NO_CHANGE_MESSAGE = "No unusually significant movement since your last visit."


@dataclass(frozen=True)
class ChangeDetectionResult:
    """What changed for one (device_id, ticker) since the last dashboard read.

    `message` is the single, authoritative status string for this ticker —
    the frontend renders exactly this and nothing else, so the three states
    above can never show conflicting text on the same card.
    """

    percent_change: float | None  # vs. the previous snapshot's price; None on first visit
    first_visit: bool  # True if there was no previous snapshot to compare against
    message: str
    signals: list[str] = field(default_factory=list)  # 0 or 1 entries: the meaningful-change message, if any

    @property
    def change_count(self) -> int:
        return len(self.signals)


def get_last_snapshot(db: Session, device_id: str, ticker: str) -> MarketSnapshot | None:
    """Return the most recently recorded market snapshot for (device_id, ticker), or None."""
    return db.execute(
        select(MarketSnapshot).where(
            MarketSnapshot.device_id == device_id,
            MarketSnapshot.ticker == ticker,
        )
    ).scalar_one_or_none()


def detect_changes(db: Session, device_id: str, ticker: str, current_price: float) -> ChangeDetectionResult:
    """Compare `current_price` against the last stored snapshot, then record the new one.

    First call for a (device, ticker): nothing to compare against yet — just
    stores the baseline snapshot and reports `first_visit=True` with the
    first-visit message; the change count stays 0 (never generate a
    meaningful change on the first visit).

    Every call after that computes `percent_change` vs. the previous
    snapshot's price and reports either the "no meaningful change" message
    or, if `abs(percent_change) >= SIGNIFICANT_MOVE_PCT`, a message naming
    the direction and size of the move.
    """
    previous = get_last_snapshot(db, device_id, ticker)

    if previous is None:
        db.add(MarketSnapshot(device_id=device_id, ticker=ticker, price=current_price, percent_change=None))
        try:
            db.commit()
        except IntegrityError:
            # Two concurrent dashboard reads for the same (device_id,
            # ticker) both found no snapshot and raced to create the
            # baseline — the unique constraint is the real guard. Whichever
            # request loses the race gets the same harmless outcome
            # (nothing to compare against yet) rather than a 500.
            db.rollback()
        return ChangeDetectionResult(percent_change=None, first_visit=True, message=FIRST_VISIT_MESSAGE, signals=[])

    percent_change = _percent_change(current_price, previous.price)

    previous.price = current_price
    previous.percent_change = percent_change
    previous.captured_at = datetime.now(timezone.utc)
    db.commit()

    if percent_change is not None and abs(percent_change) >= SIGNIFICANT_MOVE_PCT:
        message = _meaningful_change_message(percent_change)
        return ChangeDetectionResult(percent_change=percent_change, first_visit=False, message=message, signals=[message])

    return ChangeDetectionResult(percent_change=percent_change, first_visit=False, message=NO_CHANGE_MESSAGE, signals=[])


def _percent_change(current_price: float, previous_price: float) -> float | None:
    if not previous_price:  # guards divide-by-zero on a (theoretical) zero-priced snapshot
        return None
    return (current_price - previous_price) / previous_price * 100


def _meaningful_change_message(percent_change: float) -> str:
    direction = "increased" if percent_change > 0 else "decreased"
    return f"Price {direction} {abs(percent_change):.1f}% since your last visit."
