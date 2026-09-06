"""
Tests for the pure Attention Engine (backend/app/services/attention.py).

No FastAPI, no DB, no network — every case is built from plain
AttentionInput values.
"""

from app.services.attention import AttentionInput, compute_attention_score


def make_input(**overrides) -> AttentionInput:
    defaults = dict(
        current_price=100.0,
        previous_visit_price=None,
        historical_volatility_pct=None,
        latest_volume=None,
        avg_volume_20d=None,
        week52_high=None,
        week52_low=None,
    )
    defaults.update(overrides)
    return AttentionInput(**defaults)


# 1. Large move with low volatility -> high score.
def test_large_move_with_low_volatility_scores_high():
    result = compute_attention_score(
        make_input(current_price=110, previous_visit_price=100, historical_volatility_pct=1.0)
    )

    assert result.components["volatility"] == 40  # z = 10/1 = 10, capped at 3 -> max
    assert result.score >= 50


# 2. Same percentage move with high volatility -> lower score than #1.
def test_same_move_with_high_volatility_scores_lower_than_low_volatility_case():
    low_vol = compute_attention_score(
        make_input(current_price=110, previous_visit_price=100, historical_volatility_pct=1.0)
    )
    high_vol = compute_attention_score(
        make_input(current_price=110, previous_visit_price=100, historical_volatility_pct=8.0)
    )

    assert high_vol.score < low_vol.score
    assert high_vol.components["volatility"] < low_vol.components["volatility"]


# 3. High volume spike increases score.
def test_high_volume_spike_increases_score():
    baseline = compute_attention_score(make_input())
    spiked = compute_attention_score(make_input(latest_volume=400, avg_volume_20d=100))

    assert spiked.components["volume"] == 25  # ratio 4x -> (4-1)/2 capped at 1 -> max
    assert spiked.score > baseline.score


# 4. Normal volume contributes little or zero.
def test_normal_volume_contributes_zero():
    result = compute_attention_score(make_input(latest_volume=100, avg_volume_20d=100))

    assert result.components["volume"] == 0


# 5. Near 52-week high increases score.
def test_near_52_week_high_increases_score():
    far = compute_attention_score(
        make_input(current_price=100, week52_high=200, week52_low=50)
    )
    near = compute_attention_score(
        make_input(current_price=99, week52_high=100, week52_low=50)
    )

    assert near.components["range"] > far.components["range"]
    assert near.components["range"] > 0


# 6. Near 52-week low increases score.
def test_near_52_week_low_increases_score():
    far = compute_attention_score(
        make_input(current_price=100, week52_high=200, week52_low=50)
    )
    near = compute_attention_score(
        make_input(current_price=51, week52_high=200, week52_low=50)
    )

    assert near.components["range"] > far.components["range"]
    assert near.components["range"] > 0


# 7. Missing volume does not crash.
def test_missing_volume_does_not_crash():
    result = compute_attention_score(
        make_input(latest_volume=None, avg_volume_20d=None)
    )

    assert result.components["volume"] == 0
    assert 0 <= result.score <= 100


# 8. Missing volatility does not crash.
def test_missing_volatility_does_not_crash():
    result = compute_attention_score(
        make_input(previous_visit_price=90, historical_volatility_pct=None)
    )

    assert result.components["volatility"] == 0
    assert result.z_score is None
    assert 0 <= result.score <= 100


# 9. Zero volatility does not cause division by zero.
def test_zero_volatility_does_not_divide_by_zero():
    result = compute_attention_score(
        make_input(previous_visit_price=90, historical_volatility_pct=0.0)
    )

    assert result.components["volatility"] == 0
    assert 0 <= result.score <= 100


# 10. No previous visit behaves correctly.
def test_no_previous_visit_has_no_comparison_but_still_scores():
    result = compute_attention_score(
        make_input(previous_visit_price=None, latest_volume=300, avg_volume_20d=100)
    )

    assert result.comparison_available is False
    assert result.price_change_pct is None
    assert result.components["volatility"] == 0
    assert result.components["raw_move"] == 0
    assert result.components["volume"] > 0  # market context is still usable
    assert result.reasons[0].startswith("First visit")


# 11. Score is always between 0 and 100 (including extreme inputs).
def test_score_always_within_bounds():
    extreme_cases = [
        make_input(current_price=1000, previous_visit_price=1, historical_volatility_pct=0.01),
        make_input(latest_volume=10_000_000, avg_volume_20d=1),
        make_input(current_price=0.01, week52_high=1000, week52_low=0.01),
        make_input(previous_visit_price=100, current_price=100),
        make_input(),
    ]

    for inputs in extreme_cases:
        result = compute_attention_score(inputs)
        assert 0 <= result.score <= 100
        for value in result.components.values():
            assert value >= 0


# 12. Reason generator does not claim missing data.
def test_reasons_do_not_reference_unavailable_data():
    result = compute_attention_score(
        make_input(
            current_price=130,
            previous_visit_price=100,
            historical_volatility_pct=1.0,
            latest_volume=None,
            avg_volume_20d=None,
            week52_high=None,
            week52_low=None,
        )
    )

    combined_text = " ".join(result.reasons).lower()
    assert "volume" not in combined_text
    assert "52-week" not in combined_text


# 13. A large raw move still gets attention when volatility is unavailable.
def test_large_raw_move_scores_even_without_volatility():
    result = compute_attention_score(
        make_input(current_price=120, previous_visit_price=100, historical_volatility_pct=None)
    )

    assert result.components["raw_move"] > 0
    assert result.score > 0
