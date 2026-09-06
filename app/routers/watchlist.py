"""
Watchlist management endpoints.

Pure CRUD over `watchlist_items` — no scoring (the scored dashboard lives
in a separate router). It does, however, verify a ticker actually resolves
to real market data before adding it — see `add_watchlist_item` below.
"""

import logging

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.database import get_db
from app.deps import get_device_id
from app.models import WatchlistItem
from app.schemas import WatchlistItemCreate, WatchlistItemResponse
from app.services.market_data import (
    MarketDataCache,
    ProviderError,
    TickerNotFoundError,
    describe_error,
    get_market_data_cache,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/watchlist", tags=["watchlist"])


@router.post(
    "",
    response_model=WatchlistItemResponse,
    status_code=status.HTTP_201_CREATED,
)
def add_watchlist_item(
    payload: WatchlistItemCreate,
    device_id: str = Depends(get_device_id),
    db: Session = Depends(get_db),
    cache: MarketDataCache = Depends(get_market_data_cache),
) -> WatchlistItem:
    ticker = payload.ticker  # already trimmed/uppercased/format-checked by the schema

    existing = db.execute(
        select(WatchlistItem).where(
            WatchlistItem.device_id == device_id,
            WatchlistItem.ticker == ticker,
        )
    ).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"{ticker} is already on your watchlist",
        )

    # Verify the ticker actually resolves to real market data — via the
    # existing market-data cache, not a regex, since a regex can't tell you
    # a company exists. Goes through the same shared TTL cache as the
    # dashboard, so this is often a cache hit costing nothing extra.
    try:
        cache.get(ticker)
    except TickerNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=describe_error(exc))
    except ProviderError as exc:
        # The provider itself is having trouble — that says nothing about
        # whether the ticker is valid. Fail open rather than blocking
        # watchlist management (pure local state) over a transient outage;
        # a genuinely invalid ticker will still surface as "unavailable" on
        # the dashboard.
        logger.warning("Ticker-existence check skipped for '%s' — provider error: %s", ticker, exc)

    item = WatchlistItem(device_id=device_id, ticker=ticker)
    db.add(item)
    try:
        db.commit()
    except IntegrityError:
        # Two concurrent requests for the same (device_id, ticker) raced past
        # the check above; the DB's unique constraint is the real guard —
        # turn that race into the same clear 409 rather than a 500.
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"{ticker} is already on your watchlist",
        )

    db.refresh(item)
    return item


@router.get("", response_model=list[WatchlistItemResponse])
def list_watchlist_items(
    device_id: str = Depends(get_device_id),
    db: Session = Depends(get_db),
) -> list[WatchlistItem]:
    items = (
        db.execute(
            select(WatchlistItem)
            .where(WatchlistItem.device_id == device_id)
            .order_by(WatchlistItem.added_at)
        )
        .scalars()
        .all()
    )
    return list(items)


@router.delete("/{ticker}", status_code=status.HTTP_204_NO_CONTENT)
def delete_watchlist_item(
    ticker: str,
    device_id: str = Depends(get_device_id),
    db: Session = Depends(get_db),
) -> None:
    normalized = ticker.strip().upper()

    item = db.execute(
        select(WatchlistItem).where(
            WatchlistItem.device_id == device_id,
            WatchlistItem.ticker == normalized,
        )
    ).scalar_one_or_none()
    if item is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"{normalized} is not on your watchlist",
        )

    db.delete(item)
    db.commit()
