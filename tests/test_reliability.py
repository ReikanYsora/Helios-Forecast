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


class _Pt:
    def __init__(self, t, pv_w):
        self.t = t
        self.pv_w = pv_w


def _day_ms(day: int) -> float:
    return datetime(2026, 6, day, 12, tzinfo=UTC).timestamp() * 1000.0


def test_data_maturity_counts_distinct_days() -> None:
    buckets = [_Bucket(_day_ms(1), 5.0), _Bucket(_day_ms(1), 1.0), _Bucket(_day_ms(2), 4.0)]
    frac, days = data_maturity(buckets, UTC)
    assert days == 2
    assert abs(frac - 2 / MATURITY_TARGET_DAYS) < 1e-9


def test_recent_skill_perfect_and_off() -> None:
    now = datetime(2026, 6, 10, 12, tzinfo=UTC)
    # Actual 10 kWh on days 5..8; predicted hourly points summing to 10 kWh per day = perfect.
    prod = [_Bucket(datetime(2026, 6, d, 12, tzinfo=UTC).timestamp() * 1000.0, 10.0) for d in range(5, 9)]
    pts = [_Pt(datetime(2026, 6, d, 12, tzinfo=UTC), 10_000.0) for d in range(5, 9)]
    assert recent_skill(pts, prod, now, UTC) == 1.0
    # Predict double -> 100% relative error -> skill 0.
    pts_off = [_Pt(datetime(2026, 6, d, 12, tzinfo=UTC), 20_000.0) for d in range(5, 9)]
    assert recent_skill(pts_off, prod, now, UTC) == 0.0


def test_recent_skill_none_when_too_few_days() -> None:
    now = datetime(2026, 6, 10, 12, tzinfo=UTC)
    prod = [_Bucket(datetime(2026, 6, 8, 12, tzinfo=UTC).timestamp() * 1000.0, 10.0)]
    pts = [_Pt(datetime(2026, 6, 8, 12, tzinfo=UTC), 10_000.0)]
    assert recent_skill(pts, prod, now, UTC) is None


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
    pts = [_Pt(datetime(2026, 6, d, 12, tzinfo=UTC), 10_000.0) for d in range(1, 9)]
    r = compute_reliability(prod, pts, _weather_today([10, 12, 11, 10, 13]), now, UTC)
    assert 0.0 <= r.overall <= 100.0
    assert r.days_learned == 8
    assert len(r.per_day) == 7
    # Per-day reliability decays with the horizon.
    assert r.per_day[0] >= r.per_day[6]


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("all reliability tests passed")
