"""Map a config entry's data into the resolved model inputs.

Pure translation from the flat dict the config flow stores into the layout,
location and inverter cap the model consumes. Shares are normalised by kWp,
mirroring the card's pvArrays. Kept separate from the flow + coordinator so the
mapping is testable on its own.
"""

from __future__ import annotations

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
CONF_BATTERY_SOC_ENTITY = "battery_soc_entity"
CONF_INVERTER_CUTOFF_SOC = "inverter_cutoff_soc"
# Per-array keys.
CONF_TILT = "tilt"
CONF_AZIMUTH = "azimuth"
CONF_KWP = "kwp"
CONF_TRACKER = "tracker"

TRACKER_NONE = "none"
_VALID_TRACKERS = {"dual-axis", "single-axis-h", "single-axis-v"}


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

    total_kwp = sum(kwps)
    if total_kwp > 0:
        shares = [k / total_kwp for k in kwps]
    else:
        # No usable kWp: equal split keeps the arrays in lockstep; total_kwp 0
        # makes pvCalibK 0, so the forecast is flat until a peak power is set.
        shares = [1.0 / len(kwps) for _ in kwps] if kwps else []

    return PvLayout(orientations=orientations, shares=shares, coords=coords, total_kwp=total_kwp)


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


def learning_from_config(
    data: Dict[str, Any],
) -> Tuple[Optional[str], Optional[str], Optional[float]]:
    """The learning-loop wiring: (production entity, battery SoC entity, cutoff SoC).

    Production entity drives the learned correction; without it the forecast stays
    uncorrected. The SoC entity + cutoff enable the inverter-cutoff guard.
    """
    production = data.get(CONF_PRODUCTION_ENTITY) or None
    soc = data.get(CONF_BATTERY_SOC_ENTITY) or None
    cutoff = _as_float(data.get(CONF_INVERTER_CUTOFF_SOC))
    return production, soc, cutoff
