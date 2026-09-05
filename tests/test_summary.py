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


def test_day_energy_raw_kwh_sums_pv_raw_w_the_same_way_as_energy_kwh() -> None:
    # pv_raw_w set to double pv_w so a day's raw total is independently checkable against the
    # corrected total, the same watt-to-kWh conversion and local-day bucketing as energy_kwh.
    base = datetime(2026, 6, 21, tzinfo=_UTC)
    pts = [ForecastPoint(t=base + timedelta(hours=h), pv_w=100.0, pv_raw_w=200.0) for h in range(24)]
    now = base + timedelta(hours=10)
    s = summarize(pts, now=now, tz=_UTC, step_minutes=60)
    assert abs(s.days[0].energy_kwh - 2.4) < 1e-9  # 24 x 100 W x 1h
    assert abs(s.days[0].energy_raw_kwh - 4.8) < 1e-9  # 24 x 200 W x 1h
    assert abs(s.days[0].energy_raw_kwh - 2 * s.days[0].energy_kwh) < 1e-9


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


def test_energy_today_remaining_is_zero_not_none_after_the_last_bucket_of_the_day() -> None:
    # 15-minute step, points covering all of today; `now` sits 5 minutes after the day's
    # last bucket (23:45) starts, so [now, midnight) holds zero buckets. That's a genuine
    # 0 kWh left, not a gap: the horizon still covers all the way to midnight.
    base = datetime(2026, 6, 21, tzinfo=_UTC)
    pts = [ForecastPoint(t=base + timedelta(minutes=15 * i), pv_w=50.0, pv_raw_w=50.0) for i in range(96)]
    now = base + timedelta(hours=23, minutes=50)
    s = summarize(pts, now=now, tz=_UTC, step_minutes=15)
    assert s.energy_today_remaining_kwh == 0.0


def test_empty_points() -> None:
    now = datetime(2026, 6, 21, 10, tzinfo=_UTC)
    s = summarize([], now=now, tz=_UTC, step_minutes=60)
    assert s.power_now_w is None
    assert s.wh_hours == {}
    assert len(s.days) == 7
    assert s.days[0].peak_power_w == 0.0
    assert s.energy_today_remaining_kwh is None
    assert s.energy_this_hour_kwh is None
    assert s.power_now_low_w is None and s.power_now_high_w is None


def test_power_now_band_interpolates_when_both_buckets_have_one() -> None:
    base = datetime(2026, 6, 21, tzinfo=_UTC)
    pts = [
        ForecastPoint(t=base + timedelta(hours=10), pv_w=700.0, pv_raw_w=700.0, pv_p10=600.0, pv_p90=800.0),
        ForecastPoint(t=base + timedelta(hours=11), pv_w=800.0, pv_raw_w=800.0, pv_p10=650.0, pv_p90=900.0),
    ]
    now = base + timedelta(hours=10, minutes=30)
    s = summarize(pts, now=now, tz=_UTC, step_minutes=60)
    assert abs(s.power_now_low_w - 625.0) < 1e-9
    assert abs(s.power_now_high_w - 850.0) < 1e-9


def test_power_now_band_falls_back_to_the_future_side_when_the_past_side_lacks_one() -> None:
    """The realistic shape at "now": the lower bracket is always at or before
    ``now``, and past points never carry a band (enrich_points only attaches
    one to the future). #421/#51: this used to return None here, permanently,
    on any install with a solid enough analog library for the future side to
    actually have a band - which is exactly when it should stop being None."""
    base = datetime(2026, 6, 21, tzinfo=_UTC)
    pts = [
        ForecastPoint(t=base + timedelta(hours=10), pv_w=700.0, pv_raw_w=700.0, pv_p10=None, pv_p90=None),
        ForecastPoint(t=base + timedelta(hours=11), pv_w=800.0, pv_raw_w=800.0, pv_p10=650.0, pv_p90=900.0),
    ]
    now = base + timedelta(hours=10, minutes=30)
    s = summarize(pts, now=now, tz=_UTC, step_minutes=60)
    assert s.power_now_low_w == 650.0
    assert s.power_now_high_w == 900.0


def test_power_now_band_none_when_neither_bucket_has_one() -> None:
    """Genuinely no analog support yet on either side: still None, not a
    fallback to some unrelated value."""
    base = datetime(2026, 6, 21, tzinfo=_UTC)
    pts = [
        ForecastPoint(t=base + timedelta(hours=10), pv_w=700.0, pv_raw_w=700.0, pv_p10=None, pv_p90=None),
        ForecastPoint(t=base + timedelta(hours=11), pv_w=800.0, pv_raw_w=800.0, pv_p10=None, pv_p90=None),
    ]
    now = base + timedelta(hours=10, minutes=30)
    s = summarize(pts, now=now, tz=_UTC, step_minutes=60)
    assert s.power_now_low_w is None
    assert s.power_now_high_w is None


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("all summary tests passed")
