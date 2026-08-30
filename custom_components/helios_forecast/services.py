"""Response-only services exposing the computed forecasts to automations.

The integration already computes the whole production curve and the battery SoC
projection (the card draws them and the sensors carry them as attributes). These
services hand the same curves to automations on demand, so an EMS can read the
remaining forecast (with its P10/P90 band) or the projected SoC without scraping an
attribute or polling the websocket the card uses.
"""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse, callback
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import config_validation as cv

from .battery import BatterySocPoint
from .const import DOMAIN
from .forecast import forecast_point_dict

_SERVICE_GET_FORECAST = "get_forecast"
_SERVICE_GET_BATTERY_SOC = "get_battery_soc_forecast"
_SERVICES_REGISTERED = f"{DOMAIN}_services_registered"

_SCHEMA = vol.Schema({vol.Optional("config_entry_id"): cv.string})


def _soc_point_dict(p: BatterySocPoint) -> dict[str, Any]:
    """One projected SoC bucket for the response: the battery percentage at that time."""
    return {"datetime": p.t.isoformat(), "soc": p.soc}


@callback
def async_register_services(hass: HomeAssistant) -> None:
    """Register the response services once for the whole integration."""
    if hass.data.get(_SERVICES_REGISTERED):
        return
    hass.data[_SERVICES_REGISTERED] = True

    def _resolve(call: ServiceCall) -> Any:
        """The coordinator targeted by the call: the given config entry, or the only one set up."""
        coordinators = dict(hass.data.get(DOMAIN, {}))
        entry_id = call.data.get("config_entry_id")
        if entry_id is not None:
            coordinator = coordinators.get(entry_id)
            if coordinator is None:
                raise ServiceValidationError(f"No Helios Forecast installation with config entry id '{entry_id}'.")
            return coordinator
        if not coordinators:
            raise ServiceValidationError("No Helios Forecast installation is set up.")
        if len(coordinators) == 1:
            return next(iter(coordinators.values()))
        raise ServiceValidationError(
            "Several Helios Forecast installations are set up; pass 'config_entry_id' to choose one."
        )

    async def _async_get_forecast(call: ServiceCall) -> dict[str, Any]:
        """Return the production forecast curve (today onward) for one installation."""
        data = _resolve(call).data
        points = data.points if data is not None else []
        return {"forecast": [forecast_point_dict(p) for p in points]}

    async def _async_get_battery_soc(call: ServiceCall) -> dict[str, Any]:
        """Return the projected battery SoC curve (next 48 h) for one installation.

        Empty when the battery feature is off or has no usable input; the reason is logged (the
        state-of-charge sensor itself just reads "unknown", with no diagnostic attribute)."""
        data = _resolve(call).data
        soc = data.battery_soc if data is not None else []
        return {"forecast": [_soc_point_dict(p) for p in soc]}

    hass.services.async_register(
        DOMAIN,
        _SERVICE_GET_FORECAST,
        _async_get_forecast,
        schema=_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )
    hass.services.async_register(
        DOMAIN,
        _SERVICE_GET_BATTERY_SOC,
        _async_get_battery_soc,
        schema=_SCHEMA,
        supports_response=SupportsResponse.ONLY,
    )
