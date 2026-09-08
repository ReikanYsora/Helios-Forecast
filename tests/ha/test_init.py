"""Tests for the integration entry lifecycle: setup, unload, remove, and the
SoC-recovery state listener. Sensor platform forwarding is stubbed out (owned by
sensor.py, outside this module's scope); what is under test here is the wiring
__init__.py itself is responsible for.
"""

from __future__ import annotations

import asyncio
from datetime import timedelta
from unittest.mock import AsyncMock

import pytest
from homeassistant.config_entries import ConfigEntryState, current_entry
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
from homeassistant.core import CoreState
from homeassistant.const import Platform
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry, async_fire_time_changed

import custom_components.helios_forecast.archive as archive_mod
import custom_components.helios_forecast.coordinator as coordinator_mod
from custom_components.helios_forecast import async_setup_entry, async_unload_entry, _legacy_issue_id
from custom_components.helios_forecast.const import DOMAIN

from _weather import make_weather_series

pytestmark = pytest.mark.usefixtures("recorder_mock")


async def _setup(hass, monkeypatch, data=None, entry: MockConfigEntry | None = None) -> MockConfigEntry:
    """Add (unless already added) and set up a config entry, network + platform forwarding stubbed."""
    if entry is None:
        entry = MockConfigEntry(domain=DOMAIN, data=data or {}, entry_id="test_entry")
        entry.add_to_hass(hass)
    entry.mock_state(hass, ConfigEntryState.SETUP_IN_PROGRESS)

    weather = make_weather_series(dt_util.utcnow())
    monkeypatch.setattr(coordinator_mod, "fetch_weather", AsyncMock(return_value=weather))
    monkeypatch.setattr(hass.config_entries, "async_forward_entry_setups", AsyncMock(return_value=True))

    token = current_entry.set(entry)
    try:
        assert await async_setup_entry(hass, entry) is True
    finally:
        current_entry.reset(token)
    return entry


async def test_the_archive_migration_waits_for_home_assistant_to_have_started(hass, monkeypatch) -> None:
    """Never during setup. The recorder blocks its own queue until the started event, so a setup that
    waits on a recorder write waits on a start that is waiting on it, and the entry is cancelled
    after five minutes of that."""
    calls: list = []

    async def _migrate(_hass, _entry) -> None:
        calls.append(True)

    monkeypatch.setattr(archive_mod, "async_migrate", _migrate)
    hass.set_state(CoreState.starting)

    await _setup(hass, monkeypatch)
    await hass.async_block_till_done()
    assert not calls  # nothing has run yet

    hass.bus.async_fire(EVENT_HOMEASSISTANT_STARTED)
    await hass.async_block_till_done()
    assert calls == [True]


async def test_deleting_the_entry_takes_its_archived_series_with_it(hass, monkeypatch) -> None:
    """The archived series belong to the integration rather than to an entity, which is what keeps
    the recorder off them and also means Home Assistant has nothing that could offer to clean them
    up: its statistics validation only looks at series carrying an entity id."""
    from custom_components.helios_forecast import async_remove_entry
    from custom_components.helios_forecast.statistics import ARCHIVED_SERIES, external_statistic_id

    entry = await _setup(hass, monkeypatch)
    cleared: list = []
    monkeypatch.setattr(
        "homeassistant.components.recorder.get_instance",
        lambda _hass: type("I", (), {"async_clear_statistics": staticmethod(cleared.extend)})(),
    )

    await async_remove_entry(hass, entry)

    assert set(cleared) == {external_statistic_id(entry.entry_id, key) for key, _u, _n in ARCHIVED_SERIES}


async def test_a_settings_key_no_version_reads_is_cleared_from_the_entry(hass, monkeypatch) -> None:
    """An earlier build stored a write credential in the entry. Nothing reads it now, and the
    diagnostics download hands the configuration over as it stands, so it has to go from the entry."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"latitude": 48.85, "longitude": 2.35, "benchmark_key": "live-write-credential"},
        options={"benchmark_enabled": True},
        entry_id="retired_keys_entry",
    )
    entry.add_to_hass(hass)
    await _setup(hass, monkeypatch, entry=entry)

    assert "benchmark_key" not in entry.data
    # But the choice to take part is not a retired setting: an installation that said yes keeps
    # saying yes across the update, without being asked again.
    assert {**entry.data, **entry.options}["benchmark_enabled"] is True
    assert entry.data["latitude"] == 48.85

    from custom_components.helios_forecast.diagnostics import async_get_config_entry_diagnostics

    report = await async_get_config_entry_diagnostics(hass, entry)
    assert "live-write-credential" not in str(report)


async def test_setup_entry_registers_coordinator_and_forwards_sensor_platform(hass, monkeypatch) -> None:
    entry = await _setup(hass, monkeypatch)

    coordinator = hass.data[DOMAIN][entry.entry_id]
    assert coordinator.last_update_success
    hass.config_entries.async_forward_entry_setups.assert_awaited_once()
    forwarded_platforms = hass.config_entries.async_forward_entry_setups.call_args.args[1]
    assert list(forwarded_platforms) == [Platform.SENSOR]


async def test_setup_completes_even_if_the_archive_migration_never_returns(hass, monkeypatch) -> None:
    """The recorder does not process its queue until Home Assistant has finished starting, so a
    migration awaited inside setup waits for a start that is waiting for that same setup, and Home
    Assistant cancels the entry after five minutes. Setup must never depend on it finishing."""
    running = asyncio.Event()

    async def _never_returns(_hass, _entry) -> None:
        running.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(archive_mod, "async_migrate", _never_returns)
    entry = await asyncio.wait_for(_setup(hass, monkeypatch), 10)

    assert hass.data[DOMAIN][entry.entry_id].last_update_success
    assert running.is_set()  # armed and started, just not waited on
    await async_unload_entry(hass, entry)


async def test_setup_entry_deletes_legacy_multi_array_issue(hass, monkeypatch) -> None:
    from homeassistant.helpers import issue_registry as ir

    entry = MockConfigEntry(domain=DOMAIN, data={}, entry_id="test_entry")
    entry.add_to_hass(hass)
    ir.async_create_issue(
        hass,
        DOMAIN,
        _legacy_issue_id(entry),
        is_fixable=False,
        severity=ir.IssueSeverity.WARNING,
        translation_key="legacy_multi_array",
    )
    assert ir.async_get(hass).async_get_issue(DOMAIN, _legacy_issue_id(entry)) is not None

    await _setup(hass, monkeypatch, entry=entry)

    assert ir.async_get(hass).async_get_issue(DOMAIN, _legacy_issue_id(entry)) is None


async def test_unload_entry_pops_coordinator_from_hass_data(hass, monkeypatch) -> None:
    entry = await _setup(hass, monkeypatch)
    assert entry.entry_id in hass.data[DOMAIN]

    monkeypatch.setattr(hass.config_entries, "async_unload_platforms", AsyncMock(return_value=True))

    assert await async_unload_entry(hass, entry) is True
    assert entry.entry_id not in hass.data[DOMAIN]


async def test_unload_entry_keeps_coordinator_when_platform_unload_fails(hass, monkeypatch) -> None:
    entry = await _setup(hass, monkeypatch)

    monkeypatch.setattr(hass.config_entries, "async_unload_platforms", AsyncMock(return_value=False))

    assert await async_unload_entry(hass, entry) is False
    assert entry.entry_id in hass.data[DOMAIN]


async def test_soc_recovery_listener_requests_refresh_on_transition_to_available(hass, monkeypatch) -> None:
    entry = await _setup(
        hass,
        monkeypatch,
        data={"battery_capacity_kwh": 10.0, "battery_soc_entity": "sensor.battery_soc"},
    )
    coordinator = hass.data[DOMAIN][entry.entry_id]

    refresh_mock = AsyncMock()
    monkeypatch.setattr(coordinator, "async_request_refresh", refresh_mock)

    hass.states.async_set("sensor.battery_soc", "unavailable")
    await hass.async_block_till_done()
    hass.states.async_set("sensor.battery_soc", "55")
    await hass.async_block_till_done()

    refresh_mock.assert_awaited_once()


async def test_soc_recovery_listener_ignores_ordinary_value_changes(hass, monkeypatch) -> None:
    entry = await _setup(
        hass,
        monkeypatch,
        data={"battery_capacity_kwh": 10.0, "battery_soc_entity": "sensor.battery_soc"},
    )
    coordinator = hass.data[DOMAIN][entry.entry_id]

    hass.states.async_set("sensor.battery_soc", "50")
    await hass.async_block_till_done()

    refresh_mock = AsyncMock()
    monkeypatch.setattr(coordinator, "async_request_refresh", refresh_mock)

    # 50 -> 55 is neither state unavailable/unknown: an ordinary reading change,
    # left to the normal 30-minute cadence.
    hass.states.async_set("sensor.battery_soc", "55")
    await hass.async_block_till_done()

    refresh_mock.assert_not_awaited()


async def test_no_soc_listener_registered_without_battery_soc_entity(hass, monkeypatch) -> None:
    entry = await _setup(hass, monkeypatch)
    coordinator = hass.data[DOMAIN][entry.entry_id]

    refresh_mock = AsyncMock()
    monkeypatch.setattr(coordinator, "async_request_refresh", refresh_mock)

    hass.states.async_set("sensor.unrelated", "unavailable")
    await hass.async_block_till_done()
    hass.states.async_set("sensor.unrelated", "42")
    await hass.async_block_till_done()

    refresh_mock.assert_not_awaited()


async def test_hour_rollover_triggers_off_cycle_refresh(hass, monkeypatch) -> None:
    """The archive (past-forecast curve) only rebuilds once an hour, but that check only
    runs inside a refresh: without this listener, the just-elapsed hour could sit up to 30
    minutes past its own boundary still served unclamped.
    """
    entry = await _setup(hass, monkeypatch)
    coordinator = hass.data[DOMAIN][entry.entry_id]

    refresh_mock = AsyncMock()
    monkeypatch.setattr(coordinator, "async_request_refresh", refresh_mock)

    now = dt_util.utcnow()
    next_hour = (now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)).replace(second=5)
    async_fire_time_changed(hass, next_hour)
    await hass.async_block_till_done()

    refresh_mock.assert_awaited_once()
