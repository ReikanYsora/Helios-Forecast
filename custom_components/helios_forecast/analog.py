"""Analog-ensemble forecast refinement.

Instead of trusting a generic physical model, look up past hours whose conditions
(sun geometry + cloud cover) resemble the hour being forecast, and read the
distribution of what the installation ACTUALLY produced then. The median is a
site-calibrated point forecast (it already contains the real shading, soiling,
orientation error and inverter behaviour), and the 10th/90th percentiles are a
free, data-driven uncertainty band.

This refines the physical model rather than replacing it: when few close analogs
exist (cold start, unusual conditions) the prediction blends back toward the
physical value by confidence, so the forecast degrades gracefully.

Pure functions, no Home Assistant. Production buckets are duck-typed
(``.start_ms`` + ``.end_ms`` + ``.kwh``); the only dependency is the sun geometry.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, replace
from datetime import datetime, tzinfo
from typing import List, Optional

from .forecast import ForecastPoint
from .openmeteo import WeatherSeries
from .solar.geometry import sun_position

# Feature weights in the (normalised) distance. Cloud is the variable that drives
# production at a fixed geometry, so it dominates; altitude sets the available
# energy; azimuth matters least (the day is roughly symmetric morning/afternoon).
_W_CLOUD = 1.0
_W_ALT = 0.7
_W_AZ = 0.3

# Kernel bandwidth on the squared normalised distance for the analog weights.
_BANDWIDTH2 = 0.02
# Analogs closer than this squared distance count toward the confidence tally.
_CLOSE_D2 = 0.04
# Close-analog count at which confidence saturates to 1.
_CONFIDENCE_FULL = 25
# Below this confidence we keep the blended point but do not surface a band.
BAND_MIN_CONFIDENCE = 0.35
# How many nearest analogs feed the weighted percentiles.
_K = 60


@dataclass(frozen=True)
class AnalogSample:
    alt: float    # sun altitude, degrees (only daytime samples are kept)
    az: float     # sun azimuth, degrees
    cloud: float  # cloud cover, %
    watt: float   # actual production at that hour, W


@dataclass(frozen=True)
class AnalogBand:
    p10: float
    p50: float
    p90: float
    confidence: float  # 0..1


def _finite(v: object) -> bool:
    return isinstance(v, (int, float)) and math.isfinite(v)


def _sample_cloud(weather: WeatherSeries, ms: float) -> Optional[float]:
    """Linearly interpolate cloud cover at epoch-ms ``ms`` from the hourly series."""
    times = weather.times
    cloud = weather.cloud
    if not times:
        return None
    epochs = [t.timestamp() * 1000.0 for t in times]
    if ms <= epochs[0]:
        return cloud[0] if _finite(cloud[0]) else None
    if ms >= epochs[-1]:
        last = cloud[len(epochs) - 1] if len(epochs) - 1 < len(cloud) else None
        return last if _finite(last) else None
    # Bracket.
    lo, hi = 0, len(epochs) - 1
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if epochs[mid] <= ms:
            lo = mid
        else:
            hi = mid
    a = cloud[lo] if lo < len(cloud) else None
    b = cloud[hi] if hi < len(cloud) else None
    if not _finite(a):
        return b if _finite(b) else None
    if not _finite(b):
        return a
    f = (ms - epochs[lo]) / (epochs[hi] - epochs[lo])
    return a + (b - a) * f


def build_library(production: list, weather: WeatherSeries, lat: float, lon: float) -> List[AnalogSample]:
    """Turn the production history into analog samples: actual watts tagged with
    the sun geometry and cloud cover at that hour. Night hours are dropped."""
    out: List[AnalogSample] = []
    for b in production:
        if not _finite(getattr(b, "kwh", None)):
            continue
        mid_ms = (b.start_ms + b.end_ms) / 2.0
        moment = datetime.fromtimestamp(mid_ms / 1000.0, tz=weather.times[0].tzinfo) if weather.times else None
        if moment is None:
            continue
        sun = sun_position(moment, lat, lon)
        if sun.altitude <= 0:
            continue
        cloud = _sample_cloud(weather, mid_ms)
        if cloud is None:
            continue
        out.append(AnalogSample(alt=sun.altitude, az=sun.azimuth, cloud=cloud, watt=max(0.0, b.kwh * 1000.0)))
    return out


def _az_diff(a: float, b: float) -> float:
    d = abs((a - b) % 360.0)
    return min(d, 360.0 - d)


def _weighted_percentiles(pairs: List[tuple], qs: tuple) -> List[float]:
    """Weighted percentiles of (value, weight) pairs for the quantiles in ``qs``."""
    items = sorted(pairs, key=lambda p: p[0])
    total = sum(w for _, w in items)
    if total <= 0:
        return [items[len(items) // 2][0] for _ in qs]
    out: List[float] = []
    for q in qs:
        target = q * total
        acc = 0.0
        chosen = items[-1][0]
        for value, w in items:
            acc += w
            if acc >= target:
                chosen = value
                break
        out.append(chosen)
    return out


def predict(library: List[AnalogSample], alt: float, az: float, cloud: float) -> Optional[AnalogBand]:
    """Weighted P10/P50/P90 of actual production among the analogs nearest to
    (alt, az, cloud), or None when the library is empty."""
    if not library or alt <= 0:
        return None
    scored: List[tuple] = []
    for s in library:
        dalt = (s.alt - alt) / 90.0
        daz = _az_diff(s.az, az) / 180.0
        dcl = (s.cloud - cloud) / 100.0
        d2 = _W_ALT * dalt * dalt + _W_AZ * daz * daz + _W_CLOUD * dcl * dcl
        scored.append((d2, s.watt))
    scored.sort(key=lambda x: x[0])
    top = scored[:_K]
    if not top:
        return None
    weighted = [(watt, math.exp(-d2 / (2.0 * _BANDWIDTH2))) for d2, watt in top]
    p10, p50, p90 = _weighted_percentiles(weighted, (0.10, 0.50, 0.90))
    n_close = sum(1 for d2, _ in top if d2 <= _CLOSE_D2)
    confidence = min(1.0, n_close / _CONFIDENCE_FULL)
    return AnalogBand(p10=p10, p50=p50, p90=p90, confidence=confidence)


def enrich_points(
    points: List[ForecastPoint],
    library: List[AnalogSample],
    weather: WeatherSeries,
    lat: float,
    lon: float,
    now: datetime,
) -> List[ForecastPoint]:
    """Blend the analog median into the future points and attach the P10/P90 band.

    Past points (t < now) are left as the physical model output. The future P50
    blends analog and physical by confidence; the band is surfaced only once the
    analog support is solid (BAND_MIN_CONFIDENCE)."""
    if not library:
        return points
    out: List[ForecastPoint] = []
    for p in points:
        if p.t < now:
            out.append(p)
            continue
        sun = sun_position(p.t, lat, lon)
        if sun.altitude <= 0:
            out.append(p)
            continue
        cloud = _sample_cloud(weather, p.t.timestamp() * 1000.0)
        band = predict(library, sun.altitude, sun.azimuth, cloud if cloud is not None else 50.0)
        if band is None:
            out.append(p)
            continue
        c = band.confidence
        blended = c * band.p50 + (1.0 - c) * p.pv_w
        if c >= BAND_MIN_CONFIDENCE:
            out.append(replace(p, pv_w=blended, pv_p10=band.p10, pv_p90=band.p90))
        else:
            out.append(replace(p, pv_w=blended))
    return out
