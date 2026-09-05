"""Weighted multi-orientation PV power.

Sums ``compute_pv_power`` across every configured array, weighted by its share of
the total kWp. Pure, no Home Assistant imports.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime
from typing import List, Optional, Tuple

from .irradiance import PanelOrientation, PvContext, _supplied, compute_pv_power


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
    # Per-array inverter cap in watts (INF when that array has none). Empty = no per-array caps, only the combined
    # total is clipped at the entry-level cap. When present, each array is clipped at its own cap BEFORE the arrays
    # are summed, for micro-inverter strings that saturate independently.
    caps: List[float] = field(default_factory=list)


def _finite(value: Optional[float]) -> bool:
    return value is not None and math.isfinite(value)


def compute_pv_power_per_array(
    moment: datetime,
    home_lat: float,
    home_lon: float,
    sample: WeatherSample,
    layout: PvLayout,
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
            wind_ms=(sample.wind / 3.6) if sample.wind is not None else None,
            ghi_wm2=sample.ghi if has_ghi else None,
            direct_wm2=sample.direct if has_split else None,
            diffuse_wm2=sample.diffuse if has_split else None,
        )
        if base_present
        else None
    )

    orientations = layout.orientations
    # Out-of-lockstep layout: fall back to the horizontal path so the curve still renders. A single-element list
    # signals "one array over the whole layout".
    if not orientations or len(layout.shares) != len(orientations) or len(layout.coords) != len(orientations):
        return [compute_pv_power(moment, home_lat, home_lon, sample.cloud, None, base_ctx)]

    out: List[float] = []
    for orientation in orientations:
        idx = len(out)
        coord = layout.coords[idx]
        array_lat = coord[0] if coord else home_lat
        array_lon = coord[1] if coord else home_lon
        # Every array shares the same weather context; each orientation self-transposes the GHI to its
        # own plane in compute_pv_power.
        out.append(compute_pv_power(moment, array_lat, array_lon, sample.cloud, orientation, base_ctx))

    return out


def compute_pv_power_weighted(
    moment: datetime,
    home_lat: float,
    home_lon: float,
    sample: WeatherSample,
    layout: PvLayout,
) -> float:
    """Forecast PV percentage (0..100) summed across arrays, weighted by kWp share. Thin wrapper over
    ``compute_pv_power_per_array`` so the two never diverge."""
    pcts = compute_pv_power_per_array(moment, home_lat, home_lon, sample, layout)
    orientations = layout.orientations
    if not orientations or len(pcts) != len(orientations):
        return pcts[0]
    return sum(pcts[i] * layout.shares[i] for i in range(len(pcts)))
