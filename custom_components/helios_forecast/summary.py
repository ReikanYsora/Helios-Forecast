"""Derive the entity values and the Energy ``wh_hours`` from the forecast series.

Pure: takes the assembled forecast points and a reference instant, returns every
value the integration publishes (the contract's sensor set + the Energy provider
payload). Keeping this out of the Home Assistant entity classes lets it be tested
on its own and keeps the entities as thin readers.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone, tzinfo
from typing import Dict, List, Optional

from .forecast import ForecastPoint

_HORIZON_DAYS = 7


@dataclass(frozen=True)
class DayForecast:
    """One day's headline figures. ``date`` is the local calendar day (ISO)."""

    date: str
    energy_kwh: float
    peak_power_w: float
    peak_time: Optional[datetime]


@dataclass(frozen=True)
class ForecastSummary:
    """Everything the integration exposes, derived once per coordinator refresh."""

    power_now_w: Optional[float]
    power_now_low_w: Optional[float]        # analog P10 band at now, None when no band
    power_now_high_w: Optional[float]       # analog P90 band at now, None when no band
    power_next_hour_w: Optional[float]
    days: List[DayForecast]                 # day_1 (today) .. day_7
    energy_today_remaining_kwh: Optional[float]
    energy_this_hour_kwh: Optional[float]
    energy_next_hour_kwh: Optional[float]
    wh_hours: Dict[str, float]              # ISO UTC hour -> Wh, for the Energy dashboard


def _value_at(points: List[ForecastPoint], t: datetime) -> Optional[float]:
    """Forecast power at ``t``, linearly interpolated between bracketing buckets."""
    if not points or t < points[0].t or t > points[-1].t:
        return None
    lo = 0
    hi = len(points) - 1
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if points[mid].t <= t:
            lo = mid
        else:
            hi = mid
    t0, t1 = points[lo].t, points[hi].t
    if t1 <= t0:
        return points[lo].pv_w
    f = (t - t0).total_seconds() / (t1 - t0).total_seconds()
    return points[lo].pv_w + (points[hi].pv_w - points[lo].pv_w) * f


def _band_at(points: List[ForecastPoint], t: datetime, attr: str) -> Optional[float]:
    """Interpolated P10/P90 band value at ``t``, or None when either bracketing
    bucket has no band (analog support too thin there)."""
    if not points or t < points[0].t or t > points[-1].t:
        return None
    lo = 0
    hi = len(points) - 1
    while hi - lo > 1:
        mid = (lo + hi) // 2
        if points[mid].t <= t:
            lo = mid
        else:
            hi = mid
    v0 = getattr(points[lo], attr)
    v1 = getattr(points[hi], attr)
    if v0 is None or v1 is None:
        return None
    t0, t1 = points[lo].t, points[hi].t
    if t1 <= t0:
        return v0
    f = (t - t0).total_seconds() / (t1 - t0).total_seconds()
    return v0 + (v1 - v0) * f


def _energy_kwh(points: List[ForecastPoint], start: datetime, end: datetime, step_h: float) -> Optional[float]:
    """kWh over [start, end) summing each bucket's pv_w x step hours."""
    total = 0.0
    any_bucket = False
    for p in points:
        if start <= p.t < end and math.isfinite(p.pv_w):
            total += p.pv_w * step_h / 1000.0
            any_bucket = True
    return total if any_bucket else None


def summarize(
    points: List[ForecastPoint],
    *,
    now: datetime,
    tz: tzinfo,
    step_minutes: int,
) -> ForecastSummary:
    """Build the full entity/provider summary from the forecast points."""
    step_h = step_minutes / 60.0
    now_utc = now.astimezone(timezone.utc)
    hour0 = now_utc.replace(minute=0, second=0, microsecond=0)

    # Per-day figures over the local-calendar horizon (day_1 = today).
    today_local = now.astimezone(tz).date()
    days: List[DayForecast] = []
    for i in range(_HORIZON_DAYS):
        day = today_local + timedelta(days=i)
        start = datetime(day.year, day.month, day.day, tzinfo=tz)
        end = start + timedelta(days=1)
        in_day = [p for p in points if start <= p.t < end]
        energy = sum(p.pv_w * step_h / 1000.0 for p in in_day)
        peak = max(in_day, key=lambda p: p.pv_w, default=None)
        days.append(
            DayForecast(
                date=day.isoformat(),
                energy_kwh=energy,
                peak_power_w=peak.pv_w if peak else 0.0,
                peak_time=peak.t if peak else None,
            )
        )

    today_start = datetime(today_local.year, today_local.month, today_local.day, tzinfo=tz)
    today_end = today_start + timedelta(days=1)

    # Average power over the next hour.
    next_hour = [p for p in points if now_utc <= p.t < now_utc + timedelta(hours=1)]
    power_next_hour = sum(p.pv_w for p in next_hour) / len(next_hour) if next_hour else None

    # Energy aggregated into whole UTC hours for the Energy dashboard provider.
    wh_hours: Dict[str, float] = {}
    for p in points:
        if not math.isfinite(p.pv_w):
            continue
        key = p.t.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0).isoformat()
        wh_hours[key] = wh_hours.get(key, 0.0) + p.pv_w * step_h

    return ForecastSummary(
        power_now_w=_value_at(points, now_utc),
        power_now_low_w=_band_at(points, now_utc, "pv_p10"),
        power_now_high_w=_band_at(points, now_utc, "pv_p90"),
        power_next_hour_w=power_next_hour,
        days=days,
        energy_today_remaining_kwh=_energy_kwh(points, now_utc, today_end, step_h),
        energy_this_hour_kwh=_energy_kwh(points, hour0, hour0 + timedelta(hours=1), step_h),
        energy_next_hour_kwh=_energy_kwh(points, hour0 + timedelta(hours=1), hour0 + timedelta(hours=2), step_h),
        wh_hours=wh_hours,
    )
