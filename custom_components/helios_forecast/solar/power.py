"""Weighted multi-orientation PV power, port of the card's computePvPowerWeighted.

Sums ``compute_pv_power`` across every configured array, weighted by its share of
the total kWp. The card's LiDAR raycast shading path is intentionally not ported
yet: with no raster, every array is unshaded, which is exactly the branch this
covers. Pure, no Home Assistant imports.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable, List, Optional, Tuple

from .irradiance import PanelOrientation, PvContext, _supplied, compute_pv_power

# (tilt, azimuth, moment) -> plane-of-array W/m2, or None when no GTI covers it.
GtiSampler = Callable[[float, float, datetime], Optional[float]]


@dataclass(frozen=True)
class WeatherSample:
    """Interpolated weather at one instant. ``cloud`` is always present."""

    cloud: float
    ghi: Optional[float] = None
    direct: Optional[float] = None
    diffuse: Optional[float] = None
    temp: Optional[float] = None
    wind: Optional[float] = None
    snow: Optional[float] = None


@dataclass(frozen=True)
class PvLayout:
    """Resolved PV layout. The list fields stay in lockstep per array."""

    orientations: List[PanelOrientation]
    shares: List[float]  # pre-normalised, sum to 1.0
    coords: List[Optional[Tuple[float, float]]]  # per-array (lat, lon) override or None
    total_kwp: float
    # Per-array inverter cap in watts (INF when that array has none). Empty = no per-array caps at all, so the
    # forecast clips only the combined total at the entry-level cap, exactly as before. When present, each array is
    # clipped at its own cap BEFORE the arrays are summed, for micro-inverter strings that saturate independently.
    caps: List[float] = field(default_factory=list)


def _finite(value: Optional[float]) -> bool:
    return value is not None and math.isfinite(value)


def compute_pv_power_per_array(
    moment: datetime,
    home_lat: float,
    home_lon: float,
    sample: WeatherSample,
    layout: PvLayout,
    gti_sampler: Optional[GtiSampler] = None,
) -> List[float]:
    """PV percentage (0..100) for each configured array, in layout order. Returns a single-element list
    ``[horizontal_pct]`` when the layout cannot be split per array (empty or out-of-lockstep), so callers can
    still render one curve. This is the per-array primitive; the weighted total and the per-array cap both read it."""
    has_ghi = _supplied(sample.ghi)
    has_split = _supplied(sample.direct) and _supplied(sample.diffuse)
    base_present = _finite(sample.temp) or _finite(sample.wind) or has_ghi or has_split
    base_ctx = (
        PvContext(
            air_temp_c=sample.temp,
            wind_ms=sample.wind,
            ghi_wm2=sample.ghi if has_ghi else None,
            direct_wm2=sample.direct if has_split else None,
            diffuse_wm2=sample.diffuse if has_split else None,
        )
        if base_present
        else None
    )

    orientations = layout.orientations
    # Defensive: keep the per-array arrays in lockstep, else fall back to the horizontal path so the curve still
    # renders (matches the card's guard). A single-element list signals "one array over the whole layout".
    if not orientations or len(layout.shares) != len(orientations) or len(layout.coords) != len(orientations):
        return [compute_pv_power(moment, home_lat, home_lon, sample.cloud, None, base_ctx)]

    out: List[float] = []
    for orientation in orientations:
        idx = len(out)
        coord = layout.coords[idx]
        array_lat = coord[0] if coord else home_lat
        array_lon = coord[1] if coord else home_lon

        gti_poa = (
            gti_sampler(orientation.tilt_deg, orientation.azimuth_deg, moment)
            if (gti_sampler is not None and not orientation.tracker)
            else None
        )

        if base_present or gti_poa is not None:
            array_ctx: Optional[PvContext] = PvContext(
                air_temp_c=base_ctx.air_temp_c if base_ctx else None,
                wind_ms=base_ctx.wind_ms if base_ctx else None,
                ghi_wm2=base_ctx.ghi_wm2 if base_ctx else None,
                direct_wm2=base_ctx.direct_wm2 if base_ctx else None,
                diffuse_wm2=base_ctx.diffuse_wm2 if base_ctx else None,
                poa_wm2=gti_poa,
                shading=False,
            )
        else:
            array_ctx = None

        out.append(compute_pv_power(moment, array_lat, array_lon, sample.cloud, orientation, array_ctx))

    return out


def compute_pv_power_weighted(
    moment: datetime,
    home_lat: float,
    home_lon: float,
    sample: WeatherSample,
    layout: PvLayout,
    gti_sampler: Optional[GtiSampler] = None,
) -> float:
    """Forecast PV percentage (0..100) summed across arrays, weighted by kWp share. Thin wrapper over
    ``compute_pv_power_per_array`` so the two never diverge."""
    pcts = compute_pv_power_per_array(moment, home_lat, home_lon, sample, layout, gti_sampler)
    orientations = layout.orientations
    if not orientations or len(pcts) != len(orientations):
        return pcts[0]
    return sum(pcts[i] * layout.shares[i] for i in range(len(pcts)))
