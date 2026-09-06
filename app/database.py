"""
SQLite database setup via SQLAlchemy.

Holds only durable user state (watchlist items + visit snapshots) per
PROJECT_PLAN.md — market data is cached in-memory elsewhere and never
persisted here.
"""

from collections.abc import Generator

from sqlalchemy import create_engine, event
from sqlalchemy.engine import Engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import settings

engine = create_engine(
    f"sqlite:///{settings.database_path}",
    connect_args={"check_same_thread": False},
)


@event.listens_for(Engine, "connect")
def _enable_wal_mode(dbapi_connection, connection_record) -> None:
    """Enable WAL mode + foreign keys on every new SQLite connection.

    WAL mode allows concurrent readers alongside a writer, which is more
    than sufficient for this app's write volume (see PROJECT_PLAN.md
    edge case: SQLite write contention).
    """
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.close()


SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)


class Base(DeclarativeBase):
    pass


def init_db() -> None:
    """Create all tables that don't exist yet. Called on app startup."""
    # Import models here so they're registered on Base.metadata before
    # create_all runs, without forcing every importer of database.py to
    # also import models.py.
    from app import models  # noqa: F401

    Base.metadata.create_all(bind=engine)


def get_db() -> Generator[Session, None, None]:
    """FastAPI dependency yielding a request-scoped DB session."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
