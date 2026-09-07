"""Tests for the move of the archived statistics off the entity ids they used to be written under.

The recorder here is the real one (`recorder_mock`), so these exercise the actual statistics tables:
an entity-bound series is written the old way, the migration runs, and the assertions are about what
the database holds afterwards. The point of the whole change is that nothing is lost, so that is what
is asserted first.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from homeassistant.components.recorder.statistics import (
    async_import_statistics,
    get_metadata,
    statistics_during_period,
)
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.components.recorder.common import async_wait_recording_done

from custom_components.helios_forecast import archive
from custom_components.helios_forecast.const import DOMAIN
from custom_components.helios_forecast.statistics import ARCHIVED_SERIES, external_statistic_id

pytestmark = pytest.mark.usefixtures("recorder_mock")

_UTC = timezone.utc
_ENTRY_ID = "01kz0s5bnaqr1h29cyzxvdc9ee"
_HOURS = 48


def _entry(hass) -> MockConfigEntry:
    entry = MockConfigEntry(domain=DOMAIN, data={}, entry_id=_ENTRY_ID)
    entry.add_to_hass(hass)
    return entry


def _register(hass, entry, key: str, object_id: str) -> str:
    """Register the sensor the old build owned for `key` and return its entity id."""
    registry = er.async_get(hass)
    return registry.async_get_or_create(
        "sensor", DOMAIN, f"{entry.entry_id}_{key}", config_entry=entry, suggested_object_id=object_id
    ).entity_id


def _rows(start: datetime, count: int = _HOURS) -> list[dict]:
    return [
        {"start": start + timedelta(hours=i), "mean": float(i), "min": float(i), "max": float(i)} for i in range(count)
    ]


async def _read(hass, statistic_id: str) -> list[dict]:
    result = await hass.async_add_executor_job(
        statistics_during_period,
        hass,
        dt_util.utc_from_timestamp(0),
        None,
        {statistic_id},
        "hour",
        None,
        {"mean", "min", "max"},
    )
    return result.get(statistic_id, [])


async def _write_legacy(hass, entity_id: str, unit: str, start: datetime, count: int = _HOURS) -> None:
    """Write a series the way every build before 2026.9.5 did: bound to the entity id."""
    async_import_statistics(
        hass,
        {
            "has_mean": True,
            "mean_type": archive._MEAN_TYPE_ARITHMETIC,
            "has_sum": False,
            "name": None,
            "source": "recorder",
            "statistic_id": entity_id,
            "unit_of_measurement": unit,
            "unit_class": archive.UNIT_CLASSES.get(unit),
        },
        _rows(start, count),
    )
    await async_wait_recording_done(hass)


async def test_migration_moves_every_hour_and_drops_the_old_series(hass) -> None:
    entry = _entry(hass)
    start = datetime(2026, 8, 1, tzinfo=_UTC)
    entity_id = _register(hass, entry, "cloud_cover", "helios_cloud_cover")
    await _write_legacy(hass, entity_id, "%", start)
    before = await _read(hass, entity_id)
    assert len(before) == _HOURS

    await archive.async_migrate(hass, entry)
    await async_wait_recording_done(hass)

    moved = await _read(hass, external_statistic_id(entry.entry_id, "cloud_cover"))
    assert [(r["start"], r["mean"], r["min"], r["max"]) for r in moved] == [
        (r["start"], r["mean"], r["min"], r["max"]) for r in before
    ]
    assert await _read(hass, entity_id) == []


async def test_migrated_series_belongs_to_this_integration(hass) -> None:
    entry = _entry(hass)
    entity_id = _register(hass, entry, "ghi", "helios_ghi")
    await _write_legacy(hass, entity_id, "W/m²", datetime(2026, 8, 1, tzinfo=_UTC))

    await archive.async_migrate(hass, entry)
    await async_wait_recording_done(hass)

    statistic_id = external_statistic_id(entry.entry_id, "ghi")
    known = await hass.async_add_executor_job(get_metadata, hass)
    # Source and id must agree: an id with a colon left marked `recorder` is what breaks Home
    # Assistant's own statistics validation, which is why the history is copied rather than renamed.
    assert known[statistic_id][1]["source"] == DOMAIN
    assert known[statistic_id][1]["name"] == "Global irradiance"
    assert entity_id not in known


async def test_migration_retires_the_two_removed_entities(hass) -> None:
    entry = _entry(hass)
    power = _register(hass, entry, "predicted_power", "helios_predicted_power")
    energy = _register(hass, entry, "predicted_energy", "helios_predicted_energy")
    weather = _register(hass, entry, "temperature", "helios_temperature")
    await _write_legacy(hass, power, "W", datetime(2026, 8, 1, tzinfo=_UTC))

    await archive.async_migrate(hass, entry)
    await async_wait_recording_done(hass)

    registry = er.async_get(hass)
    assert registry.async_get(power) is None
    assert registry.async_get(energy) is None
    # Only the two retired keys go: the weather sensors stay, they just no longer anchor a statistic.
    assert registry.async_get(weather) is not None
    assert len(await _read(hass, external_statistic_id(entry.entry_id, "predicted_power"))) == _HOURS


async def test_migration_is_idempotent_and_costs_nothing_once_done(hass) -> None:
    entry = _entry(hass)
    entity_id = _register(hass, entry, "wind_speed", "helios_wind_speed")
    await _write_legacy(hass, entity_id, "km/h", datetime(2026, 8, 1, tzinfo=_UTC))

    for _ in range(3):
        await archive.async_migrate(hass, entry)
        await async_wait_recording_done(hass)

    # Re-running must not duplicate an hour: the recorder updates an hour it already holds.
    assert len(await _read(hass, external_statistic_id(entry.entry_id, "wind_speed"))) == _HOURS


async def test_migration_is_a_no_op_on_a_fresh_install(hass) -> None:
    entry = _entry(hass)
    await archive.async_migrate(hass, entry)
    await async_wait_recording_done(hass)

    known = await hass.async_add_executor_job(get_metadata, hass)
    assert known == {}


async def test_migration_leaves_a_series_it_does_not_own_alone(hass) -> None:
    """A statistic under one of our entity ids that the recorder did not compile is not ours to move."""
    entry = _entry(hass)
    entity_id = _register(hass, entry, "snow_depth", "helios_snow_depth")
    known_before = await hass.async_add_executor_job(get_metadata, hass)
    assert entity_id not in known_before

    await archive.async_migrate(hass, entry)
    await async_wait_recording_done(hass)

    assert await _read(hass, external_statistic_id(entry.entry_id, "snow_depth")) == []


async def test_a_failed_move_keeps_the_old_series_and_the_entity(hass, monkeypatch) -> None:
    """If the copy cannot be read back, nothing is deleted: the history stays where it is."""
    entry = _entry(hass)
    entity_id = _register(hass, entry, "predicted_energy", "helios_predicted_energy")
    await _write_legacy(hass, entity_id, "kWh", datetime(2026, 8, 1, tzinfo=_UTC))

    async def _nothing(hass_, statistic_id):
        return [] if ":" in statistic_id else await _read(hass_, statistic_id)

    monkeypatch.setattr(archive, "_read", _nothing)
    monkeypatch.setattr(archive, "_COMMIT_TIMEOUT", 0.0)
    await archive.async_migrate(hass, entry)
    await async_wait_recording_done(hass)

    assert len(await _read(hass, entity_id)) == _HOURS
    assert er.async_get(hass).async_get(entity_id) is not None


async def test_the_move_waits_for_the_copy_instead_of_trusting_the_queue(hass, monkeypatch) -> None:
    """The recorder answers "queue empty" from the moment it takes the import off the queue, before
    the rows are written, so the read-back has to be retried rather than taken once."""
    entry = _entry(hass)
    entity_id = _register(hass, entry, "direct", "helios_direct_irradiance")
    await _write_legacy(hass, entity_id, "W/m²", datetime(2026, 8, 1, tzinfo=_UTC))

    real_read = archive._read
    late = {"calls": 0}

    async def _slow_to_appear(hass_, statistic_id):
        if ":" in statistic_id:
            late["calls"] += 1
            if late["calls"] < 3:  # the copy is not visible yet on the first two looks
                return []
        return await real_read(hass_, statistic_id)

    monkeypatch.setattr(archive, "_read", _slow_to_appear)
    monkeypatch.setattr(archive, "_POLL", 0.01)
    await archive.async_migrate(hass, entry)
    await async_wait_recording_done(hass)

    assert late["calls"] >= 3
    assert len(await _read(hass, external_statistic_id(entry.entry_id, "direct"))) == _HOURS
    assert await _read(hass, entity_id) == []


async def test_every_archived_series_has_a_valid_statistic_id(hass) -> None:
    from homeassistant.components.recorder.statistics import valid_statistic_id

    for key, _unit, _name in ARCHIVED_SERIES:
        assert valid_statistic_id(external_statistic_id(_ENTRY_ID, key))
