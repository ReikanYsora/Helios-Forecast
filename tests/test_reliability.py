"""Tests for the forecast reliability index.

Pure blend of data maturity, recent predicted-vs-actual skill and today's cloud
predictability. Runnable with ``python3 tests/test_reliability.py`` or pytest.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from custom_components.helios_forecast.openmeteo import WeatherSeries  # noqa: E402
from custom_components.helios_forecast.reliability import (  # noqa: E402
    MATURITY_TARGET_DAYS,
    SKILL_MIN_DAY_KWH,
    SKILL_WINDOW_DAYS,
    _W_MATURITY,
    _W_PREDICT,
    _W_SKILL,
    _blend,
    _day_predictability,
    _horizon_decay,
    compute_reliability,
    data_maturity,
    recent_skill,
    today_predictability,
)

UTC = timezone.utc


class _Bucket:
    def __init__(self, start_ms, kwh):
        self.start_ms = start_ms
        self.kwh = kwh


def _day_ms(day: int) -> float:
    return datetime(2026, 6, day, 12, tzinfo=UTC).timestamp() * 1000.0


def test_data_maturity_counts_distinct_days() -> None:
    buckets = [_Bucket(_day_ms(1), 5.0), _Bucket(_day_ms(1), 1.0), _Bucket(_day_ms(2), 4.0)]
    frac, days = data_maturity(buckets, UTC)
    assert days == 2
    assert abs(frac - 2 / MATURITY_TARGET_DAYS) < 1e-9


def _predicted(days, kwh) -> dict:
    """What was written down for each of those days, the day before it happened."""
    return {datetime(2026, 6, d, tzinfo=UTC).date(): kwh for d in days}


def test_recent_skill_perfect_and_off() -> None:
    now = datetime(2026, 6, 10, 12, tzinfo=UTC)
    prod = [_Bucket(datetime(2026, 6, d, 12, tzinfo=UTC).timestamp() * 1000.0, 10.0) for d in range(5, 9)]
    assert recent_skill(_predicted(range(5, 9), 10.0), prod, now, UTC) == 1.0
    # Predict double -> 100% relative error -> skill 0.
    assert recent_skill(_predicted(range(5, 9), 20.0), prod, now, UTC) == 0.0


def test_recent_skill_none_when_too_few_days() -> None:
    now = datetime(2026, 6, 10, 12, tzinfo=UTC)
    prod = [_Bucket(datetime(2026, 6, 8, 12, tzinfo=UTC).timestamp() * 1000.0, 10.0)]
    assert recent_skill(_predicted([8], 10.0), prod, now, UTC) is None


def test_recent_skill_none_without_a_day_ahead_record() -> None:
    # A fresh install, or one whose record was lost: no day was predicted before it happened, so
    # there is nothing to score and the term must be absent rather than assumed good.
    now = datetime(2026, 6, 10, 12, tzinfo=UTC)
    prod = [_Bucket(datetime(2026, 6, d, 12, tzinfo=UTC).timestamp() * 1000.0, 10.0) for d in range(5, 9)]
    assert recent_skill({}, prod, now, UTC) is None


def test_recent_skill_ignores_days_below_the_minimum_actual_kwh() -> None:
    # A near-zero actual day (overcast / sensor gap) must not count as a comparable day,
    # however wrong the prediction was for it, or it would dominate the relative error.
    now = datetime(2026, 6, 10, 12, tzinfo=UTC)
    below_floor = SKILL_MIN_DAY_KWH / 2.0
    prod = [_Bucket(datetime(2026, 6, d, 12, tzinfo=UTC).timestamp() * 1000.0, 10.0) for d in range(5, 8)]
    prod.append(_Bucket(datetime(2026, 6, 8, 12, tzinfo=UTC).timestamp() * 1000.0, below_floor))
    predicted = _predicted(range(5, 8), 10.0)
    # Wildly wrong prediction on the excluded day; if it were counted, skill would collapse.
    predicted[datetime(2026, 6, 8, tzinfo=UTC).date()] = 999.0
    assert recent_skill(predicted, prod, now, UTC) == 1.0


def _weather_today(clouds, *, ghi=500.0) -> WeatherSeries:
    base = datetime(2026, 6, 10, 8, tzinfo=UTC)
    times = [base + timedelta(hours=i) for i in range(len(clouds))]
    n = len(clouds)
    return WeatherSeries(
        times=times,
        cloud=list(clouds),
        shortwave=[ghi] * n,
        direct=[0.0] * n,
        diffuse=[0.0] * n,
        temp=[20.0] * n,
        wind=[5.0] * n,
        snow=[0.0] * n,
    )


def _weather_today_with_spread(clouds, spreads, *, ghi=500.0) -> WeatherSeries:
    weather = _weather_today(clouds, ghi=ghi)
    weather.cloud_spread.extend(spreads)
    return weather


def test_day_predictability_falls_when_model_ensemble_disagrees() -> None:
    # Same steady cloud reading (zero temporal variance) but a high cross-model spread:
    # predictability must be pulled down by the spread signal, not stay at the variance-only 1.0.
    day = datetime(2026, 6, 10, tzinfo=UTC).date()
    steady_clouds = [10.0, 10.0, 10.0, 10.0, 10.0]
    agree = _weather_today_with_spread(steady_clouds, [0.0, 0.0, 0.0, 0.0, 0.0])
    disagree = _weather_today_with_spread(steady_clouds, [30.0, 30.0, 30.0, 30.0, 30.0])

    p_agree = _day_predictability(agree, day, UTC)
    p_disagree = _day_predictability(disagree, day, UTC)
    assert p_agree is not None and p_disagree is not None
    assert p_agree == 1.0  # zero temporal variance, zero model spread
    assert p_disagree < p_agree


def test_day_predictability_none_without_enough_daytime_samples() -> None:
    # Fewer than 3 daytime (above-GHI-gate) hours: neither signal has enough support.
    day = datetime(2026, 6, 10, tzinfo=UTC).date()
    weather = _weather_today([10.0, 10.0], ghi=500.0)
    assert _day_predictability(weather, day, UTC) is None


def test_today_predictability_clear_vs_broken() -> None:
    now = datetime(2026, 6, 10, 12, tzinfo=UTC)
    steady = today_predictability(_weather_today([10, 10, 10, 10, 10]), now, UTC)
    broken = today_predictability(_weather_today([0, 90, 10, 95, 5]), now, UTC)
    assert steady is not None and broken is not None
    assert steady > broken
    assert steady == 1.0  # zero spread -> fully predictable


def test_compute_reliability_shape_and_range() -> None:
    now = datetime(2026, 6, 10, 12, tzinfo=UTC)
    prod = [_Bucket(datetime(2026, 6, d, 12, tzinfo=UTC).timestamp() * 1000.0, 10.0) for d in range(1, 9)]
    predicted = _predicted(range(1, 9), 10.0)
    r = compute_reliability(prod, predicted, _weather_today([10, 12, 11, 10, 13]), now, UTC)
    assert 0.0 <= r.overall <= 100.0
    assert r.days_learned == 8
    assert len(r.per_day) == 7
    # Per-day reliability decays with the horizon.
    assert r.per_day[0] >= r.per_day[6]


def test_compute_reliability_today_entry_matches_predict() -> None:
    # The n=0 per-day entry reuses the already-computed `predict` value rather than
    # recomputing today's predictability a second time; horizon decay at n=0 is 1.0,
    # so the entry must equal the blend built with `predict` exactly.
    now = datetime(2026, 6, 10, 12, tzinfo=UTC)
    prod = [_Bucket(datetime(2026, 6, d, 12, tzinfo=UTC).timestamp() * 1000.0, 10.0) for d in range(1, 9)]
    predicted = _predicted(range(1, 9), 10.0)
    weather = _weather_today([10, 12, 11, 10, 13])
    r = compute_reliability(prod, predicted, weather, now, UTC)
    maturity, _ = data_maturity(prod, UTC)
    skill = recent_skill(predicted, prod, now, UTC)
    predict = today_predictability(weather, now, UTC)
    expected_today = round(_blend(maturity, skill, predict), 1)
    assert r.per_day[0] == expected_today


def test_the_published_index_stands_on_these_numbers() -> None:
    """The weights and windows of a number shown to users as a percentage. Changing one is a
    deliberate recalibration, not a tidy-up, so it has to come through here."""
    assert (MATURITY_TARGET_DAYS, SKILL_WINDOW_DAYS, SKILL_MIN_DAY_KWH) == (60, 14, 0.5)
    assert (_W_MATURITY, _W_SKILL, _W_PREDICT) == (0.35, 0.45, 0.20)
    assert _W_MATURITY + _W_SKILL + _W_PREDICT == 1.0
    # Sixty days of history is what "fully warmed up" means, and half of it is half the term.
    spread = [datetime(2026, 6, 1, 12, tzinfo=UTC) + timedelta(days=d) for d in range(60)]
    assert data_maturity([_Bucket(t.timestamp() * 1000.0, 5.0) for t in spread], UTC)[0] == 1.0
    assert data_maturity([_Bucket(t.timestamp() * 1000.0, 5.0) for t in spread[:30]], UTC)[0] == 0.5


def test_today_is_not_scored_while_it_is_still_running() -> None:
    """Half a day of production against a whole day's prediction reads as a large error, every
    morning, on every installation."""
    now = datetime(2026, 6, 10, 12, tzinfo=UTC)
    days = list(range(5, 10))
    prod = [_Bucket(datetime(2026, 6, d, 12, tzinfo=UTC).timestamp() * 1000.0, 10.0) for d in days]
    # Today has only started: two of the ten kWh are in, against a prediction of ten.
    prod.append(_Bucket(datetime(2026, 6, 10, 9, tzinfo=UTC).timestamp() * 1000.0, 2.0))
    assert recent_skill(_predicted(days + [10], 10.0), prod, now, UTC) == 1.0


def test_a_day_older_than_the_window_is_not_scored() -> None:
    """The window is what makes the term recent; widened, a summer month props up a December."""
    now = datetime(2026, 6, 30, 12, tzinfo=UTC)
    recent = list(range(28, 30))
    prod = [_Bucket(datetime(2026, 6, d, 12, tzinfo=UTC).timestamp() * 1000.0, 10.0) for d in recent]
    # A day well outside the window, predicted wildly wrong. Counted, it would halve the skill.
    prod.append(_Bucket(datetime(2026, 6, 1, 12, tzinfo=UTC).timestamp() * 1000.0, 10.0))
    predicted = _predicted(recent, 10.0)
    predicted[datetime(2026, 6, 1, tzinfo=UTC).date()] = 30.0
    assert recent_skill(predicted, prod, now, UTC) == 1.0


def test_predictability_reads_the_size_of_the_swing_not_only_its_sign() -> None:
    """A cloud forecast that swings across the whole sky is not nearly as predictable as one that
    wobbles by ten points, and the scale that separates them is part of the published number."""
    day = datetime(2026, 6, 10, tzinfo=UTC).date()
    # Standard deviation of 20 points, half the 40-point scale, so exactly half the signal is left.
    swing = _day_predictability(_weather_today([30.0, 70.0, 30.0, 70.0]), day, UTC)
    assert swing is not None and abs(swing - 0.5) < 1e-9


def test_an_unanswered_ensemble_is_not_perfect_agreement() -> None:
    """The ensemble call is best effort and its window does not always cover every hour. An hour it
    said nothing about has no spread, which must not read as every model agreeing exactly."""
    day = datetime(2026, 6, 10, tzinfo=UTC).date()
    broken = [0.0, 90.0, 10.0, 95.0, 5.0]
    unknown = _day_predictability(_weather_today_with_spread(broken, [None] * 5), day, UTC)
    agreed = _day_predictability(_weather_today_with_spread(broken, [0.0] * 5), day, UTC)
    variance_only = _day_predictability(_weather_today(broken), day, UTC)

    assert unknown is not None and agreed is not None
    assert unknown == variance_only  # the signal drops out
    assert unknown < agreed  # rather than raising the score


def test_a_missing_signal_never_raises_the_index() -> None:
    """Recent skill is the only term that measures accuracy, and it is the one that goes missing on a
    site with too few comparable days: a northern winter, a fortnight of overcast. Renormalised over
    the survivors, its absence made the published index rise exactly when it should not be trusted."""
    everything = _blend(1.0, 1.0, 1.0)
    without_skill = _blend(1.0, None, 1.0)
    without_predict = _blend(1.0, 1.0, None)

    assert without_skill < everything
    assert without_predict < everything
    # What is left is what could actually be measured: maturity and predictability, 0.35 + 0.20.
    assert abs(without_skill - 55.0) < 1e-9


def test_horizon_decay_is_gentle() -> None:
    # The horizon decay must degrade gently: exponential toward a 0.5 floor, matching how
    # NWP skill actually degrades with lead time.
    vals = [_horizon_decay(n) for n in range(7)]
    assert vals[0] == 1.0
    assert all(vals[i] > vals[i + 1] for i in range(6))  # strictly decreasing
    assert vals[6] >= 0.5  # floor
    assert vals[3] > 0.65  # J+3 still meaningfully reliable


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("all reliability tests passed")
