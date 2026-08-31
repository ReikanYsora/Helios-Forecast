"""Synthetic WeatherSeries builder shared by the HA-coupled coordinator/init tests.

Not a pytest fixture module (no `hass` dependency), just a plain helper so both
test_coordinator.py and test_init.py can build a fetch_weather() stand-in without
hitting the network.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from custom_components.helios_forecast.openmeteo import WeatherSeries


def make_weather_series(now_utc: datetime, *, past_days: int = 60, forecast_days: int = 7) -> WeatherSeries:
    """An hourly WeatherSeries spanning [now - past_days, now + forecast_days], constant values.

    Mirrors the real fetch_weather() shape (UTC-aware, top-of-hour times) closely enough for the
    coordinator's orchestration to run end to end without asserting on the forecast numbers
    themselves (that belongs to the other modules' own tests).
    """
    start = (now_utc - timedelta(days=past_days)).replace(minute=0, second=0, microsecond=0)
    end = now_utc + timedelta(days=forecast_days)
    times = []
    t = start
    while t <= end:
        times.append(t)
        t += timedelta(hours=1)
    n = len(times)
    return WeatherSeries(
        times=times,
        cloud=[20.0] * n,
        shortwave=[300.0] * n,
        direct=[200.0] * n,
        diffuse=[100.0] * n,
        temp=[15.0] * n,
        wind=[10.0] * n,
        snow=[0.0] * n,
        cloud_spread=[5.0] * n,
    )
