"""Forecast assembly: weather interpolation, per-bucket PV watts, daily kWh.

Ports the deterministic core of the card's buildForecast. Walks the horizon at a
sub-hourly step, interpolates the hourly Open-Meteo weather between samples with
a moving cursor (so the magnitude stays smooth at any cadence), computes the
weighted PV percentage, maps it to watts (x pvCalibK x snow), and clips at the
inverter cap. The learned correction is the next phase; here ``pv_w`` equals
``pv_raw_w`` (ratio 1).
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone, tzinfo
from typing import Dict, List, Optional

from .openmeteo import WeatherSeries
from .solar.geometry import sun_position
from .solar.gti import GtiStore, sample_gti
from .solar.irradiance import snow_cover_factor
from .solar.power import PvLayout, WeatherSample, compute_pv_power_weighted
from .solar.residual import SkyResidualMap, sample_sky_residual

INF = float("inf")


@dataclass(frozen=True)
class ForecastPoint:
    """One forecast bucket. pv_w is the chosen forecast (analog-blended when enough
    history exists, else the residual-corrected physical model). pv_raw_w is the pure
    physical model. pv_p10 / pv_p90 are the analog uncertainty band, None when the
    analog support is too thin to surface one."""

    t: datetime
    pv_w: float
    pv_raw_w: float
    pv_p10: Optional[float] = None
    pv_p90: Optional[float] = None
    # Weather at this bucket, surfaced so the card can correlate a production dip with the
    # irradiance / cloud at that exact instant when scrubbing (already computed for the model).
    ghi: Optional[float] = None
    cloud: Optional[float] = None


def lerp_plain(a: float, b: float, f: float) -> float:
    return a + (b - a) * f


def lerp_rad(a: Optional[float], b: Optional[float], f: float) -> Optional[float]:
    """Interpolate an irradiance field, guarding the missing / negative case."""
    if a is None or not math.isfinite(a) or a < 0:
        return b if (b is not None and math.isfinite(b) and b >= 0) else None
    if b is None or not math.isfinite(b) or b < 0:
        return a
    return a + (b - a) * f


def lerp_finite(a: Optional[float], b: Optional[float], f: float) -> Optional[float]:
    """Interpolate a temp / wind / snow field, guarding the missing case."""
    if a is None or not math.isfinite(a):
        return b if (b is not None and math.isfinite(b)) else None
    if b is None or not math.isfinite(b):
        return a
    return a + (b - a) * f


def _at(arr: List, i: int) -> Optional[float]:
    return arr[i] if 0 <= i < len(arr) else None


def build_forecast_series(
    weather: WeatherSeries,
    gti_store: Optional[GtiStore],
    layout: PvLayout,
    home_lat: float,
    home_lon: float,
    *,
    inverter_max_w: float = INF,
    start: datetime,
    end: datetime,
    step_minutes: int = 15,
    residual_map: Optional[SkyResidualMap] = None,
) -> List[ForecastPoint]:
    """Forecast watt curve over [start, end). pv_w applies the learned residual
    ratio when a map is given (else equals pv_raw_w), pv_raw_w is the pure model."""
    k = layout.total_kwp * 10.0
    step = timedelta(minutes=step_minutes)
    times = weather.times
    epochs = [t.timestamp() for t in times]
    sampler = (lambda tilt, az, m: sample_gti(gti_store, tilt, az, m)) if gti_store else None

    points: List[ForecastPoint] = []
    if not times:
        return points

    wi = 0
    t = start
    while t < end:
        t_ms = t.timestamp()
        # Bracket between two hourly weather samples, moving cursor (ascending t).
        while wi < len(times) - 1 and epochs[wi + 1] <= t_ms:
            wi += 1
        i0 = wi
        i1 = min(len(times) - 1, wi + 1)
        t0 = epochs[i0]
        t1 = epochs[i1]
        f = max(0.0, min(1.0, (t_ms - t0) / (t1 - t0))) if t1 > t0 else 0.0

        cloud = lerp_finite(_at(weather.cloud, i0), _at(weather.cloud, i1), f)
        sample = WeatherSample(
            cloud=cloud if cloud is not None else 0.0,
            ghi=lerp_finite(_at(weather.shortwave, i0), _at(weather.shortwave, i1), f),
            direct=lerp_rad(_at(weather.direct, i0), _at(weather.direct, i1), f),
            diffuse=lerp_rad(_at(weather.diffuse, i0), _at(weather.diffuse, i1), f),
            temp=lerp_finite(_at(weather.temp, i0), _at(weather.temp, i1), f),
            wind=lerp_finite(_at(weather.wind, i0), _at(weather.wind, i1), f),
            snow=lerp_finite(_at(weather.snow, i0), _at(weather.snow, i1), f),
        )

        pct = compute_pv_power_weighted(t, home_lat, home_lon, sample, layout, sampler)
        raw_w = pct * k * snow_cover_factor(sample.snow, sample.temp)
        if math.isfinite(raw_w):
            raw_clamped = min(inverter_max_w, max(0.0, raw_w))
            if residual_map is not None:
                sun = sun_position(t, home_lat, home_lon)
                ratio = sample_sky_residual(residual_map, sun.azimuth, sun.altitude)
            else:
                ratio = 1.0
            corrected = min(inverter_max_w, max(0.0, raw_w * ratio))
            points.append(
                ForecastPoint(
                    t=t,
                    pv_w=corrected,
                    pv_raw_w=raw_clamped,
                    ghi=sample.ghi,
                    cloud=sample.cloud,
                )
            )
        t += step

    return points


def integrate_daily_kwh(
    points: List[ForecastPoint],
    step_minutes: int,
    day_tz: Optional[tzinfo] = None,
) -> Dict[str, float]:
    """Sum the watt curve into kWh per calendar day (ISO date string keys).

    Each bucket contributes ``pv_w * step_hours / 1000``. ``day_tz`` sets the day
    boundary (defaults to UTC); the coordinator passes Home Assistant's local zone
    so today / tomorrow land on the user's midnight.
    """
    step_h = step_minutes / 60.0
    tz = day_tz or timezone.utc
    totals: Dict[str, float] = {}
    for p in points:
        day = p.t.astimezone(tz).date().isoformat()
        totals[day] = totals.get(day, 0.0) + p.pv_w * step_h / 1000.0
    return totals
