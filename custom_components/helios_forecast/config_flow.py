"""Config + options flow.

The config flow collects the global settings (optional location override, inverter
cap) then one or more panel arrays through an add-another loop. The options flow
edits the inverter cap. Array editing after setup is a later refinement; for now
arrays are removed/re-added by reconfiguring the entry.
"""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import callback
from homeassistant.helpers import selector

from .config import (
    CONF_AZIMUTH,
    CONF_BATTERY_SOC_ENTITY,
    CONF_INVERTER_CUTOFF_SOC,
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

def _user_schema(home_lat: float, home_lon: float) -> vol.Schema:
    """User-step schema with the location prefilled from the HA home."""
    return vol.Schema(
        {
            vol.Optional("name", default=_DEFAULT_NAME): str,
            vol.Optional(CONF_LATITUDE, description={"suggested_value": home_lat}): vol.Coerce(float),
            vol.Optional(CONF_LONGITUDE, description={"suggested_value": home_lon}): vol.Coerce(float),
            vol.Optional(CONF_INVERTER_MAX_KW): vol.Coerce(float),
            vol.Optional(CONF_PRODUCTION_ENTITY): _SENSOR,
            vol.Optional(CONF_BATTERY_SOC_ENTITY): _SENSOR,
            vol.Optional(CONF_INVERTER_CUTOFF_SOC): vol.Coerce(float),
            vol.Optional(CONF_TREND_ANCHOR_HOUR, default=DEFAULT_TREND_ANCHOR_HOUR): _HOUR,
        }
    )

_ARRAY_SCHEMA = vol.Schema(
    {
        vol.Required(CONF_TILT, default=30): vol.Coerce(float),
        vol.Required(CONF_AZIMUTH, default=180): vol.Coerce(float),
        vol.Required(CONF_KWP): vol.Coerce(float),
        vol.Optional(CONF_LATITUDE): vol.Coerce(float),
        vol.Optional(CONF_LONGITUDE): vol.Coerce(float),
        # Tracker is a secondary setting, kept at the bottom of the form.
        vol.Required(CONF_TRACKER, default=TRACKER_NONE): _TRACKER,
    }
)


def _clean(user_input: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in user_input.items() if v is not None}


class HeliosForecastConfigFlow(ConfigFlow, domain=DOMAIN):
    """Config flow for Helios Solar Forecast."""

    VERSION = 1

    def __init__(self) -> None:
        self._title = _DEFAULT_NAME
        self._data: dict[str, Any] = {}
        self._arrays: list[dict[str, Any]] = []

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            self._title = user_input.pop("name", self._title)
            self._data = _clean(user_input)
            return await self.async_step_array()
        schema = _user_schema(self.hass.config.latitude, self.hass.config.longitude)
        return self.async_show_form(step_id="user", data_schema=schema)

    async def async_step_array(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            self._arrays.append(_clean(user_input))
            return await self.async_step_menu()
        return self.async_show_form(step_id="array", data_schema=_ARRAY_SCHEMA)

    async def async_step_menu(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return self.async_show_menu(step_id="menu", menu_options=["add_array", "finish"])

    async def async_step_add_array(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return await self.async_step_array()

    async def async_step_finish(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return self.async_create_entry(title=self._title, data={**self._data, "arrays": self._arrays})

    @staticmethod
    @callback
    def async_get_options_flow(config_entry) -> OptionsFlow:
        return HeliosForecastOptionsFlow()


_SETTING_KEYS = (
    CONF_INVERTER_MAX_KW,
    CONF_LATITUDE,
    CONF_LONGITUDE,
    CONF_PRODUCTION_ENTITY,
    CONF_BATTERY_SOC_ENTITY,
    CONF_INVERTER_CUTOFF_SOC,
    CONF_TREND_ANCHOR_HOUR,
)


class HeliosForecastOptionsFlow(OptionsFlow):
    """Edit settings and add panel arrays after setup, from the Configure button."""

    def __init__(self) -> None:
        self._loaded = False
        self._arrays: list[dict[str, Any]] = []
        self._settings: dict[str, Any] = {}

    def _load(self) -> None:
        if self._loaded:
            return
        current = {**self.config_entry.data, **self.config_entry.options}
        self._arrays = list(current.get("arrays") or [])
        self._settings = {k: current[k] for k in _SETTING_KEYS if current.get(k) is not None}
        self._loaded = True

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        self._load()
        return self.async_show_menu(step_id="init", menu_options=["add_array", "settings", "save"])

    async def async_step_add_array(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            self._arrays.append(_clean(user_input))
            return await self.async_step_init()
        return self.async_show_form(step_id="add_array", data_schema=_ARRAY_SCHEMA)

    async def async_step_settings(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        if user_input is not None:
            self._settings = _clean(user_input)
            return await self.async_step_init()
        s = self._settings
        home_lat = self.hass.config.latitude
        home_lon = self.hass.config.longitude
        schema = vol.Schema(
            {
                vol.Optional(CONF_INVERTER_MAX_KW, description={"suggested_value": s.get(CONF_INVERTER_MAX_KW)}): vol.Coerce(float),
                vol.Optional(CONF_LATITUDE, description={"suggested_value": s.get(CONF_LATITUDE, home_lat)}): vol.Coerce(float),
                vol.Optional(CONF_LONGITUDE, description={"suggested_value": s.get(CONF_LONGITUDE, home_lon)}): vol.Coerce(float),
                vol.Optional(CONF_PRODUCTION_ENTITY, description={"suggested_value": s.get(CONF_PRODUCTION_ENTITY)}): _SENSOR,
                vol.Optional(CONF_BATTERY_SOC_ENTITY, description={"suggested_value": s.get(CONF_BATTERY_SOC_ENTITY)}): _SENSOR,
                vol.Optional(CONF_INVERTER_CUTOFF_SOC, description={"suggested_value": s.get(CONF_INVERTER_CUTOFF_SOC)}): vol.Coerce(float),
                vol.Optional(CONF_TREND_ANCHOR_HOUR, description={"suggested_value": s.get(CONF_TREND_ANCHOR_HOUR, DEFAULT_TREND_ANCHOR_HOUR)}): _HOUR,
            }
        )
        return self.async_show_form(step_id="settings", data_schema=schema)

    async def async_step_save(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return self.async_create_entry(title="", data={**self._settings, "arrays": self._arrays})
