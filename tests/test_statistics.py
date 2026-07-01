"""Tests for the weather-archive statistics helpers.

Pure transforms, no Home Assistant: the current-hour snapshot and the hourly
statistic rows derived from an Open-Meteo weather window. Runnable with
``python3 tests/test_statistics.py`` or under pytest.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from custom_components.helios_forecast.openmeteo import WeatherSeries  # noqa: E402
from custom_components.helios_forecast.statistics import (  # noqa: E402
    FORECAST_ENERGY_KEY,
    FORECAST_POWER_KEY,
    WEATHER_FIELDS,
    forecast_statistics,
    hourly_statistics,
    observed_snapshot,
    observed_value,
)


class _Pt:
    def __init__(self, t, pv_w):
        self.t = t
        self.pv_w = pv_w


def _h(hour: int) -> datetime:
    return datetime(2026, 6, 11, hour, tzinfo=timezone.utc)


def _series(times, values) -> WeatherSeries:
    # Same value in every array so we can assert per-field plumbing cheaply.
    return WeatherSeries(
        times=times,
        cloud=values,
        shortwave=values,
        direct=values,
        diffuse=values,
        temp=values,
        wind=values,
        snow=values,
    )


def test_observed_value_picks_current_hour() -> None:
    times = [_h(10), _h(11), _h(12), _h(13)]
    values = [100.0, 200.0, 300.0, 400.0]
    # 11:45 sits in the 11:00 bucket, the latest sample at or before now.
    assert observed_value(times, values, datetime(2026, 6, 11, 11, 45, tzinfo=timezone.utc)) == 200.0
    # Exactly on a boundary takes that hour.
    assert observed_value(times, values, _h(12)) == 300.0


def test_observed_value_skips_non_finite_and_empty() -> None:
    times = [_h(10), _h(11)]
    assert observed_value(times, [float("nan"), float("nan")], _h(11)) is None
    assert observed_value([], [], _h(11)) is None
    # A NaN current hour falls back to the last finite earlier hour.
    assert observed_value(times, [100.0, float("nan")], _h(11)) == 100.0


def test_observed_snapshot_covers_every_field() -> None:
    snap = observed_snapshot(_series([_h(10), _h(11)], [10.0, 20.0]), _h(11))
    assert set(snap) == {f.key for f in WEATHER_FIELDS}
    assert all(v == 20.0 for v in snap.values())


def test_hourly_statistics_drops_current_and_future_hours() -> None:
    times = [_h(9), _h(10), _h(11), _h(12)]
    values = [90.0, 100.0, 110.0, 120.0]
    cutoff = _h(11)  # top of the in-progress hour
    rows = hourly_statistics(times, values, cutoff)
    # Only completed hours strictly before the cutoff (09:00, 10:00).
    assert [r["start"] for r in rows] == [_h(9), _h(10)]
    assert rows[0] == {"start": _h(9), "mean": 90.0, "min": 90.0, "max": 90.0}


def test_hourly_statistics_skips_non_finite() -> None:
    times = [_h(9), _h(10)]
    rows = hourly_statistics(times, [float("nan"), 100.0], _h(11))
    assert [r["start"] for r in rows] == [_h(10)]


def test_forecast_statistics_power_and_energy() -> None:
    pts = [_Pt(_h(10), 2000.0), _Pt(_h(11), 0.0), _Pt(_h(12), float("nan"))]
    rows = forecast_statistics(pts)
    # NaN point dropped, two rows each for power and energy.
    assert [r["start"] for r in rows[FORECAST_POWER_KEY]] == [_h(10), _h(11)]
    assert rows[FORECAST_POWER_KEY][0] == {"start": _h(10), "mean": 2000.0, "min": 2000.0, "max": 2000.0}
    # Energy is the hour's Wh expressed in kWh: 2000 W over 1 h = 2.0 kWh.
    assert rows[FORECAST_ENERGY_KEY][0]["mean"] == 2.0
    assert rows[FORECAST_ENERGY_KEY][1]["mean"] == 0.0


def test_forecast_statistics_clamps_negative() -> None:
    rows = forecast_statistics([_Pt(_h(9), -50.0)])
    assert rows[FORECAST_POWER_KEY][0]["mean"] == 0.0


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("all statistics tests passed")
