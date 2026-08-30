"""Tests for the learned sky-residual correction.

sample_sky_residual is golden-tested against the card's sampleSkyResidual (same
constructed map, same queries). build_sky_residual_map is checked behaviourally:
ratio = production / model, the clamp, the too-little-history None, and that the
forecast assembly applies the ratio.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO_ROOT))

from custom_components.helios_forecast.forecast import build_forecast_series  # noqa: E402
from custom_components.helios_forecast.openmeteo import WeatherSeries  # noqa: E402
from custom_components.helios_forecast.solar.irradiance import PanelOrientation, snow_cover_factor  # noqa: E402
from custom_components.helios_forecast.solar.power import (  # noqa: E402
    PvLayout,
    WeatherSample,
    compute_pv_power_per_array,
    compute_pv_power_weighted,
)
from custom_components.helios_forecast.solar.residual import (  # noqa: E402
    LEARN_SUBSAMPLES,
    M_MAX,
    ProductionBucket,
    SkyResidualInput,
    SkyResidualMap,
    _capped_model_kwh,
    _dt,
    _nearest_cloud_idx,
    build_sky_residual_map,
    sample_sky_residual,
)

_LAT, _LON = 48.8566, 2.3522


# --- golden: sample_sky_residual ------------------------------------------------


def test_sample_matches_card() -> None:
    data = json.loads((_REPO_ROOT / "tests" / "fixtures" / "sky_sample.json").read_text())
    mp = data["map"]
    sky_map = SkyResidualMap(
        n_az=mp["nAz"],
        n_alt=mp["nAlt"],
        m=mp["m"],
        conf=mp["conf"],
        global_ratio=mp["globalRatio"],
        total_weight=0.0,
        visited_cells=0,
    )
    for q in data["queries"]:
        got = sample_sky_residual(sky_map, q["az"], q["alt"])
        assert abs(got - q["expected"]) < 1e-9, q


# --- helpers --------------------------------------------------------------------


def test_nearest_cloud_idx() -> None:
    times = [0.0, 10.0, 20.0, 30.0]
    assert _nearest_cloud_idx(times, 11.0) == 1
    assert _nearest_cloud_idx(times, 16.0) == 2
    assert _nearest_cloud_idx(times, -5.0) == 0
    assert _nearest_cloud_idx([], 5.0) == -1


# --- build scaffolding ----------------------------------------------------------


def _layout() -> PvLayout:
    return PvLayout(orientations=[PanelOrientation(30, 180)], shares=[1.0], coords=[None], total_kwp=5.0)


def _midday_buckets() -> list[ProductionBucket]:
    buckets = []
    for day in (21, 22):
        for hour in range(9, 16):
            start = datetime(2026, 6, day, hour, tzinfo=timezone.utc).timestamp() * 1000.0
            buckets.append(ProductionBucket(start_ms=start, end_ms=start + 3_600_000, kwh=0.0))
    return buckets


def _constant_weather(n: int) -> dict:
    return {
        "cloud": [10.0] * n,
        "shortwave": [600.0] * n,
        "direct": [450.0] * n,
        "diffuse": [120.0] * n,
        "temp": [20.0] * n,
        "wind": [2.0] * n,
        "snow": [0.0] * n,
    }


def _model_kwh(bucket: ProductionBucket, inp: SkyResidualInput) -> float:
    """Replicate the build's per-bucket model so we can set production to a known ratio."""
    k = inp.layout.total_kwp * 10.0
    mid = (bucket.start_ms + bucket.end_ms) / 2
    ci = _nearest_cloud_idx(inp.cloud_times, mid)
    sample = WeatherSample(
        cloud=inp.cloud[ci],
        ghi=inp.shortwave[ci],
        direct=inp.direct[ci],
        diffuse=inp.diffuse[ci],
        temp=inp.temp[ci],
        wind=inp.wind[ci],
    )
    w_sum, w_n = 0.0, 0
    for s in range(LEARN_SUBSAMPLES):
        sub_t = bucket.start_ms + (s + 0.5) * (bucket.end_ms - bucket.start_ms) / LEARN_SUBSAMPLES
        moment = _dt(sub_t)
        w_sum += compute_pv_power_weighted(moment, inp.lat, inp.lon, sample, inp.layout)
        w_n += 1
    return (w_sum / w_n) * k * snow_cover_factor(inp.snow[ci], inp.temp[ci]) / 1000.0


def _input(buckets: list[ProductionBucket], **over) -> SkyResidualInput:
    times = [b.start_ms for b in buckets]
    w = _constant_weather(len(times))
    base = dict(
        lat=_LAT,
        lon=_LON,
        layout=_layout(),
        production=buckets,
        cloud_times=times,
        cloud=w["cloud"],
        shortwave=w["shortwave"],
        direct=w["direct"],
        diffuse=w["diffuse"],
        temp=w["temp"],
        wind=w["wind"],
        snow=w["snow"],
        now_ms=datetime(2026, 6, 23, tzinfo=timezone.utc).timestamp() * 1000.0,
    )
    base.update(over)
    return SkyResidualInput(**base)


# --- build behaviour ------------------------------------------------------------


def test_build_none_cases() -> None:
    assert build_sky_residual_map(_input([])) is None  # no production
    buckets = _midday_buckets()
    assert build_sky_residual_map(_input(buckets, layout=PvLayout([], [], [], 0.0))) is None  # no kWp


def test_ratio_one_when_production_matches_model() -> None:
    buckets = _midday_buckets()
    inp0 = _input(buckets)
    matched = [ProductionBucket(b.start_ms, b.end_ms, _model_kwh(b, inp0)) for b in buckets]
    sky_map = build_sky_residual_map(_input(matched))
    assert sky_map is not None
    assert abs(sky_map.global_ratio - 1.0) < 1e-9


def test_ratio_clamped_high() -> None:
    buckets = _midday_buckets()
    inp0 = _input(buckets)
    over = [ProductionBucket(b.start_ms, b.end_ms, 5.0 * _model_kwh(b, inp0)) for b in buckets]
    sky_map = build_sky_residual_map(_input(over))
    assert sky_map is not None
    assert sky_map.global_ratio == M_MAX  # 5x clamps to the ceiling


def _capped_layout() -> PvLayout:
    # Same 5 kWp single-array layout as _layout(), plus a per-array inverter cap tight enough that
    # a clear midday hour saturates it (5 kWp at full pct -> up to 5000 W; the cap is 2000 W).
    return PvLayout(orientations=[PanelOrientation(30, 180)], shares=[1.0], coords=[None], total_kwp=5.0, caps=[2000.0])


def _theoretical_model_kwh(bucket: ProductionBucket, inp: SkyResidualInput) -> float:
    """Model kWh ignoring any per-array cap (what an uncapped panel would theoretically produce)."""
    k = inp.layout.total_kwp * 10.0
    mid = (bucket.start_ms + bucket.end_ms) / 2
    ci = _nearest_cloud_idx(inp.cloud_times, mid)
    sample = WeatherSample(
        cloud=inp.cloud[ci],
        ghi=inp.shortwave[ci],
        direct=inp.direct[ci],
        diffuse=inp.diffuse[ci],
        temp=inp.temp[ci],
        wind=inp.wind[ci],
    )
    w_sum, w_n = 0.0, 0
    for s in range(LEARN_SUBSAMPLES):
        sub_t = bucket.start_ms + (s + 0.5) * (bucket.end_ms - bucket.start_ms) / LEARN_SUBSAMPLES
        moment = _dt(sub_t)
        w_sum += compute_pv_power_weighted(moment, inp.lat, inp.lon, sample, inp.layout)
        w_n += 1
    return (w_sum / w_n) * k * snow_cover_factor(inp.snow[ci], inp.temp[ci]) / 1000.0


def _capped_bucket_model_kwh(bucket: ProductionBucket, inp: SkyResidualInput) -> float:
    """Model kWh with the layout's per-array cap applied, the way build_sky_residual_map computes it."""
    k = inp.layout.total_kwp * 10.0
    mid = (bucket.start_ms + bucket.end_ms) / 2
    ci = _nearest_cloud_idx(inp.cloud_times, mid)
    sample = WeatherSample(
        cloud=inp.cloud[ci],
        ghi=inp.shortwave[ci],
        direct=inp.direct[ci],
        diffuse=inp.diffuse[ci],
        temp=inp.temp[ci],
        wind=inp.wind[ci],
    )
    snow_factor = snow_cover_factor(inp.snow[ci], inp.temp[ci])
    w_sum_kwh, w_n = 0.0, 0
    for s in range(LEARN_SUBSAMPLES):
        sub_t = bucket.start_ms + (s + 0.5) * (bucket.end_ms - bucket.start_ms) / LEARN_SUBSAMPLES
        moment = _dt(sub_t)
        pcts = compute_pv_power_per_array(moment, inp.lat, inp.lon, sample, inp.layout)
        w_sum_kwh += _capped_model_kwh(pcts, inp.layout, k, snow_factor)
        w_n += 1
    return w_sum_kwh / w_n


def test_build_applies_per_array_cap_to_the_model() -> None:
    # Production set to the CAPPED model kWh (what a real, cap-limited install actually harvests) must
    # learn ratio 1, not a ratio that conflates the hardware clip with weather bias. Before the fix the
    # build used the uncapped theoretical model, which is strictly higher whenever the cap bites, so the
    # learned ratio would come out under 1 even though production exactly matches the physically capped
    # model.
    buckets = _midday_buckets()
    inp0 = _input(buckets, layout=_capped_layout())
    theoretical = [_theoretical_model_kwh(b, inp0) for b in buckets]
    capped = [_capped_bucket_model_kwh(b, inp0) for b in buckets]
    assert any(c < t - 1e-9 for c, t in zip(capped, theoretical))  # the cap actually bites at some hour

    matched = [ProductionBucket(b.start_ms, b.end_ms, c) for b, c in zip(buckets, capped)]
    sky_map = build_sky_residual_map(_input(matched, layout=_capped_layout()))
    assert sky_map is not None
    assert abs(sky_map.global_ratio - 1.0) < 1e-9


def test_build_survives_missing_cloud_hours() -> None:
    # Regression for issue #14: Open-Meteo can leave a cloud hour as None, and _clamp_pct used to
    # crash on it (math.isfinite(None) -> TypeError). The build must instead treat the gap as clear
    # (0 %) and still produce a map.
    buckets = _midday_buckets()
    inp0 = _input(buckets)
    matched = [ProductionBucket(b.start_ms, b.end_ms, _model_kwh(b, inp0)) for b in buckets]
    cloud_with_gaps = [None if i % 3 == 0 else 10.0 for i in range(len(matched))]
    sky_map = build_sky_residual_map(_input(matched, cloud=cloud_with_gaps))
    assert sky_map is not None


def test_forecast_applies_ratio() -> None:
    # A flat map returning 0.5 everywhere halves the corrected curve, raw untouched.
    n = 36 * 18
    half = SkyResidualMap(
        n_az=36, n_alt=18, m=[0.5] * n, conf=[1.0] * n, global_ratio=0.5, total_weight=99, visited_cells=n
    )
    base = datetime(2026, 6, 21, tzinfo=timezone.utc)
    weather = WeatherSeries(
        times=[base + timedelta(hours=h) for h in range(25)],
        cloud=[20.0] * 25,
        shortwave=[500.0] * 25,
        direct=[350.0] * 25,
        diffuse=[120.0] * 25,
        temp=[18.0] * 25,
        wind=[3.0] * 25,
        snow=[0.0] * 25,
    )
    start = datetime(2026, 6, 21, tzinfo=timezone.utc)
    end = datetime(2026, 6, 21, 23, 59, tzinfo=timezone.utc)
    pts = build_forecast_series(
        weather, _layout(), _LAT, _LON, start=start, end=end, step_minutes=60, residual_map=half
    )
    for p in pts:
        assert abs(p.pv_w - 0.5 * p.pv_raw_w) < 1e-9


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("all residual tests passed")
