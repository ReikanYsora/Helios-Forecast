"""Tests for the entity / Energy-provider summary derivation."""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from custom_components.helios_forecast.forecast import ForecastPoint  # noqa: E402
from custom_components.helios_forecast.summary import summarize  # noqa: E402

_UTC = timezone.utc


def _triangular_points() -> list[ForecastPoint]:
    # 7 days, hourly, a triangular day peaking 1000 W at 12:00, zero by ~04:00 / 20:00.
    points = []
    base = datetime(2026, 6, 21, tzinfo=_UTC)
    for d in range(7):
        for h in range(24):
            pv = max(0.0, 1000.0 - abs(h - 12) * 120.0)
            points.append(ForecastPoint(t=base + timedelta(days=d, hours=h), pv_w=pv, pv_raw_w=pv))
    return points


def test_power_now_and_next_hour() -> None:
    pts = _triangular_points()
    now = datetime(2026, 6, 21, 10, tzinfo=_UTC)
    s = summarize(pts, now=now, tz=_UTC, step_minutes=60)
    assert s.power_now_w == 1000.0 - 2 * 120.0  # 760 at 10:00
    assert s.power_next_hour_w == 760.0  # only the 10:00 bucket in [10,11)


def test_seven_days_with_peak() -> None:
    pts = _triangular_points()
    now = datetime(2026, 6, 21, 10, tzinfo=_UTC)
    s = summarize(pts, now=now, tz=_UTC, step_minutes=60)
    assert len(s.days) == 7
    assert s.days[0].date == "2026-06-21"
    assert s.days[6].date == "2026-06-27"
    assert s.days[0].peak_power_w == 1000.0
    assert s.days[0].peak_time == datetime(2026, 6, 21, 12, tzinfo=_UTC)
    day0_energy = sum(max(0.0, 1000.0 - abs(h - 12) * 120.0) for h in range(24)) / 1000.0
    assert abs(s.days[0].energy_kwh - day0_energy) < 1e-9


def test_hourly_buckets_and_wh_hours() -> None:
    pts = _triangular_points()
    now = datetime(2026, 6, 21, 10, tzinfo=_UTC)
    s = summarize(pts, now=now, tz=_UTC, step_minutes=60)
    # this hour [10,11): 760 W -> 0.76 kWh; next hour [11,12): 880 W -> 0.88 kWh
    assert abs(s.energy_this_hour_kwh - 0.76) < 1e-9
    assert abs(s.energy_next_hour_kwh - 0.88) < 1e-9
    # wh_hours: the noon bucket is 1000 W over 1 h = 1000 Wh
    assert s.wh_hours["2026-06-21T12:00:00+00:00"] == 1000.0
    # remaining today = sum of hours 10..23
    remaining = sum(max(0.0, 1000.0 - abs(h - 12) * 120.0) for h in range(10, 24)) / 1000.0
    assert abs(s.energy_today_remaining_kwh - remaining) < 1e-9


def test_empty_points() -> None:
    now = datetime(2026, 6, 21, 10, tzinfo=_UTC)
    s = summarize([], now=now, tz=_UTC, step_minutes=60)
    assert s.power_now_w is None
    assert s.wh_hours == {}
    assert len(s.days) == 7
    assert s.days[0].peak_power_w == 0.0


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("all summary tests passed")
