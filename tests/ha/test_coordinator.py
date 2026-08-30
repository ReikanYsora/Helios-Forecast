"""Tests for HeliosForecastCoordinator: the refresh orchestration, not the model math
(the forecast/analog/battery/consumption modules have their own pure tests).
"""

from __future__ import annotations

import logging
from datetime import timedelta
from unittest.mock import AsyncMock

import pytest
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

import custom_components.helios_forecast.coordinator as coordinator_mod
from custom_components.helios_forecast.const import DOMAIN
from custom_components.helios_forecast.coordinator import HeliosForecastCoordinator
from custom_components.helios_forecast.summary import DayForecast, ForecastSummary

from _weather import make_weather_series

pytestmark = pytest.mark.usefixtures("recorder_mock")


def _entry(hass, data=None) -> MockConfigEntry:
    entry = MockConfigEntry(domain=DOMAIN, data=data or {}, entry_id="test_entry")
    entry.add_to_hass(hass)
    return entry


def _summary(energy_kwh: float) -> ForecastSummary:
    day = DayForecast(date="2026-06-12", energy_kwh=energy_kwh, peak_power_w=0.0, peak_time=None)
    return ForecastSummary(
        power_now_w=None,
        power_now_low_w=None,
        power_now_high_w=None,
        power_next_hour_w=None,
        days=[day],
        energy_today_remaining_kwh=None,
        energy_this_hour_kwh=None,
        energy_next_hour_kwh=None,
        wh_hours={},
    )


# --- _async_update_data: weather fetch orchestration -----------------------------------------


async def test_update_data_success_populates_everything(hass, monkeypatch) -> None:
    entry = _entry(hass)
    coordinator = HeliosForecastCoordinator(hass, entry)
    weather = make_weather_series(dt_util.utcnow())
    monkeypatch.setattr(coordinator_mod, "fetch_weather", AsyncMock(return_value=weather))

    await coordinator.async_refresh()

    assert coordinator.last_update_success
    assert coordinator.data is not None
    assert coordinator.data.points
    assert coordinator.weather_series is weather


async def test_transient_empty_response_reuses_last_good_weather(hass, monkeypatch, caplog) -> None:
    entry = _entry(hass)
    coordinator = HeliosForecastCoordinator(hass, entry)
    weather = make_weather_series(dt_util.utcnow())
    fetch_mock = AsyncMock(side_effect=[weather, None])
    monkeypatch.setattr(coordinator_mod, "fetch_weather", fetch_mock)

    await coordinator.async_refresh()
    assert coordinator.last_update_success

    with caplog.at_level(logging.WARNING, logger=coordinator_mod._LOGGER.name):
        await coordinator.async_refresh()

    assert coordinator.last_update_success
    assert coordinator.weather_series is weather
    assert "reusing the last successful fetch" in caplog.text


async def test_empty_response_without_prior_fetch_fails_update(hass, monkeypatch) -> None:
    entry = _entry(hass)
    coordinator = HeliosForecastCoordinator(hass, entry)
    monkeypatch.setattr(coordinator_mod, "fetch_weather", AsyncMock(return_value=None))

    await coordinator.async_refresh()

    assert coordinator.last_update_success is False
    assert "Open-Meteo returned no weather data" in str(coordinator.last_exception)


async def test_fetch_exception_becomes_update_failed(hass, monkeypatch) -> None:
    entry = _entry(hass)
    coordinator = HeliosForecastCoordinator(hass, entry)

    async def _boom(*_args, **_kwargs):
        raise RuntimeError("network down")

    monkeypatch.setattr(coordinator_mod, "fetch_weather", _boom)

    await coordinator.async_refresh()

    assert coordinator.last_update_success is False
    assert "Open-Meteo fetch failed" in str(coordinator.last_exception)


# --- battery SoC projection: the "off" reasons ------------------------------------------------


async def test_battery_off_missing_config_logs_once(hass, caplog) -> None:
    entry = _entry(hass)
    coordinator = HeliosForecastCoordinator(hass, entry)
    now = dt_util.now()

    with caplog.at_level(logging.WARNING, logger=coordinator_mod._LOGGER.name):
        result1 = await coordinator._project_battery_soc({}, [], now)
        result2 = await coordinator._project_battery_soc({}, [], now)

    assert result1 == [] and result2 == []
    matching = [r for r in caplog.records if "battery SoC projection is off" in r.message]
    assert len(matching) == 1, "the second identical reason must not re-log"


async def test_battery_off_soc_entity_missing_state(hass) -> None:
    entry = _entry(
        hass,
        data={"battery_capacity_kwh": 10.0, "battery_soc_entity": "sensor.battery_soc"},
    )
    coordinator = HeliosForecastCoordinator(hass, entry)
    # No hass.states.async_set: the entity has no state at all.
    result = await coordinator._project_battery_soc(entry.data, [], dt_util.now())
    assert result == []


async def test_battery_off_soc_entity_unavailable_logs_transient(hass, caplog) -> None:
    entry = _entry(
        hass,
        data={"battery_capacity_kwh": 10.0, "battery_soc_entity": "sensor.battery_soc"},
    )
    coordinator = HeliosForecastCoordinator(hass, entry)
    hass.states.async_set("sensor.battery_soc", "unavailable")

    with caplog.at_level(logging.INFO, logger=coordinator_mod._LOGGER.name):
        result = await coordinator._project_battery_soc(entry.data, [], dt_util.now())

    assert result == []
    assert "transient" in caplog.text


async def test_battery_off_soc_entity_non_numeric(hass) -> None:
    entry = _entry(
        hass,
        data={"battery_capacity_kwh": 10.0, "battery_soc_entity": "sensor.battery_soc"},
    )
    coordinator = HeliosForecastCoordinator(hass, entry)
    hass.states.async_set("sensor.battery_soc", "not-a-number")

    result = await coordinator._project_battery_soc(entry.data, [], dt_util.now())
    assert result == []


async def test_battery_off_reason_re_logs_once_recovered(hass, caplog) -> None:
    """A distinct new reason logs again even though a prior reason was already logged."""
    entry = _entry(
        hass,
        data={"battery_capacity_kwh": 10.0, "battery_soc_entity": "sensor.battery_soc"},
    )
    coordinator = HeliosForecastCoordinator(hass, entry)

    with caplog.at_level(logging.INFO, logger=coordinator_mod._LOGGER.name):
        # First: entity has no state at all.
        await coordinator._project_battery_soc(entry.data, [], dt_util.now())
        # Then: entity appears but reads a bad value - a different reason.
        hass.states.async_set("sensor.battery_soc", "not-a-number")
        await coordinator._project_battery_soc(entry.data, [], dt_util.now())

    off_lines = [r for r in caplog.records if "battery SoC projection is off" in r.message]
    assert len(off_lines) == 2


# --- consumption profile: caching + resilience ------------------------------------------------


async def test_consumption_profile_cached_within_the_hour(hass, monkeypatch) -> None:
    entry = _entry(hass)
    coordinator = HeliosForecastCoordinator(hass, entry)
    sentinel = object()
    now = dt_util.now()
    coordinator._consumption_profile = sentinel
    coordinator._last_consumption_hour = now.replace(minute=0, second=0, microsecond=0)

    manager_mock = AsyncMock()
    monkeypatch.setattr("homeassistant.components.energy.async_get_manager", manager_mock)

    result = await coordinator._consumption_profile_for(now)

    assert result is sentinel
    manager_mock.assert_not_called()


async def test_consumption_profile_no_energy_sources_keeps_previous(hass, caplog) -> None:
    entry = _entry(hass)
    coordinator = HeliosForecastCoordinator(hass, entry)

    with caplog.at_level(logging.WARNING, logger=coordinator_mod._LOGGER.name):
        result = await coordinator._consumption_profile_for(dt_util.now())

    assert result is None  # nothing configured yet, nothing to fall back to either
    assert "no configured sources" in caplog.text


async def test_consumption_profile_throttle_engages_even_when_profile_stays_none(hass, monkeypatch) -> None:
    """Sources are configured but the recorder has no history yet, so the build keeps returning
    None. The hourly throttle must still engage on the attempt, not only on a successful build."""
    entry = _entry(hass)
    coordinator = HeliosForecastCoordinator(hass, entry)
    now = dt_util.now().replace(minute=5, second=0, microsecond=0)

    class _Manager:
        data = {"energy_sources": [{"type": "solar", "stat_energy_from": "sensor.solar_production"}]}

    manager_mock = AsyncMock(return_value=_Manager())
    monkeypatch.setattr("homeassistant.components.energy.async_get_manager", manager_mock)

    result1 = await coordinator._consumption_profile_for(now)
    result2 = await coordinator._consumption_profile_for(now + timedelta(minutes=30))

    assert result1 is None
    assert result2 is None
    manager_mock.assert_awaited_once()


async def test_consumption_profile_source_fetch_failure_logs_and_uses_remaining_sources(
    hass, monkeypatch, caplog
) -> None:
    entry = _entry(hass)
    coordinator = HeliosForecastCoordinator(hass, entry)
    now = dt_util.now().replace(minute=5, second=0, microsecond=0)

    class _Manager:
        data = {
            "energy_sources": [
                {"type": "solar", "stat_energy_from": "sensor.solar_production"},
                {
                    "type": "grid",
                    "stat_energy_from": "sensor.grid_import",
                    "stat_energy_to": "sensor.grid_export",
                },
            ]
        }

    monkeypatch.setattr("homeassistant.components.energy.async_get_manager", AsyncMock(return_value=_Manager()))

    from custom_components.helios_forecast.solar.residual import ProductionBucket

    good_bucket = ProductionBucket(start_ms=now.timestamp() * 1000.0, end_ms=(now.timestamp() + 3600) * 1000.0, kwh=1.0)

    async def _fetch(stat_id, _start, _end):
        if stat_id == "sensor.solar_production":
            raise RuntimeError("recorder timeout")
        return [good_bucket]

    monkeypatch.setattr(coordinator, "_fetch_change_buckets", _fetch)

    with caplog.at_level(logging.WARNING, logger=coordinator_mod._LOGGER.name):
        result = await coordinator._consumption_profile_for(now)

    assert result is not None
    assert "sensor.solar_production" in caplog.text


async def test_consumption_profile_energy_manager_error_keeps_previous(hass, monkeypatch, caplog) -> None:
    entry = _entry(hass)
    coordinator = HeliosForecastCoordinator(hass, entry)
    sentinel = object()
    coordinator._consumption_profile = sentinel
    coordinator._last_consumption_hour = dt_util.now().replace(minute=0, second=0, microsecond=0) - timedelta(hours=5)

    async def _boom(_hass):
        raise RuntimeError("energy store corrupt")

    monkeypatch.setattr("homeassistant.components.energy.async_get_manager", _boom)

    with caplog.at_level(logging.WARNING, logger=coordinator_mod._LOGGER.name):
        result = await coordinator._consumption_profile_for(dt_util.now())

    assert result is sentinel
    assert "Energy dashboard unavailable" in caplog.text


# --- today-trend: capture + persistence --------------------------------------------------------


async def test_today_trend_captures_reference_at_anchor_and_persists(hass) -> None:
    entry = _entry(hass)
    coordinator = HeliosForecastCoordinator(hass, entry)
    now = dt_util.now().replace(hour=6, minute=5, second=0, microsecond=0)

    trend = await coordinator._today_trend({}, now, _summary(12.5))

    assert trend.reference_kwh == 12.5
    stored = await coordinator._trend_store.async_load()
    assert stored["kwh"] == 12.5
    assert stored["date"] == now.date().isoformat()


async def test_today_trend_does_not_recapture_same_day(hass) -> None:
    entry = _entry(hass)
    coordinator = HeliosForecastCoordinator(hass, entry)
    morning = dt_util.now().replace(hour=6, minute=5, second=0, microsecond=0)
    await coordinator._today_trend({}, morning, _summary(10.0))

    later = morning + timedelta(hours=3)
    trend = await coordinator._today_trend({}, later, _summary(30.0))

    # Reference frozen at the morning capture, current tracks the new total.
    assert trend.reference_kwh == 10.0
    assert trend.current_kwh == 30.0


async def test_today_trend_reference_survives_a_restart(hass) -> None:
    entry = _entry(hass)
    first = HeliosForecastCoordinator(hass, entry)
    morning = dt_util.now().replace(hour=6, minute=5, second=0, microsecond=0)
    await first._today_trend({}, morning, _summary(15.0))

    # A fresh coordinator instance, as after an HA restart, sharing the same Store on disk.
    second = HeliosForecastCoordinator(hass, entry)
    later = morning + timedelta(hours=2)
    trend = await second._today_trend({}, later, _summary(22.0))

    assert trend.reference_kwh == 15.0
    assert trend.current_kwh == 22.0


# --- residual map: learning entity resolution ---------------------------------------------------


async def test_build_residual_map_none_without_production_entity(hass) -> None:
    entry = _entry(hass)
    coordinator = HeliosForecastCoordinator(hass, entry)
    now = dt_util.now()
    weather = make_weather_series(dt_util.utcnow())

    from custom_components.helios_forecast.solar.power import PvLayout

    layout = PvLayout(orientations=[], shares=[], coords=[], total_kwp=0.0, caps=[])
    result = await coordinator._build_residual_map({}, 45.0, 5.0, layout, weather, now)

    assert result is None
    assert coordinator._production_buckets == []


async def test_build_residual_map_none_when_production_history_empty(hass, caplog) -> None:
    entry = _entry(hass, data={"production_entity": "sensor.pv_production"})
    coordinator = HeliosForecastCoordinator(hass, entry)
    now = dt_util.now()
    weather = make_weather_series(dt_util.utcnow())

    from custom_components.helios_forecast.solar.power import PvLayout

    layout = PvLayout(orientations=[], shares=[], coords=[], total_kwp=0.0, caps=[])
    with caplog.at_level(logging.WARNING, logger=coordinator_mod._LOGGER.name):
        result = await coordinator._build_residual_map(entry.data, 45.0, 5.0, layout, weather, now)

    assert result is None
    assert "learning is off" in caplog.text


# --- weather statistics archive: entity-registration gating -----------------------------------


async def test_write_weather_statistics_noop_before_entities_registered(hass) -> None:
    entry = _entry(hass)
    coordinator = HeliosForecastCoordinator(hass, entry)
    coordinator.weather_series = make_weather_series(dt_util.utcnow())

    coordinator.write_weather_statistics(dt_util.utcnow(), full=True)

    # No sensor entity registered yet for any weather field: nothing counted as written,
    # so the high-water mark must not advance (or the real backfill would be skipped later).
    assert coordinator._last_weather_stat_hour is None


async def test_write_weather_statistics_advances_marker_once_entity_exists(hass) -> None:
    from homeassistant.helpers import entity_registry as er

    entry = _entry(hass)
    coordinator = HeliosForecastCoordinator(hass, entry)
    coordinator.weather_series = make_weather_series(dt_util.utcnow())

    registry = er.async_get(hass)
    registry.async_get_or_create(
        "sensor", DOMAIN, f"{entry.entry_id}_cloud_cover", suggested_object_id="helios_cloud_cover"
    )

    now_utc = dt_util.utcnow()
    coordinator.write_weather_statistics(now_utc, full=True)

    assert coordinator._last_weather_stat_hour == now_utc.replace(minute=0, second=0, microsecond=0)
