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

from dataclasses import dataclass, field
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
            # A grid source carries its meters in two required lists and never at the top level
            # (homeassistant/components/energy/data.py, GRID_SOURCE_SCHEMA).
            for flow in src.get("flow_from") or []:
                _add(signed, flow.get("stat_energy_from"), +1)  # import
            for flow in src.get("flow_to") or []:
                _add(signed, flow.get("stat_energy_to"), -1)  # export
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
    # Per statistic id, the share of the hours ANY source reported that this one also reported
    # (0..1, see source_coverage). A source far below the others is a meter reporting
    # intermittently, which dilutes the profile rather than leaving a visible gap (see checkup.py).
    coverage: Dict[str, float] = field(default_factory=dict)

    def at(self, moment_local: datetime) -> float:
        slot = moment_local.weekday() * _HOURS + moment_local.hour
        if slot in self.slot_w:
            return self.slot_w[slot]
        if moment_local.hour in self.hour_w:
            return self.hour_w[moment_local.hour]
        return self.overall_w


# A source with a bucket for fewer than this share of the hours the best-covered source has is
# sparse: the profile is then built only from the hours it does cover. The recorder writes an hourly
# row as soon as a sensor had a valid state in that hour, even unchanged, so a missing row means no
# data, not zero; summing the other sources in those hours would silently pull the profile down.
SPARSE_COVERAGE_RATIO = 0.5


def source_coverage(sources: ConsumptionSources, buckets_by_id: Dict[str, List[ProductionBucket]]) -> Dict[str, float]:
    """Share of the union of hours each source has a bucket for, by statistic id (0..1)."""
    hours_by_id = {sid: {int(b.start_ms) for b in buckets_by_id.get(sid, [])} for sid in sources.signed}
    union: set = set()
    for hours in hours_by_id.values():
        union |= hours
    if not union:
        return {sid: 0.0 for sid in sources.signed}
    return {sid: len(hours) / len(union) for sid, hours in hours_by_id.items()}


def build_consumption_profile(
    sources: ConsumptionSources,
    buckets_by_id: Dict[str, List[ProductionBucket]],
    tz: tzinfo,
) -> Optional[ConsumptionProfile]:
    """Average the signed hourly history into a per-weekday-hour consumption profile (watts).

    All ids share the recorder's hourly grid, so their buckets sum per hour by start. A kWh over
    one hour is that many mean watts; net consumption is floored at 0 (a derivation that dips
    slightly negative is meter noise, never real). None when no history backs any source.

    An hour only counts when every sparse source (see SPARSE_COVERAGE_RATIO) has a bucket for it:
    a battery whose discharge meter reports a quarter of the time would otherwise carry the night
    load a quarter of the time and the profile would learn a house that barely consumes after dark.
    """
    coverage = source_coverage(sources, buckets_by_id)
    best = max(coverage.values(), default=0.0)
    sparse = [sid for sid, share in coverage.items() if 0 < share < SPARSE_COVERAGE_RATIO * best]
    required: Optional[set] = None
    for sid in sparse:
        hours = {int(b.start_ms) for b in buckets_by_id.get(sid, [])}
        required = hours if required is None else required & hours

    per_hour_kwh: Dict[int, float] = {}
    for stat_id, sign in sources.signed.items():
        for bucket in buckets_by_id.get(stat_id, []):
            key = int(bucket.start_ms)
            if required is not None and key not in required:
                continue
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
    return ConsumptionProfile(slot_w=slot_w, hour_w=hour_w, overall_w=overall_w, samples=n, coverage=coverage)
