"""Tests for the learned sky-residual correction.

sample_sky_residual is golden-tested against the card's sampleSkyResidual (same
constructed map, same queries). build_sky_residual_map is checked behaviourally:
ratio = production / model, the clamp, the inverter-cutoff guard, the
too-little-history None, and that the forecast assembly applies the ratio.
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
from custom_components.helios_forecast.solar.gti import sample_gti  # noqa: E402
from custom_components.helios_forecast.solar.irradiance import PanelOrientation, snow_cover_factor  # noqa: E402
from custom_components.helios_forecast.solar.power import PvLayout, WeatherSample, compute_pv_power_weighted  # noqa: E402
from custom_components.helios_forecast.solar.residual import (  # noqa: E402
    LEARN_SUBSAMPLES,
    M_MAX,
    ProductionBucket,
    SkyResidualInput,
    SkyResidualMap,
    SocBucket,
    _dt,
    _nearest_cloud_idx,
    _soc_at_ms,
    build_sky_residual_map,
    sample_sky_residual,
)

_LAT, _LON = 48.8566, 2.3522


# --- golden: sample_sky_residual ------------------------------------------------

def test_sample_matches_card() -> None:
    data = json.loads((_REPO_ROOT / "tests" / "fixtures" / "sky_sample.json").read_text())
    mp = data["map"]
    sky_map = SkyResidualMap(
        n_az=mp["nAz"], n_alt=mp["nAlt"], m=mp["m"], conf=mp["conf"],
        global_ratio=mp["globalRatio"], total_weight=0.0, visited_cells=0,
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


def test_soc_at_ms() -> None:
    series = [SocBucket(0, 10, 50.0), SocBucket(10, 20, 90.0)]
    assert _soc_at_ms(series, 5) == 50.0       # inside first bucket
    assert _soc_at_ms(series, 15) == 90.0      # inside second
    assert _soc_at_ms(None, 5) is None
    assert _soc_at_ms([], 5) is None


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
        "cloud": [10.0] * n, "shortwave": [600.0] * n, "direct": [450.0] * n,
        "diffuse": [120.0] * n, "temp": [20.0] * n, "wind": [2.0] * n, "snow": [0.0] * n,
    }


def _model_kwh(bucket: ProductionBucket, inp: SkyResidualInput) -> float:
    """Replicate the build's per-bucket model so we can set production to a known ratio."""
    k = inp.layout.total_kwp * 10.0
    mid = (bucket.start_ms + bucket.end_ms) / 2
    ci = _nearest_cloud_idx(inp.cloud_times, mid)
    sample = WeatherSample(
        cloud=inp.cloud[ci], ghi=inp.shortwave[ci], direct=inp.direct[ci],
        diffuse=inp.diffuse[ci], temp=inp.temp[ci], wind=inp.wind[ci],
    )
    w_sum, w_n = 0.0, 0
    for s in range(LEARN_SUBSAMPLES):
        sub_t = bucket.start_ms + (s + 0.5) * (bucket.end_ms - bucket.start_ms) / LEARN_SUBSAMPLES
        moment = _dt(sub_t)
        w_sum += compute_pv_power_weighted(moment, inp.lat, inp.lon, sample, inp.layout, None)
        w_n += 1
    return (w_sum / w_n) * k * snow_cover_factor(inp.snow[ci], inp.temp[ci]) / 1000.0


def _input(buckets: list[ProductionBucket], **over) -> SkyResidualInput:
    times = [b.start_ms for b in buckets]
    w = _constant_weather(len(times))
    base = dict(
        lat=_LAT, lon=_LON, layout=_layout(), production=buckets,
        cloud_times=times, cloud=w["cloud"], shortwave=w["shortwave"], direct=w["direct"],
        diffuse=w["diffuse"], temp=w["temp"], wind=w["wind"], snow=w["snow"],
        gti_store=None, soc_series=None, cutoff_soc=None,
        now_ms=datetime(2026, 6, 23, tzinfo=timezone.utc).timestamp() * 1000.0,
    )
    base.update(over)
    return SkyResidualInput(**base)


# --- build behaviour ------------------------------------------------------------

def test_build_none_cases() -> None:
    assert build_sky_residual_map(_input([])) is None                          # no production
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
    assert sky_map.global_ratio == M_MAX   # 5x clamps to the ceiling


def test_cutoff_guard_drops_full_battery_hours() -> None:
    buckets = _midday_buckets()
    inp0 = _input(buckets)
    matched = [ProductionBucket(b.start_ms, b.end_ms, _model_kwh(b, inp0)) for b in buckets]
    # Battery full (95%) across the whole window, cutoff at 90: every hour is dropped.
    soc = [SocBucket(b.start_ms, b.end_ms, 95.0) for b in matched]
    sky_map = build_sky_residual_map(_input(matched, soc_series=soc, cutoff_soc=90.0))
    assert sky_map is None   # all samples curtailed -> nothing learned


def test_forecast_applies_ratio() -> None:
    # A flat map returning 0.5 everywhere halves the corrected curve, raw untouched.
    n = 36 * 18
    half = SkyResidualMap(n_az=36, n_alt=18, m=[0.5] * n, conf=[1.0] * n, global_ratio=0.5, total_weight=99, visited_cells=n)
    base = datetime(2026, 6, 21, tzinfo=timezone.utc)
    weather = WeatherSeries(
        times=[base + timedelta(hours=h) for h in range(25)],
        cloud=[20.0] * 25, shortwave=[500.0] * 25, direct=[350.0] * 25,
        diffuse=[120.0] * 25, temp=[18.0] * 25, wind=[3.0] * 25, snow=[0.0] * 25,
    )
    start = datetime(2026, 6, 21, tzinfo=timezone.utc)
    end = datetime(2026, 6, 21, 23, 59, tzinfo=timezone.utc)
    pts = build_forecast_series(weather, None, _layout(), _LAT, _LON, start=start, end=end, step_minutes=60, residual_map=half)
    for p in pts:
        assert abs(p.pv_w - 0.5 * p.pv_raw_w) < 1e-9


if __name__ == "__main__":
    for name, fn in sorted(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"ok  {name}")
    print("all residual tests passed")
