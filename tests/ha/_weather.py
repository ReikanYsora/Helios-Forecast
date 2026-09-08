"""Synthetic WeatherSeries builder shared by the HA-coupled coordinator/init tests.

Not a pytest fixture module (no `hass` dependency), just a plain helper so both
test_coordinator.py and test_init.py can build a fetch_weather() stand-in without
hitting the network.
"""

from __future__ import annotations

from datetime import datetime, timedelta

from custom_components.helios_forecast.openmeteo import WeatherSeries


def make_weather_series(now_utc: datetime, *, past_days: int = 60, forecast_days: int = 7) -> WeatherSeries:
    """An hourly WeatherSeries with the window Open-Meteo actually returns, constant values.

    The service answers whole UTC days: from (today - past_days) 00:00 to (today + forecast_days - 1)
    23:00, never a window rolling from the current instant. The difference is what makes a horizon
    that overruns the weather reachable in a test at all, so the shape is copied exactly and the
    values are left flat (the forecast numbers belong to the pure modules' own tests).
    """
    midnight = now_utc.replace(hour=0, minute=0, second=0, microsecond=0)
    start = midnight - timedelta(days=past_days)
    end = midnight + timedelta(days=forecast_days) - timedelta(hours=1)
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
