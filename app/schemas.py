"""
Pydantic request/response schemas.

Response schemas intentionally expose only what a client needs — no raw
database ids or device ids leak out (the caller already knows its own
device id; it sent it).
"""

import re
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

# A permissive syntax check only — letters/digits/dot/hyphen (covers things
# like "BRK.B", "BF-B"), capped at a sane length. This catches obviously
# malformed input (spaces, punctuation, absurd length) cheaply, before ever
# touching the market data provider. It does NOT attempt to decide whether
# a ticker is a *real* one — that's what the market-data existence check in
# routers/watchlist.py is for (see its docstring). Regex alone can't tell
# you a company exists; it can only reject obvious garbage.
_TICKER_FORMAT = re.compile(r"^[A-Z0-9.\-]{1,10}$")


class WatchlistItemCreate(BaseModel):
    """Request body for POST /api/watchlist."""

    ticker: str

    @field_validator("ticker")
    @classmethod
    def normalize_ticker(cls, value: str) -> str:
        normalized = value.strip().upper()
        if not normalized:
            raise ValueError("ticker must not be empty")
        if not _TICKER_FORMAT.match(normalized):
            raise ValueError(
                "ticker must contain only letters, numbers, '.' or '-' (max 10 characters)"
            )
        return normalized


class WatchlistItemResponse(BaseModel):
    """A single watchlist item as returned to the client."""

    model_config = ConfigDict(from_attributes=True)

    ticker: str
    added_at: datetime


class MarketContext(BaseModel):
    """Display-only market context — never fed into the Attention Score.

    Distinguishes "what the market did today" from "what changed since your
    last visit" (see PROJECT_PLAN.md §7/§8) — most relevant on a first-time
    view, where there is no last-visit baseline to compare against.
    """

    change_vs_prev_close_pct: float | None = None


class DashboardItem(BaseModel):
    """One watchlist ticker, enriched with market data, score, and explanation.

    `status == "unavailable"` means market data couldn't be obtained for this
    ticker at all (invalid symbol, or a provider outage with nothing cached
    yet) — every other field is then null. One bad ticker never fails the
    rest of the dashboard.
    """

    ticker: str
    status: Literal["ok", "unavailable"]
    error: str | None = None

    price: float | None = None
    prev_close: float | None = None
    as_of: datetime | None = None
    stale: bool = False
    stale_reason: str | None = None
    data_age_seconds: float | None = None
    partial_history: bool | None = None

    comparison_available: bool = False
    baseline_price: float | None = None
    baseline_captured_at: datetime | None = None
    change_since_baseline_pct: float | None = None

    market_context: MarketContext | None = None

    attention_score: int | None = None
    score_components: dict[str, int] | None = None
    reasons: list[str] | None = None

    # --- Meaningful-change detection (services/change_detection.py) --------
    # A simpler, separate signal from the Attention Score above: a single
    # configurable percent-change threshold against the *previous dashboard
    # read*, rather than the Attention Score's once-per-trading-day
    # baseline. `change_message` is the single authoritative status string
    # for this ticker (first-visit / no-change / meaningful-change) — it is
    # the only change-status text the frontend renders, so a card never
    # shows two conflicting messages at once. `percent_change` is None on a
    # ticker's first-ever read for this device (`first_visit=True`), which
    # never counts as a meaningful change (`change_count` stays 0).
    percent_change: float | None = None
    change_count: int = 0
    signals: list[str] = Field(default_factory=list)
    first_visit: bool = False
    change_message: str = ""


class VisitCompleteItem(BaseModel):
    """Per-ticker outcome of POST /api/visit/complete.

    - "updated": the baseline moved forward to today's price (first snapshot
      for this ticker, or the first completed visit on a new trading day).
    - "skipped": a snapshot for the current trading day already existed —
      idempotent no-op, per PROJECT_PLAN.md §6. `baseline_price` reflects the
      (unchanged) existing baseline.
    - "unavailable": no usable market data could be fetched for this ticker
      right now, so no baseline could be written; the previous baseline (if
      any) is left untouched.
    """

    ticker: str
    status: Literal["updated", "skipped", "unavailable"]
    baseline_price: float | None = None


class VisitCompleteResponse(BaseModel):
    """Response body for POST /api/visit/complete.

    `updated`/`skipped`/`unavailable` are convenience ticker lists for a
    quick glance; `items` carries the same information per-ticker with the
    resulting baseline price, for callers that want more detail.
    """

    updated: list[str]
    skipped: list[str]
    unavailable: list[str]
    items: list[VisitCompleteItem]
