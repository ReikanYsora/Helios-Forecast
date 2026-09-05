"""Curtailment detection for the learning: which produced hours say nothing about the sky.

An hour where the inverter was held back (a full battery, a zero-export rule, a grid limit) is not a low
hour, it is a right-censored one: true production was at least what was measured. Learning it as low burns
a depressed ratio into the residual map's sky cells and the analog library, which then underestimate the
days the battery is empty and nothing is clipped, on top of the cap the forecast already applies forward.

Two signals mark an hour, either is enough:
  - the battery was full AND the measured average power sat at the configured inverter cap: both are
    already in the config (state of charge entity, inverter cap), so the common hybrid case needs nothing new;
  - an optional curtailment entity the user provides (binary sensor, input boolean or switch) was on for
    at least half the hour: zero-export and grid-limited setups the integration cannot see by itself.

Pure, no Home Assistant imports.
"""

from __future__ import annotations

import math
from dataclasses import replace
from typing import Dict, List, Optional, Sequence, Tuple

from .solar.residual import ProductionBucket

# Hourly maximum state of charge (%) at or above which the battery had nowhere to put more.
FULL_SOC_PCT = 98.0
# Hourly average power at or above this share of the inverter cap = the inverter sat at its limit.
CAP_FRACTION = 0.92
# The curtailment entity has to be on for at least this share of the hour.
ON_FRACTION = 0.5


def _on_share(start_ms: float, end_ms: float, intervals: Sequence[Tuple[float, float]]) -> float:
    """Share of [start_ms, end_ms) covered by the on-intervals (each [from, to) in epoch ms)."""
    span = end_ms - start_ms
    if span <= 0:
        return 0.0
    covered = 0.0
    for a, b in intervals:
        lo = max(a, start_ms)
        hi = min(b, end_ms)
        if hi > lo:
            covered += hi - lo
    return covered / span


def flag_curtailed(
    buckets: List[ProductionBucket],
    *,
    soc_max_by_start_ms: Optional[Dict[float, float]] = None,
    cap_w: Optional[float] = None,
    on_intervals: Optional[Sequence[Tuple[float, float]]] = None,
) -> List[ProductionBucket]:
    """The same buckets, `curtailed` set where either signal says the inverter was held back.

    `soc_max_by_start_ms` maps a bucket's start (epoch ms) to the hour's maximum state of charge (%);
    `cap_w` is the inverter cap in watts (None or non-finite = unknown, the battery signal is then
    unusable, a full battery under a heavy house load is not curtailment); `on_intervals` are the
    curtailment entity's on periods."""
    cap = cap_w if (cap_w is not None and math.isfinite(cap_w) and cap_w > 0) else None
    out: List[ProductionBucket] = []
    for b in buckets:
        curtailed = False
        if cap is not None and soc_max_by_start_ms:
            soc_max = soc_max_by_start_ms.get(b.start_ms)
            hours = (b.end_ms - b.start_ms) / 3_600_000.0
            avg_w = b.kwh * 1000.0 / hours if hours > 0 else 0.0
            if soc_max is not None and soc_max >= FULL_SOC_PCT and avg_w >= CAP_FRACTION * cap:
                curtailed = True
        if not curtailed and on_intervals and _on_share(b.start_ms, b.end_ms, on_intervals) >= ON_FRACTION:
            curtailed = True
        out.append(replace(b, curtailed=True) if curtailed and not b.curtailed else b)
    return out


def on_intervals_from_states(
    states: Sequence[Tuple[float, str]], start_ms: float, end_ms: float
) -> List[Tuple[float, float]]:
    """Turn a (timestamp ms, state) history into the [from, to) periods the entity was "on", clipped to
    [start_ms, end_ms). The history must include the state in force at `start_ms` as its first item."""
    out: List[Tuple[float, float]] = []
    on_since: Optional[float] = None
    for t, state in states:
        is_on = state == "on"
        if is_on and on_since is None:
            on_since = max(t, start_ms)
        elif not is_on and on_since is not None:
            if t > on_since:
                out.append((on_since, min(t, end_ms)))
            on_since = None
    if on_since is not None and end_ms > on_since:
        out.append((on_since, end_ms))
    return out
