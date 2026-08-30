"""Tests for the config + options flow (custom_components/helios_forecast/config_flow.py).

HA-coupled: drives the real flow machinery (hass.config_entries.flow / .options) rather than
calling the flow classes directly, so schema defaults, selector validation and the show_form /
create_entry plumbing are all exercised for real.
"""

from __future__ import annotations

from homeassistant import config_entries
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType

from custom_components.helios_forecast.config import (
    CONF_ARRAYS,
    CONF_INVERTER_MAX_KW,
    CONF_KWP,
    CONF_LATITUDE,
    CONF_LONGITUDE,
    CONF_TRACKER,
)
from custom_components.helios_forecast.const import DOMAIN
from pytest_homeassistant_custom_component.common import MockConfigEntry

_LINE_A = {"tilt": 30, "azimuth": 90, "kwp": 4.0, "tracker": "none"}
_LINE_B = {"tilt": 20, "azimuth": 270, "kwp": 2.0, "tracker": "none"}


async def test_user_flow_single_line(recorder_mock, hass: HomeAssistant, enable_custom_integrations) -> None:
    """No "add another" tick: one line, entry created straight away."""
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "user"

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {**_LINE_A, "add_another": False})
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["title"] == "Helios Forecast"
    assert len(result["data"][CONF_ARRAYS]) == 1
    assert result["data"][CONF_ARRAYS][0][CONF_KWP] == 4.0
    assert result["data"][CONF_ARRAYS][0][CONF_TRACKER] == "none"


async def test_user_flow_add_another_loops_to_second_line(
    recorder_mock, hass: HomeAssistant, enable_custom_integrations
) -> None:
    """Ticking "add another" on the first line shows a second "line" step, then finishes."""
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": config_entries.SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], {**_LINE_A, "name": "Roof", "add_another": True}
    )
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "line"

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {**_LINE_B, "add_another": False})
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["title"] == "Roof"
    arrays = result["data"][CONF_ARRAYS]
    assert len(arrays) == 2
    assert arrays[0][CONF_KWP] == 4.0
    assert arrays[1][CONF_KWP] == 2.0


async def test_options_init_shows_menu(recorder_mock, hass: HomeAssistant, enable_custom_integrations) -> None:
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_ARRAYS: [_LINE_A]})
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] == FlowResultType.MENU
    assert set(result["menu_options"]) == {"settings", "lines"}


async def test_options_settings_step_keeps_lines_untouched(
    recorder_mock, hass: HomeAssistant, enable_custom_integrations
) -> None:
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_ARRAYS: [_LINE_A, _LINE_B], CONF_INVERTER_MAX_KW: 5.0})
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "settings"})
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "settings"

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {"inverter_max_kw": 8.0, "trend_anchor_hour": 6, "battery_min_soc": 10, "battery_efficiency": 90},
    )
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_INVERTER_MAX_KW] == 8.0
    assert result["data"][CONF_ARRAYS] == [_LINE_A, _LINE_B]


async def test_options_lines_step_edits_and_removes(
    recorder_mock, hass: HomeAssistant, enable_custom_integrations
) -> None:
    """Walking a 2-line entry: edit the first, remove the second -> one line survives."""
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_ARRAYS: [_LINE_A, _LINE_B]})
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "lines"})
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "lines"
    assert result["description_placeholders"] == {"index": "1", "total": "2"}

    # Editing line 1: change its tilt, do not remove, no explicit add_another (defaults True: more_existing).
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {**_LINE_A, "tilt": 45, "remove_this_line": False}
    )
    assert result["type"] == FlowResultType.FORM
    assert result["description_placeholders"]["index"] == "2"

    # Line 2: remove it instead of keeping it.
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {**_LINE_B, "remove_this_line": True, "add_another": False}
    )
    assert result["type"] == FlowResultType.CREATE_ENTRY
    arrays = result["data"][CONF_ARRAYS]
    assert len(arrays) == 1
    assert arrays[0]["tilt"] == 45


async def test_options_lines_step_removing_every_line_keeps_existing(
    recorder_mock, hass: HomeAssistant, enable_custom_integrations
) -> None:
    """Removing every line in the walk must not leave the entry with zero lines."""
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_ARRAYS: [_LINE_A, _LINE_B]})
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "lines"})

    result = await hass.config_entries.options.async_configure(result["flow_id"], {**_LINE_A, "remove_this_line": True})
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {**_LINE_B, "remove_this_line": True, "add_another": False}
    )
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_ARRAYS] == [_LINE_A, _LINE_B]


async def test_options_lines_step_can_append_a_new_line(
    recorder_mock, hass: HomeAssistant, enable_custom_integrations
) -> None:
    """A single-line entry: keep the existing line, tick add_another, append a second."""
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_ARRAYS: [_LINE_A]})
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "lines"})
    # Only one existing line: no remove toggle offered.
    assert "remove_this_line" not in result["data_schema"].schema

    result = await hass.config_entries.options.async_configure(result["flow_id"], {**_LINE_A, "add_another": True})
    assert result["type"] == FlowResultType.FORM

    result = await hass.config_entries.options.async_configure(result["flow_id"], {**_LINE_B, "add_another": False})
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert len(result["data"][CONF_ARRAYS]) == 2


async def test_options_settings_step_decimal_latitude_longitude_roundtrip(
    recorder_mock, hass: HomeAssistant, enable_custom_integrations
) -> None:
    """Latitude/longitude go through NumberSelectors: a decimal value must survive the round-trip
    through the real schema at full precision, the way it does for kWp (regression for the same
    browser-locale misparsing class of bug)."""
    entry = MockConfigEntry(domain=DOMAIN, data={CONF_ARRAYS: [_LINE_A]})
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], {"next_step_id": "settings"})
    assert result["type"] == FlowResultType.FORM

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            CONF_LATITUDE: 45.7597,
            CONF_LONGITUDE: 4.8422,
            "trend_anchor_hour": 6,
            "battery_min_soc": 10,
            "battery_efficiency": 90,
        },
    )
    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_LATITUDE] == 45.7597
    assert result["data"][CONF_LONGITUDE] == 4.8422
