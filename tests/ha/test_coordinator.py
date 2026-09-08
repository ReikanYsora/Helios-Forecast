"""Tests for HeliosForecastCoordinator: the refresh orchestration, not the model math
(the forecast/analog/battery/consumption modules have their own pure tests).
"""

from __future__ import annotations

import logging
import math
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock

import pytest
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

import custom_components.helios_forecast.coordinator as coordinator_mod
from custom_components.helios_forecast.const import DOMAIN
from custom_components.helios_forecast.coordinator import HeliosForecastCoordinator
from custom_components.helios_forecast.statistics import WEATHER_FIELDS as _WEATHER_FIELDS
from custom_components.helios_forecast.statistics import external_statistic_id
from custom_components.helios_forecast.checkup import Problem
from custom_components.helios_forecast.consumption import ConsumptionProfile
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


async def test_the_day_ahead_prediction_is_written_down_once_and_survives_a_restart(hass, monkeypatch, freezer) -> None:
    """The skill term is only worth anything if the prediction predates the measurement, so tomorrow's
    total is recorded the first time it is seen and never rewritten with a better-informed one."""
    entry = _entry(hass)
    coordinator = HeliosForecastCoordinator(hass, entry)
    monkeypatch.setattr(coordinator_mod, "fetch_weather", AsyncMock(return_value=make_weather_series(dt_util.utcnow())))

    await coordinator.async_refresh()
    tomorrow = dt_util.now().date() + timedelta(days=1)
    assert tomorrow in coordinator._day_ahead
    first = coordinator._day_ahead[tomorrow]

    # Later the same day the forecast has moved on; the record must not follow it.
    freezer.move_to(dt_util.utcnow() + timedelta(hours=6))
    await coordinator.async_refresh()
    assert coordinator._day_ahead[tomorrow] == first

    # And a fresh coordinator, as after a restart, reads it back rather than starting blank.
    revived = HeliosForecastCoordinator(hass, entry)
    await revived.async_refresh()
    assert revived._day_ahead[tomorrow] == first


async def test_a_stale_weather_series_archives_nothing_it_cannot_know(hass, monkeypatch, freezer) -> None:
    """Open-Meteo answers nothing for hours at a time; the refresh then reuses the last good series.

    That series knows the past only up to the moment it was fetched. Archiving past it writes a
    forecast into the observed record, and moves the high-water mark over hours nobody measured, so
    the real values never land when the service comes back.
    """
    entry = _entry(hass)
    coordinator = HeliosForecastCoordinator(hass, entry)
    start = dt_util.utcnow()
    weather = make_weather_series(start)
    monkeypatch.setattr(coordinator_mod, "fetch_weather", AsyncMock(side_effect=[weather, None]))
    # Only the weather archive is at stake: the predicted-production archive carries no mark and is
    # rebuilt whole every hour, so a pass over stale inputs is overwritten as soon as the service is back.
    weather_ids = {external_statistic_id(entry.entry_id, field.key) for field in _WEATHER_FIELDS}
    written: list = []
    monkeypatch.setattr(
        coordinator_mod,
        "async_add_external_statistics",
        lambda _h, meta, rows: written.append((meta["statistic_id"], rows)),
    )

    await coordinator.async_refresh()
    mark_after_the_good_fetch = coordinator._last_weather_stat_hour
    written.clear()

    freezer.move_to(start + timedelta(hours=8))
    await coordinator.async_refresh()

    assert coordinator.last_update_success  # the reuse path, not a failed refresh
    assert coordinator._last_weather_stat_hour == mark_after_the_good_fetch
    assert not [row for sid, rows in written if sid in weather_ids for row in rows if row["start"] >= start]

    # And the half that matters: once the service answers again, every hour the outage covered lands.
    written.clear()
    recovered = make_weather_series(dt_util.utcnow())
    monkeypatch.setattr(coordinator_mod, "fetch_weather", AsyncMock(return_value=recovered))
    await coordinator.async_refresh()

    hours = {row["start"] for sid, rows in written if sid in weather_ids for row in rows}
    missed = start.replace(minute=0, second=0, microsecond=0)
    while missed < dt_util.utcnow().replace(minute=0, second=0, microsecond=0):
        assert missed in hours, missed
        missed += timedelta(hours=1)


async def test_the_local_horizon_is_covered_by_real_weather_west_of_greenwich(hass, monkeypatch) -> None:
    """The horizon runs on local midnights, the weather window on whole UTC days.

    West of Greenwich the local horizon ends after the last UTC hour the service answers, so the
    request has to reach a day further or the last local day is forecast from weather nobody has.
    """
    await hass.config.async_set_time_zone("America/Los_Angeles")
    entry = _entry(hass)
    coordinator = HeliosForecastCoordinator(hass, entry)

    async def _fetch(session, lat, lon, *, past_days=0, forecast_days=7):
        return make_weather_series(dt_util.utcnow(), past_days=past_days, forecast_days=forecast_days)

    monkeypatch.setattr(coordinator_mod, "fetch_weather", _fetch)
    await coordinator.async_refresh()

    points = coordinator.data.points
    horizon_end = dt_util.now().replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(
        days=coordinator_mod.FORECAST_DAYS
    )
    assert points[-1].t >= horizon_end - timedelta(minutes=15)


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


async def test_battery_off_missing_config_logs_once_at_info(hass, caplog) -> None:
    """No battery configured is a normal PV-only setup, not a misconfiguration:
    it belongs at INFO, not WARNING, and every restart otherwise nags for nothing."""
    entry = _entry(hass)
    coordinator = HeliosForecastCoordinator(hass, entry)
    now = dt_util.now()

    with caplog.at_level(logging.INFO, logger=coordinator_mod._LOGGER.name):
        result1 = await coordinator._project_battery_soc({}, [], now)
        result2 = await coordinator._project_battery_soc({}, [], now)

    assert result1 == [] and result2 == []
    matching = [r for r in caplog.records if "battery SoC projection is off" in r.message]
    assert len(matching) == 1, "the second identical reason must not re-log"
    assert matching[0].levelno == logging.INFO
    assert "transient" not in matching[0].message, "not a retry - there is simply no battery to project"


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
                    "flow_from": [{"stat_energy_from": "sensor.grid_import"}],
                    "flow_to": [{"stat_energy_to": "sensor.grid_export"}],
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
    result = await coordinator._build_residual_map({}, 45.0, 5.0, layout, math.inf, weather, now)

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
        result = await coordinator._build_residual_map(entry.data, 45.0, 5.0, layout, math.inf, weather, now)

    assert result is None
    assert "learning is off" in caplog.text


# --- weather statistics archive: entity-registration gating -----------------------------------


async def test_write_weather_statistics_needs_no_entity(hass, monkeypatch) -> None:
    from custom_components.helios_forecast.statistics import WEATHER_FIELDS, external_statistic_id

    entry = _entry(hass)
    coordinator = HeliosForecastCoordinator(hass, entry)
    coordinator.weather_series = make_weather_series(dt_util.utcnow())

    written: list = []
    monkeypatch.setattr(
        coordinator_mod, "async_add_external_statistics", lambda _hass, meta, rows: written.append((meta, rows))
    )
    now_utc = dt_util.utcnow()
    coordinator.write_weather_statistics(now_utc, full=True)

    # No sensor entity is registered, and that no longer matters: the series belong to the
    # integration, so the backfill lands and the high-water mark advances. It sits behind the newest
    # hour written, by the window the archive keeps re-offering; the newest completed hour is the one
    # before the current one, which is where the import stops.
    newest = now_utc.replace(minute=0, second=0, microsecond=0) - timedelta(hours=1)
    assert coordinator._last_weather_stat_hour == newest - timedelta(hours=coordinator_mod.ARCHIVE_RETRY_HOURS)
    assert {meta["statistic_id"] for meta, _rows in written} == {
        external_statistic_id(entry.entry_id, field.key) for field in WEATHER_FIELDS
    }
    assert all(meta["source"] == DOMAIN and meta["name"] for meta, _rows in written)


async def test_write_forecast_statistics_writes_both_series_under_their_own_ids(hass, monkeypatch) -> None:
    """The only writer of the predicted-production history since the two archive sensors were
    removed, so nothing else would show that it stopped, wrote elsewhere, or mislabelled its units."""
    from custom_components.helios_forecast.statistics import (
        FORECAST_ENERGY_KEY,
        FORECAST_POWER_KEY,
        external_statistic_id,
    )

    entry = _entry(hass)
    coordinator = HeliosForecastCoordinator(hass, entry)
    hour = dt_util.utcnow().replace(minute=0, second=0, microsecond=0)
    coordinator._forecast_stat_rows = {
        FORECAST_POWER_KEY: [{"start": hour, "mean": 1500.0, "min": 1500.0, "max": 1500.0}],
        FORECAST_ENERGY_KEY: [{"start": hour, "mean": 1.5, "min": 1.5, "max": 1.5}],
    }

    written: list = []
    monkeypatch.setattr(
        coordinator_mod, "async_add_external_statistics", lambda _h, meta, rows: written.append((meta, rows))
    )
    coordinator.write_forecast_statistics()

    by_id = {meta["statistic_id"]: (meta, rows) for meta, rows in written}
    power = by_id[external_statistic_id(entry.entry_id, FORECAST_POWER_KEY)]
    energy = by_id[external_statistic_id(entry.entry_id, FORECAST_ENERGY_KEY)]
    assert power[0]["unit_of_measurement"] == "W" and energy[0]["unit_of_measurement"] == "kWh"
    assert power[1][0]["mean"] == 1500.0 and energy[1][0]["mean"] == 1.5
    assert all(meta["source"] == DOMAIN and meta["name"] for meta, _rows in written)


async def test_a_refresh_does_not_retire_and_recreate_the_issues_it_is_about_to_publish(hass, monkeypatch) -> None:
    """Retiring an issue and creating it again destroys the registry entry, and with it the user's
    decision to ignore it. On a thirty-minute refresh that happens forty-eight times a day."""
    from homeassistant.helpers import issue_registry as ir

    entry = _entry(hass)
    coordinator = HeliosForecastCoordinator(hass, entry)
    monkeypatch.setattr(coordinator_mod, "fetch_weather", AsyncMock(return_value=make_weather_series(dt_util.utcnow())))
    # A problem the data checks find, so it is published by the second pass and not the first.
    monkeypatch.setattr(coordinator, "_check_configuration", lambda _data: [])
    data_problem = Problem("production_at_night", "error", {"entity": "sensor.meter", "kwh": "3.0"}, "meter")
    monkeypatch.setattr(coordinator_mod, "check_consumption_coverage", lambda _c: [data_problem])
    coordinator._consumption_profile = ConsumptionProfile(slot_w={}, hour_w={}, overall_w=0.0, samples=0, coverage={})

    await coordinator.async_refresh()
    published = set(coordinator._issue_ids)
    assert published

    deleted: list = []
    monkeypatch.setattr(ir, "async_delete_issue", lambda _h, _d, issue_id: deleted.append(issue_id))
    await coordinator.async_refresh()

    assert not (published & set(deleted))


async def test_a_trend_store_missing_a_field_does_not_break_every_refresh(hass, monkeypatch) -> None:
    """A store file that is valid JSON with a key missing must not raise out of the refresh: that
    fails the config entry setup, and the file is not something a user can go and repair."""
    entry = _entry(hass)
    coordinator = HeliosForecastCoordinator(hass, entry)
    monkeypatch.setattr(coordinator_mod, "fetch_weather", AsyncMock(return_value=make_weather_series(dt_util.utcnow())))
    monkeypatch.setattr(
        coordinator._trend_store,
        "async_load",
        AsyncMock(return_value={"date": "2026-09-08", "captured_at": "2026-09-08T06:00:00+00:00"}),
    )

    await coordinator.async_refresh()

    assert coordinator.last_update_success


async def test_the_weather_mark_follows_the_data_not_the_clock(hass, monkeypatch) -> None:
    """The mark must not run past the hours the service has actually published.

    Open-Meteo publishes a past hour with a delay, and a wide delay is normal: taken from the clock,
    the mark leaps over everything the response did not carry, and those hours are then never
    written. This is the defect ARCHIVE_RETRY_HOURS addresses, one window further out.
    """
    entry = _entry(hass)
    coordinator = HeliosForecastCoordinator(hass, entry)
    now = dt_util.utcnow()
    behind = make_weather_series(now)
    # The service is twelve hours behind: everything after that is missing from the response.
    last = now.replace(minute=0, second=0, microsecond=0) - timedelta(hours=12)
    keep = sum(1 for t in behind.times if t <= last)
    for attr in ("times", "cloud", "shortwave", "direct", "diffuse", "temp", "wind", "snow", "cloud_spread"):
        del getattr(behind, attr)[keep:]
    monkeypatch.setattr(coordinator_mod, "fetch_weather", AsyncMock(return_value=behind))

    await coordinator.async_refresh()

    assert coordinator._last_weather_stat_hour == last - timedelta(hours=coordinator_mod.ARCHIVE_RETRY_HOURS)


async def test_power_now_reads_the_curve_the_card_draws(hass, monkeypatch) -> None:
    """ "Now" always sits inside a step that began in the past, and the elapsed stretch exists twice:
    raw, meaning what the forecast said at the time, and clamped by the learned ceiling, which is what
    the card draws. Reading the raw one made the headline sensor and the card disagree, by the whole
    height of the ceiling on a shaded roof, for the same instant on the same screen."""
    entry = _entry(hass)
    coordinator = HeliosForecastCoordinator(hass, entry)
    monkeypatch.setattr(coordinator_mod, "fetch_weather", AsyncMock(return_value=make_weather_series(dt_util.utcnow())))

    def _untouched(points, *_args, **_kwargs):
        return list(points)

    def _clamped(points, *_args, **_kwargs):
        # Stands in for the learned ceiling: whatever the physics said, the site never does this much.
        return [replace(p, pv_w=p.pv_w + 500.0) for p in points]

    monkeypatch.setattr(coordinator_mod, "enrich_archive_points", _untouched)
    await coordinator.async_refresh()
    raw_today = coordinator.data.summary.days[0].energy_kwh

    monkeypatch.setattr(coordinator_mod, "enrich_archive_points", _clamped)
    await coordinator.async_refresh()

    assert coordinator.elapsed_points  # there is an elapsed stretch to disagree about
    # Today's figures move with the clamped copy, which is the proof the sensors read it and not the
    # raw elapsed points beside it.
    assert coordinator.data.summary.days[0].energy_kwh > raw_today


# --- curtailment flagging ------------------------------------------------------------------


async def test_flag_curtailed_marks_full_battery_hours_at_the_cap(hass, monkeypatch) -> None:
    from custom_components.helios_forecast.solar.residual import ProductionBucket

    entry = _entry(hass)
    coordinator = HeliosForecastCoordinator(hass, entry)
    hour = 3_600_000.0
    buckets = [ProductionBucket(start_ms=h * hour, end_ms=(h + 1) * hour, kwh=1.15) for h in (10, 11)]

    async def _statistics(stat_id, start, end, types, units):
        assert stat_id == "sensor.soc" and types == {"max"}
        return [
            {"start": 10 * 3600.0, "end": 11 * 3600.0, "max": 100.0},
            {"start": 11 * 3600.0, "end": 12 * 3600.0, "max": 80.0},
        ]

    monkeypatch.setattr(coordinator, "_statistics", _statistics)
    data = {"battery_soc_entity": "sensor.soc", "inverter_max_kw": 1.2}
    start = datetime.fromtimestamp(0, tz=timezone.utc)
    out = await coordinator._flag_curtailed(data, buckets, start, start + timedelta(hours=24))
    assert [b.curtailed for b in out] == [True, False]

    # No cap configured: the battery signal is unusable, nothing is flagged and the history is not read.
    async def _no_call(*_a, **_k):
        raise AssertionError("statistics must not be read without a cap")

    monkeypatch.setattr(coordinator, "_statistics", _no_call)
    out = await coordinator._flag_curtailed(
        {"battery_soc_entity": "sensor.soc"}, buckets, start, start + timedelta(hours=24)
    )
    assert [b.curtailed for b in out] == [False, False]


async def test_an_hour_the_weather_service_publishes_late_still_reaches_the_archive(hass, monkeypatch) -> None:
    """The defect this guards against was found on a contributor's instance, on MariaDB.

    Open-Meteo publishes a past hour with a delay. A refresh that has an older hour to write while the
    newest is still missing used to move the mark past the gap, and that hour was never written again:
    before 2026.9.5 the recorder compiled the same entities, so it arrived through the entity's own
    state and the hole never showed, but the archive is the only writer now.
    """
    from custom_components.helios_forecast.openmeteo import WeatherSeries
    from custom_components.helios_forecast.statistics import external_statistic_id

    entry = _entry(hass)
    coordinator = HeliosForecastCoordinator(hass, entry)
    written: list = []
    monkeypatch.setattr(
        coordinator_mod,
        "async_add_external_statistics",
        lambda _hass, meta, rows: written.append((meta["statistic_id"], [r["start"] for r in rows])),
    )

    top = dt_util.utcnow().replace(minute=0, second=0, microsecond=0)

    def window(hours: list, values: list) -> WeatherSeries:
        return WeatherSeries(
            times=hours,
            cloud=list(values),
            shortwave=list(values),
            direct=list(values),
            diffuse=list(values),
            temp=list(values),
            wind=list(values),
            snow=list(values),
            cloud_spread=[0.0] * len(hours),
        )

    # Two hours back is published, one hour back is not yet.
    two_back, one_back = top - timedelta(hours=2), top - timedelta(hours=1)
    coordinator.weather_series = window([two_back, one_back], [10.0, float("nan")])
    coordinator.write_weather_statistics(top)
    first = {h for _sid, hours in written for h in hours}
    assert two_back in first and one_back not in first

    # The next refresh, an hour later: the missing hour has arrived. It must be written now.
    written.clear()
    later = top + timedelta(hours=1)
    coordinator.weather_series = window([two_back, one_back, top], [10.0, 20.0, 30.0])
    coordinator.write_weather_statistics(later)
    second = {h for _sid, hours in written for h in hours}
    assert one_back in second, "the late hour was left behind by the mark"
    assert top in second
    # Every field caught up, not just the first one.
    ids = {sid for sid, _hours in written}
    assert ids == {external_statistic_id(entry.entry_id, f.key) for f in _WEATHER_FIELDS}


async def test_the_archive_mark_never_moves_backwards(hass, monkeypatch) -> None:
    # The mark is derived from the current hour, so a clock that jumps back, or a refresh running late
    # behind another, must not make the archive re-offer a window it has already left.
    entry = _entry(hass)
    coordinator = HeliosForecastCoordinator(hass, entry)
    monkeypatch.setattr(coordinator_mod, "async_add_external_statistics", lambda *_a: None)
    coordinator.weather_series = make_weather_series(dt_util.utcnow())

    now_utc = dt_util.utcnow()
    coordinator.write_weather_statistics(now_utc)
    ahead = coordinator._last_weather_stat_hour
    coordinator.write_weather_statistics(now_utc - timedelta(hours=3))
    assert coordinator._last_weather_stat_hour == ahead
