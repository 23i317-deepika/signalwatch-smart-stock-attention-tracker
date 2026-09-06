"""
Environment-driven configuration for SignalWatch.

Everything here is a plain, named setting with a sane default so the app runs
out of the box for local/hackathon use, but can be overridden via environment
variables (or a .env file) without touching code.
"""

from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_DIR = Path(__file__).resolve().parent.parent


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    # --- Database -----------------------------------------------------
    # Path to the SQLite file. Only persists user state (watchlist items +
    # visit snapshots) — see PROJECT_PLAN.md.
    database_path: Path = BACKEND_DIR / "signalwatch.db"

    # --- Market data cache ----------------------------------------------
    # TTL (seconds) for the in-memory market data cache, keyed by ticker.
    # Not persisted — see PROJECT_PLAN.md §2/§9.
    market_data_cache_ttl_seconds: int = 600  # 10 minutes

    # --- CORS -------------------------------------------------------------
    # Origins allowed to call the API from the browser. The frontend is
    # plain static HTML/JS served separately during local development.
    cors_origins: list[str] = [
        "http://localhost:5500",
        "http://127.0.0.1:5500",
        "http://localhost:8000",
        "http://127.0.0.1:8000",
    ]

    # --- Meaningful-change detection -------------------------------------
    # Minimum |percent change| since a device's last dashboard read for a
    # ticker to count as a "meaningful change" (services/change_detection.py).
    # A simple, configurable threshold for now — see PROJECT_PLAN.md §6a.
    meaningful_change_threshold_pct: float = 2.0

    # --- Demo mode ----------------------------------------------------
    # When true, market data comes from a small synthetic, deterministic
    # fixture set (app/services/demo_data.py) instead of yfinance — immune
    # to live network/rate-limit flakiness, and includes tickers that
    # deliberately demonstrate the stale-fallback and provider-failure
    # stories on command. See README.md's demo script. Off by default: this
    # never silently substitutes fake data for real data.
    demo_mode: bool = False

    # TTL used instead of market_data_cache_ttl_seconds while demo_mode is
    # on — short on purpose, so a live demo can show data going stale
    # within a comfortable pause rather than a real 10-minute wait.
    demo_cache_ttl_seconds: int = 15


settings = Settings()
