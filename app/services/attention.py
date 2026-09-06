"""
Attention Engine.

Pure, deterministic, explainable scoring — deliberately isolated from
FastAPI, SQLAlchemy, and yfinance so it can be constructed and tested with
plain values, with no mocking. Nothing here does I/O.

Not a prediction: the score only prioritizes which watchlist stocks are
worth a human's attention, and why. It never says "buy", "sell", "bullish",
or "bearish", and never forecasts future prices.
"""

from dataclasses import dataclass, field

# --- Component weights (max points each; they sum to 100) -----------------
MAX_VOLATILITY = 40
MAX_VOLUME = 25
MAX_RANGE = 20
MAX_RAW_MOVE = 15

# --- Tuning constants -------------------------------------------------------
Z_SCORE_CAP = 3.0          # a move >= 3x typical daily volatility maxes out the component
VOLUME_RATIO_SPAN = 2.0    # volume_ratio - 1 is scaled by this; ratio >= 3x maxes out
RANGE_BAND_PCT = 5.0       # within this % of the 52-week high/low maxes out the component
RAW_MOVE_CAP_PCT = 10.0    # a move >= 10% since last visit maxes out the backstop

# --- Reason-generation thresholds ------------------------------------------
QUIET_THRESHOLD = 15   # below this, nothing meaningful happened
HIGH_THRESHOLD = 60    # at/above this, name the specific signals

EPSILON = 1e-9  # guards against division by (near-)zero volatility/volume/price


@dataclass(frozen=True)
class AttentionInput:
    """Plain, normalized inputs the engine needs. No ORM/DataFrame objects."""

    current_price: float
    previous_visit_price: float | None = None  # None => first-time view, no baseline
    historical_volatility_pct: float | None = None  # 20-day daily return stdev, in percent
    latest_volume: float | None = None
    avg_volume_20d: float | None = None
    week52_high: float | None = None
    week52_low: float | None = None


@dataclass(frozen=True)
class AttentionResult:
    """Score + everything needed to explain and consume it."""

    score: int
    components: dict[str, int]
    comparison_available: bool
    price_change_pct: float | None
    z_score: float | None
    volume_ratio: float | None
    reasons: list[str] = field(default_factory=list)


def compute_attention_score(inputs: AttentionInput) -> AttentionResult:
    """Compute the 0-100 Attention Score and its explanation for one ticker."""
    comparison_available = inputs.previous_visit_price is not None

    price_change_pct = _price_change_pct(inputs)
    volatility_component, z_score = _volatility_component(inputs, price_change_pct)
    volume_component, volume_ratio = _volume_component(inputs)
    range_component, nearer_dist_pct, near_high = _range_component(inputs)
    raw_move_component = _raw_move_component(price_change_pct)

    components = {
        "volatility": round(volatility_component),
        "volume": round(volume_component),
        "range": round(range_component),
        "raw_move": round(raw_move_component),
    }
    score = max(0, min(100, sum(components.values())))

    reasons = _generate_reasons(
        comparison_available=comparison_available,
        score=score,
        price_change_pct=price_change_pct,
        z_score=z_score,
        volatility_component=components["volatility"],
        volume_ratio=volume_ratio,
        volume_component=components["volume"],
        nearer_dist_pct=nearer_dist_pct,
        near_high=near_high,
        range_component=components["range"],
    )

    return AttentionResult(
        score=score,
        components=components,
        comparison_available=comparison_available,
        price_change_pct=price_change_pct,
        z_score=z_score,
        volume_ratio=volume_ratio,
        reasons=reasons,
    )


# --- Component calculations -------------------------------------------------


def _price_change_pct(inputs: AttentionInput) -> float | None:
    if inputs.previous_visit_price is None or abs(inputs.previous_visit_price) < EPSILON:
        return None
    return (inputs.current_price - inputs.previous_visit_price) / inputs.previous_visit_price * 100


def _volatility_component(
    inputs: AttentionInput, price_change_pct: float | None
) -> tuple[float, float | None]:
    """A. Volatility-adjusted move — requires both a baseline and known volatility."""
    if price_change_pct is None:
        return 0.0, None
    if inputs.historical_volatility_pct is None or inputs.historical_volatility_pct < EPSILON:
        return 0.0, None

    z_score = abs(price_change_pct) / inputs.historical_volatility_pct
    component = min(z_score / Z_SCORE_CAP, 1.0) * MAX_VOLATILITY
    return component, z_score


def _volume_component(inputs: AttentionInput) -> tuple[float, float | None]:
    """B. Volume anomaly — 0 if volume data is missing (never guessed)."""
    if inputs.latest_volume is None or inputs.avg_volume_20d is None:
        return 0.0, None
    if inputs.avg_volume_20d < EPSILON:
        return 0.0, None

    ratio = inputs.latest_volume / inputs.avg_volume_20d
    component = min(max(ratio - 1, 0.0) / VOLUME_RATIO_SPAN, 1.0) * MAX_VOLUME
    return component, ratio


def _range_component(inputs: AttentionInput) -> tuple[float, float | None, bool | None]:
    """C. 52-week range context — proximity only, no judgment about direction."""
    if (
        inputs.week52_high is None
        or inputs.week52_low is None
        or inputs.week52_high <= inputs.week52_low
        or inputs.current_price < EPSILON
    ):
        return 0.0, None, None

    dist_to_high = max(0.0, (inputs.week52_high - inputs.current_price) / inputs.current_price)
    dist_to_low = max(0.0, (inputs.current_price - inputs.week52_low) / inputs.current_price)
    near_high = dist_to_high <= dist_to_low
    nearer_dist_pct = min(dist_to_high, dist_to_low) * 100

    component = max(0.0, 1 - nearer_dist_pct / RANGE_BAND_PCT) * MAX_RANGE
    return component, nearer_dist_pct, near_high


def _raw_move_component(price_change_pct: float | None) -> float:
    """D. Raw move backstop — a floor so a big move is never scored as zero."""
    if price_change_pct is None:
        return 0.0
    return min(abs(price_change_pct) / RAW_MOVE_CAP_PCT, 1.0) * MAX_RAW_MOVE


# --- Human-readable reasons --------------------------------------------------


def _generate_reasons(
    *,
    comparison_available: bool,
    score: int,
    price_change_pct: float | None,
    z_score: float | None,
    volatility_component: int,
    volume_ratio: float | None,
    volume_component: int,
    nearer_dist_pct: float | None,
    near_high: bool | None,
    range_component: int,
) -> list[str]:
    if not comparison_available:
        return [_first_visit_reason(volume_ratio, nearer_dist_pct, near_high)]

    if score < QUIET_THRESHOLD:
        return ["No unusually significant movement since your last visit."]

    candidates = _signal_candidates(
        z_score, volatility_component, volume_ratio, volume_component, nearer_dist_pct, near_high, range_component
    )

    if score >= HIGH_THRESHOLD and candidates:
        candidates.sort(key=lambda c: -c[0])
        top = candidates[:2]
        if len(top) == 1:
            return [top[0][3]]
        return [_combine_top_two(top[0], top[1])]

    return ["Price movement is larger than usual."]


def _combine_top_two(
    first: tuple[int, str, str, str], second: tuple[int, str, str, str]
) -> str:
    _, first_kind, first_fragment, _ = first
    _, second_kind, second_fragment, _ = second

    # "moved X on Y volume" reads naturally (matches the spec's own example);
    # any other pairing (e.g. volatility + range) reads better as a plain list.
    joiner = " on " if {first_kind, second_kind} == {"volatility", "volume"} else ", "

    capitalized = f"{first_fragment[0].upper()}{first_fragment[1:]}"
    return f"{capitalized}{joiner}{second_fragment}."


def _signal_candidates(
    z_score: float | None,
    volatility_component: int,
    volume_ratio: float | None,
    volume_component: int,
    nearer_dist_pct: float | None,
    near_high: bool | None,
    range_component: int,
) -> list[tuple[int, str, str, str]]:
    """Each candidate is (weight, kind, fragment_for_combining, standalone_sentence)."""
    candidates: list[tuple[int, str, str, str]] = []

    if z_score is not None:
        fragment = f"moved {z_score:.1f}× its typical volatility"
        full = f"Moved {z_score:.1f}× more than its typical daily volatility."
        candidates.append((volatility_component, "volatility", fragment, full))

    if volume_ratio is not None and volume_ratio > 1.0:
        fragment = f"{volume_ratio:.1f}× average volume"
        full = f"Trading at {volume_ratio:.1f}× its average volume."
        candidates.append((volume_component, "volume", fragment, full))

    if nearer_dist_pct is not None and nearer_dist_pct <= RANGE_BAND_PCT:
        bound = "52-week high" if near_high else "52-week low"
        fragment = f"within {nearer_dist_pct:.1f}% of its {bound}"
        full = f"Within {nearer_dist_pct:.1f}% of its {bound}."
        candidates.append((range_component, "range", fragment, full))

    return candidates


def _first_visit_reason(
    volume_ratio: float | None, nearer_dist_pct: float | None, near_high: bool | None
) -> str:
    """First-time view: market context only, never phrased as a comparison."""
    parts: list[str] = []

    if volume_ratio is not None and volume_ratio > 1.0:
        parts.append(f"currently trading at {volume_ratio:.1f}× average volume")

    if nearer_dist_pct is not None and nearer_dist_pct <= RANGE_BAND_PCT:
        bound = "52-week high" if near_high else "52-week low"
        parts.append(f"within {nearer_dist_pct:.1f}% of its {bound}")

    if not parts:
        return "First visit — no unusual market context detected."

    return f"First visit — {' and '.join(parts)}."
