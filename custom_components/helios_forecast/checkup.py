"""Systematic check-up of an entry's configuration and of the data it feeds on.

A forecast that runs on a wrong configuration produces wrong numbers with a straight face: a peak
power typed in watts, an inverter limit in watts, a meter that counts the whole house, a
battery whose charge power is ten times its capacity. None of it crashes, all of it makes the
integration look bad. So every field is checked, at startup and after every refresh, and each
problem found becomes a repair issue the user sees in Home Assistant, with the value at fault and
what to do about it.

Pure functions, no Home Assistant imports: the coordinator gathers the facts (entity states,
recorder history) into the small snapshots defined here and calls the checks; the tests do the
same from fixtures.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from .config import (
    CONF_ARRAYS,
    CONF_AZIMUTH,
    CONF_BATTERY_CAPACITY_KWH,
    CONF_BATTERY_EFFICIENCY,
    CONF_BATTERY_MAX_CHARGE_KW,
    CONF_BATTERY_MAX_DISCHARGE_KW,
    CONF_BATTERY_MIN_SOC,
    CONF_BATTERY_SOC_ENTITY,
    CONF_CURTAILMENT_ENTITY,
    CONF_INVERTER_MAX_KW,
    CONF_KWP,
    CONF_LATITUDE,
    CONF_LINE_INVERTER_MAX_KW,
    CONF_LONGITUDE,
    CONF_PRODUCTION_ENTITY,
    CONF_TILT,
    CONF_TRACKER,
    CONF_TREND_ANCHOR_HOUR,
    TRACKER_NONE,
    _VALID_TRACKERS,
)
from .solar.geometry import sun_position
from .solar.residual import ABOVE_PANELS_RATIO, ProductionBucket

ERROR = "error"
WARNING = "warning"

# Bounds. A home line above 100 kWp is a value typed in watts; an inverter limit far above the peak
# power is the same mistake on the other field, and one below a quarter of it is a limit that would
# clip most of the day (possible, so a warning). The upper ratio is deliberately wide: a hybrid
# inverter is sized for the house rather than for the array, so three kilowatts of panels behind a
# ten-kilowatt battery inverter is an ordinary installation and must not be called an error, while
# the mistake this catches is a factor of a thousand. Panel coordinates far from the home or a
# configured location far from Home Assistant's are almost always a typo in a decimal.
KWP_MIN = 0.05
KWP_MAX = 100.0
CAP_RATIO_MAX = 10.0
CAP_RATIO_MIN = 0.25
LINE_DISTANCE_MAX_KM = 20.0
LOCATION_DISTANCE_MAX_KM = 50.0
BATTERY_CAPACITY_MIN_KWH = 0.5
BATTERY_CAPACITY_MAX_KWH = 200.0
BATTERY_C_RATE_MAX = 3.0
# Data. Night is the sun well under the horizon, so dusk never counts; a meter that records more
# than half a kWh, or three percent of the day's energy, in the dark measures more than the panels.
# An hour above the declared panels by a third, several times, says the peak power is too small or
# the meter counts something else. No production for three days is worth a word, not an alarm.
NIGHT_ALTITUDE_DEG = -6.0
NIGHT_KWH_FLOOR = 0.5
NIGHT_SHARE = 0.03
# ABOVE_PANELS_RATIO comes from the learning itself (solar/residual.py), which drops such an hour:
# the owner is told about exactly the hours their learning refused, and the two cannot drift apart.
ABOVE_PANELS_HOURS = 3
STALE_DAYS = 3
# A consumption source that covers less than half the hours the best-covered one does dilutes the
# learned profile (see consumption.py); below this it is named.
SPARSE_COVERAGE_RATIO = 0.5

ENERGY_UNITS = ("kWh", "Wh", "MWh")
CUMULATIVE_STATE_CLASSES = ("total", "total_increasing")


@dataclass(frozen=True)
class Problem:
    """One thing wrong. `key` names the repair issue's text, `scope` tells two problems of the same
    kind apart (a line index, a statistic id), `placeholders` fill the text with the value at fault."""

    key: str
    severity: str
    placeholders: Dict[str, str] = field(default_factory=dict)
    scope: str = ""

    @property
    def issue_id(self) -> str:
        return f"{self.key}_{self.scope}" if self.scope else self.key


@dataclass(frozen=True)
class EntitySnapshot:
    """What the check-up reads of an entity: whether it exists and the attributes that type it."""

    entity_id: str
    exists: bool
    state: Optional[str] = None
    unit: Optional[str] = None
    device_class: Optional[str] = None
    state_class: Optional[str] = None


def _as_float(value: Any) -> Optional[float]:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _fmt(value: float) -> str:
    """A number for a sentence: no trailing zeros, no float noise."""
    if abs(value - round(value)) < 1e-9:
        return str(int(round(value)))
    return f"{value:.2f}".rstrip("0").rstrip(".")


def _distance_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance, haversine."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = p2 - p1
    dlmb = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlmb / 2) ** 2
    return 2 * 6371.0 * math.asin(math.sqrt(min(1.0, a)))


# --- the configuration itself -----------------------------------------------------------------


def check_config(data: Dict[str, Any], home_lat: float, home_lon: float) -> List[Problem]:
    """Every problem the stored configuration shows on its own, without looking at any data."""
    problems: List[Problem] = []
    lat = _as_float(data.get(CONF_LATITUDE))
    lon = _as_float(data.get(CONF_LONGITUDE))
    if lat is not None and lon is not None:
        if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
            problems.append(Problem("location_invalid", ERROR, {"latitude": _fmt(lat), "longitude": _fmt(lon)}))
            lat, lon = home_lat, home_lon
        else:
            km = _distance_km(lat, lon, home_lat, home_lon)
            if km > LOCATION_DISTANCE_MAX_KM:
                problems.append(Problem("location_far", WARNING, {"km": _fmt(round(km))}))
    else:
        lat, lon = home_lat, home_lon

    lines = list(data.get(CONF_ARRAYS) or [])
    if not lines:
        problems.append(Problem("no_lines", ERROR))
    total_kwp = 0.0
    for index, line in enumerate(lines, start=1):
        scope = str(index)
        ph = {"line": scope}
        kwp = _as_float(line.get(CONF_KWP))
        if kwp is None or kwp < KWP_MIN:
            problems.append(Problem("line_kwp_missing", ERROR, ph, scope))
        elif kwp > KWP_MAX:
            problems.append(Problem("line_kwp_unit", ERROR, {**ph, "kwp": _fmt(kwp)}, scope))
        else:
            total_kwp += kwp
        tilt = _as_float(line.get(CONF_TILT))
        if tilt is None or not (0.0 <= tilt <= 90.0):
            problems.append(Problem("line_tilt", ERROR, {**ph, "tilt": "?" if tilt is None else _fmt(tilt)}, scope))
        azimuth = _as_float(line.get(CONF_AZIMUTH))
        if azimuth is None or not (0.0 <= azimuth <= 360.0):
            problems.append(
                Problem("line_azimuth", ERROR, {**ph, "azimuth": "?" if azimuth is None else _fmt(azimuth)}, scope)
            )
        tracker = line.get(CONF_TRACKER)
        if tracker not in (None, "", TRACKER_NONE) and tracker not in _VALID_TRACKERS:
            problems.append(Problem("line_tracker", ERROR, {**ph, "tracker": str(tracker)}, scope))
        line_lat = _as_float(line.get(CONF_LATITUDE))
        line_lon = _as_float(line.get(CONF_LONGITUDE))
        if line_lat is not None and line_lon is not None:
            if not (-90.0 <= line_lat <= 90.0 and -180.0 <= line_lon <= 180.0):
                problems.append(Problem("line_location_invalid", ERROR, ph, scope))
            else:
                km = _distance_km(line_lat, line_lon, lat, lon)
                if km > LINE_DISTANCE_MAX_KM:
                    problems.append(Problem("line_location_far", WARNING, {**ph, "km": _fmt(round(km))}, scope))
        line_cap = _as_float(line.get(CONF_LINE_INVERTER_MAX_KW))
        if line_cap is not None and line_cap > 0 and kwp is not None and KWP_MIN <= kwp <= KWP_MAX:
            if line_cap > CAP_RATIO_MAX * kwp:
                problems.append(Problem("line_cap_unit", ERROR, {**ph, "cap": _fmt(line_cap), "kwp": _fmt(kwp)}, scope))

    cap = _as_float(data.get(CONF_INVERTER_MAX_KW))
    if cap is not None and cap > 0 and total_kwp > 0:
        if cap > CAP_RATIO_MAX * total_kwp:
            problems.append(Problem("inverter_cap_unit", ERROR, {"cap": _fmt(cap), "kwp": _fmt(total_kwp)}))
        elif cap < CAP_RATIO_MIN * total_kwp:
            problems.append(Problem("inverter_cap_low", WARNING, {"cap": _fmt(cap), "kwp": _fmt(total_kwp)}))

    if not (data.get(CONF_PRODUCTION_ENTITY) or None):
        problems.append(Problem("production_entity_unset", WARNING))

    anchor = _as_float(data.get(CONF_TREND_ANCHOR_HOUR))
    if anchor is not None and not (0 <= anchor <= 23):
        problems.append(Problem("trend_anchor_hour", ERROR, {"hour": _fmt(anchor)}))

    problems.extend(_check_battery_config(data))

    return problems


def _check_battery_config(data: Dict[str, Any]) -> List[Problem]:
    """The battery block: only judged when the feature is on (a capacity and a SoC entity)."""
    problems: List[Problem] = []
    capacity = _as_float(data.get(CONF_BATTERY_CAPACITY_KWH))
    soc_entity = data.get(CONF_BATTERY_SOC_ENTITY) or None
    if capacity is None and not soc_entity:
        return problems
    if capacity is not None and capacity > 0 and not soc_entity:
        problems.append(Problem("battery_soc_entity_unset", WARNING, {"capacity": _fmt(capacity)}))
    if soc_entity and (capacity is None or capacity <= 0):
        problems.append(Problem("battery_capacity_unset", WARNING))
    if capacity is not None and capacity > 0 and not (BATTERY_CAPACITY_MIN_KWH <= capacity <= BATTERY_CAPACITY_MAX_KWH):
        problems.append(Problem("battery_capacity", ERROR, {"capacity": _fmt(capacity)}))
    if capacity is not None and capacity > 0:
        for conf, name in ((CONF_BATTERY_MAX_CHARGE_KW, "charge"), (CONF_BATTERY_MAX_DISCHARGE_KW, "discharge")):
            power = _as_float(data.get(conf))
            if power is not None and power > BATTERY_C_RATE_MAX * capacity:
                problems.append(
                    Problem("battery_power", WARNING, {"power": _fmt(power), "capacity": _fmt(capacity)}, name)
                )
    min_soc = _as_float(data.get(CONF_BATTERY_MIN_SOC))
    if min_soc is not None and not (0.0 <= min_soc <= 100.0):
        problems.append(Problem("battery_min_soc", ERROR, {"value": _fmt(min_soc)}))
    efficiency = _as_float(data.get(CONF_BATTERY_EFFICIENCY))
    if efficiency is not None and not (50.0 <= efficiency <= 100.0):
        problems.append(Problem("battery_efficiency", ERROR, {"value": _fmt(efficiency)}))
    return problems


# --- the entities the configuration points at ------------------------------------------------


# --- the benchmark collector's verdict --------------------------------------------------------


def check_benchmark_quality(quality: Optional[Dict[str, Any]]) -> List[Problem]:
    """What the collector answered about this installation: excluded from the public figures, and why.

    The owner hears it from their own instance rather than from the site, because the reason is
    always something only they can change.
    """
    if not isinstance(quality, dict):
        return []
    reason = quality.get("excluded")
    if not reason:
        return []
    return [Problem("benchmark_excluded", WARNING, {"reason": str(reason)}, str(reason))]


def check_entities(
    data: Dict[str, Any],
    production: Optional[EntitySnapshot],
    battery_soc: Optional[EntitySnapshot],
    curtailment: Optional[EntitySnapshot],
) -> List[Problem]:
    """Whether each configured entity exists and is the kind of entity the feature needs."""
    problems: List[Problem] = []
    if data.get(CONF_PRODUCTION_ENTITY) and production is not None:
        if not production.exists:
            problems.append(Problem("production_entity_missing", ERROR, {"entity": production.entity_id}))
        else:
            unit_ok = production.unit in ENERGY_UNITS
            class_ok = production.state_class in CUMULATIVE_STATE_CLASSES
            if not unit_ok or not class_ok:
                problems.append(
                    Problem(
                        "production_entity_kind",
                        ERROR,
                        {
                            "entity": production.entity_id,
                            "unit": production.unit or "?",
                            "state_class": production.state_class or "?",
                        },
                    )
                )
    if data.get(CONF_BATTERY_SOC_ENTITY) and battery_soc is not None:
        if not battery_soc.exists:
            problems.append(Problem("battery_soc_entity_missing", ERROR, {"entity": battery_soc.entity_id}))
        elif battery_soc.unit != "%":
            problems.append(
                Problem(
                    "battery_soc_entity_unit", ERROR, {"entity": battery_soc.entity_id, "unit": battery_soc.unit or "?"}
                )
            )
    if data.get(CONF_CURTAILMENT_ENTITY) and curtailment is not None and not curtailment.exists:
        problems.append(Problem("curtailment_entity_missing", ERROR, {"entity": curtailment.entity_id}))
    return problems


# --- the production history ------------------------------------------------------------------


def check_production_history(
    buckets: List[ProductionBucket],
    entity_id: str,
    lat: float,
    lon: float,
    total_kwp: float,
    now: datetime,
    learn_days: int,
) -> List[Problem]:
    """What the meter's own history says about the meter: empty, silent, counting at night, or
    counting more than the declared panels can make."""
    problems: List[Problem] = []
    ph = {"entity": entity_id}
    if not buckets:
        problems.append(Problem("production_history_empty", WARNING, ph))
        return problems

    now_utc = now.astimezone(timezone.utc) if now.tzinfo else now.replace(tzinfo=timezone.utc)
    night = 0.0
    day = 0.0
    last_positive_ms: Optional[float] = None
    above = 0
    peak_kw = 0.0
    for b in buckets:
        if b.kwh <= 0:
            continue
        mid = datetime.fromtimestamp((b.start_ms + b.end_ms) / 2000.0, timezone.utc)
        if sun_position(mid, lat, lon).altitude < NIGHT_ALTITUDE_DEG:
            night += b.kwh
        else:
            day += b.kwh
        last_positive_ms = max(last_positive_ms or 0.0, b.end_ms)
        hours = max(1e-6, (b.end_ms - b.start_ms) / 3_600_000.0)
        kw = b.kwh / hours
        peak_kw = max(peak_kw, kw)
        if total_kwp > 0 and kw > ABOVE_PANELS_RATIO * total_kwp:
            above += 1

    if night > max(NIGHT_KWH_FLOOR, NIGHT_SHARE * day):
        problems.append(Problem("production_at_night", ERROR, {**ph, "kwh": _fmt(round(night, 1))}))
    if above >= ABOVE_PANELS_HOURS:
        problems.append(
            Problem("production_above_panels", WARNING, {**ph, "kw": _fmt(round(peak_kw, 1)), "kwp": _fmt(total_kwp)})
        )
    if last_positive_ms is None:
        # The window spans the learn period; a meter that never moved in all of it is not learning anything.
        problems.append(Problem("production_stale", WARNING, {**ph, "days": _fmt(learn_days)}))
    else:
        silent = now_utc - datetime.fromtimestamp(last_positive_ms / 1000.0, timezone.utc)
        if silent >= timedelta(days=STALE_DAYS):
            problems.append(Problem("production_stale", WARNING, {**ph, "days": _fmt(silent.days)}))
    return problems


# --- the consumption sources ----------------------------------------------------------------


def check_consumption_coverage(coverage: Dict[str, float]) -> List[Problem]:
    """A source far behind the best-covered one dilutes the profile: named, with its share."""
    problems: List[Problem] = []
    if not coverage:
        return problems
    best = max(coverage.values())
    if best <= 0:
        return problems
    for stat_id, share in sorted(coverage.items()):
        if share < SPARSE_COVERAGE_RATIO * best:
            problems.append(
                Problem(
                    "consumption_source_sparse",
                    WARNING,
                    {"source": stat_id, "pct": _fmt(round(100 * share)), "best": _fmt(round(100 * best))},
                    stat_id.replace(".", "_"),
                )
            )
    return problems
