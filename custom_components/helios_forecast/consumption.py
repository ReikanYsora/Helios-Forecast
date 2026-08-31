"""Home consumption profile derived from the Home Assistant Energy dashboard.

Home consumption is not a single sensor. The Energy dashboard defines it as
solar + grid_import - grid_export + battery_discharge - battery_charge, so we read
the dashboard's configured statistic ids, sign them, and let the coordinator fetch
their recorder history (reusing its hourly change-bucket fetch). The signed hourly
sums are the home's real past consumption, which we average into a per-weekday-hour
profile the SoC projection queries for any future step.

Pure functions, no Home Assistant: the coordinator passes the prefs dict and the
fetched buckets, so the derivation and the profile can be unit-tested on their own.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, tzinfo
from typing import Dict, List, Optional

from .solar.residual import ProductionBucket

_HOURS = 24  # slots are weekday (0=Mon) * 24 + hour


@dataclass(frozen=True)
class ConsumptionSources:
    """Statistic ids whose signed hourly change sums to home consumption.

    +1 adds to consumption (solar produced, grid imported, battery discharged);
    -1 subtracts (grid exported, battery charged)."""

    signed: Dict[str, int]


def _add(signed: Dict[str, int], stat_id: Optional[str], sign: int) -> None:
    if stat_id:
        signed[stat_id] = sign


def consumption_sources(prefs: Optional[dict]) -> ConsumptionSources:
    """Extract the signed statistic ids for home consumption from the Energy prefs.

    Handles both the unified grid format (``stat_energy_from`` / ``stat_energy_to``) and the
    legacy one (``flow_from`` / ``flow_to`` lists). Missing sources (no battery, no export)
    simply drop out.
    """
    signed: Dict[str, int] = {}
    for src in (prefs or {}).get("energy_sources") or []:
        source_type = src.get("type")
        if source_type == "solar":
            _add(signed, src.get("stat_energy_from"), +1)
        elif source_type == "battery":
            _add(signed, src.get("stat_energy_from"), +1)  # discharge, out of the battery
            _add(signed, src.get("stat_energy_to"), -1)  # charge, into the battery
        elif source_type == "grid":
            _add(signed, src.get("stat_energy_from"), +1)  # import (unified format)
            _add(signed, src.get("stat_energy_to"), -1)  # export (unified format)
            for flow in src.get("flow_from") or []:  # legacy import list
                _add(signed, flow.get("stat_energy_from"), +1)
            for flow in src.get("flow_to") or []:  # legacy export list
                _add(signed, flow.get("stat_energy_to"), -1)
    return ConsumptionSources(signed=signed)


@dataclass(frozen=True)
class ConsumptionProfile:
    """Average home consumption in watts, by (weekday, hour) slot, with fallbacks.

    ``at`` resolves a future instant to its slot, falling back to the same hour across all
    days, then to the overall average, so a sparsely-covered slot never leaves a gap."""

    slot_w: Dict[int, float]
    hour_w: Dict[int, float]
    overall_w: float
    samples: int  # hours of history that fed the profile, for the caller to gauge confidence

    def at(self, moment_local: datetime) -> float:
        slot = moment_local.weekday() * _HOURS + moment_local.hour
        if slot in self.slot_w:
            return self.slot_w[slot]
        if moment_local.hour in self.hour_w:
            return self.hour_w[moment_local.hour]
        return self.overall_w


def build_consumption_profile(
    sources: ConsumptionSources,
    buckets_by_id: Dict[str, List[ProductionBucket]],
    tz: tzinfo,
) -> Optional[ConsumptionProfile]:
    """Average the signed hourly history into a per-weekday-hour consumption profile (watts).

    All ids share the recorder's hourly grid, so their buckets sum per hour by start. A kWh over
    one hour is that many mean watts; net consumption is floored at 0 (a derivation that dips
    slightly negative is meter noise, never real). None when no history backs any source.
    """
    per_hour_kwh: Dict[int, float] = {}
    for stat_id, sign in sources.signed.items():
        for bucket in buckets_by_id.get(stat_id, []):
            key = int(bucket.start_ms)
            per_hour_kwh[key] = per_hour_kwh.get(key, 0.0) + sign * bucket.kwh
    if not per_hour_kwh:
        return None

    slot_sum: Dict[int, float] = {}
    slot_n: Dict[int, int] = {}
    hour_sum: Dict[int, float] = {}
    hour_n: Dict[int, int] = {}
    total_w = 0.0
    n = 0
    for ms, kwh in per_hour_kwh.items():
        watts = max(0.0, kwh * 1000.0)
        moment = datetime.fromtimestamp(ms / 1000.0, tz)
        slot = moment.weekday() * _HOURS + moment.hour
        slot_sum[slot] = slot_sum.get(slot, 0.0) + watts
        slot_n[slot] = slot_n.get(slot, 0) + 1
        hour_sum[moment.hour] = hour_sum.get(moment.hour, 0.0) + watts
        hour_n[moment.hour] = hour_n.get(moment.hour, 0) + 1
        total_w += watts
        n += 1

    slot_w = {slot: slot_sum[slot] / slot_n[slot] for slot in slot_sum}
    hour_w = {hour: hour_sum[hour] / hour_n[hour] for hour in hour_sum}
    overall_w = total_w / n if n else 0.0
    return ConsumptionProfile(slot_w=slot_w, hour_w=hour_w, overall_w=overall_w, samples=n)
