"""Replay the learning offline, day after day, against what the roof really produced.

Everything the forecast is made of is a pure function, so a day can be re-forecast as the
integration would have forecast it that morning: the residual map and the analog library are
rebuilt from the history available at 06:00 local, then the day is predicted and scored against the
meter. The archived weather stands in for the forecast weather, which removes the weather error
from the picture on purpose: what is measured here is the learning, not Open-Meteo.

Variants of the pipeline are scored side by side, by sun altitude, so a change to the learning is
measured on sixty days before it is written into the integration.

Usage: .venv/bin/python tools/replay/replay.py [--days 30]  (data from tools/replay/fetch.mjs)
"""

from __future__ import annotations

import argparse
import json
import math
import os
import sys
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from custom_components.helios_forecast import analog  # noqa: E402
from custom_components.helios_forecast.analog import AnalogSample, build_library, enrich_points  # noqa: E402
from custom_components.helios_forecast.forecast import ForecastPoint, build_forecast_series  # noqa: E402
from custom_components.helios_forecast.openmeteo import WeatherSeries  # noqa: E402
from custom_components.helios_forecast.solar.geometry import sun_position  # noqa: E402
from custom_components.helios_forecast.solar.power import (  # noqa: E402
    PanelOrientation, PvLayout, WeatherSample, compute_pv_power_per_array,
)
from custom_components.helios_forecast.solar import residual as RS  # noqa: E402
from custom_components.helios_forecast.solar.residual import (  # noqa: E402
    LEARN_DAYS, ProductionBucket, SkyResidualInput, SkyResidualMap, build_sky_residual_map,
)

HERE = os.path.dirname(__file__)
TZ = ZoneInfo("Europe/Paris")

# Jerome's installation, as declared in the integration (core.config_entries, 2026-09-06).
LAT, LON = 44.10974, 1.40441
LAYOUT = PvLayout(orientations=[PanelOrientation(tilt_deg=13.0, azimuth_deg=188.0, tracker=None)],
                  shares=[1.0], coords=[None], total_kwp=3.0, caps=[])
CAP_W = 3200.0


def load_weather() -> WeatherSeries:
    raw = json.load(open(os.path.join(HERE, "data", "weather.json")))
    by_t: Dict[int, Dict[str, float]] = {}
    for field, rows in raw.items():
        for start_ms, mean in rows:
            if mean is not None:
                by_t.setdefault(int(start_ms), {})[field] = float(mean)
    times = sorted(by_t)
    pick = lambda t, f, default: by_t[t].get(f, default)  # noqa: E731
    return WeatherSeries(
        times=[datetime.fromtimestamp(t / 1000, tz=timezone.utc) for t in times],
        cloud=[pick(t, "cloud_cover", 0.0) for t in times],
        shortwave=[pick(t, "global_irradiance", 0.0) for t in times],
        direct=[pick(t, "direct_irradiance", 0.0) for t in times],
        diffuse=[pick(t, "diffuse_irradiance", 0.0) for t in times],
        temp=[pick(t, "temperature", 20.0) for t in times],
        wind=[pick(t, "wind_speed", 0.0) for t in times],
        snow=[pick(t, "snow_depth", 0.0) for t in times],
    )


def load_production() -> List[ProductionBucket]:
    rows = json.load(open(os.path.join(HERE, "data", "production.json")))
    out = []
    for start_ms, end_ms, change in rows:
        if change is None or not math.isfinite(change) or change < 0:
            continue  # a meter reset reads as a huge negative hour; the integration skips it too
        out.append(ProductionBucket(start_ms=float(start_ms), end_ms=float(end_ms), kwh=float(change)))
    return out


def measured_map(buckets: List[ProductionBucket]) -> Dict[int, float]:
    """kWh per local hour, keyed on the epoch ms of the hour's start."""
    return {int(b.start_ms): b.kwh for b in buckets}


def physics_w(moment: datetime, weather: WeatherSeries, epochs: List[float]) -> float:
    """Bare physical watts at an instant, from the archived weather, the way the residual map sees it."""
    ms = moment.timestamp() * 1000
    i = max(0, min(len(epochs) - 1, _nearest(epochs, ms)))
    sample = WeatherSample(cloud=weather.cloud[i] or 0.0, ghi=weather.shortwave[i], direct=weather.direct[i],
                           diffuse=weather.diffuse[i], temp=weather.temp[i], wind=weather.wind[i])
    pcts = compute_pv_power_per_array(moment, LAT, LON, sample, LAYOUT)
    return max(0.0, min(CAP_W, pcts[0] * LAYOUT.total_kwp * 10.0))


def _nearest(epochs: List[float], ms: float) -> int:
    lo, hi = 0, len(epochs) - 1
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if epochs[mid] <= ms:
            lo = mid
        else:
            hi = mid
    return lo if abs(epochs[lo] - ms) <= abs(epochs[hi] - ms) else hi


# ---- variants of the analog step ------------------------------------------------------------------

def predict_variant(library, alt, az, cloud, temp, *, narrow_alt=False, az_weight=None, az_norm=180.0):
    """analog.predict with switches: altitude distance relative to the altitude itself (a low sun only
    matches a low sun), and a heavier, tighter azimuth term so a September evening at 271 degrees is not
    told by July evenings at 290 that lay behind a different piece of horizon."""
    if not library or alt <= 0:
        return None
    w_az = analog._W_AZ if az_weight is None else az_weight
    scored = []
    for s in library:
        dalt = (s.alt - alt) / (max(alt, 10.0) if narrow_alt else 90.0)
        daz = analog._az_diff(s.az, az) / az_norm
        dcl = (s.cloud - cloud) / 100.0
        d2 = analog._W_ALT * dalt * dalt + w_az * daz * daz + analog._W_CLOUD * dcl * dcl
        if temp is not None and s.temp is not None:
            dt = (s.temp - temp) / analog._TEMP_SCALE
            d2 += analog._W_TEMP * dt * dt
        else:
            d2 += analog._TEMP_MISSING_PENALTY
        scored.append((d2, s.watt))
    scored.sort(key=lambda x: x[0])
    top = scored[: analog._K]
    weighted = [(v, math.exp(-d2 / (2.0 * analog._BANDWIDTH2))) for d2, v in top]
    p10, p50, p90 = analog._weighted_percentiles(weighted, (0.10, 0.50, 0.90))
    n_close = sum(1 for d2, _ in top if d2 <= analog._CLOSE_D2)
    confidence = min(1.0, n_close / analog._CONFIDENCE_FULL)
    ceiling = p90 * analog._CEILING_MARGIN if n_close >= analog._CEILING_MIN_ANALOGS else None
    return analog.AnalogBand(p10=p10, p50=p50, p90=p90, confidence=confidence, ceiling=ceiling)


def enrich_variant(points, library, weather, w_epochs, now, *, ratio_lib=False, narrow_alt=False, max_conf=1.0,
                   az_weight=None, az_norm=180.0):
    out = []
    for p in points:
        if p.t < now:
            out.append(p)
            continue
        sun = sun_position(p.t, LAT, LON)
        if sun.altitude <= 0:
            out.append(replace(p, pv_p10=0.0, pv_p90=0.0))
            continue
        ms = p.t.timestamp() * 1000.0
        cloud = analog._sample_series(weather.times, weather.cloud, ms, w_epochs)
        temp = analog._sample_series(weather.times, weather.temp, ms, w_epochs)
        band = predict_variant(library, sun.altitude, sun.azimuth, cloud if cloud is not None else 50.0, temp,
                               narrow_alt=narrow_alt, az_weight=az_weight, az_norm=az_norm)
        if band is None:
            out.append(p)
            continue
        c = min(max_conf, band.confidence)
        if ratio_lib:
            # The library holds actual/physics ratios: the analog's word is a ratio applied to today's physics.
            p50, p10, p90 = band.p50 * p.pv_raw_w, band.p10 * p.pv_raw_w, band.p90 * p.pv_raw_w
            ceiling = band.ceiling * p.pv_raw_w if band.ceiling is not None else None
        else:
            p50, p10, p90, ceiling = band.p50, band.p10, band.p90, band.ceiling
        blended = c * p50 + (1.0 - c) * p.pv_w
        if ceiling is not None:
            blended = min(blended, ceiling)
        out.append(replace(p, pv_w=blended, pv_p10=p10, pv_p90=p90) if band.confidence >= analog.BAND_MIN_CONFIDENCE
                   else replace(p, pv_w=blended))
    return out


def ratio_library(buckets, weather, epochs) -> List[AnalogSample]:
    """The analog library with each sample's watts replaced by its ratio to the physics of that hour."""
    lib = build_library(buckets, weather, LAT, LON)
    out = []
    it = iter(lib)
    for b in buckets:
        mid = datetime.fromtimestamp((b.start_ms + b.end_ms) / 2000.0, tz=timezone.utc)
        if sun_position(mid, LAT, LON).altitude <= 0 or not math.isfinite(b.kwh):
            continue
        s = next(it, None)
        if s is None:
            break
        model = physics_w(mid, weather, epochs)
        if model < 30.0:
            continue  # a ratio against nothing says nothing
        out.append(AnalogSample(alt=s.alt, az=s.az, cloud=s.cloud, watt=s.watt / model, temp=s.temp))
    return out


# ---- a variant of the residual map's smoothing ---------------------------------------------------

def vertical_map(inp: SkyResidualInput) -> Optional[SkyResidualMap]:
    """The residual map with its thin cells filled from the sky ABOVE and BELOW them at the same azimuth
    first, and from the side cells only when that column is empty.

    Near-field shading is a property of azimuth: an obstacle at 288 degrees darkens every cell under its
    own height at 288 and nothing at 271. The integration's smoothing pulls a thin cell toward all eight
    neighbours alike, so a cell the sun only reaches this month borrows the shading of the azimuths it
    reached last month. The column above it is a better witness."""
    keep = RS.SMOOTH_NEIGHBOR_W
    # Not zero: the integration's own smoothing divides by the pull, so an all-but-absent pull leaves the
    # raw cells untouched without tripping it.
    RS.SMOOTH_NEIGHBOR_W = 1e-9
    try:
        raw = build_sky_residual_map(inp)
    finally:
        RS.SMOOTH_NEIGHBOR_W = keep
    if raw is None:
        return None
    m, conf, n_az, n_alt = list(raw.m), list(raw.conf), raw.n_az, raw.n_alt
    m_s, c_s = list(m), list(conf)
    for aj in range(n_alt):
        for ai in range(n_az):
            idx = aj * n_az + ai
            if conf[idx] >= RS.SMOOTH_CONF_KEEP:
                continue
            num = den = best = 0.0
            for d_alt in (-1, 1, -2, 2):
                nj = aj + d_alt
                if 0 <= nj < n_alt:
                    c = conf[nj * n_az + ai] / abs(d_alt)
                    num += c * m[nj * n_az + ai]
                    den += c
                    best = max(best, conf[nj * n_az + ai])
            if den <= 0:
                for d_az in (-1, 1):
                    ni = (ai + d_az) % n_az
                    c = conf[aj * n_az + ni]
                    num += c * m[aj * n_az + ni]
                    den += c
                    best = max(best, c)
            if den <= 0 or best <= 0:
                continue
            pull = keep * best
            m_s[idx] = (conf[idx] * m[idx] + pull * (num / den)) / (conf[idx] + pull)
            c_s[idx] = min(1.0, conf[idx] + pull)
    return replace(raw, m=m_s, conf=c_s)


# ---- the replay ------------------------------------------------------------------------------------

BANDS = ["AM 0-15", "AM 15-30", "AM 30+", "PM 30+", "PM 15-30", "PM 0-15"]


def band_of(sun) -> str:
    half = "AM" if sun.azimuth < 180 else "PM"
    alt = sun.altitude
    return "%s %s" % (half, "0-15" if alt < 15 else ("15-30" if alt < 30 else "30+"))


def replay(days: int) -> None:
    weather = load_weather()
    epochs = [t.timestamp() * 1000.0 for t in weather.times]
    buckets = load_production()
    measured = measured_map(buckets)
    last_full = datetime.now(TZ).replace(hour=0, minute=0, second=0, microsecond=0)
    first = last_full - timedelta(days=days)

    VARIANTS = ["physique", "residuel seul", "pipeline actuel", "analogues azimut pese", "analogues sur ratios",
                "ratios + azimut pese", "lissage vertical seul", "vertical + pipeline", "vertical + ratios + azimut"]
    acc = {v: {b: {"n": 0, "abs": 0.0, "pred": 0.0, "truth": 0.0} for b in BANDS + ["total"]} for v in VARIANTS}
    daily = {v: [] for v in VARIANTS}
    band_cov = {v: [0, 0] for v in VARIANTS}
    w_epochs = analog.series_epochs(weather.times)

    day = first
    while day < last_full:
        now = day.replace(hour=6)
        now_ms = now.timestamp() * 1000.0
        history = [b for b in buckets if b.end_ms <= now_ms and b.start_ms >= (now - timedelta(days=LEARN_DAYS)).timestamp() * 1000]
        if len(history) < 24 * 10:
            day += timedelta(days=1)
            continue
        inp = SkyResidualInput(
            lat=LAT, lon=LON, layout=LAYOUT, production=history, cloud_times=epochs, cloud=weather.cloud,
            shortwave=weather.shortwave, direct=weather.direct, diffuse=weather.diffuse, temp=weather.temp,
            wind=weather.wind, snow=weather.snow, now_ms=now_ms)
        sky = build_sky_residual_map(inp)
        vsky = vertical_map(inp)
        start, end = day, day + timedelta(days=1)
        base = build_forecast_series(weather, LAYOUT, LAT, LON, inverter_max_w=CAP_W, start=start, end=end,
                                     step_minutes=15, residual_map=sky)
        vbase = build_forecast_series(weather, LAYOUT, LAT, LON, inverter_max_w=CAP_W, start=start, end=end,
                                      step_minutes=15, residual_map=vsky)
        library = build_library(history, weather, LAT, LON)
        rlib = ratio_library(history, weather, epochs)
        AZ = dict(az_weight=0.7, az_norm=90.0)
        series = {
            "physique": [replace(p, pv_w=p.pv_raw_w) for p in base],
            "residuel seul": base,
            "pipeline actuel": enrich_points(base, library, weather, LAT, LON, now),
            "analogues azimut pese": enrich_variant(base, library, weather, w_epochs, now, **AZ),
            "analogues sur ratios": enrich_variant(base, rlib, weather, w_epochs, now, ratio_lib=True),
            "ratios + azimut pese": enrich_variant(base, rlib, weather, w_epochs, now, ratio_lib=True, **AZ),
            "lissage vertical seul": vbase,
            "vertical + pipeline": enrich_points(vbase, library, weather, LAT, LON, now),
            "vertical + ratios + azimut": enrich_variant(vbase, rlib, weather, w_epochs, now, ratio_lib=True, **AZ),
        }
        for name, pts in series.items():
            by_hour: Dict[int, List[ForecastPoint]] = {}
            for p in pts:
                by_hour.setdefault(int(p.t.replace(minute=0, second=0, microsecond=0).timestamp() * 1000), []).append(p)
            day_pred = day_truth = 0.0
            for hour_ms, group in by_hour.items():
                truth = measured.get(hour_ms)
                mid = datetime.fromtimestamp(hour_ms / 1000 + 1800, tz=timezone.utc)
                sun = sun_position(mid, LAT, LON)
                if truth is None or truth <= 0 or sun.altitude <= 0 or mid < now:
                    continue
                pred_w = sum(p.pv_w for p in group) / len(group)
                truth_w = truth * 1000.0
                for key in (band_of(sun), "total"):
                    a = acc[name][key]
                    a["n"] += 1; a["abs"] += abs(pred_w - truth_w); a["pred"] += pred_w; a["truth"] += truth_w
                day_pred += pred_w / 1000; day_truth += truth
                lo = [p.pv_p10 for p in group if p.pv_p10 is not None]
                hi = [p.pv_p90 for p in group if p.pv_p90 is not None]
                if lo and hi:
                    band_cov[name][1] += 1
                    band_cov[name][0] += (min(lo) <= truth_w <= max(hi))
            if day_truth > 0.3:
                daily[name].append((day_pred - day_truth) / day_truth * 100)
        day += timedelta(days=1)

    print("Replay on %d days, hourly, sun up, meter above zero. Per cell: nMAE (W/kWc) and bias; last column: mean absolute error on the day's energy." % days)
    print("%-27s" % "" + "".join("%11s" % b for b in BANDS) + "%11s %8s" % ("TOTAL", "jour"))
    for v in VARIANTS:
        cells = []
        for b in BANDS + ["total"]:
            a = acc[v][b]
            cells.append("%3.0f %+4.0f%%" % (a["abs"] / a["n"] / LAYOUT.total_kwp, 100 * (a["pred"] - a["truth"]) / a["truth"]) if a["n"] else "    -    ")
        d = daily[v]
        mape = sum(abs(x) for x in d) / len(d) if d else float("nan")
        print("%-27s" % v + "".join("%11s" % c for c in cells) + "%8s" % ("%.1f%%" % mape))
    print("  n par cellule : " + " ".join("%s=%d" % (b, acc["physique"][b]["n"]) for b in BANDS))
    print("\nCouverture de la fourchette P10-P90 (objectif 80 %) :")
    for v in VARIANTS:
        hit, n = band_cov[v]
        if n:
            print("  %-27s %5.1f%%  (n=%d)" % (v, 100 * hit / n, n))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=30)
    args = ap.parse_args()
    replay(args.days)
