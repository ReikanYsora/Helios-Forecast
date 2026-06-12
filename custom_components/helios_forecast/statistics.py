"""Pure helpers for archiving the Open-Meteo weather to HA statistics.

The 7 weather variables here are orientation-independent: they describe the sky
over the home, not the panels, so this archive stays valid across any PV layout
change. GTI is deliberately not archived, it depends on orientation and is
recomputable from the direct / diffuse / global radiation kept here plus the sun
geometry (see solar/irradiance.py).

Open-Meteo only serves a rolling 60-day past window. By copying each refresh's
past hours into Home Assistant's long-term statistics (which are never purged),
the history grows without bound and stays consultable well beyond those 60 days.

No Home Assistant imports, so the transforms can be unit-tested on their own.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, List, Optional

from .openmeteo import WeatherSeries


@dataclass(frozen=True)
class WeatherField:
    """One archived series: the entity / statistic key, the WeatherSeries
    attribute it reads, and the HA unit string (kept here so it stays the single
    source of truth shared by the sensor entity and the statistics metadata)."""

    key: str
    attr: str
    unit: str


# Single source of truth for the archived variables, in display order. The unit
# strings are the standard HA unit symbols so they match the sensor entities'
# units and the unit HA stores on each statistic.
WEATHER_FIELDS: tuple[WeatherField, ...] = (
    WeatherField("cloud_cover", "cloud", "%"),
    WeatherField("ghi", "shortwave", "W/m²"),
    WeatherField("direct", "direct", "W/m²"),
    WeatherField("diffuse", "diffuse", "W/m²"),
    WeatherField("temperature", "temp", "°C"),
    WeatherField("wind_speed", "wind", "km/h"),
    WeatherField("snow_depth", "snow", "m"),
)


def _finite(value: object) -> bool:
    return isinstance(value, (int, float)) and math.isfinite(value)


def observed_value(times: List[datetime], values: List[float], now: datetime) -> Optional[float]:
    """Value of the hourly bucket containing ``now``: the latest finite sample
    at or before ``now``. None when none is usable. ``times`` is ascending."""
    best: Optional[float] = None
    for t, v in zip(times, values):
        if t > now:
            break
        if _finite(v):
            best = float(v)
    return best


def observed_snapshot(weather: WeatherSeries, now: datetime) -> Dict[str, Optional[float]]:
    """Current-hour value for every archived field, keyed by field key."""
    return {
        field.key: observed_value(weather.times, getattr(weather, field.attr), now)
        for field in WEATHER_FIELDS
    }


# Archive entity keys for the predicted production. Their long-term statistics, backfilled by the
# coordinator from the model run over the past weather window, are the stored forecast history.
FORECAST_POWER_KEY = "predicted_power"
FORECAST_ENERGY_KEY = "predicted_energy"


def forecast_statistics(points: list) -> Dict[str, List[dict]]:
    """Per-hour statistic rows for the predicted-power and predicted-energy archive entities.

    ``points`` is an iterable of hourly forecast points (objects with ``.t`` UTC datetime and
    ``.pv_w`` watts). Each hour becomes one row; predicted energy is the hour's Wh expressed in kWh
    (power in watts over one hour = that many Wh). Non-finite points are skipped.
    """
    power: List[dict] = []
    energy: List[dict] = []
    for p in points:
        w = getattr(p, "pv_w", None)
        if not _finite(w):
            continue
        w = float(max(0.0, w))
        kwh = w / 1000.0
        power.append({"start": p.t, "mean": w, "min": w, "max": w})
        energy.append({"start": p.t, "mean": kwh, "min": kwh, "max": kwh})
    return {FORECAST_POWER_KEY: power, FORECAST_ENERGY_KEY: energy}


def hourly_statistics(times: List[datetime], values: List[float], cutoff: datetime) -> List[dict]:
    """Per-hour statistic rows for completed hours strictly before ``cutoff``.

    Each Open-Meteo hourly sample is one row with mean = min = max = the sample
    (a single value per hour). Non-finite samples and the in-progress current
    hour (``start >= cutoff``) are dropped. ``times`` are already top-of-hour UTC.
    """
    rows: List[dict] = []
    for t, v in zip(times, values):
        if t >= cutoff or not _finite(v):
            continue
        rows.append({"start": t, "mean": float(v), "min": float(v), "max": float(v)})
    return rows
