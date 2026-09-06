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
    _sample_series,
    _weighted_percentiles,
    build_library,
    enrich_archive_points,
    enrich_points,
    predict,
    series_epochs,
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


def test_weighted_percentiles_empty_list_does_not_raise() -> None:
    assert _weighted_percentiles([], (0.10, 0.50, 0.90)) == []


def test_weighted_percentiles_monotonic() -> None:
    pairs = [(v, 1.0) for v in [0, 10, 20, 30, 40, 50, 60, 70, 80, 90, 100]]
    p10, p50, p90 = _weighted_percentiles(pairs, (0.10, 0.50, 0.90))
    assert p10 <= p50 <= p90
    assert p10 <= 20 and p90 >= 80


def test_predict_median_and_confidence() -> None:
    # Tight cluster of analogs at one condition, all producing ~2000 W. Temperature matches the
    # query on both sides so the missing-data penalty does not enter into it.
    lib = [AnalogSample(alt=40.0, az=180.0, cloud=50.0, watt=2000.0 + (i % 5) * 10, temp=20.0) for i in range(40)]
    band = predict(lib, 40.0, 180.0, 50.0, temp=20.0)
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
    # No query temperature: every analog takes the same missing-data penalty, so the match
    # falls back to geometry+cloud alone (still a valid band, just untied to either regime).
    band_none = predict(lib, 40.0, 180.0, 30.0)
    assert band_none is not None


def test_missing_temperature_is_penalised_not_a_perfect_match() -> None:
    # Three equally-sized groups at identical geometry+cloud (so distance is temperature-only),
    # tagged with distinct watts so the winning group is visible in the percentiles: an exact
    # temperature match, a small real mismatch, and no temperature reading at all.
    exact = [AnalogSample(alt=40.0, az=180.0, cloud=30.0, watt=1000.0, temp=20.0) for _ in range(10)]
    small_mismatch = [AnalogSample(alt=40.0, az=180.0, cloud=30.0, watt=2000.0, temp=21.0) for _ in range(10)]
    no_temp = [AnalogSample(alt=40.0, az=180.0, cloud=30.0, watt=3000.0, temp=None) for _ in range(10)]
    lib = exact + small_mismatch + no_temp
    band = predict(lib, 40.0, 180.0, 30.0, temp=20.0)
    assert band is not None
    # Ranking by distance (closest wins the lowest quantiles first): exact is closest (wins p10),
    # small mismatch is second (wins p50 and p90 too). No-temperature-data never wins a quantile
    # here: it is ranked last, not tied with the exact match as it was before the fix (where it
    # used to pull p90 all the way to its own 3000 W).
    assert band.p10 == 1000.0
    assert band.p50 == 2000.0
    assert band.p90 == 2000.0


def test_sample_series_clamps_outside_range() -> None:
    times = [datetime(2026, 1, 1, h, tzinfo=UTC) for h in range(3)]
    values = [10.0, 20.0, 30.0]
    epochs = series_epochs(times)
    assert _sample_series(times, values, epochs[0] - 1000.0, epochs) == 10.0
    assert _sample_series(times, values, epochs[-1] + 1000.0, epochs) == 30.0


def test_sample_series_interpolates_between_brackets() -> None:
    times = [datetime(2026, 1, 1, h, tzinfo=UTC) for h in range(3)]
    values = [10.0, 20.0, 30.0]
    epochs = series_epochs(times)
    mid = (epochs[0] + epochs[1]) / 2.0
    assert _sample_series(times, values, mid, epochs) == 15.0
    # epochs argument is optional: recomputing internally must agree.
    assert _sample_series(times, values, mid) == 15.0


def test_sample_series_skips_missing_bracket_side() -> None:
    times = [datetime(2026, 1, 1, h, tzinfo=UTC) for h in range(3)]
    values = [10.0, None, 30.0]
    epochs = series_epochs(times)
    # Querying inside [hour0, hour1] where hour1 is missing falls back to hour0's value.
    just_after_0 = epochs[0] + 1.0
    assert _sample_series(times, values, just_after_0, epochs) == 10.0
    # Querying inside [hour1, hour2] where hour1 is missing falls back to hour2's value.
    just_before_2 = epochs[2] - 1.0
    assert _sample_series(times, values, just_before_2, epochs) == 30.0


def test_sample_series_empty_series_returns_none() -> None:
    assert _sample_series([], [], 0.0) is None


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


def test_build_library_drops_curtailed_hours() -> None:
    lat, lon = 45.0, 0.0
    noon = _june_noon(12)
    one = _june_noon(13)
    prod = [
        _Bucket(noon.timestamp() * 1000.0, (noon + timedelta(hours=1)).timestamp() * 1000.0, 3.0),
        _Bucket(one.timestamp() * 1000.0, (one + timedelta(hours=1)).timestamp() * 1000.0, 1.2),
    ]
    prod[1].curtailed = True
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
    assert [s.watt for s in lib] == [3000.0]


def test_enrich_points_past_untouched_future_blended() -> None:
    lat, lon = 45.0, 0.0
    now = _june_noon(12)
    past = ForecastPoint(t=_june_noon(8), pv_w=1000.0, pv_raw_w=1000.0)
    fut = ForecastPoint(t=_june_noon(13), pv_w=1000.0, pv_raw_w=1000.0)

    # Seed the library at the future point's exact sun position so analogs are close. Temperature
    # matches the weather series below so the missing-data penalty does not enter into it.
    sun = sun_position(fut.t, lat, lon)
    lib = [AnalogSample(alt=sun.altitude, az=sun.azimuth, cloud=30.0, watt=2500.0, temp=20.0) for _ in range(40)]
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


def test_enrich_points_future_night_gets_zero_band() -> None:
    """Below the horizon the output isn't uncertain, it's known: 0 W either side, not unknown."""
    lat, lon = 45.0, 0.0
    now = _june_noon(12)
    night = ForecastPoint(t=_june_noon(1) + timedelta(days=1), pv_w=0.0, pv_raw_w=0.0)

    lib = [AnalogSample(alt=10.0, az=180.0, cloud=30.0, watt=1000.0, temp=20.0)]
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
    out = enrich_points([night], lib, weather, lat, lon, now)
    assert out[0].pv_p10 == 0.0
    assert out[0].pv_p90 == 0.0
    assert out[0].pv_w == night.pv_w  # only the band is set, the physical model's power is untouched


def _flat_weather() -> WeatherSeries:
    times = [_june_noon(h) for h in range(24)]
    return WeatherSeries(
        times=times,
        cloud=[30.0] * 24,
        shortwave=[0.0] * 24,
        direct=[0.0] * 24,
        diffuse=[0.0] * 24,
        temp=[20.0] * 24,
        wind=[5.0] * 24,
        snow=[0.0] * 24,
    )


def test_ceiling_caps_overprediction() -> None:
    # A shaded site: the physical model predicts far more than the site ever produces at this sun
    # position. With enough close analogs, the learned ceiling caps the forecast to p90 * margin (#28).
    lat, lon = 45.0, 0.0
    now = _june_noon(12)
    fut = ForecastPoint(t=_june_noon(13), pv_w=6500.0, pv_raw_w=6500.0)  # physical over-predicts
    sun = sun_position(fut.t, lat, lon)
    # Temperature matches _flat_weather()'s 20.0 so the missing-data penalty does not enter into it.
    lib = [AnalogSample(alt=sun.altitude, az=sun.azimuth, cloud=30.0, watt=4500.0, temp=20.0) for _ in range(10)]
    out = enrich_points([fut], lib, _flat_weather(), lat, lon, now)
    assert out[0].pv_w <= 4500.0 * 1.25 + 1e-6  # capped at p90 * margin, well below the physical 6500
    assert out[0].pv_w < 6500.0


def test_enrich_archive_points_caps_a_past_point_enrich_points_would_leave_untouched() -> None:
    # Same over-prediction as test_ceiling_caps_overprediction, but on a point that is already in
    # the past relative to any "now" - enrich_points() would leave it as the raw physical model by
    # design (see test_enrich_points_past_untouched_future_blended). The archive has no future side
    # to gate on, so every one of its points needs this same ceiling clamp, not just future ones (#52).
    lat, lon = 45.0, 0.0
    past = ForecastPoint(t=_june_noon(13), pv_w=6500.0, pv_raw_w=6500.0)  # physical over-predicts
    sun = sun_position(past.t, lat, lon)
    lib = [AnalogSample(alt=sun.altitude, az=sun.azimuth, cloud=30.0, watt=4500.0, temp=20.0) for _ in range(10)]

    # enrich_points, called with "now" after the point, leaves it exactly as the raw model (the
    # bug: this is what the archive pipeline used to do for every single one of its points).
    untouched = enrich_points([past], lib, _flat_weather(), lat, lon, now=_june_noon(23))
    assert untouched[0].pv_w == 6500.0

    # enrich_archive_points has no "now" to gate on and clamps it like enrich_points does for a
    # future point.
    out = enrich_archive_points([past], lib, _flat_weather(), lat, lon)
    assert out[0].pv_w <= 4500.0 * 1.25 + 1e-6
    assert out[0].pv_w < 6500.0


def test_enrich_archive_points_empty_library_is_a_noop() -> None:
    past = ForecastPoint(t=_june_noon(13), pv_w=1000.0, pv_raw_w=1000.0)
    out = enrich_archive_points([past], [], _flat_weather(), 45.0, 0.0)
    assert out == [past]


def test_ceiling_skipped_when_analogs_thin() -> None:
    # Too few close analogs to trust a ceiling (cold start): no cap, the forecast stays near physical.
    lat, lon = 45.0, 0.0
    now = _june_noon(12)
    fut = ForecastPoint(t=_june_noon(13), pv_w=6500.0, pv_raw_w=6500.0)
    sun = sun_position(fut.t, lat, lon)
    lib = [AnalogSample(alt=sun.altitude, az=sun.azimuth, cloud=30.0, watt=4500.0) for _ in range(3)]
    out = enrich_points([fut], lib, _flat_weather(), lat, lon, now)
    assert out[0].pv_w > 4500.0 * 1.25  # no ceiling applied with thin support


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("all analog tests passed")


# --- ratio analogs: the analog's word is a ratio to the physics, applied to today's physics ----------


def _weather_flat(cloud: float = 20.0, ghi: float = 600.0) -> WeatherSeries:
    times = [_june_noon(h) for h in range(24)]
    return WeatherSeries(
        times=times,
        cloud=[cloud] * 24,
        shortwave=[ghi] * 24,
        direct=[ghi * 0.7] * 24,
        diffuse=[ghi * 0.3] * 24,
        temp=[20.0] * 24,
        wind=[5.0] * 24,
        snow=[0.0] * 24,
    )


def _layout(kwp: float = 3.0):
    from custom_components.helios_forecast.solar.irradiance import PanelOrientation
    from custom_components.helios_forecast.solar.power import PvLayout

    return PvLayout(
        orientations=[PanelOrientation(tilt_deg=30.0, azimuth_deg=180.0, tracker=None)],
        shares=[1.0],
        coords=[None],
        total_kwp=kwp,
        caps=[],
    )


def test_build_library_carries_a_ratio_when_the_layout_is_known() -> None:
    lat, lon = 45.0, 0.0
    noon = _june_noon(12)
    prod = [_Bucket(noon.timestamp() * 1000.0, (noon + timedelta(hours=1)).timestamp() * 1000.0, 1.5)]
    lib = build_library(prod, _weather_flat(), lat, lon, _layout(), 3200.0)
    assert len(lib) == 1
    assert lib[0].watt == 1500.0
    assert lib[0].ratio is not None and 0.2 < lib[0].ratio < 2.0
    # Without a layout there is no model to divide by: watts only.
    assert build_library(prod, _weather_flat(), lat, lon)[0].ratio is None
    # A model too small to divide by (no irradiance) leaves the sample as watts.
    assert build_library(prod, _weather_flat(ghi=0.0), lat, lon, _layout(), 3200.0)[0].ratio is None


def test_ratio_samples_need_a_spread() -> None:
    from custom_components.helios_forecast.analog import ratio_samples

    few = [AnalogSample(40.0, 180.0, 20.0, 1000.0, 20.0, ratio=0.8) for _ in range(10)]
    assert ratio_samples(few) == []
    many = few * 3
    assert len(ratio_samples(many)) == 30
    mixed = many + [AnalogSample(40.0, 180.0, 20.0, 1000.0, 20.0)]
    assert len(ratio_samples(mixed)) == 30


def test_predict_on_ratio_reads_the_ratio_column() -> None:
    lib = [AnalogSample(40.0, 180.0, 50.0, 2000.0 + i, 20.0, ratio=0.7 + (i % 5) * 0.01) for i in range(40)]
    band = predict(lib, 40.0, 180.0, 50.0, temp=20.0, on_ratio=True)
    assert band is not None
    assert 0.69 <= band.p50 <= 0.75
    assert band.confidence > 0.9


def test_ratio_analogs_follow_todays_physics_not_yesterdays_watts() -> None:
    """The mechanism the fleet showed on clear mornings: analogs at the same altitude but a more
    northerly azimuth made less. Read as watts they pull the forecast down; read as ratios (the site
    made 90 % of its physics) they leave today's geometry to the physics."""
    lat, lon = 45.0, 0.0
    now = _june_noon(6)
    fut = ForecastPoint(t=_june_noon(9), pv_w=2000.0, pv_raw_w=2000.0)
    sun = sun_position(fut.t, lat, lon)
    lib = [
        AnalogSample(alt=sun.altitude, az=sun.azimuth, cloud=30.0, watt=1000.0, temp=20.0, ratio=0.9) for _ in range(40)
    ]
    weather = _weather_flat(cloud=30.0)
    out = enrich_points([fut], lib, weather, lat, lon, now)[0]
    # 0.9 x 2000 W of physics, not the 1000 W those analogs made under their own geometry.
    assert abs(out.pv_w - 1800.0) < 60.0
    assert out.pv_p10 is not None and abs(out.pv_p10 - 1800.0) < 60.0
    # The same library without ratios is read as watts and drags the point toward 1000 W.
    watts_only = [AnalogSample(alt=s.alt, az=s.az, cloud=s.cloud, watt=s.watt, temp=s.temp) for s in lib]
    assert enrich_points([fut], watts_only, weather, lat, lon, now)[0].pv_w < 1200.0
