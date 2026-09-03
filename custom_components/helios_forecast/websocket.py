"""Websocket command serving the dense forecast detail series to the card.

This is the card's enhanced layer (sub-hourly raw + corrected curve). The card
reads it when this integration is present and falls back to HA's standard
solar-forecast otherwise.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any, Optional

import voluptuous as vol
from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant, callback
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .forecast import ForecastPoint

_WS_REGISTERED = f"{DOMAIN}_ws_registered"


@callback
def async_register(hass: HomeAssistant) -> None:
    """Register the series command once for the whole integration."""
    if hass.data.get(_WS_REGISTERED):
        return
    hass.data[_WS_REGISTERED] = True
    websocket_api.async_register_command(hass, ws_series)


def _point_dict(p: ForecastPoint) -> dict[str, Any]:
    """One native-resolution bucket for the response."""
    return {
        "t": p.t.isoformat(),
        "pv_w": p.pv_w,
        "pv_raw_w": p.pv_raw_w,
        "pv_p10": p.pv_p10,
        "pv_p90": p.pv_p90,
        "ghi": getattr(p, "ghi", None),
        "cloud": getattr(p, "cloud", None),
    }


def _native_step_minutes(points: list[ForecastPoint]) -> Optional[float]:
    """The finest spacing between consecutive points in the series, or None when there
    are fewer than two (nothing to compare a resolution against)."""
    deltas = [(b.t - a.t).total_seconds() / 60.0 for a, b in zip(points, points[1:]) if b.t > a.t]
    return min(deltas) if deltas else None


def _bucket_average(values: list[Optional[float]]) -> Optional[float]:
    present = [v for v in values if v is not None]
    return sum(present) / len(present) if present else None


def _resample(points: list[ForecastPoint], resolution_min: int) -> list[dict[str, Any]]:
    """Downsample into resolution_min-wide buckets aligned to the series' first point,
    averaging pv_w / pv_raw_w / pv_p10 / pv_p90 (None values excluded from the average;
    a bucket with no non-None values for a field stays None for that field)."""
    if not points:
        return []
    origin = points[0].t
    width = timedelta(minutes=resolution_min)
    buckets: dict[int, list[ForecastPoint]] = {}
    for p in points:
        buckets.setdefault(int((p.t - origin) // width), []).append(p)
    return [
        {
            "t": (origin + idx * width).isoformat(),
            "pv_w": _bucket_average([p.pv_w for p in buckets[idx]]),
            "pv_raw_w": _bucket_average([p.pv_raw_w for p in buckets[idx]]),
            "pv_p10": _bucket_average([p.pv_p10 for p in buckets[idx]]),
            "pv_p90": _bucket_average([p.pv_p90 for p in buckets[idx]]),
            "ghi": None,
            "cloud": None,
        }
        for idx in sorted(buckets)
    ]


@websocket_api.websocket_command(
    {
        vol.Required("type"): "helios_forecast/series",
        vol.Required("entry_id"): str,
        vol.Optional("start"): str,
        vol.Optional("end"): str,
        vol.Optional("resolution_min"): vol.All(int, vol.Range(min=1)),
    }
)
@callback
def ws_series(hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]) -> None:
    """Return the forecast points (and per-day kWh) for one config entry."""
    coordinator = hass.data.get(DOMAIN, {}).get(msg["entry_id"])
    if coordinator is None or coordinator.data is None:
        connection.send_error(msg["id"], "not_found", "no forecast for that entry")
        return

    start = dt_util.parse_datetime(msg["start"]) if msg.get("start") else None
    end = dt_util.parse_datetime(msg["end"]) if msg.get("end") else None
    for label, value in (("start", start), ("end", end)):
        if value is not None and value.tzinfo is None:
            connection.send_error(msg["id"], "invalid_format", f"'{label}' must include a UTC offset")
            return

    # Full curve = the hourly past archive followed by the live sub-hourly points. Split on the
    # archive's own last point, not on the live series' start (today's midnight): the live series
    # covers the whole day from midnight, but enrich_points() deliberately leaves its already-elapsed
    # points unclamped (a past point there means "what the forecast said at the time"), so today's own
    # elapsed hours need the archive's analog-clamped values too, exactly like yesterday's do - only
    # the still-uncovered stretch between the archive's cutoff and now falls back to the live points.
    live = coordinator.data.points
    archive = coordinator.archive_points
    archive_end = archive[-1].t if archive else None
    series = list(archive)
    series.extend(p for p in live if archive_end is None or p.t > archive_end)

    in_range = []
    for p in series:
        if start is not None and p.t < start:
            continue
        if end is not None and p.t >= end:
            continue
        in_range.append(p)

    resolution_min = msg.get("resolution_min")
    native_step = _native_step_minutes(in_range)
    if resolution_min is not None and native_step is not None and resolution_min > native_step:
        points = _resample(in_range, resolution_min)
    else:
        points = [_point_dict(p) for p in in_range]

    daily = [{"date": d.date, "kwh": d.energy_kwh, "kwh_raw": d.energy_raw_kwh} for d in coordinator.data.summary.days]
    connection.send_result(msg["id"], {"points": points, "daily": daily})
