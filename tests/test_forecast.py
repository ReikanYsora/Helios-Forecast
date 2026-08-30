"""Tests for the weighted PV orchestration and the forecast assembly.

compute_pv_power is parity-proven elsewhere, so here we pin the orchestration:
the kWp-weighted sum, weather interpolation, the watts mapping (pct x pvCalibK x
snow), the inverter clip and the daily kWh integration. Runnable with
``python3 tests/test_forecast.py`` or under pytest.
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
from custom_components.helios_forecast.openmeteo import WeatherSeries  # noqa: E402
from custom_components.helios_forecast.solar.irradiance import (  # noqa: E402
    PanelOrientation,
    PvContext,
    compute_pv_power,
)
from custom_components.helios_forecast.solar.power import (  # noqa: E402
    PvLayout,
    WeatherSample,
    compute_pv_power_per_array,
    compute_pv_power_weighted,
)
from custom_components.helios_forecast.solar.residual import SkyResidualMap  # noqa: E402

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
    # Every array self-transposes the same GHI to its own plane; the weighted total is the kWp-share sum.
    sample = WeatherSample(cloud=30, ghi=600, direct=400, diffuse=150, temp=18, wind=3)
    layout = _east_west_layout()
    weighted = compute_pv_power_weighted(_NOON, _LAT, _LON, sample, layout)

    expected = 0.0
    for orientation, share in zip(layout.orientations, layout.shares):
        ctx = PvContext(air_temp_c=18, wind_ms=3 / 3.6, ghi_wm2=600, direct_wm2=400, diffuse_wm2=150)
        expected += compute_pv_power(_NOON, _LAT, _LON, 30, orientation, ctx) * share
    assert abs(weighted - expected) < 1e-12


def test_wind_is_converted_from_kmh_to_ms() -> None:
    layout = _single_south_layout()
    panel = layout.orientations[0]
    sample = WeatherSample(cloud=0, ghi=900, direct=750, diffuse=150, temp=25, wind=18.0)
    got = compute_pv_power_weighted(_NOON, _LAT, _LON, sample, layout)

    def with_wind(wind_ms: float) -> float:
        ctx = PvContext(air_temp_c=25, wind_ms=wind_ms, ghi_wm2=900, direct_wm2=750, diffuse_wm2=150)
        return compute_pv_power(_NOON, _LAT, _LON, 0, panel, ctx)

    assert abs(got - with_wind(18.0 / 3.6)) < 1e-12
    assert abs(got - with_wind(18.0)) > 1e-6


def test_wind_conversion_tolerates_missing_wind() -> None:
    sample = WeatherSample(cloud=0, ghi=900, direct=750, diffuse=150, temp=25, wind=None)
    assert compute_pv_power_weighted(_NOON, _LAT, _LON, sample, _single_south_layout()) > 0


def test_lerp_helpers() -> None:
    assert lerp_plain(0.0, 10.0, 0.5) == 5.0
    # rad guards the missing / negative sentinel, on either side of the pair
    assert lerp_rad(100.0, 200.0, 0.5) == 150.0
    assert lerp_rad(None, 200.0, 0.5) == 200.0
    assert lerp_rad(-1.0, 200.0, 0.5) == 200.0
    assert lerp_rad(100.0, None, 0.5) == 100.0
    assert lerp_rad(100.0, -1.0, 0.5) == 100.0
    assert lerp_rad(None, None, 0.5) is None
    # finite guards the missing case, on either side of the pair
    assert lerp_finite(10.0, 20.0, 0.25) == 12.5
    assert lerp_finite(None, 20.0, 0.5) == 20.0
    assert lerp_finite(10.0, None, 0.5) == 10.0
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
        weather, layout, _LAT, _LON, inverter_max_w=cap, start=start, end=end, step_minutes=60
    )
    assert len(points) == 24
    for p in points:
        sample = WeatherSample(cloud=20, ghi=500, direct=350, diffuse=120, temp=18, wind=3, snow=0)
        pct = compute_pv_power_weighted(p.t, _LAT, _LON, sample, layout)
        expected = min(cap, max(0.0, pct * k))  # snow factor is 1 here
        assert abs(p.pv_w - expected) < 1e-9
        assert p.pv_w == p.pv_raw_w  # ratio 1 in phase 1
        assert p.pv_w <= cap


def test_per_line_inverter_cap_clips_each_array_before_summing() -> None:
    # East string capped tight, west string uncapped: the east micro-inverter saturates on its own, and the
    # forecast must clip it BEFORE summing, not the combined total. Two 0.5-share arrays over 6 kWp -> 3 kWp each.
    east_cap = 800.0
    layout = PvLayout(
        orientations=[PanelOrientation(30, 90), PanelOrientation(30, 270)],
        shares=[0.5, 0.5],
        coords=[None, None],
        total_kwp=6.0,
        caps=[east_cap, float("inf")],
    )
    weather = _constant_weather()
    start = datetime(2026, 6, 21, 0, tzinfo=timezone.utc)
    end = start + timedelta(days=1)
    points = build_forecast_series(  # no entry-level cap, so only the per-line cap can bite
        weather, layout, _LAT, _LON, start=start, end=end, step_minutes=60
    )
    assert len(points) == 24
    saw_clip = False
    for p in points:
        sample = WeatherSample(cloud=20, ghi=500, direct=350, diffuse=120, temp=18, wind=3, snow=0)
        pcts = compute_pv_power_per_array(p.t, _LAT, _LON, sample, layout)
        watts = [pcts[i] * layout.shares[i] * layout.total_kwp * 10.0 for i in range(len(pcts))]  # snow factor 1
        expected = min(east_cap, max(0.0, watts[0])) + max(0.0, watts[1])
        assert abs(p.pv_w - expected) < 1e-9
        if watts[0] > east_cap:
            saw_clip = True
    assert saw_clip  # the per-line cap actually bit at some hour of the day


def test_daily_integration() -> None:
    weather = _constant_weather()
    layout = _single_south_layout()
    start = datetime(2026, 6, 21, 0, tzinfo=timezone.utc)
    end = start + timedelta(days=1)
    points = build_forecast_series(weather, layout, _LAT, _LON, start=start, end=end, step_minutes=60)

    totals = integrate_daily_kwh(points, step_minutes=60)
    expected_kwh = sum(p.pv_w for p in points) * (60 / 60.0) / 1000.0
    assert abs(totals["2026-06-21"] - expected_kwh) < 1e-9
    assert expected_kwh > 0


def test_daily_integration_buckets_by_the_given_day_tz() -> None:
    # A bucket at 22:00 UTC falls on the next local day at UTC+3: the day split must follow
    # day_tz, not the UTC calendar date, or a coordinator in that zone would misfile production.
    from datetime import timezone as _tz

    plus3 = _tz(timedelta(hours=3))
    weather = _constant_weather()
    layout = _single_south_layout()
    start = datetime(2026, 6, 21, 20, tzinfo=timezone.utc)
    end = start + timedelta(hours=4)
    points = build_forecast_series(weather, layout, _LAT, _LON, start=start, end=end, step_minutes=60)

    totals_utc = integrate_daily_kwh(points, step_minutes=60)
    totals_local = integrate_daily_kwh(points, step_minutes=60, day_tz=plus3)

    assert set(totals_utc.keys()) == {"2026-06-21"}
    # 20:00-22:00 UTC is still 2026-06-21 at +3, 23:00 UTC is already 2026-06-22 at +3.
    assert set(totals_local.keys()) == {"2026-06-21", "2026-06-22"}
    assert abs(sum(totals_local.values()) - sum(totals_utc.values())) < 1e-9


def test_build_forecast_series_empty_when_no_weather_or_no_span() -> None:
    layout = _single_south_layout()
    start = datetime(2026, 6, 21, 0, tzinfo=timezone.utc)
    empty_weather = WeatherSeries(times=[], cloud=[], shortwave=[], direct=[], diffuse=[], temp=[], wind=[], snow=[])
    assert build_forecast_series(empty_weather, layout, _LAT, _LON, start=start, end=start + timedelta(hours=1)) == []

    weather = _constant_weather()
    # end <= start: no bucket should be produced regardless of available weather.
    assert build_forecast_series(weather, layout, _LAT, _LON, start=start, end=start) == []


def test_orientation_less_layout_uses_single_element_fallback() -> None:
    # No per-array orientations configured: compute_pv_power_per_array falls back to a single
    # horizontal percentage, and _sum_arrays must take its own matching fallback branch
    # (pcts[0] * total_kwp), not try to zip it against a per-array shares/caps list.
    layout = PvLayout(orientations=[], shares=[], coords=[], total_kwp=5.0)
    weather = _constant_weather()
    start = datetime(2026, 6, 21, 0, tzinfo=timezone.utc)
    end = start + timedelta(hours=1)

    points = build_forecast_series(weather, layout, _LAT, _LON, start=start, end=end, step_minutes=60)
    assert len(points) == 1
    p = points[0]
    sample = WeatherSample(cloud=20, ghi=500, direct=350, diffuse=120, temp=18, wind=3, snow=0)
    pcts = compute_pv_power_per_array(p.t, _LAT, _LON, sample, layout)
    assert len(pcts) == 1
    expected = max(0.0, pcts[0] * layout.total_kwp * 10.0)
    assert abs(p.pv_w - expected) < 1e-9


def test_residual_map_scales_pv_w_but_not_pv_raw_w() -> None:
    # Flat ratio-2 sky map (single cell): during daytime the learned correction should
    # double pv_w relative to the untouched physical model, pv_raw_w must stay the pure model.
    residual_map = SkyResidualMap(
        n_az=1, n_alt=1, m=[2.0], conf=[1.0], global_ratio=2.0, total_weight=10.0, visited_cells=1
    )
    layout = _single_south_layout()
    weather = _constant_weather()
    start = datetime(2026, 6, 21, 10, tzinfo=timezone.utc)
    end = start + timedelta(hours=1)

    points = build_forecast_series(
        weather, layout, _LAT, _LON, start=start, end=end, step_minutes=60, residual_map=residual_map
    )
    assert len(points) == 1
    p = points[0]
    assert p.pv_raw_w > 0.0  # daytime bucket, physical model produces power
    assert abs(p.pv_w - 2.0 * p.pv_raw_w) < 1e-6


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("all forecast tests passed")
