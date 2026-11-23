"""360-degree horizon shading interpolation and beam attenuation factor.

Pure functions, no Home Assistant imports. Defines a 24-sector (15 deg per sector)
horizon obstacle profile, circular linear azimuth interpolation, and differentiable
sigmoidal direct beam attenuation.
"""

from __future__ import annotations

import math
from typing import List, Optional

HORIZON_SECTORS = 24
AZ_STEP_DEG = 360.0 / HORIZON_SECTORS  # 15.0 deg
DEFAULT_WIDTH_DEG = 2.0


def default_horizon_profile() -> List[float]:
    """An unshaded 24-sector horizon profile (all 0.0 degrees)."""
    return [0.0] * HORIZON_SECTORS


def interpolate_horizon_elevation(azimuth_deg: float, profile: Optional[List[float]]) -> float:
    """Obstacle elevation angle (degrees) interpolated at azimuth_deg.

    Uses circular linear interpolation across 24 sectors with periodic wrapping
    (360 deg = 0 deg). Unset or invalid profiles return 0.0 deg.
    """
    if not profile or len(profile) != HORIZON_SECTORS:
        return 0.0

    az = azimuth_deg % 360.0
    idx_f = az / AZ_STEP_DEG
    i0 = math.floor(idx_f) % HORIZON_SECTORS
    i1 = (i0 + 1) % HORIZON_SECTORS
    frac = idx_f - math.floor(idx_f)

    h0 = profile[i0]
    h1 = profile[i1]

    if not (math.isfinite(h0) and math.isfinite(h1)):
        return 0.0

    return h0 + (h1 - h0) * frac


def beam_shading_factor(
    solar_alt_deg: float,
    solar_az_deg: float,
    profile: Optional[List[float]],
    width_deg: float = DEFAULT_WIDTH_DEG,
) -> float:
    """Direct beam attenuation factor in [0.0, 1.0].

    Returns 1.0 (unshaded) when profile is unset or all zeroes. Uses a smooth
    sigmoid centered at the interpolated horizon obstacle elevation.
    """
    if solar_alt_deg <= 0.0:
        return 0.0

    if not profile or not any(profile):
        return 1.0

    h_elev = interpolate_horizon_elevation(solar_az_deg, profile)
    if h_elev <= 0.0:
        return 1.0

    w = max(0.1, width_deg)
    # Smooth sigmoid transition: 1.0 well above obstacle, 0.0 well below.
    return 1.0 / (1.0 + math.exp(-(solar_alt_deg - h_elev) / w))


def parse_horizon_profile(value: object) -> List[float]:
    """Parse user-provided horizon input (string of comma-separated floats, list, etc.) into 24 floats.

    Accepts comma-separated strings ("0,0,5,10,...") or numeric lists. Pads short inputs with
    0.0, truncates long inputs to 24 elements, and clamps angles to [0.0, 90.0].
    """
    if isinstance(value, str):
        parts = [p.strip() for p in value.split(",") if p.strip()]
        raw: List[float] = []
        for p in parts:
            try:
                raw.append(float(p))
            except ValueError:
                continue
    elif isinstance(value, (list, tuple)):
        raw = []
        for item in value:
            try:
                raw.append(float(item))
            except (TypeError, ValueError):
                continue
    else:
        return default_horizon_profile()

    out: List[float] = []
    for val in raw[:HORIZON_SECTORS]:
        if math.isfinite(val):
            out.append(max(0.0, min(90.0, val)))
        else:
            out.append(0.0)

    if len(out) < HORIZON_SECTORS:
        out.extend([0.0] * (HORIZON_SECTORS - len(out)))

    return out
