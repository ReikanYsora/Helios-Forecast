"""Tests for the weighted PV orchestration and the forecast assembly.

compute_pv_power is parity-proven elsewhere, so here we pin the orchestration:
the kWp-weighted sum, per-orientation GTI selection, weather interpolation, the
watts mapping (pct x pvCalibK x snow), the inverter clip and the daily kWh
integration. Runnable with ``python3 tests/test_forecast.py`` or under pytest.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from custom_components.helios_forecast.forecast import (  # noqa: E402
    build_forecast_series,
    integrate_daily_kwh,
    lerp_finite,
    lerp_plain,
    lerp_rad,
)
from custom_components.helios_forecast.openmeteo import GtiSeries, WeatherSeries  # noqa: E402
from custom_components.helios_forecast.solar.gti import sample_gti  # noqa: E402
from custom_components.helios_forecast.solar.irradiance import (  # noqa: E402
    PanelOrientation,
    PvContext,
    compute_pv_power,
)
from custom_components.helios_forecast.solar.power import (  # noqa: E402
    PvLayout,
    WeatherSample,
    compute_pv_power_weighted,
)

_LAT, _LON = 48.8566, 2.3522
_NOON = datetime(2026, 6, 21, 12, tzinfo=timezone.utc)


def _east_west_layout() -> PvLayout:
    return PvLayout(
        orientations=[PanelOrientation(30, 90), PanelOrientation(30, 270)],
        shares=[0.5, 0.5],
        coords=[None, None],
        total_kwp=6.0,
    )


def test_weighted_equals_share_weighted_sum() -> None:
    sample = WeatherSample(cloud=30, ghi=600, direct=400, diffuse=150, temp=18, wind=3)
    layout = _east_west_layout()
    weighted = compute_pv_power_weighted(_NOON, _LAT, _LON, sample, layout, gti_sampler=None)

    expected = 0.0
    for orientation, share in zip(layout.orientations, layout.shares):
        ctx = PvContext(
            air_temp_c=18, wind_ms=3, ghi_wm2=600, direct_wm2=400, diffuse_wm2=150, poa_wm2=None, shading=False
        )
        expected += compute_pv_power(_NOON, _LAT, _LON, 30, orientation, ctx) * share
    assert abs(weighted - expected) < 1e-12


def test_weighted_uses_gti_per_orientation() -> None:
    sample = WeatherSample(cloud=30, ghi=600, direct=400, diffuse=150, temp=18, wind=3)
    layout = _east_west_layout()
    # GTI only for the east array; west stays on transposition.
    store = {"30|90": GtiSeries(times=[datetime(2026, 6, 21, 12, tzinfo=timezone.utc)], poa=[710.0])}

    def sampler(tilt: float, az: float, m: datetime):
        return sample_gti(store, tilt, az, m)

    weighted = compute_pv_power_weighted(_NOON, _LAT, _LON, sample, layout, gti_sampler=sampler)

    east_ctx = PvContext(
        air_temp_c=18, wind_ms=3, ghi_wm2=600, direct_wm2=400, diffuse_wm2=150, poa_wm2=710.0, shading=False
    )
    west_ctx = PvContext(
        air_temp_c=18, wind_ms=3, ghi_wm2=600, direct_wm2=400, diffuse_wm2=150, poa_wm2=None, shading=False
    )
    expected = (
        compute_pv_power(_NOON, _LAT, _LON, 30, PanelOrientation(30, 90), east_ctx) * 0.5
        + compute_pv_power(_NOON, _LAT, _LON, 30, PanelOrientation(30, 270), west_ctx) * 0.5
    )
    assert abs(weighted - expected) < 1e-12


def test_sample_gti_interpolates_and_guards() -> None:
    store = {
        "30|180": GtiSeries(
            times=[datetime(2026, 6, 21, 12, tzinfo=timezone.utc), datetime(2026, 6, 21, 13, tzinfo=timezone.utc)],
            poa=[600.0, 800.0],
        )
    }
    half = datetime(2026, 6, 21, 12, 30, tzinfo=timezone.utc)
    assert sample_gti(store, 30, 180, half) == 700.0
    assert sample_gti(store, 30, 180, datetime(2026, 6, 21, 12, tzinfo=timezone.utc)) == 600.0
    assert sample_gti(None, 30, 180, half) is None  # no store
    assert sample_gti(store, 30, 90, half) is None  # orientation absent


def test_lerp_helpers() -> None:
    assert lerp_plain(0.0, 10.0, 0.5) == 5.0
    # rad guards the missing / negative sentinel
    assert lerp_rad(100.0, 200.0, 0.5) == 150.0
    assert lerp_rad(None, 200.0, 0.5) == 200.0
    assert lerp_rad(-1.0, 200.0, 0.5) == 200.0
    assert lerp_rad(None, None, 0.5) is None
    # finite guards the missing case
    assert lerp_finite(10.0, 20.0, 0.25) == 12.5
    assert lerp_finite(None, 20.0, 0.5) == 20.0
    assert lerp_finite(None, None, 0.5) is None


def _constant_weather() -> WeatherSeries:
    times = [datetime(2026, 6, 21, 0, tzinfo=timezone.utc) + timedelta(hours=h) for h in range(25)]
    n = len(times)
    return WeatherSeries(
        times=times,
        cloud=[20.0] * n,
        shortwave=[500.0] * n,
        direct=[350.0] * n,
        diffuse=[120.0] * n,
        temp=[18.0] * n,
        wind=[3.0] * n,
        snow=[0.0] * n,
    )


def _single_south_layout() -> PvLayout:
    return PvLayout(orientations=[PanelOrientation(30, 180)], shares=[1.0], coords=[None], total_kwp=5.0)


def test_build_forecast_watts_mapping_and_cap() -> None:
    weather = _constant_weather()
    layout = _single_south_layout()
    start = datetime(2026, 6, 21, 0, tzinfo=timezone.utc)
    end = start + timedelta(days=1)
    cap = 3000.0
    k = layout.total_kwp * 10.0

    points = build_forecast_series(
        weather, None, layout, _LAT, _LON, inverter_max_w=cap, start=start, end=end, step_minutes=60
    )
    assert len(points) == 24
    for p in points:
        sample = WeatherSample(cloud=20, ghi=500, direct=350, diffuse=120, temp=18, wind=3, snow=0)
        pct = compute_pv_power_weighted(p.t, _LAT, _LON, sample, layout, gti_sampler=None)
        expected = min(cap, max(0.0, pct * k))  # snow factor is 1 here
        assert abs(p.pv_w - expected) < 1e-9
        assert p.pv_w == p.pv_raw_w  # ratio 1 in phase 1
        assert p.pv_w <= cap


def test_daily_integration() -> None:
    weather = _constant_weather()
    layout = _single_south_layout()
    start = datetime(2026, 6, 21, 0, tzinfo=timezone.utc)
    end = start + timedelta(days=1)
    points = build_forecast_series(weather, None, layout, _LAT, _LON, start=start, end=end, step_minutes=60)

    totals = integrate_daily_kwh(points, step_minutes=60)
    expected_kwh = sum(p.pv_w for p in points) * (60 / 60.0) / 1000.0
    assert abs(totals["2026-06-21"] - expected_kwh) < 1e-9
    assert expected_kwh > 0


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("all forecast tests passed")
