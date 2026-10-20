"""A local day contains 23, 24 or 25 real hours, including both autumn folds."""

from datetime import datetime, timedelta, timezone
from unittest.mock import patch
from zoneinfo import ZoneInfo

from custom_components.helios_forecast.forecast import build_forecast_series, integrate_daily_kwh
from custom_components.helios_forecast.openmeteo import WeatherSeries
from custom_components.helios_forecast.solar.power import PvLayout


def _day(day, hours):
    zone = ZoneInfo("Europe/Berlin")
    start = datetime.fromisoformat(day).replace(tzinfo=zone)
    end = start + timedelta(days=1)
    first = start.astimezone(timezone.utc)
    times = [first + timedelta(hours=h) for h in range(hours + 1)]
    n = len(times)
    weather = WeatherSeries(
        times=times,
        cloud=[0.0] * n,
        shortwave=[600.0] * n,
        direct=[400.0] * n,
        diffuse=[200.0] * n,
        temp=[20.0] * n,
        wind=[0.0] * n,
        snow=[0.0] * n,
    )
    layout = PvLayout(orientations=[], shares=[], coords=[], total_kwp=1.0)
    # A constant nonzero source makes duplicated/missing energy visible even at night.
    with patch("custom_components.helios_forecast.forecast._sum_arrays", return_value=(1000.0, 1000.0)):
        local = build_forecast_series(weather, layout, 48.0, 2.0, start=start, end=end)
        utc = build_forecast_series(weather, layout, 48.0, 2.0, start=first, end=end.astimezone(timezone.utc))
    assert len(local) == hours * 4
    epochs = [p.t.timestamp() for p in local]
    assert len(set(epochs)) == len(epochs)
    assert all(b - a == 900 for a, b in zip(epochs, epochs[1:]))
    assert all(p.t.date() == start.date() and p.t.tzinfo is zone for p in local)
    assert [(p.t.timestamp(), p.pv_w) for p in local] == [(p.t.timestamp(), p.pv_w) for p in utc]
    assert integrate_daily_kwh(local, 15, zone) == {day: float(hours)}
    assert epochs[-1] + 900 == end.timestamp()
    return local


def test_spring_day_has_92_distinct_quarters():
    _day("2026-03-29", 23)


def test_ordinary_day_has_96_distinct_quarters():
    _day("2026-10-24", 24)


def test_autumn_day_has_100_distinct_quarters_and_both_offsets():
    points = _day("2026-10-25", 25)
    repeated_hour = [p.t for p in points if p.t.hour == 2]
    assert len(repeated_hour) == 8
    assert {p.utcoffset() for p in repeated_hour} == {timedelta(hours=1), timedelta(hours=2)}
