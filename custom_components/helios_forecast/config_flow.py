"""Config + options flow.

One config entry describes ONE panel line (a single group of co-oriented panels)
so each line gets its own forecast entities and can be wired to its own card on the
dashboard. Add the integration once per line. The single user step collects the
line geometry together with the location / inverter / learning settings; the
options flow (Configure button) edits the same fields in place so history is kept.
"""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import callback
from homeassistant.helpers import selector

from .config import (
    CONF_ARRAYS,
    CONF_AZIMUTH,
    CONF_INVERTER_MAX_KW,
    CONF_KWP,
    CONF_LATITUDE,
    CONF_LONGITUDE,
    CONF_PRODUCTION_ENTITY,
    CONF_TILT,
    CONF_TRACKER,
    CONF_TREND_ANCHOR_HOUR,
    DEFAULT_TREND_ANCHOR_HOUR,
    TRACKER_NONE,
)
from .const import DOMAIN

_DEFAULT_NAME = "Helios Forecast"
_SENSOR = selector.EntitySelector(selector.EntitySelectorConfig(domain="sensor"))
_HOUR = selector.NumberSelector(
    selector.NumberSelectorConfig(min=0, max=23, step=1, mode=selector.NumberSelectorMode.BOX)
)
_TRACKER = selector.SelectSelector(
    selector.SelectSelectorConfig(
        options=[TRACKER_NONE, "dual-axis", "single-axis-h", "single-axis-v"],
        translation_key="tracker",
        mode=selector.SelectSelectorMode.DROPDOWN,
    )
)

# The per-line geometry keys, split out of the flat form into the single-element
# ``arrays`` list the model consumes.
_LINE_KEYS = (CONF_TILT, CONF_AZIMUTH, CONF_KWP, CONF_TRACKER)


def _line_schema(
    home_lat: float,
    home_lon: float,
    *,
    arr: dict[str, Any] | None = None,
    settings: dict[str, Any] | None = None,
    include_name: bool = True,
) -> vol.Schema:
    """The one-and-only line form: geometry + location/inverter/learning settings.

    ``arr``/``settings`` pre-fill the fields when editing so the same schema serves
    both creating a line and editing it in place. ``include_name`` is on for the
    config flow (the name becomes the entry title) and off for the options flow,
    where the entry is renamed through Home Assistant's own rename action.
    """
    arr = arr or {}
    s = settings or {}
    fields: dict[Any, Any] = {}
    if include_name:
        fields[vol.Optional("name", default=_DEFAULT_NAME)] = str
    # Panel geometry. kWp has no static default (it must be typed on add); on edit
    # we seed it with the current value so the field comes up filled.
    fields[vol.Required(CONF_TILT, default=arr.get(CONF_TILT, 30))] = vol.Coerce(float)
    fields[vol.Required(CONF_AZIMUTH, default=arr.get(CONF_AZIMUTH, 180))] = vol.Coerce(float)
    kwp_key = vol.Required(CONF_KWP, default=arr[CONF_KWP]) if CONF_KWP in arr else vol.Required(CONF_KWP)
    fields[kwp_key] = vol.Coerce(float)
    fields[vol.Required(CONF_TRACKER, default=arr.get(CONF_TRACKER, TRACKER_NONE))] = _TRACKER
    # Settings. Location defaults to the HA home; the rest are optional overrides.
    fields[vol.Optional(CONF_LATITUDE, description={"suggested_value": s.get(CONF_LATITUDE, home_lat)})] = vol.Coerce(float)
    fields[vol.Optional(CONF_LONGITUDE, description={"suggested_value": s.get(CONF_LONGITUDE, home_lon)})] = vol.Coerce(float)
    fields[vol.Optional(CONF_INVERTER_MAX_KW, description={"suggested_value": s.get(CONF_INVERTER_MAX_KW)})] = vol.Coerce(float)
    fields[vol.Optional(CONF_PRODUCTION_ENTITY, description={"suggested_value": s.get(CONF_PRODUCTION_ENTITY)})] = _SENSOR
    fields[vol.Optional(CONF_TREND_ANCHOR_HOUR, default=s.get(CONF_TREND_ANCHOR_HOUR, DEFAULT_TREND_ANCHOR_HOUR))] = _HOUR
    return vol.Schema(fields)


def _clean(user_input: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in user_input.items() if v is not None}


def _entry_data(user_input: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """Split the flat form into (title, entry data with a single-element arrays list)."""
    ui = _clean(dict(user_input))
    title = ui.pop("name", _DEFAULT_NAME)
    line = {k: ui.pop(k) for k in _LINE_KEYS if k in ui}
    return title, {**ui, CONF_ARRAYS: [line]}


class HeliosForecastConfigFlow(ConfigFlow, domain=DOMAIN):
    """Config flow for Helios Solar Forecast — one entry per panel line."""

    VERSION = 1

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            title, data = _entry_data(user_input)
            return self.async_create_entry(title=title, data=data)
        schema = _line_schema(self.hass.config.latitude, self.hass.config.longitude)
        return self.async_show_form(step_id="user", data_schema=schema)

    @staticmethod
    @callback
    def async_get_options_flow(config_entry) -> OptionsFlow:
        return HeliosForecastOptionsFlow()


class HeliosForecastOptionsFlow(OptionsFlow):
    """Edit the line's geometry and settings in place, from the Configure button."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            _, data = _entry_data(user_input)
            return self.async_create_entry(title="", data=data)
        current = {**self.config_entry.data, **self.config_entry.options}
        arrays = current.get(CONF_ARRAYS) or []
        arr = arrays[0] if arrays else {}
        schema = _line_schema(
            self.hass.config.latitude,
            self.hass.config.longitude,
            arr=arr,
            settings=current,
            include_name=False,
        )
        return self.async_show_form(step_id="init", data_schema=schema)
