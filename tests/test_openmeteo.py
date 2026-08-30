"""Tests for the Open-Meteo client.

Pure URL construction + payload parsing, no network. The request mirrors the
card's: same model selection (pick_models_for_location, median-fused), the three
cloud layers fused into one weighted cover, and the instant irradiance variables.
Runnable with ``python3 tests/test_openmeteo.py`` or under pytest.
"""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

import custom_components.helios_forecast.openmeteo as om  # noqa: E402
from custom_components.helios_forecast.openmeteo import (  # noqa: E402
    build_weather_url,
    cloud_effective,
    fetch_weather,
    parse_cloud_spread,
    parse_times,
    parse_weather,
    pick_models_for_location,
)

_BASE = "https://api.open-meteo.com/v1/forecast"


def test_pick_models_for_location() -> None:
    # A regional high-res model paired with a global one, matching the card's picker.
    assert pick_models_for_location(48.8566, 2.3522) == ["meteofrance_seamless", "ecmwf_ifs025"]  # Paris
    assert pick_models_for_location(40.0, 140.0) == ["jma_seamless", "ecmwf_ifs025"]  # Japan
    assert pick_models_for_location(0.0, -30.0) == ["ecmwf_ifs025", "gfs_seamless"]  # open ocean fallback


def test_pick_models_resolves_overlapping_boxes() -> None:
    # Korea's box sits entirely inside Japan's; the enclosed box still wins.
    assert pick_models_for_location(37.5665, 126.9780) == ["kma_seamless", "ecmwf_ifs025"]  # Seoul
    # Border overlaps resolve to whichever box the point sits most centrally inside.
    assert pick_models_for_location(50.9, -1.4) == ["ukmo_seamless", "ecmwf_ifs025"]  # Southampton
    assert pick_models_for_location(50.94, 6.96) == ["dwd_icon_seamless", "ecmwf_ifs025"]  # Cologne
    assert pick_models_for_location(47.24, 6.02) == ["meteofrance_seamless", "ecmwf_ifs025"]  # Besançon


def test_weather_url_uses_picker_and_layers() -> None:
    # Values request: pick_models_for_location median, cloud layers, instant irradiance (matches the card).
    got = build_weather_url(48.8566, 2.3522, past_days=0, forecast_days=7)
    assert got == (
        f"{_BASE}?latitude=48.8566&longitude=2.3522"
        "&hourly=cloud_cover_low,cloud_cover_mid,cloud_cover_high,"
        "shortwave_radiation_instant,direct_radiation_instant,diffuse_radiation_instant,"
        "temperature_2m,wind_speed_10m,snow_depth"
        "&models=meteofrance_seamless,ecmwf_ifs025"
        "&past_days=0&forecast_days=7&timezone=UTC"
    )


def test_weather_url_ensemble_is_cloud_only_over_the_wide_set() -> None:
    got = build_weather_url(48.8566, 2.3522, past_days=0, forecast_days=7, ensemble=True)
    assert (
        "&hourly=cloud_cover&models=ecmwf_ifs025,gfs_seamless,icon_seamless,gem_seamless,meteofrance_seamless"
    ) in got


def test_parse_weather_fuses_models_and_weights_layers() -> None:
    # Two models per layer; each layer is median-fused, then combined into the weighted cover
    # (low + 0.6*mid + 0.2*high). Hour 0 exercises the weighting, hour 1 is low-only.
    payload = {
        "hourly": {
            "time": ["2026-06-11T00:00", "2026-06-11T01:00"],
            "cloud_cover_low_ecmwf_ifs025": [10, 80],
            "cloud_cover_low_gfs_seamless": [30, 100],  # median 20, 90
            "cloud_cover_mid_ecmwf_ifs025": [40, 0],
            "cloud_cover_mid_gfs_seamless": [40, 0],  # median 40, 0
            "cloud_cover_high_ecmwf_ifs025": [50, 0],
            "cloud_cover_high_gfs_seamless": [50, 0],  # median 50, 0
            "shortwave_radiation_instant_ecmwf_ifs025": [0, 100],
            "shortwave_radiation_instant_gfs_seamless": [0, 200],
        }
    }
    w = parse_weather(payload)
    assert w is not None
    assert w.cloud == [54.0, 90.0]  # 20 + 0.6*40 + 0.2*50 = 54 ; 90 + 0 + 0 = 90
    assert w.shortwave == [0.0, 150.0]
    assert w.cloud_spread == [0.0, 0.0]  # baseline; the ensemble call overlays the real spread


def test_cloud_effective_weights_and_clamps() -> None:
    # Plain weighted blend, well inside [0, 100].
    assert cloud_effective(20.0, 40.0, 50.0) == 54.0  # 20 + 0.6*40 + 0.2*50
    # Missing layers (None) count as clear, matching the docstring.
    assert cloud_effective(None, None, None) == 0.0
    assert cloud_effective(50.0, None, None) == 50.0
    # Out-of-range inputs are clamped to [0, 100] before weighting.
    assert cloud_effective(-10.0, 40.0, 50.0) == 34.0  # low clamped to 0
    assert cloud_effective(150.0, 0.0, 0.0) == 100.0  # low clamped to 100
    # The weighted sum itself is capped at 100 even if every layer is saturated.
    assert cloud_effective(100.0, 100.0, 100.0) == 100.0


def test_get_json_returns_none_on_timeout() -> None:
    # A stalled connection must not hang the refresh: _get_json degrades to None instead of raising.
    class _HangingResp:
        async def __aenter__(self):
            await asyncio.sleep(1.0)
            return self

        async def __aexit__(self, *exc):
            return False

    class _HangingSession:
        def get(self, url):
            return _HangingResp()

    original_timeout = om._REQUEST_TIMEOUT_S
    om._REQUEST_TIMEOUT_S = 0.01
    try:
        result = asyncio.run(om._get_json(_HangingSession(), "https://example.invalid"))
    finally:
        om._REQUEST_TIMEOUT_S = original_timeout
    assert result is None


def test_parse_cloud_spread_asserts_on_layer_key_collision() -> None:
    # If a payload ever carried both the per-layer keys and the aggregate 'cloud_cover' key together,
    # the prefix match in _model_arrays would silently fold the per-layer arrays into the spread
    # lookup. This must fail loudly instead of returning a corrupted result.
    payload = {
        "hourly": {
            "time": ["2026-06-11T00:00"],
            "cloud_cover": [50.0],
            "cloud_cover_low": [10.0],
        }
    }
    with pytest.raises(AssertionError):
        parse_cloud_spread(payload)


def test_parse_cloud_spread() -> None:
    # From an ensemble payload: per-hour stdev of cloud across the models, with its own times.
    payload = {
        "hourly": {
            "time": ["2026-06-11T00:00", "2026-06-11T01:00"],
            "cloud_cover_ecmwf_ifs025": [10, 80],
            "cloud_cover_gfs_seamless": [30, 80],
        }
    }
    res = parse_cloud_spread(payload)
    assert res is not None
    times, spread = res
    assert len(times) == 2
    assert spread[0] > 0  # models disagree at hour 0
    assert spread[1] == 0.0  # models agree at hour 1
    assert parse_cloud_spread({"hourly": {"time": [], "cloud_cover": []}}) is None


def test_parse_times_are_utc() -> None:
    times = parse_times(["2026-06-11T00:00", "2026-06-11T12:00"])
    assert times[0] == datetime(2026, 6, 11, 0, tzinfo=timezone.utc)
    assert times[1] == datetime(2026, 6, 11, 12, tzinfo=timezone.utc)


def test_parse_weather_maps_all_series() -> None:
    # Single-model response (bare keys): the weighted cover from the layers, instant irradiance passed through.
    payload = {
        "hourly": {
            "time": ["2026-06-11T00:00", "2026-06-11T01:00"],
            "cloud_cover_low": [10, 20],
            "cloud_cover_mid": [0, 0],
            "cloud_cover_high": [0, 0],
            "shortwave_radiation_instant": [0, 5],
            "direct_radiation_instant": [0, 3],
            "diffuse_radiation_instant": [0, 2],
            "temperature_2m": [12.0, 11.5],
            "wind_speed_10m": [8.0, 9.0],
            "snow_depth": [0.0, 0.0],
        }
    }
    w = parse_weather(payload)
    assert w is not None
    assert len(w.times) == 2
    assert w.cloud == [10.0, 20.0]  # low only, mid/high clear
    assert w.shortwave == [0, 5]
    assert w.direct == [0, 3]
    assert w.diffuse == [0, 2]
    assert w.temp == [12.0, 11.5]
    assert w.wind == [8.0, 9.0]
    assert w.snow == [0.0, 0.0]


def test_parse_weather_none_when_empty() -> None:
    assert parse_weather({}) is None
    assert parse_weather({"hourly": {"time": [], "cloud_cover_low": []}}) is None
    assert parse_weather({"hourly": {"time": ["2026-06-11T00:00"], "cloud_cover_low": []}}) is None


_GOOD_WEATHER = {
    "hourly": {
        "time": ["2026-06-11T10:00", "2026-06-11T11:00"],
        "cloud_cover_low": [50.0, 60.0],
        "shortwave_radiation_instant": [100.0, 200.0],
        "direct_radiation_instant": [10.0, 20.0],
        "diffuse_radiation_instant": [5.0, 6.0],
        "temperature_2m": [15.0, 16.0],
        "wind_speed_10m": [3.0, 4.0],
        "snow_depth": [0.0, 0.0],
    }
}


class _FakeResp:
    def __init__(self, status, payload):
        self.status = status
        self._payload = payload

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def json(self):
        return self._payload


class _FakeSession:
    """Serves a scripted list of responses, one per get() call."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = 0

    def get(self, url):
        self.calls += 1
        return self._responses.pop(0)


_ENSEMBLE_SPREAD = {
    "hourly": {
        "time": ["2026-06-11T10:00", "2026-06-11T11:00"],
        "cloud_cover_ecmwf_ifs025": [40.0, 60.0],
        "cloud_cover_gfs_seamless": [60.0, 60.0],
    }
}


def test_fetch_weather_values_with_ensemble_spread() -> None:
    om._RETRY_DELAY_S = 0.0  # do not actually sleep between retries in tests
    # Values call: empty, non-200, then good (the third try wins). Then the ensemble call overlays
    # the cross-model cloud spread onto the values.
    session = _FakeSession(
        [
            _FakeResp(200, {"hourly": {}}),
            _FakeResp(429, None),
            _FakeResp(200, _GOOD_WEATHER),
            _FakeResp(200, _ENSEMBLE_SPREAD),
        ]
    )
    result = asyncio.run(fetch_weather(session, 1.0, 2.0))
    assert result is not None
    assert result.cloud == [50.0, 60.0]  # weighted cover, low-only here
    assert result.cloud_spread == [10.0, 0.0]  # stdev([40,60])=10 then stdev([60,60])=0, overlaid from the ensemble
    assert session.calls == 4  # 3 values tries + 1 ensemble


def test_fetch_weather_ensemble_failure_degrades_to_zero_spread() -> None:
    om._RETRY_DELAY_S = 0.0
    # Values succeed on the first try; the ensemble call fails every retry. The series still returns,
    # with the baseline zero spread rather than failing the refresh (best-effort).
    session = _FakeSession(
        [_FakeResp(200, _GOOD_WEATHER), _FakeResp(500, None), _FakeResp(500, None), _FakeResp(500, None)]
    )
    result = asyncio.run(fetch_weather(session, 1.0, 2.0))
    assert result is not None
    assert result.cloud == [50.0, 60.0]
    assert result.cloud_spread == [0.0, 0.0]  # no ensemble spread available this refresh
    assert session.calls == 4  # 1 values + 3 ensemble tries


def test_fetch_weather_none_after_exhausting_retries() -> None:
    om._RETRY_DELAY_S = 0.0
    session = _FakeSession([_FakeResp(500, None), _FakeResp(500, None), _FakeResp(500, None)])
    result = asyncio.run(fetch_weather(session, 1.0, 2.0))
    assert result is None
    assert session.calls == 3  # values call capped at _RETRY_ATTEMPTS; the ensemble call is never reached


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("all openmeteo tests passed")
