"""Tests for the analog-ensemble refinement.

Pure: builds a library of (sun geometry + cloud -> actual watts) samples, looks up
the nearest analogs, and blends their median + P10/P90 band into the forecast.
Runnable with ``python3 tests/test_analog.py`` or pytest.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from custom_components.helios_forecast.analog import (  # noqa: E402
    AnalogSample,
    _az_diff,
    _weighted_percentiles,
    build_library,
    enrich_points,
    predict,
)
from custom_components.helios_forecast.forecast import ForecastPoint  # noqa: E402
from custom_components.helios_forecast.openmeteo import WeatherSeries  # noqa: E402
from custom_components.helios_forecast.solar.geometry import sun_position  # noqa: E402

UTC = timezone.utc


class _Bucket:
    def __init__(self, start_ms, end_ms, kwh):
        self.start_ms = start_ms
        self.end_ms = end_ms
        self.kwh = kwh


def test_az_diff_wraps() -> None:
    assert _az_diff(10, 350) == 20
    assert _az_diff(180, 0) == 180
    assert _az_diff(90, 95) == 5


def test_weighted_percentiles_monotonic() -> None:
    pairs = [(v, 1.0) for v in [0, 10, 20, 30, 40, 50, 60, 70, 80, 90, 100]]
    p10, p50, p90 = _weighted_percentiles(pairs, (0.10, 0.50, 0.90))
    assert p10 <= p50 <= p90
    assert p10 <= 20 and p90 >= 80


def test_predict_median_and_confidence() -> None:
    # Tight cluster of analogs at one condition, all producing ~2000 W.
    lib = [AnalogSample(alt=40.0, az=180.0, cloud=50.0, watt=2000.0 + (i % 5) * 10) for i in range(40)]
    band = predict(lib, 40.0, 180.0, 50.0)
    assert band is not None
    assert 1980.0 <= band.p50 <= 2060.0
    assert band.p10 <= band.p50 <= band.p90
    assert band.confidence == 1.0  # >= 25 close analogs saturates confidence


def test_predict_low_confidence_far_conditions() -> None:
    lib = [AnalogSample(alt=10.0, az=90.0, cloud=0.0, watt=500.0) for _ in range(40)]
    # Query a very different geometry + cloud: analogs are far, confidence collapses.
    band = predict(lib, 60.0, 270.0, 100.0)
    assert band is not None
    assert band.confidence < 0.35


def test_predict_none_on_empty_or_night() -> None:
    assert predict([], 40.0, 180.0, 50.0) is None
    assert predict([AnalogSample(40, 180, 50, 2000)], -5.0, 180.0, 50.0) is None


def test_temperature_influences_match() -> None:
    # Same sky + geometry, two temperature regimes producing differently (hot panels produce less).
    # The match must lean toward the analogs whose temperature is closest to the query, so a cool
    # query reads higher than a hot one; without the temperature feature both would return the mix.
    cool = [AnalogSample(alt=40.0, az=180.0, cloud=30.0, watt=3000.0, temp=5.0) for _ in range(40)]
    hot = [AnalogSample(alt=40.0, az=180.0, cloud=30.0, watt=2000.0, temp=35.0) for _ in range(40)]
    lib = cool + hot
    band_cool = predict(lib, 40.0, 180.0, 30.0, temp=5.0)
    band_hot = predict(lib, 40.0, 180.0, 30.0, temp=35.0)
    assert band_cool is not None and band_hot is not None
    assert band_cool.p50 > band_hot.p50
    # No query temperature (or samples without temp) falls back to the old geometry+cloud match.
    band_none = predict(lib, 40.0, 180.0, 30.0)
    assert band_none is not None


def _june_noon(hour: int) -> datetime:
    return datetime(2026, 6, 15, hour, tzinfo=UTC)


def test_build_library_drops_night() -> None:
    lat, lon = 45.0, 0.0
    # Two hourly buckets: noon (sun up) and midnight (sun down).
    noon = _june_noon(12)
    midnight = _june_noon(0)
    prod = [
        _Bucket(noon.timestamp() * 1000.0, (noon + timedelta(hours=1)).timestamp() * 1000.0, 3.0),
        _Bucket(midnight.timestamp() * 1000.0, (midnight + timedelta(hours=1)).timestamp() * 1000.0, 0.0),
    ]
    times = [_june_noon(h) for h in range(24)]
    weather = WeatherSeries(
        times=times,
        cloud=[20.0] * 24,
        shortwave=[0.0] * 24,
        direct=[0.0] * 24,
        diffuse=[0.0] * 24,
        temp=[20.0] * 24,
        wind=[5.0] * 24,
        snow=[0.0] * 24,
    )
    lib = build_library(prod, weather, lat, lon)
    assert len(lib) == 1  # only the daytime bucket survives
    assert lib[0].watt == 3.0 * 1000.0  # kWh -> W over the hour


def test_enrich_points_past_untouched_future_blended() -> None:
    lat, lon = 45.0, 0.0
    now = _june_noon(12)
    past = ForecastPoint(t=_june_noon(8), pv_w=1000.0, pv_raw_w=1000.0)
    fut = ForecastPoint(t=_june_noon(13), pv_w=1000.0, pv_raw_w=1000.0)

    # Seed the library at the future point's exact sun position so analogs are close.
    sun = sun_position(fut.t, lat, lon)
    lib = [AnalogSample(alt=sun.altitude, az=sun.azimuth, cloud=30.0, watt=2500.0) for _ in range(40)]
    times = [_june_noon(h) for h in range(24)]
    weather = WeatherSeries(
        times=times,
        cloud=[30.0] * 24,
        shortwave=[0.0] * 24,
        direct=[0.0] * 24,
        diffuse=[0.0] * 24,
        temp=[20.0] * 24,
        wind=[5.0] * 24,
        snow=[0.0] * 24,
    )
    out = enrich_points([past, fut], lib, weather, lat, lon, now)
    # Past point is unchanged.
    assert out[0] == past
    # Future point blends toward the analog median (2500) and gains a band.
    assert out[1].pv_w > 1000.0
    assert out[1].pv_p10 is not None and out[1].pv_p90 is not None
    assert out[1].pv_p10 <= out[1].pv_w <= out[1].pv_p90 + 1e-6


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("all analog tests passed")
