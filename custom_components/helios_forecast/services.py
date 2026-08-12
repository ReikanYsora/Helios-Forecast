"""Response-only service exposing the full forecast series to automations.

The integration already computes the whole production curve (the card draws it and
the ``power_now`` sensor carries it as an attribute). This service hands that same
curve to automations on demand, so an EMS can read the remaining forecast and its
P10/P90 band without scraping an attribute or polling the websocket the card uses.
"""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse, callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv

from .const import DOMAIN
from .forecast import ForecastPoint

_SERVICE_GET_FORECAST = "get_forecast"
_SERVICES_REGISTERED = f"{DOMAIN}_services_registered"

_SCHEMA = vol.Schema({vol.Optional("config_entry_id"): cv.string})


def _point_dict(p: ForecastPoint) -> dict[str, Any]:
    """One forecast bucket for the response: the chosen watts plus the P10/P90 band
    (null when the analog support is too thin to surface one)."""
    return {
        "datetime": p.t.isoformat(),
        "watts": round(p.pv_w, 2),
        "p10": round(p.pv_p10, 2) if p.pv_p10 is not None else None,
        "p90": round(p.pv_p90, 2) if p.pv_p90 is not None else None,
    }


@callback
def async_register_services(hass: HomeAssistant) -> None:
    """Register the response services once for the whole integration."""
    if hass.data.get(_SERVICES_REGISTERED):
        return
    hass.data[_SERVICES_REGISTERED] = True

    async def _async_get_forecast(call: ServiceCall) -> dict[str, Any]:
        """Return the forecast curve (today onward) for one installation as response data."""
        coordinators = dict(hass.data.get(DOMAIN, {}))
        entry_id = call.data.get("config_entry_id")
        if entry_id is not None:
            coordinator = coordinators.get(entry_id)
            if coordinator is None:
                raise ServiceValidationError(
                    f"No Helios Forecast installation with config entry id '{entry_id}'."
                )
        elif not coordinators:
            raise ServiceValidationError("No Helios Forecast installation is set up.")
        elif len(coordinators) == 1:
            coordinator = next(iter(coordinators.values()))
        else:
            raise ServiceValidationError(
                "Several Helios Forecast installations are set up; pass 'config_entry_id' to choose one."
            )

        data = coordinator.data
        points = data.points if data is not None else []
        return {"forecast": [_point_dict(p) for p in points]}

    hass.services.async_register(
        DOMAIN,
        _SERVICE_GET_FORECAST,
        _async_get_forecast,
        schema=_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )
