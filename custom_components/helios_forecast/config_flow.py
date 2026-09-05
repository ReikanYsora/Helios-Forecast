"""Config + options flow.

One config entry describes a photovoltaic installation feeding a single production
sensor. It holds one or more panel lines (each a group of co-oriented panels); the
model sums the lines by kWp share and the entry-level inverter cap applies to their
combined output. This is what lets two strings on one inverter (only a combined
production sensor available) share a single entry: add a line per orientation.

Adding the integration walks the first line + the entry-level settings, then loops
"add another line?" for as many lines as needed. The options flow (Configure button)
edits the settings and the lines in place, so history is kept.
"""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult, OptionsFlow
from homeassistant.core import callback
from homeassistant.helpers import selector

from .benchmark import DEFAULT_ENDPOINT
from .config import (
    CONF_AZIMUTH,
    CONF_BATTERY_CAPACITY_KWH,
    CONF_BATTERY_EFFICIENCY,
    CONF_BATTERY_MAX_CHARGE_KW,
    CONF_BATTERY_MAX_DISCHARGE_KW,
    CONF_BATTERY_MIN_SOC,
    CONF_BATTERY_SOC_ENTITY,
    CONF_BENCHMARK_ENABLED,
    CONF_BENCHMARK_KEY,
    CONF_BENCHMARK_URL,
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
    DEFAULT_BATTERY_EFFICIENCY,
    DEFAULT_BATTERY_MIN_SOC,
    DEFAULT_TREND_ANCHOR_HOUR,
    TRACKER_NONE,
    lines_from_config,
    location_from_config,
    merge_entry_data,
    split_line,
    split_settings,
)
from .const import DOMAIN

_DEFAULT_NAME = "Helios Forecast"
_NAME = "name"
# Control keys, read for flow branching and never stored (split_line / split_settings
# only keep the geometry / settings keys).
_ADD_ANOTHER = "add_another"
_REMOVE = "remove_this_line"

_BOX = selector.NumberSelectorMode.BOX
# Energy sensors only: the learning reads hourly "sum" statistics (change in kWh), which a
# power (W) sensor lacks; picking one would silently disable the learning and cap the
# reliability index (a power sensor only records a mean).
_SENSOR = selector.EntitySelector(selector.EntitySelectorConfig(domain="sensor", device_class="energy"))
_BOOL = selector.BooleanSelector()
# The curtailment signal: anything that reads on/off.
_CURTAIL_ENTITY = selector.EntitySelector(
    selector.EntitySelectorConfig(domain=["binary_sensor", "input_boolean", "switch"])
)
_HOUR = selector.NumberSelector(selector.NumberSelectorConfig(min=0, max=23, step=1, mode=_BOX))
# Numeric geometry / power fields as explicit NumberSelectors: a proper numeric input
# with a defined decimal step, so decimals (e.g. 2.61 kWp) are entered and stored as
# real floats instead of being misparsed by the browser locale.
_TILT = selector.NumberSelector(
    selector.NumberSelectorConfig(min=0, max=90, step=1, mode=_BOX, unit_of_measurement="°")
)
_AZIMUTH = selector.NumberSelector(
    selector.NumberSelectorConfig(min=0, max=360, step=1, mode=_BOX, unit_of_measurement="°")
)
_KWP = selector.NumberSelector(selector.NumberSelectorConfig(min=0, step=0.01, mode=_BOX, unit_of_measurement="kWp"))
_INVERTER = selector.NumberSelector(selector.NumberSelectorConfig(min=0, step=0.1, mode=_BOX, unit_of_measurement="kW"))
# step "any": user input is not rounded to it (HA only uses it for the slider / keyboard step), so
# coordinates keep whatever precision is typed instead of being rounded to a fixed decimal count.
_LATITUDE = selector.NumberSelector(selector.NumberSelectorConfig(min=-90, max=90, step="any", mode=_BOX))
_LONGITUDE = selector.NumberSelector(selector.NumberSelectorConfig(min=-180, max=180, step="any", mode=_BOX))
_TRACKER = selector.SelectSelector(
    selector.SelectSelectorConfig(
        options=[TRACKER_NONE, "dual-axis", "single-axis-h", "single-axis-v"],
        translation_key="tracker",
        mode=selector.SelectSelectorMode.DROPDOWN,
    )
)
# Battery SoC projection fields. Capacity in kWh, the live SoC entity (a % battery sensor), and the optional
# limits / efficiency as percentages. Charge / discharge power reuse the kW input.
_KWH = selector.NumberSelector(selector.NumberSelectorConfig(min=0, step=0.1, mode=_BOX, unit_of_measurement="kWh"))
_SOC_ENTITY = selector.EntitySelector(selector.EntitySelectorConfig(domain="sensor", device_class="battery"))
_PERCENT = selector.NumberSelector(
    selector.NumberSelectorConfig(min=0, max=100, step=1, mode=_BOX, unit_of_measurement="%")
)
# Benchmark upload: the write key is the only secret this integration holds, so it is typed and
# redisplayed as a password rather than sitting in clear in the form.
_TEXT = selector.TextSelector(selector.TextSelectorConfig(type=selector.TextSelectorType.TEXT))
_SECRET = selector.TextSelector(selector.TextSelectorConfig(type=selector.TextSelectorType.PASSWORD))


def _optional(fields: dict[Any, Any], key: str, sel: Any, suggested_value: Any) -> None:
    """Add one optional field to ``fields``, pre-filled with ``suggested_value`` when given."""
    fields[vol.Optional(key, description={"suggested_value": suggested_value})] = sel


def _line_fields(
    arr: dict[str, Any] | None,
    home_lat: float,
    home_lon: float,
    *,
    with_add_another: bool,
    add_another_default: bool = False,
    allow_remove: bool = False,
) -> dict[Any, Any]:
    """One panel line's geometry fields, pre-filled from ``arr`` when editing.

    ``with_add_another`` appends the "add another line" toggle; ``allow_remove`` (options
    flow, when the entry has more than one line) appends a "remove this line" toggle.
    """
    arr = arr or {}
    fields: dict[Any, Any] = {
        vol.Required(CONF_TILT, default=arr.get(CONF_TILT, 30)): _TILT,
        vol.Required(CONF_AZIMUTH, default=arr.get(CONF_AZIMUTH, 180)): _AZIMUTH,
    }
    # kWp has no static default (it must be typed on add); on edit we seed it with the
    # current value so the field comes up filled.
    kwp_key = vol.Required(CONF_KWP, default=arr[CONF_KWP]) if CONF_KWP in arr else vol.Required(CONF_KWP)
    fields[kwp_key] = _KWP
    # Optional per-line inverter cap: a micro-inverter string that saturates on its own, distinct from the
    # entry-level cap that bounds the combined total. Left empty, the line is uncapped.
    _optional(fields, CONF_LINE_INVERTER_MAX_KW, _INVERTER, arr.get(CONF_LINE_INVERTER_MAX_KW))
    fields[vol.Required(CONF_TRACKER, default=arr.get(CONF_TRACKER, TRACKER_NONE))] = _TRACKER
    # Optional per-line location override, for a line far enough from the entry's home coordinates
    # (e.g. a detached outbuilding) that it needs its own sun geometry. Suggested with the entry's
    # home coordinates so the field never comes up blank, but it is only stored once actually set.
    _optional(fields, CONF_LATITUDE, _LATITUDE, arr.get(CONF_LATITUDE, home_lat))
    _optional(fields, CONF_LONGITUDE, _LONGITUDE, arr.get(CONF_LONGITUDE, home_lon))
    if allow_remove:
        fields[vol.Optional(_REMOVE, default=False)] = _BOOL
    if with_add_another:
        fields[vol.Optional(_ADD_ANOTHER, default=add_another_default)] = _BOOL
    return fields


def _settings_fields(
    home_lat: float,
    home_lon: float,
    *,
    settings: dict[str, Any] | None = None,
) -> dict[Any, Any]:
    """Entry-level settings: location, inverter cap, production sensor, trend anchor.

    Location defaults to the Home Assistant home; the rest are optional. The entry name is
    collected separately in the config flow (it becomes the title) and edited through Home
    Assistant's own rename action, so it is not part of these fields.
    """
    s = settings or {}
    fields: dict[Any, Any] = {}
    _optional(fields, CONF_LATITUDE, _LATITUDE, s.get(CONF_LATITUDE, home_lat))
    _optional(fields, CONF_LONGITUDE, _LONGITUDE, s.get(CONF_LONGITUDE, home_lon))
    _optional(fields, CONF_INVERTER_MAX_KW, _INVERTER, s.get(CONF_INVERTER_MAX_KW))
    _optional(fields, CONF_PRODUCTION_ENTITY, _SENSOR, s.get(CONF_PRODUCTION_ENTITY))
    fields[vol.Optional(CONF_TREND_ANCHOR_HOUR, default=s.get(CONF_TREND_ANCHOR_HOUR, DEFAULT_TREND_ANCHOR_HOUR))] = (
        _HOUR
    )
    # Battery SoC projection (all optional): capacity + the live SoC entity switch the feature on; the rest
    # keep their defaults when left empty. Consumption is not asked for, it comes from the Energy dashboard.
    _optional(fields, CONF_BATTERY_CAPACITY_KWH, _KWH, s.get(CONF_BATTERY_CAPACITY_KWH))
    _optional(fields, CONF_BATTERY_SOC_ENTITY, _SOC_ENTITY, s.get(CONF_BATTERY_SOC_ENTITY))
    _optional(fields, CONF_BATTERY_MAX_CHARGE_KW, _INVERTER, s.get(CONF_BATTERY_MAX_CHARGE_KW))
    _optional(fields, CONF_BATTERY_MAX_DISCHARGE_KW, _INVERTER, s.get(CONF_BATTERY_MAX_DISCHARGE_KW))
    fields[vol.Optional(CONF_BATTERY_MIN_SOC, default=s.get(CONF_BATTERY_MIN_SOC, DEFAULT_BATTERY_MIN_SOC))] = _PERCENT
    fields[
        vol.Optional(CONF_BATTERY_EFFICIENCY, default=s.get(CONF_BATTERY_EFFICIENCY, DEFAULT_BATTERY_EFFICIENCY))
    ] = _PERCENT
    # Curtailment signal: on while the inverter is held back for a reason the sky cannot explain, so those
    # hours are not learned as low production (zero export, grid limits; a full battery is detected without it).
    _optional(fields, CONF_CURTAILMENT_ENTITY, _CURTAIL_ENTITY, s.get(CONF_CURTAILMENT_ENTITY))
    # Benchmark upload, off unless deliberately switched on and given a key. What it sends, and why it
    # has to be sent at the moment it is predicted rather than reconstructed later, is in benchmark.py.
    fields[vol.Optional(CONF_BENCHMARK_ENABLED, default=bool(s.get(CONF_BENCHMARK_ENABLED, False)))] = _BOOL
    _optional(fields, CONF_BENCHMARK_KEY, _SECRET, s.get(CONF_BENCHMARK_KEY))
    _optional(fields, CONF_BENCHMARK_URL, _TEXT, s.get(CONF_BENCHMARK_URL, DEFAULT_ENDPOINT))
    return fields


class HeliosForecastConfigFlow(ConfigFlow, domain=DOMAIN):  # type: ignore[call-arg]  # HA passes `domain` to __init_subclass__
    """Config flow for Helios Solar Forecast: settings + one or more panel lines."""

    VERSION = 1

    def __init__(self) -> None:
        self._title: str = _DEFAULT_NAME
        self._settings: dict[str, Any] = {}
        self._lines: list[dict[str, Any]] = []

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """First step: the entry-level settings and the first panel line."""
        if user_input is not None:
            self._title = user_input.get(_NAME) or _DEFAULT_NAME
            self._settings = split_settings(user_input)
            self._lines.append(split_line(user_input))
            if user_input.get(_ADD_ANOTHER):
                return await self.async_step_line()
            return self._finish()
        home_lat, home_lon = self.hass.config.latitude, self.hass.config.longitude
        fields = {vol.Optional(_NAME, default=_DEFAULT_NAME): str}
        fields.update(_line_fields(None, home_lat, home_lon, with_add_another=False))
        fields.update(_settings_fields(home_lat, home_lon))
        fields[vol.Optional(_ADD_ANOTHER, default=False)] = _BOOL
        return self.async_show_form(step_id="user", data_schema=vol.Schema(fields))

    async def async_step_line(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Repeated step: one more panel line, looping while "add another" stays ticked."""
        if user_input is not None:
            self._lines.append(split_line(user_input))
            if user_input.get(_ADD_ANOTHER):
                return await self.async_step_line()
            return self._finish()
        # The entry-level location was already collected on the first step: suggest it (falling
        # back to the Home Assistant home) rather than the raw HA home for this additional line.
        home_lat, home_lon = location_from_config(self._settings, self.hass.config.latitude, self.hass.config.longitude)
        return self.async_show_form(
            step_id="line",
            data_schema=vol.Schema(_line_fields(None, home_lat, home_lon, with_add_another=True)),
            description_placeholders={"index": str(len(self._lines) + 1)},
        )

    def _finish(self) -> ConfigFlowResult:
        return self.async_create_entry(title=self._title, data=merge_entry_data(self._settings, self._lines))

    @staticmethod
    @callback
    def async_get_options_flow(config_entry) -> OptionsFlow:
        return HeliosForecastOptionsFlow()


class HeliosForecastOptionsFlow(OptionsFlow):
    """Edit the entry's settings and its panel lines in place, from the Configure button."""

    def __init__(self) -> None:
        # Lines kept as they are re-entered through the manage-lines loop.
        self._lines: list[dict[str, Any]] = []
        self._index: int = 0

    def _current(self) -> dict[str, Any]:
        return {**self.config_entry.data, **self.config_entry.options}

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        return self.async_show_menu(step_id="init", menu_options=["settings", "lines"])

    async def async_step_settings(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Edit the entry-level settings, keeping the existing panel lines untouched."""
        current = self._current()
        if user_input is not None:
            data = merge_entry_data(split_settings(user_input), lines_from_config(current))
            return self.async_create_entry(title="", data=data)
        schema = vol.Schema(_settings_fields(self.hass.config.latitude, self.hass.config.longitude, settings=current))
        return self.async_show_form(step_id="settings", data_schema=schema)

    async def async_step_lines(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Walk the existing lines (edit / remove each), then optionally append new ones."""
        current = self._current()
        existing = lines_from_config(current)
        if user_input is not None:
            if not user_input.get(_REMOVE):
                self._lines.append(split_line(user_input))
            self._index += 1
            # Keep walking while existing lines remain to review, or the user asked to add one.
            if self._index < len(existing) or user_input.get(_ADD_ANOTHER):
                return await self.async_step_lines()
            # Never let the entry end up with zero lines (e.g. every line removed).
            lines = self._lines or existing
            data = merge_entry_data(split_settings(current), lines)
            return self.async_create_entry(title="", data=data)

        editing_existing = self._index < len(existing)
        arr = existing[self._index] if editing_existing else None
        # Auto-advance through the remaining existing lines without forcing a manual tick.
        more_existing = self._index + 1 < len(existing)
        home_lat, home_lon = location_from_config(current, self.hass.config.latitude, self.hass.config.longitude)
        return self.async_show_form(
            step_id="lines",
            data_schema=vol.Schema(
                _line_fields(
                    arr,
                    home_lat,
                    home_lon,
                    with_add_another=True,
                    add_another_default=more_existing,
                    allow_remove=editing_existing and len(existing) > 1,
                )
            ),
            description_placeholders={
                "index": str(self._index + 1),
                "total": str(max(len(existing), self._index + 1)),
            },
        )
