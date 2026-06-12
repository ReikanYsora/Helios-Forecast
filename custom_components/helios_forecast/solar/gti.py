"""Open-Meteo GTI lookup, port of the sampling side of the card's gti.ts.

One plane-of-array series per distinct orientation, keyed in Helios azimuth
convention (0 = north). ``sample_gti`` linearly interpolates between the two
bracketing hourly samples so a sub-hourly forecast bucket reads a smooth POA.
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Dict, Optional

from ..openmeteo import GtiSeries

GtiStore = Dict[str, GtiSeries]


def orientation_key(tilt_deg: float, azimuth_deg: float) -> str:
    """Stable 1-degree-binned key in Helios convention, matching gti.ts."""
    t = round(tilt_deg)
    a = round(((azimuth_deg % 360) + 360) % 360)
    return f"{t}|{a}"


def _epoch(t: datetime) -> float:
    return (t if t.tzinfo else t.replace(tzinfo=timezone.utc)).timestamp()


def _bad(v: float) -> bool:
    return not (isinstance(v, (int, float)) and math.isfinite(v) and v >= 0)


def sample_gti(store: Optional[GtiStore], tilt_deg: float, azimuth_deg: float, moment: datetime) -> Optional[float]:
    """POA at ``moment`` for one orientation, or None when no usable data.

    None falls the caller back to its own transposition, exactly as in the card.
    """
    if not store:
        return None
    s = store.get(orientation_key(tilt_deg, azimuth_deg))
    if s is None or not s.times:
        return None

    t_ms = _epoch(moment)
    times = [_epoch(t) for t in s.times]

    # i1 = first sample at or after the target, i0 its predecessor.
    i1 = len(times) - 1
    for i in range(len(times)):
        if times[i] >= t_ms:
            i1 = i
            break
    i0 = max(0, i1 - 1)
    v0 = s.poa[i0]
    v1 = s.poa[i1]
    b0 = _bad(v0)
    b1 = _bad(v1)
    if b0 and b1:
        return None
    if b0:
        return v1
    if b1:
        return v0
    t0 = times[i0]
    t1 = times[i1]
    if t1 <= t0:
        return v1
    f = max(0.0, min(1.0, (t_ms - t0) / (t1 - t0)))
    return v0 + (v1 - v0) * f
