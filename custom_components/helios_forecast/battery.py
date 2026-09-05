"""Battery state-of-charge projection.

Walks the PV forecast against the learned consumption profile over the coming hours and
integrates the battery's charge. The default arbitration is the universal one: surplus
(PV above the house load) charges the battery, a deficit discharges it to cover the house,
each bounded by the configured charge / discharge power and the usable capacity. Round-trip
losses are split evenly across the two sides.

It does not model a vendor's own charge schedule (time-of-use force-charging and the like):
those are invisible from the outside, so the projection assumes the battery follows the
house. It carries no charge command either, only the predicted curve. Pure functions, no
Home Assistant.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta, tzinfo
from typing import List

from .config import BatteryConfig
from .consumption import ConsumptionProfile
from .forecast import ForecastPoint

# Tolerance (minutes) on how far a real timestamp gap may drift from the declared step_minutes
# before it is treated as a cadence mismatch, not float/scheduling jitter.
_STEP_TOLERANCE_MIN = 0.5


class StepCadenceError(ValueError):
    """Raised when the forecast points' real timestamp spacing does not match step_minutes."""


@dataclass(frozen=True)
class BatterySocPoint:
    """One projected point: the battery state of charge (percent) at time ``t``."""

    t: datetime
    soc: float


def project_battery_soc(
    config: BatteryConfig,
    start_soc_frac: float,
    points: List[ForecastPoint],
    profile: ConsumptionProfile,
    *,
    now: datetime,
    tz: tzinfo,
    horizon_hours: float = 24.0,
    step_minutes: int = 15,
) -> List[BatterySocPoint]:
    """Project the SoC forward from ``start_soc_frac`` over ``horizon_hours``.

    Steps through the forecast points inside [now, now + horizon] at their native cadence:
    per step, PV minus the profile's consumption is charged into (or discharged out of) the
    battery, clamped to [min reserve, full] and to the charge / discharge power. Returns one
    point per step (SoC in percent); empty when the battery is unusable or no points fall in
    the window.

    Raises ``StepCadenceError`` if two consecutive points inside the window are spaced further
    than ``step_minutes`` (beyond a small tolerance) apart: ``dt_h`` trusts ``step_minutes``
    alone, so a real gap that disagrees with it would otherwise mis-integrate silently.
    """
    cap_wh = config.capacity_kwh * 1000.0
    if cap_wh <= 0:
        return []

    min_wh = config.min_soc_frac * cap_wh
    # Split the round-trip loss evenly: a full cycle in and back out keeps `efficiency`.
    side_eff = math.sqrt(config.efficiency)
    dt_h = step_minutes / 60.0
    end = now + timedelta(hours=horizon_hours)

    soc_wh = min(cap_wh, max(min_wh, start_soc_frac * cap_wh))
    out: List[BatterySocPoint] = []
    prev_t: datetime | None = None
    for point in points:
        if point.t < now or point.t >= end:
            continue
        if prev_t is not None:
            gap_min = (point.t - prev_t).total_seconds() / 60.0
            if abs(gap_min - step_minutes) > _STEP_TOLERANCE_MIN:
                raise StepCadenceError(
                    f"forecast points are {gap_min:g} min apart, step_minutes={step_minutes} was declared"
                )
        prev_t = point.t
        net_w = point.pv_w - profile.at(point.t.astimezone(tz))
        if net_w >= 0.0:
            # Surplus charges the battery, capped by the charge power and the room left.
            terminal_wh = min(net_w, config.max_charge_w) * dt_h
            soc_wh = min(cap_wh, soc_wh + terminal_wh * side_eff)
        else:
            # Deficit discharges it, capped by the discharge power and the usable charge above the reserve.
            terminal_wh = min(-net_w, config.max_discharge_w) * dt_h
            soc_wh = max(min_wh, soc_wh - terminal_wh / side_eff)
        out.append(BatterySocPoint(t=point.t, soc=round(soc_wh / cap_wh * 100.0, 2)))
    return out
