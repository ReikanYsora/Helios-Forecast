"""Tests for the check-up wiring: problems become repair issues and retire when they are fixed.
The rules themselves are tested in tests/test_checkup.py."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest
from homeassistant.core import CoreState
from homeassistant.helpers import issue_registry as ir
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

import custom_components.helios_forecast.coordinator as coordinator_mod
from custom_components.helios_forecast.config import (
    CONF_ARRAYS,
    CONF_KWP,
    CONF_PRODUCTION_ENTITY,
)
from custom_components.helios_forecast.const import DOMAIN
from custom_components.helios_forecast.coordinator import HeliosForecastCoordinator

from _weather import make_weather_series

pytestmark = pytest.mark.usefixtures("recorder_mock")

LINE = {"azimuth": 180.0, "tilt": 30.0, CONF_KWP: 3.0, "tracker": "none"}


def _entry(hass, data, options=None) -> MockConfigEntry:
    entry = MockConfigEntry(domain=DOMAIN, data=data, options=options or {}, entry_id="checkup_entry", title="Roof")
    entry.add_to_hass(hass)
    return entry


def _issues(hass):
    registry = ir.async_get(hass)
    return sorted(issue_id for (domain, issue_id) in registry.issues if domain == DOMAIN)


async def _refresh(hass, monkeypatch, entry) -> HeliosForecastCoordinator:
    coordinator = HeliosForecastCoordinator(hass, entry)
    monkeypatch.setattr(coordinator_mod, "fetch_weather", AsyncMock(return_value=make_weather_series(dt_util.utcnow())))
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    return coordinator


async def test_a_peak_power_in_watts_becomes_an_error_issue(hass, monkeypatch) -> None:
    entry = _entry(hass, {CONF_ARRAYS: [LINE, {**LINE, CONF_KWP: 4050.0}]})
    coordinator = await _refresh(hass, monkeypatch, entry)
    assert _issues(hass) == ["checkup_entry_line_kwp_unit_2", "checkup_entry_production_entity_unset"]
    issue = ir.async_get(hass).async_get_issue(DOMAIN, "checkup_entry_line_kwp_unit_2")
    assert issue.severity == ir.IssueSeverity.ERROR
    assert issue.translation_key == "line_kwp_unit"
    assert issue.translation_placeholders == {"entry": "Roof", "line": "2", "kwp": "4050"}
    assert [p.key for p in coordinator.problems] == ["line_kwp_unit", "production_entity_unset"]


async def test_issues_are_published_even_when_the_weather_fetch_fails(hass, monkeypatch) -> None:
    entry = _entry(hass, {CONF_ARRAYS: []})
    coordinator = HeliosForecastCoordinator(hass, entry)
    monkeypatch.setattr(coordinator_mod, "fetch_weather", AsyncMock(side_effect=RuntimeError("down")))
    await coordinator.async_refresh()
    assert not coordinator.last_update_success
    assert "checkup_entry_no_lines" in _issues(hass)


async def test_a_corrected_configuration_retires_its_issue(hass, monkeypatch) -> None:
    entry = _entry(hass, {CONF_ARRAYS: [{**LINE, CONF_KWP: 4050.0}]})
    coordinator = await _refresh(hass, monkeypatch, entry)
    assert "checkup_entry_line_kwp_unit_1" in _issues(hass)
    hass.config_entries.async_update_entry(entry, data={CONF_ARRAYS: [LINE]})
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert "checkup_entry_line_kwp_unit_1" not in _issues(hass)


async def test_entities_are_judged_only_once_home_assistant_runs(hass, monkeypatch) -> None:
    data = {CONF_ARRAYS: [LINE], CONF_PRODUCTION_ENTITY: "sensor.pv_energy"}
    entry = _entry(hass, data)
    hass.set_state(CoreState.starting)
    coordinator = await _refresh(hass, monkeypatch, entry)
    assert "checkup_entry_production_entity_missing" not in _issues(hass)
    hass.set_state(CoreState.running)
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert "checkup_entry_production_entity_missing" in _issues(hass)
    # A power sensor under that id is the wrong kind of entity.
    hass.states.async_set(
        "sensor.pv_energy", "1200", {"unit_of_measurement": "W", "device_class": "power", "state_class": "measurement"}
    )
    await coordinator.async_refresh()
    await hass.async_block_till_done()
    assert "checkup_entry_production_entity_missing" not in _issues(hass)
    assert "checkup_entry_production_entity_kind" in _issues(hass)


async def test_removing_the_entry_clears_its_issues(hass, monkeypatch) -> None:
    from custom_components.helios_forecast import async_remove_entry

    entry = _entry(hass, {CONF_ARRAYS: []})
    await _refresh(hass, monkeypatch, entry)
    assert _issues(hass)
    await async_remove_entry(hass, entry)
    assert _issues(hass) == []
