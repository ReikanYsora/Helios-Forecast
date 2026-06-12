"""Irradiance and PV power, faithful port of the card's computePvPower.

Pure functions. ``compute_pv_power`` returns 0..100 % of STC for one panel
orientation, exactly as the card does, built from:

  1. Haurwitz clear-sky GHI, attenuated by a Kasten-Czeplak cloud law, OR the
     supplied measured / forecast GHI when present.
  2. An optional tilt transposition (Liu-Jordan isotropic), or Open-Meteo's
     anisotropic plane-of-array (GTI) when supplied, with a direct / diffuse
     split from real radiation when available else cloud-derived.
  3. A Sandia-style cell-temperature derate when air temperature is known.

Kept identical to the TypeScript so the server-side forecast matches the card
and can be proven by parity against golden values from the same function.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from .geometry import sun_position

_D = math.pi / 180.0

# Cell-temperature model (port of pv-thermal.ts).
NOCT_CELL_C = 44.0
NOCT_IRRADIANCE = 800.0
NOCT_AIR_REF_C = 20.0
WIND_COOLING_K = 1.5
GAMMA_PMP_PER_C = -0.0035
STC_REF_C = 25.0

# Snow-cover derate (port of pv.ts).
SNOW_COVER_M = 0.01
SNOW_MELT_LO_C = 0.0
SNOW_MELT_HI_C = 4.0
SNOW_MIN_FACTOR = 0.1

Tracker = str  # 'dual-axis' | 'single-axis-h' | 'single-axis-v'


@dataclass(frozen=True)
class PanelOrientation:
    """One co-oriented group of panels. ``tracker`` None means a fixed panel."""

    tilt_deg: float
    azimuth_deg: float
    tracker: Optional[Tracker] = None


@dataclass(frozen=True)
class PvContext:
    """Optional refinements; an empty context reproduces the bare model."""

    air_temp_c: Optional[float] = None
    wind_ms: Optional[float] = None
    shading: bool = False
    ghi_wm2: Optional[float] = None
    direct_wm2: Optional[float] = None
    diffuse_wm2: Optional[float] = None
    poa_wm2: Optional[float] = None


def cell_temperature_c(air_temp_c: float, ghi_wm2: float, wind_ms: float) -> float:
    """Cell temperature in degC, NaN when air temperature is not finite."""
    if not math.isfinite(air_temp_c):
        return math.nan
    g = max(0.0, ghi_wm2)
    w = max(0.0, wind_ms) if math.isfinite(wind_ms) else 0.0
    return air_temp_c + (NOCT_CELL_C - NOCT_AIR_REF_C) / NOCT_IRRADIANCE * g - WIND_COOLING_K * w


def thermal_derating(cell_temp_c: float) -> float:
    """Multiplicative derate, floored at 0.6, uncapped above (cold-weather gain)."""
    if not math.isfinite(cell_temp_c):
        return 1.0
    return max(0.6, 1.0 + GAMMA_PMP_PER_C * (cell_temp_c - STC_REF_C))


def snow_cover_factor(snow_depth_m: Optional[float], air_temp_c: Optional[float]) -> float:
    """Snow-cover derate in [SNOW_MIN_FACTOR, 1]. Unknown air is treated as cold."""
    if snow_depth_m is None or not math.isfinite(snow_depth_m) or snow_depth_m < SNOW_COVER_M:
        return 1.0
    t = SNOW_MELT_LO_C if (air_temp_c is None or not math.isfinite(air_temp_c)) else air_temp_c
    melt = max(0.0, min(1.0, (t - SNOW_MELT_LO_C) / (SNOW_MELT_HI_C - SNOW_MELT_LO_C)))
    return SNOW_MIN_FACTOR + (1.0 - SNOW_MIN_FACTOR) * melt


def _supplied(value: Optional[float]) -> bool:
    """True when an optional irradiance field is present and non-negative."""
    return value is not None and value >= 0


def compute_pv_power(
    moment: datetime,
    lat: float,
    lon: float,
    cloud_cover_pct: float,
    panel: Optional[PanelOrientation] = None,
    ctx: Optional[PvContext] = None,
) -> float:
    """PV output as a percentage of STC (0..100) for one orientation."""
    sun = sun_position(moment, lat, lon)
    alt = sun.altitude
    if alt <= 0:
        return 0.0

    cos_z = math.sin(alt * _D)
    ghi_clear = 1098.0 * cos_z * math.exp(-0.059 / cos_z)

    cc = max(0.0, min(100.0, cloud_cover_pct)) / 100.0
    k_cloud = 1.0 - 0.75 * (cc ** 3.4)

    ghi_eff = ctx.ghi_wm2 if (ctx is not None and _supplied(ctx.ghi_wm2)) else ghi_clear * k_cloud

    shading = bool(ctx and ctx.shading)

    if panel is None or (panel.tilt_deg <= 0 and not panel.tracker):
        # Horizontal panel: GHI already is the plane-of-array value. A shaded
        # flat panel keeps only ~25 % (typical clear-sky diffuse fraction).
        poa_eff = ghi_eff * 0.25 if shading else ghi_eff
    else:
        beta_deg = panel.tilt_deg
        az_deg = panel.azimuth_deg
        if panel.tracker == "dual-axis":
            beta_deg = 90.0 - alt
            az_deg = sun.azimuth
        elif panel.tracker == "single-axis-h":
            beta_deg = 90.0 - alt
        elif panel.tracker == "single-axis-v":
            az_deg = sun.azimuth

        beta = beta_deg * _D
        d_az = (sun.azimuth - az_deg) * _D
        alt_r = alt * _D

        cos_theta = math.sin(alt_r) * math.cos(beta) + math.cos(alt_r) * math.sin(beta) * math.cos(d_az)
        r_b = max(0.0, cos_theta) / max(0.087, cos_z) if cos_theta > 0 else 0.0

        has_split = (
            ctx is not None
            and _supplied(ctx.direct_wm2)
            and _supplied(ctx.diffuse_wm2)
            and (ctx.direct_wm2 + ctx.diffuse_wm2) > 0
        )
        if has_split:
            direct_fraction = ctx.direct_wm2 / (ctx.direct_wm2 + ctx.diffuse_wm2)
        else:
            direct_fraction = max(0.0, min(0.85, (k_cloud - 0.25) / 0.75 * 0.85))
        diffuse_fraction = 1.0 - direct_fraction

        direct_poa = 0.0 if shading else ghi_eff * direct_fraction * r_b
        diffuse_poa = ghi_eff * diffuse_fraction * (1.0 + math.cos(beta)) / 2.0
        ground_poa = ghi_eff * 0.2 * (1.0 - math.cos(beta)) / 2.0

        if ctx is not None and _supplied(ctx.poa_wm2):
            poa_eff = min(ctx.poa_wm2, diffuse_poa + ground_poa) if shading else ctx.poa_wm2
        else:
            poa_eff = direct_poa + diffuse_poa + ground_poa

    p_stc = max(0.0, poa_eff / 1000.0)

    if ctx is not None and ctx.air_temp_c is not None and math.isfinite(ctx.air_temp_c):
        t_cell = cell_temperature_c(ctx.air_temp_c, poa_eff, ctx.wind_ms if ctx.wind_ms is not None else 0.0)
        p_stc *= thermal_derating(t_cell)

    return max(0.0, min(100.0, p_stc * 100.0))
