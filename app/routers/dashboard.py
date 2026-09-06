"""
Dashboard endpoints — the "smart" view that ties the watchlist, market data,
and Attention Engine together.

GET /api/dashboard is a pure read with respect to the Attention Score: it
scores against the *existing* visit snapshot and never writes one.
POST /api/visit/complete is the only thing that moves that baseline
forward, and only when the frontend calls it after a successful render.
See PROJECT_PLAN.md §6 for why these are split.

The separate meaningful-change detection (services/change_detection.py) is
the one deliberate exception: it compares against, and then advances, its
own MarketSnapshot row on every dashboard read — see that module's
docstring for why this signal is allowed to update on a GET while the
Attention Score baseline is not.
"""

import logging

from fastapi import APIRouter, Depends
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.database import get_db
from app.deps import get_device_id
from app.models import WatchlistItem
from app.schemas import DashboardItem, MarketContext, VisitCompleteItem, VisitCompleteResponse
from app.services import change_detection, snapshot_service
from app.services.attention import AttentionInput, compute_attention_score
from app.services.market_data import MarketDataCache, MarketDataError, describe_error, get_market_data_cache

router = APIRouter(prefix="/api", tags=["dashboard"])

# TEMPORARY diagnostic logger — see main.py's logging.basicConfig(). Remove
# once market-data issues are no longer being actively diagnosed.
logger = logging.getLogger(__name__)


@router.get("/dashboard", response_model=list[DashboardItem])
def get_dashboard(
    device_id: str = Depends(get_device_id),
    db: Session = Depends(get_db),
    cache: MarketDataCache = Depends(get_market_data_cache),
) -> list[DashboardItem]:
    watchlist_items = (
        db.execute(
            select(WatchlistItem)
            .where(WatchlistItem.device_id == device_id)
            .order_by(WatchlistItem.added_at)
        )
        .scalars()
        .all()
    )

    items = [_build_dashboard_item(db, cache, device_id, w.ticker) for w in watchlist_items]
    return _rank(items)


@router.post("/visit/complete", response_model=VisitCompleteResponse)
def complete_visit(
    device_id: str = Depends(get_device_id),
    db: Session = Depends(get_db),
    cache: MarketDataCache = Depends(get_market_data_cache),
) -> VisitCompleteResponse:
    watchlist_items = (
        db.execute(select(WatchlistItem).where(WatchlistItem.device_id == device_id))
        .scalars()
        .all()
    )

    items: list[VisitCompleteItem] = []
    for watchlist_item in watchlist_items:
        ticker = watchlist_item.ticker
        try:
            result = cache.get(ticker)
        except MarketDataError:
            # No usable price for this ticker right now — can't establish a
            # baseline. Skip it; the user's existing baseline (if any) is
            # left untouched, and this is safe to retry later.
            items.append(VisitCompleteItem(ticker=ticker, status="unavailable"))
            continue

        wrote = snapshot_service.complete_visit(
            db,
            device_id,
            ticker,
            result.data.current_price,
            result.data.trading_date,
        )
        items.append(
            VisitCompleteItem(
                ticker=ticker,
                status="updated" if wrote else "skipped",
                baseline_price=result.data.current_price,
            )
        )

    return VisitCompleteResponse(
        updated=[i.ticker for i in items if i.status == "updated"],
        skipped=[i.ticker for i in items if i.status == "skipped"],
        unavailable=[i.ticker for i in items if i.status == "unavailable"],
        items=items,
    )


def _build_dashboard_item(db: Session, cache: MarketDataCache, device_id: str, ticker: str) -> DashboardItem:
    try:
        result = cache.get(ticker)
    except MarketDataError as exc:
        # This is the exact boundary that turns a market-data failure into
        # the "Data unavailable" card the frontend shows — logged here (with
        # the real exception) so a bad ticker is distinguishable from a
        # genuine provider outage without digging through market_data.py.
        # Only the sanitized, user-facing message crosses the API boundary —
        # `exc` may embed raw provider/network detail that has no business
        # reaching a client.
        logger.warning("Dashboard: '%s' unavailable (%s)", ticker, exc)
        return DashboardItem(ticker=ticker, status="unavailable", error=describe_error(exc))

    data = result.data
    snapshot = snapshot_service.get_snapshot(db, device_id, ticker)
    change_result = change_detection.detect_changes(db, device_id, ticker, data.current_price)

    score_result = compute_attention_score(
        AttentionInput(
            current_price=data.current_price,
            previous_visit_price=snapshot.snapshot_price if snapshot else None,
            historical_volatility_pct=data.volatility_20d_pct,
            latest_volume=data.latest_volume,
            avg_volume_20d=data.avg_volume_20d,
            week52_high=data.week52_high,
            week52_low=data.week52_low,
        )
    )

    return DashboardItem(
        ticker=ticker,
        status="ok",
        price=data.current_price,
        prev_close=data.prev_close,
        as_of=data.fetched_at,
        stale=result.stale,
        stale_reason=result.stale_reason,
        data_age_seconds=result.data_age_seconds,
        partial_history=data.partial_history,
        comparison_available=score_result.comparison_available,
        baseline_price=snapshot.snapshot_price if snapshot else None,
        baseline_captured_at=snapshot.captured_at if snapshot else None,
        change_since_baseline_pct=score_result.price_change_pct,
        market_context=MarketContext(change_vs_prev_close_pct=_change_vs_prev_close(data)),
        attention_score=score_result.score,
        score_components=score_result.components,
        reasons=score_result.reasons,
        percent_change=change_result.percent_change,
        change_count=change_result.change_count,
        signals=change_result.signals,
        first_visit=change_result.first_visit,
        change_message=change_result.message,
    )


def _change_vs_prev_close(data) -> float | None:
    if not data.prev_close:
        return None
    return (data.current_price - data.prev_close) / data.prev_close * 100


def _rank(items: list[DashboardItem]) -> list[DashboardItem]:
    """Highest attention score first; ties broken by larger |change|.

    Items with no usable market data (`status == "unavailable"`) have no
    score to rank by — they're kept, just moved to the end, in their
    original watchlist order.
    """
    ok_items = [item for item in items if item.status == "ok"]
    unavailable_items = [item for item in items if item.status != "ok"]

    ok_items.sort(
        key=lambda item: (
            -(item.attention_score or 0),
            -abs(item.change_since_baseline_pct) if item.change_since_baseline_pct is not None else 0.0,
        )
    )
    return ok_items + unavailable_items
