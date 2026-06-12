"""Websocket command serving the dense forecast detail series to the card.

This is the card's enhanced layer (sub-hourly raw + corrected curve). The card
reads it when this integration is present and falls back to HA's standard
solar-forecast otherwise.
"""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant, callback
from homeassistant.util import dt as dt_util

from .const import DOMAIN

_WS_REGISTERED = f"{DOMAIN}_ws_registered"


@callback
def async_register(hass: HomeAssistant) -> None:
    """Register the series command once for the whole integration."""
    if hass.data.get(_WS_REGISTERED):
        return
    hass.data[_WS_REGISTERED] = True
    websocket_api.async_register_command(hass, ws_series)


@websocket_api.websocket_command(
    {
        vol.Required("type"): "helios_forecast/series",
        vol.Required("entry_id"): str,
        vol.Optional("start"): str,
        vol.Optional("end"): str,
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

    # Full curve = the hourly past archive (before the live series starts) followed by the live
    # sub-hourly points (today onward). The live points are higher resolution, so the past archive is
    # only used for the hours the live series does not cover.
    live = coordinator.data.points
    live_start = live[0].t if live else None
    series = [p for p in coordinator.archive_points if live_start is None or p.t < live_start]
    series.extend(live)

    points = []
    for p in series:
        if start is not None and p.t < start:
            continue
        if end is not None and p.t >= end:
            continue
        points.append({
            "t": p.t.isoformat(),
            "pv_w": p.pv_w,
            "pv_raw_w": p.pv_raw_w,
            "pv_p10": p.pv_p10,
            "pv_p90": p.pv_p90,
        })

    daily = [
        {"date": d.date, "kwh": d.energy_kwh, "kwh_raw": d.energy_kwh}
        for d in coordinator.data.summary.days
    ]
    connection.send_result(msg["id"], {"points": points, "daily": daily})
