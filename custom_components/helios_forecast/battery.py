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
from typing import Dict, List

from .config import BatteryConfig
from .consumption import ConsumptionProfile
from .forecast import ForecastPoint


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

    ``step_minutes`` is the declared cadence, used only for the last step, which has no successor
    to measure against; every other step lasts the real time to the next point.
    """
    cap_wh = config.capacity_kwh * 1000.0
    if cap_wh <= 0:
        return []

    min_wh = config.min_soc_frac * cap_wh
    # Split the round-trip loss evenly: a full cycle in and back out keeps `efficiency`.
    side_eff = math.sqrt(config.efficiency)
    end = now + timedelta(hours=horizon_hours)

    # The charge the battery actually holds, not the reserve. A battery sitting under its reserve,
    # after an outage or a manual discharge, was read up to it and the whole chart then started
    # above the level the house can see on the inverter.
    soc_wh = min(cap_wh, max(0.0, start_soc_frac * cap_wh))
    out: List[BatterySocPoint] = []
    # One point per real instant, in real order. The forecast series is built on the local clock, so
    # on the day daylight saving moves the clock forward it names four quarter-hours that never
    # happen, and those land on the very instants of the four that follow: the series is not even
    # ascending in real time. Keying on the POSIX timestamp keeps, of each pair, the one a clock in
    # the house would show, so no instant is integrated twice.
    by_instant: Dict[float, ForecastPoint] = {}
    for p in sorted((p for p in points if now <= p.t < end), key=lambda p: p.t.timestamp()):
        by_instant[p.t.timestamp()] = p
    window = list(by_instant.values())

    for i, point in enumerate(window):
        # A step lasts the real time to the next point, off the timestamps again. Subtracting the
        # datetimes would not do: two aware datetimes sharing one tzinfo subtract on the wall clock,
        # so the hour daylight saving adds in autumn would be integrated as an ordinary quarter of
        # an hour and a whole hour of house load would go missing. The last step has no successor to
        # measure against and falls back on the declared cadence.
        if i + 1 < len(window):
            dt_h = (window[i + 1].t.timestamp() - point.t.timestamp()) / 3600.0
        else:
            dt_h = step_minutes / 60.0
        net_w = point.pv_w - profile.at(point.t.astimezone(tz))
        if net_w >= 0.0:
            # Surplus charges the battery, capped by the charge power and the room left.
            terminal_wh = min(net_w, config.max_charge_w) * dt_h
            soc_wh = min(cap_wh, soc_wh + terminal_wh * side_eff)
        else:
            # Deficit discharges it, capped by the discharge power and the usable charge above the
            # reserve. Below the reserve there is nothing to draw and the level holds where it is:
            # floored to the reserve instead, the projection would hand back energy that is not there.
            terminal_wh = min(-net_w, config.max_discharge_w) * dt_h
            soc_wh -= min(terminal_wh / side_eff, max(0.0, soc_wh - min_wh))
        out.append(BatterySocPoint(t=point.t, soc=round(soc_wh / cap_wh * 100.0, 2)))
    return out
