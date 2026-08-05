"""Open-Meteo GTI lookup, port of the sampling side of the card's gti.ts.

One plane-of-array series per distinct orientation, keyed in Helios azimuth
convention (0 = north). ``sample_gti`` linearly interpolates between the two
bracketing hourly samples so a sub-hourly forecast bucket reads a smooth POA.
"""

from __future__ import annotations

import math
from bisect import bisect_left
from datetime import datetime, timezone
from typing import Callable, Dict, Optional

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


# A (tilt, azimuth, moment) -> POA sampler, the shape build_forecast_series + the residual map expect.
GtiSampler = Callable[[float, float, datetime], Optional[float]]


class _PreparedGti:
    """A GtiStore with each series' epoch axis precomputed once.

    ``sample_gti`` rebuilt ``[_epoch(t) for t in series.times]`` on every call (thousands of times per
    refresh, each over ~1600 hourly stamps), which dominated the refresh CPU. Computing the epoch axis
    once per store and binary-searching it makes each sample O(log n) with no per-call allocation.
    """

    __slots__ = ("store", "epochs")

    def __init__(self, store: GtiStore) -> None:
        self.store = store
        # Same keys as ``store``, so a key found in ``store`` is always present here too.
        self.epochs: Dict[str, list] = {key: [_epoch(t) for t in s.times] for key, s in store.items()}


def make_gti_sampler(store: Optional[GtiStore]) -> Optional[GtiSampler]:
    """Build a POA sampler that precomputes each series' epoch axis once, or None when there is no
    store (matching the old ``... if store else None`` guard at the call sites)."""
    if not store:
        return None
    prepared = _PreparedGti(store)

    def _sample(tilt_deg: float, azimuth_deg: float, moment: datetime) -> Optional[float]:
        return _sample_gti_prepared(prepared, tilt_deg, azimuth_deg, moment)

    return _sample


def _sample_gti_prepared(
    prepared: _PreparedGti, tilt_deg: float, azimuth_deg: float, moment: datetime
) -> Optional[float]:
    """Identical result to ``sample_gti``, but reads the store's precomputed epoch axis and locates the
    bracket with ``bisect`` instead of rebuilding + linearly scanning it on every call."""
    key = orientation_key(tilt_deg, azimuth_deg)
    s = prepared.store.get(key)
    if s is None or not s.times:
        return None
    times = prepared.epochs[key]

    t_ms = _epoch(moment)
    # i1 = first sample at or after the target, i0 its predecessor. bisect_left gives the first index
    # whose epoch is >= t_ms; when the target is past the last sample it returns len(times), which we
    # clamp to the last index, reproducing the old scan's "i1 stays len-1" fallthrough exactly.
    i1 = bisect_left(times, t_ms)
    if i1 >= len(times):
        i1 = len(times) - 1
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
