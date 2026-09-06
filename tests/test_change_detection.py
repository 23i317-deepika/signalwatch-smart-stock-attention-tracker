"""
Unit tests for meaningful-change detection
(backend/app/services/change_detection.py). Direct DB session use — no HTTP.
"""

import pytest

from app.database import SessionLocal
from app.services import change_detection


def test_first_read_stores_a_baseline_with_the_first_visit_message():
    db = SessionLocal()
    try:
        result = change_detection.detect_changes(db, "device-1", "AAPL", 100.0)
        snapshot = change_detection.get_last_snapshot(db, "device-1", "AAPL")

        assert result.first_visit is True
        assert result.percent_change is None
        assert result.message == "First visit — baseline saved for future comparison."
        assert result.signals == []
        assert result.change_count == 0
        assert snapshot is not None
        assert snapshot.price == 100.0
        assert snapshot.percent_change is None
    finally:
        db.close()


def test_small_move_reports_no_meaningful_change():
    db = SessionLocal()
    try:
        change_detection.detect_changes(db, "device-1", "AAPL", 100.0)
        result = change_detection.detect_changes(db, "device-1", "AAPL", 101.0)  # +1%

        assert result.first_visit is False
        assert result.percent_change == 1.0
        assert result.message == "No unusually significant movement since your last visit."
        assert result.signals == []
        assert result.change_count == 0
    finally:
        db.close()


def test_move_at_or_above_threshold_is_a_meaningful_increase():
    db = SessionLocal()
    try:
        change_detection.detect_changes(db, "device-1", "AAPL", 100.0)
        result = change_detection.detect_changes(db, "device-1", "AAPL", 102.8)  # +2.8%

        assert result.percent_change == pytest.approx(2.8)
        assert result.message == "Price increased 2.8% since your last visit."
        assert result.signals == [result.message]
        assert result.change_count == 1
    finally:
        db.close()


def test_significant_drop_is_a_meaningful_decrease():
    db = SessionLocal()
    try:
        change_detection.detect_changes(db, "device-1", "AAPL", 100.0)
        result = change_detection.detect_changes(db, "device-1", "AAPL", 95.0)  # -5%

        assert result.percent_change == -5.0
        assert result.message == "Price decreased 5.0% since your last visit."
        assert result.change_count == 1
    finally:
        db.close()


def test_move_exactly_at_threshold_counts_as_meaningful():
    db = SessionLocal()
    try:
        change_detection.detect_changes(db, "device-1", "AAPL", 100.0)
        result = change_detection.detect_changes(db, "device-1", "AAPL", 102.0)  # exactly +2%

        assert result.change_count == 1
        assert result.message == "Price increased 2.0% since your last visit."
    finally:
        db.close()


def test_snapshots_are_isolated_per_device():
    db = SessionLocal()
    try:
        change_detection.detect_changes(db, "device-1", "AAPL", 100.0)

        result = change_detection.detect_changes(db, "device-2", "AAPL", 200.0)

        assert result.first_visit is True  # device-2 has never seen AAPL before
    finally:
        db.close()


def test_snapshot_advances_after_each_read_so_the_next_comparison_uses_the_latest_price():
    db = SessionLocal()
    try:
        change_detection.detect_changes(db, "device-1", "AAPL", 100.0)
        change_detection.detect_changes(db, "device-1", "AAPL", 110.0)

        snapshot = change_detection.get_last_snapshot(db, "device-1", "AAPL")
        assert snapshot.price == 110.0
        assert snapshot.percent_change == 10.0

        # A third read compares against 110.0, not the original 100.0 baseline.
        result = change_detection.detect_changes(db, "device-1", "AAPL", 111.0)  # +0.9% vs 110.0
        assert result.change_count == 0
    finally:
        db.close()


def test_concurrent_first_visit_race_does_not_crash(monkeypatch):
    """Two near-simultaneous dashboard reads for a brand-new (device, ticker)
    can both see "no previous snapshot" and race to INSERT the baseline row.
    The DB's unique constraint means the loser must hit IntegrityError —
    this must be handled gracefully (see change_detection.py), not raised
    as a 500.

    Simulated deterministically (no real threads needed): a snapshot
    already exists, but `get_last_snapshot` is patched to still report
    `None`, forcing detect_changes down the "insert a first snapshot" path
    it would have taken had it lost the race.
    """
    db = SessionLocal()
    try:
        change_detection.detect_changes(db, "device-1", "AAPL", 100.0)  # the "winning" request

        monkeypatch.setattr(change_detection, "get_last_snapshot", lambda *a, **kw: None)

        result = change_detection.detect_changes(db, "device-1", "AAPL", 105.0)  # the "losing" request

        # Handled the same as a normal first visit — no exception, no
        # meaningful-change spuriously reported.
        assert result.first_visit is True
        assert result.change_count == 0
    finally:
        db.close()
