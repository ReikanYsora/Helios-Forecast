"""Tests for the Open-Meteo client.

Pure URL construction + payload parsing, no network. The URLs are pinned to the
exact strings the card builds (same endpoint, variable list, azimuth conversion)
so a drift from the card's request is caught. Runnable with
``python3 tests/test_openmeteo.py`` or under pytest.
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from custom_components.helios_forecast.openmeteo import (  # noqa: E402
    build_gti_url,
    build_weather_url,
    om_azimuth,
    parse_gti,
    parse_times,
    parse_weather,
)

_BASE = "https://api.open-meteo.com/v1/forecast"


def test_weather_url_has_variables_and_models() -> None:
    got = build_weather_url(48.8566, 2.3522, past_days=0, forecast_days=7)
    assert got == (
        f"{_BASE}?latitude=48.8566&longitude=2.3522"
        "&hourly=cloud_cover,shortwave_radiation,direct_radiation,diffuse_radiation,"
        "temperature_2m,wind_speed_10m,snow_depth"
        "&models=ecmwf_ifs025,gfs_seamless,icon_seamless,gem_seamless,meteofrance_seamless"
        "&past_days=0&forecast_days=7&timezone=UTC"
    )


def test_parse_weather_fuses_models_to_median() -> None:
    # Two models for cloud + shortwave; the fused series is the per-hour median, and the cloud spread
    # is the cross-model standard deviation.
    payload = {
        "hourly": {
            "time": ["2026-06-11T00:00", "2026-06-11T01:00"],
            "cloud_cover_ecmwf_ifs025": [10, 80],
            "cloud_cover_gfs_seamless": [30, 100],
            "shortwave_radiation_ecmwf_ifs025": [0, 100],
            "shortwave_radiation_gfs_seamless": [0, 200],
        }
    }
    w = parse_weather(payload)
    assert w is not None
    assert w.cloud == [20.0, 90.0]  # median of the two models per hour
    assert w.shortwave == [0.0, 150.0]
    assert w.cloud_spread[1] > 0  # models disagree -> non-zero spread


def test_gti_url_matches_card_with_azimuth_conversion() -> None:
    # Helios south (180) -> Open-Meteo south (0); tilt rounded.
    got = build_gti_url(48.8566, 2.3522, 30.0, 180.0, past_days=0, forecast_days=7)
    assert got == (
        f"{_BASE}?latitude=48.8566&longitude=2.3522"
        "&hourly=global_tilted_irradiance_instant"
        "&tilt=30&azimuth=0&past_days=0&forecast_days=7&timezone=UTC"
    )


def test_om_azimuth_conversion() -> None:
    assert om_azimuth(180) == 0  # south
    assert om_azimuth(90) == -90  # east
    assert om_azimuth(270) == 90  # west
    assert om_azimuth(0) == -180  # north
    assert om_azimuth(225) == 45  # south-west


def test_parse_times_are_utc() -> None:
    times = parse_times(["2026-06-11T00:00", "2026-06-11T12:00"])
    assert times[0] == datetime(2026, 6, 11, 0, tzinfo=timezone.utc)
    assert times[1] == datetime(2026, 6, 11, 12, tzinfo=timezone.utc)


def test_parse_weather_maps_all_series() -> None:
    payload = {
        "hourly": {
            "time": ["2026-06-11T00:00", "2026-06-11T01:00"],
            "cloud_cover": [10, 20],
            "shortwave_radiation": [0, 5],
            "direct_radiation": [0, 3],
            "diffuse_radiation": [0, 2],
            "temperature_2m": [12.0, 11.5],
            "wind_speed_10m": [8.0, 9.0],
            "snow_depth": [0.0, 0.0],
        }
    }
    w = parse_weather(payload)
    assert w is not None
    assert len(w.times) == 2
    assert w.cloud == [10, 20]
    assert w.shortwave == [0, 5]
    assert w.direct == [0, 3]
    assert w.diffuse == [0, 2]
    assert w.temp == [12.0, 11.5]
    assert w.wind == [8.0, 9.0]
    assert w.snow == [0.0, 0.0]


def test_parse_weather_none_when_empty() -> None:
    assert parse_weather({}) is None
    assert parse_weather({"hourly": {"time": [], "cloud_cover": []}}) is None
    assert parse_weather({"hourly": {"time": ["2026-06-11T00:00"], "cloud_cover": []}}) is None


def test_parse_gti() -> None:
    payload = {
        "hourly": {
            "time": ["2026-06-11T00:00", "2026-06-11T12:00"],
            "global_tilted_irradiance_instant": [0, 620],
        }
    }
    g = parse_gti(payload)
    assert g is not None
    assert g.poa == [0, 620]
    assert g.times[1] == datetime(2026, 6, 11, 12, tzinfo=timezone.utc)
    assert parse_gti({"hourly": {"time": ["2026-06-11T00:00"], "global_tilted_irradiance_instant": []}}) is None


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("all openmeteo tests passed")
