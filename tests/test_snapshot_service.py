"""
Unit tests for visit-snapshot persistence
(backend/app/services/snapshot_service.py). Direct DB session use — no HTTP.
"""

from datetime import date

from app.database import SessionLocal
from app.services import snapshot_service


def test_first_complete_visit_creates_a_snapshot():
    db = SessionLocal()
    try:
        created = snapshot_service.complete_visit(db, "device-1", "AAPL", 100.0, date(2026, 9, 1))
        snapshot = snapshot_service.get_snapshot(db, "device-1", "AAPL")

        assert created is True
        assert snapshot is not None
        assert snapshot.snapshot_price == 100.0
        assert snapshot.snapshot_trading_date == date(2026, 9, 1)
    finally:
        db.close()


def test_repeat_visit_same_trading_day_is_a_noop():
    db = SessionLocal()
    try:
        snapshot_service.complete_visit(db, "device-1", "AAPL", 100.0, date(2026, 9, 1))
        changed = snapshot_service.complete_visit(db, "device-1", "AAPL", 105.0, date(2026, 9, 1))
        snapshot = snapshot_service.get_snapshot(db, "device-1", "AAPL")

        assert changed is False
        assert snapshot.snapshot_price == 100.0  # unchanged — same trading day
    finally:
        db.close()


def test_visit_on_a_new_trading_day_moves_the_baseline():
    db = SessionLocal()
    try:
        snapshot_service.complete_visit(db, "device-1", "AAPL", 100.0, date(2026, 9, 1))
        changed = snapshot_service.complete_visit(db, "device-1", "AAPL", 105.0, date(2026, 9, 2))
        snapshot = snapshot_service.get_snapshot(db, "device-1", "AAPL")

        assert changed is True
        assert snapshot.snapshot_price == 105.0
        assert snapshot.snapshot_trading_date == date(2026, 9, 2)
    finally:
        db.close()


def test_get_snapshot_returns_none_when_absent():
    db = SessionLocal()
    try:
        assert snapshot_service.get_snapshot(db, "device-1", "MSFT") is None
    finally:
        db.close()


def test_snapshots_are_isolated_per_device():
    db = SessionLocal()
    try:
        snapshot_service.complete_visit(db, "device-1", "AAPL", 100.0, date(2026, 9, 1))

        assert snapshot_service.get_snapshot(db, "device-2", "AAPL") is None
    finally:
        db.close()
