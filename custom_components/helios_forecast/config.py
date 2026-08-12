"""Map a config entry's data into the resolved model inputs.

Pure translation from the flat dict the config flow stores into the layout,
location and inverter cap the model consumes. Shares are normalised by kWp,
mirroring the card's pvArrays. Kept separate from the flow + coordinator so the
mapping is testable on its own.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

from .solar.irradiance import PanelOrientation
from .solar.power import PvLayout

INF = float("inf")

# Config entry keys.
CONF_LATITUDE = "latitude"
CONF_LONGITUDE = "longitude"
CONF_INVERTER_MAX_KW = "inverter_max_kw"
CONF_ARRAYS = "arrays"
# Learning loop.
CONF_PRODUCTION_ENTITY = "production_entity"
# Today-trend reference anchor (local hour at which the day's reference is frozen).
CONF_TREND_ANCHOR_HOUR = "trend_anchor_hour"
DEFAULT_TREND_ANCHOR_HOUR = 6
# Battery state-of-charge projection (optional). The whole feature is off until both the capacity and the
# live SoC entity are set; the rest carry sensible defaults. Consumption is not configured here: it is derived
# from the Home Assistant Energy dashboard.
CONF_BATTERY_CAPACITY_KWH = "battery_capacity_kwh"
CONF_BATTERY_SOC_ENTITY = "battery_soc_entity"
CONF_BATTERY_MAX_CHARGE_KW = "battery_max_charge_kw"
CONF_BATTERY_MAX_DISCHARGE_KW = "battery_max_discharge_kw"
CONF_BATTERY_MIN_SOC = "battery_min_soc"
CONF_BATTERY_EFFICIENCY = "battery_efficiency"
DEFAULT_BATTERY_MIN_SOC = 10.0
DEFAULT_BATTERY_EFFICIENCY = 90.0
# Per-array keys.
CONF_TILT = "tilt"
CONF_AZIMUTH = "azimuth"
CONF_KWP = "kwp"
CONF_TRACKER = "tracker"
# Optional per-line inverter cap (kW). For micro-inverter strings that saturate on their own: two very different
# orientations sharing one meter never peak together, so summing then clipping loses that per-string ceiling. When
# set, the line is clipped at this cap before the arrays are summed; the entry-level cap still bounds the total.
CONF_LINE_INVERTER_MAX_KW = "line_inverter_max_kw"

TRACKER_NONE = "none"
_VALID_TRACKERS = {"dual-axis", "single-axis-h", "single-axis-v"}

# Per-line geometry keys, split out of the flat form into one entry in the
# ``arrays`` list. An entry may hold several lines (e.g. two strings on one
# inverter): the model sums them by kWp share and the entry-level inverter cap
# applies to their combined output.
LINE_KEYS: Tuple[str, ...] = (CONF_TILT, CONF_AZIMUTH, CONF_KWP, CONF_TRACKER, CONF_LINE_INVERTER_MAX_KW)
# Entry-level settings, shared by every line in the entry.
SETTINGS_KEYS: Tuple[str, ...] = (
    CONF_LATITUDE,
    CONF_LONGITUDE,
    CONF_INVERTER_MAX_KW,
    CONF_PRODUCTION_ENTITY,
    CONF_TREND_ANCHOR_HOUR,
    CONF_BATTERY_CAPACITY_KWH,
    CONF_BATTERY_SOC_ENTITY,
    CONF_BATTERY_MAX_CHARGE_KW,
    CONF_BATTERY_MAX_DISCHARGE_KW,
    CONF_BATTERY_MIN_SOC,
    CONF_BATTERY_EFFICIENCY,
)


def split_line(user_input: Dict[str, Any]) -> Dict[str, Any]:
    """The per-line geometry fields of a submitted form (drops empty values)."""
    return {k: user_input[k] for k in LINE_KEYS if user_input.get(k) is not None}


def split_settings(user_input: Dict[str, Any]) -> Dict[str, Any]:
    """The entry-level settings of a submitted form (drops empty values)."""
    return {k: user_input[k] for k in SETTINGS_KEYS if user_input.get(k) is not None}


def merge_entry_data(settings: Dict[str, Any], lines: List[Dict[str, Any]]) -> Dict[str, Any]:
    """Assemble a config entry's data from entry-level settings + its panel lines."""
    return {**settings, CONF_ARRAYS: list(lines)}


def lines_from_config(data: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The panel lines stored in an entry, in order (empty when none)."""
    return list(data.get(CONF_ARRAYS) or [])


def _as_float(value: Any) -> Optional[float]:
    try:
        f = float(value)
    except (TypeError, ValueError):
        return None
    return f


def layout_from_config(data: Dict[str, Any]) -> PvLayout:
    """Resolve the configured arrays into orientations + kWp-normalised shares."""
    arrays = data.get(CONF_ARRAYS) or []
    orientations: List[PanelOrientation] = []
    coords: List[Optional[Tuple[float, float]]] = []
    kwps: List[float] = []
    caps: List[float] = []

    for arr in arrays:
        tilt = _as_float(arr.get(CONF_TILT)) or 0.0
        azimuth = _as_float(arr.get(CONF_AZIMUTH)) or 0.0
        # "none" (and anything not a known tracker) is a fixed array.
        raw_tracker = arr.get(CONF_TRACKER)
        tracker = raw_tracker if raw_tracker in _VALID_TRACKERS else None
        orientations.append(PanelOrientation(tilt_deg=tilt, azimuth_deg=azimuth, tracker=tracker))

        kwp = _as_float(arr.get(CONF_KWP))
        kwps.append(max(0.0, kwp) if kwp is not None else 0.0)

        lat = _as_float(arr.get(CONF_LATITUDE))
        lon = _as_float(arr.get(CONF_LONGITUDE))
        coords.append((lat, lon) if (lat is not None and lon is not None) else None)

        cap_kw = _as_float(arr.get(CONF_LINE_INVERTER_MAX_KW))
        caps.append(cap_kw * 1000.0 if (cap_kw is not None and cap_kw > 0) else INF)

    total_kwp = sum(kwps)
    if total_kwp > 0:
        shares = [k / total_kwp for k in kwps]
    else:
        # No usable kWp: equal split keeps the arrays in lockstep; total_kwp 0
        # makes pvCalibK 0, so the forecast is flat until a peak power is set.
        shares = [1.0 / len(kwps) for _ in kwps] if kwps else []

    # Empty caps list when no line carries one, so the model keeps clipping only the combined total (unchanged).
    line_caps = caps if any(c != INF for c in caps) else []
    return PvLayout(orientations=orientations, shares=shares, coords=coords, total_kwp=total_kwp, caps=line_caps)


def location_from_config(
    data: Dict[str, Any],
    home_lat: float,
    home_lon: float,
) -> Tuple[float, float]:
    """The configured location, or the Home Assistant home when not overridden."""
    lat = _as_float(data.get(CONF_LATITUDE))
    lon = _as_float(data.get(CONF_LONGITUDE))
    if lat is not None and lon is not None:
        return lat, lon
    return home_lat, home_lon


def inverter_max_w_from_config(data: Dict[str, Any]) -> float:
    """Inverter clip in watts, INF when unset, matching the card's pvInverterMaxW."""
    kw = _as_float(data.get(CONF_INVERTER_MAX_KW))
    return kw * 1000.0 if (kw is not None and kw > 0) else INF


def learning_from_config(data: Dict[str, Any]) -> Optional[str]:
    """The PV production entity that drives the learned correction, or None.

    The learning reads this entity's real production (curtailment included), so the
    forecast tracks what the home actually harvests; without it the forecast stays
    uncorrected.
    """
    return data.get(CONF_PRODUCTION_ENTITY) or None


def trend_anchor_hour_from_config(data: Dict[str, Any]) -> int:
    """Local hour (0-23) at which today's trend reference is frozen, default 06:00."""
    h = _as_float(data.get(CONF_TREND_ANCHOR_HOUR))
    if h is None:
        return DEFAULT_TREND_ANCHOR_HOUR
    return int(max(0, min(23, h)))


@dataclass(frozen=True)
class BatteryConfig:
    """Resolved battery inputs for the SoC projection. Powers are in watts (INF when uncapped), the SoC
    bounds are fractions 0..1, and efficiency is the round-trip fraction applied on the charging side."""

    capacity_kwh: float
    soc_entity: str
    max_charge_w: float
    max_discharge_w: float
    min_soc_frac: float
    efficiency: float


def battery_from_config(data: Dict[str, Any]) -> Optional[BatteryConfig]:
    """Resolve the battery config, or None when the feature is off.

    The projection needs a usable capacity and a live SoC entity to start from; without either the whole
    feature stays off. The remaining fields fall back to sensible defaults (10% reserve, 90% round-trip).
    """
    capacity = _as_float(data.get(CONF_BATTERY_CAPACITY_KWH))
    soc_entity = data.get(CONF_BATTERY_SOC_ENTITY) or None
    if capacity is None or capacity <= 0 or not soc_entity:
        return None

    max_charge_kw = _as_float(data.get(CONF_BATTERY_MAX_CHARGE_KW))
    max_discharge_kw = _as_float(data.get(CONF_BATTERY_MAX_DISCHARGE_KW))
    min_soc = _as_float(data.get(CONF_BATTERY_MIN_SOC))
    efficiency = _as_float(data.get(CONF_BATTERY_EFFICIENCY))

    if min_soc is None:
        min_soc = DEFAULT_BATTERY_MIN_SOC
    if efficiency is None:
        efficiency = DEFAULT_BATTERY_EFFICIENCY

    return BatteryConfig(
        capacity_kwh=capacity,
        soc_entity=soc_entity,
        max_charge_w=max_charge_kw * 1000.0 if (max_charge_kw is not None and max_charge_kw > 0) else INF,
        max_discharge_w=max_discharge_kw * 1000.0 if (max_discharge_kw is not None and max_discharge_kw > 0) else INF,
        min_soc_frac=max(0.0, min(1.0, min_soc / 100.0)),
        efficiency=max(0.1, min(1.0, efficiency / 100.0)),
    )
