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
    async_add_external_statistics,
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


def test_every_metadata_key_is_a_column_the_recorder_can_store() -> None:
    """The recorder builds its row with StatisticsMeta(**metadata).

    An unmapped key raises there, on the recorder thread, where the per-task guard swallows it into a
    logged traceback: no series is created and the whole archive writes nothing while looking healthy.
    That is what a key newer than the declared minimum Home Assistant does, so the set is pinned here.
    """
    from homeassistant.components.recorder.db_schema import StatisticsMeta

    meta = archive.metadata("helios_forecast:entry_temperature", "\u00b0C", "Temperature")
    columns = {column.name for column in StatisticsMeta.__table__.columns}
    assert set(meta) <= columns


async def _write_owned(hass, statistic_id: str, unit: str, start: datetime, count: int) -> None:
    """Rows already at the destination when the move runs: the coordinator's own backfill writes the
    trailing window to these ids from _initial_statistics_archive, before the started event fires."""
    async_add_external_statistics(
        hass,
        archive.metadata(statistic_id, unit, "Cloud cover"),
        [{"start": start + timedelta(hours=i), "mean": 50.0, "min": 50.0, "max": 50.0} for i in range(count)],
    )
    await async_wait_recording_done(hass)


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

    async def _nothing(hass_, statistic_id, units=None):
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

    async def _slow_to_appear(hass_, statistic_id, units=None, **window):
        if ":" in statistic_id:
            late["calls"] += 1
            # The read-back looks only at the hours it copied, never at the whole series.
            assert window["start"] is not None and window["end"] is not None
            if late["calls"] < 3:  # the copy is not visible yet on the first two looks
                return []
        return await real_read(hass_, statistic_id, units, **window)

    monkeypatch.setattr(archive, "_read", _slow_to_appear)
    monkeypatch.setattr(archive, "_POLL", 0.01)
    await archive.async_migrate(hass, entry)
    await async_wait_recording_done(hass)

    assert late["calls"] >= 3
    assert len(await _read(hass, external_statistic_id(entry.entry_id, "direct"))) == _HOURS
    assert await _read(hass, entity_id) == []


async def test_an_empty_legacy_series_is_the_one_dropped(hass, monkeypatch) -> None:
    """A metadata row with no hours behind it is cleared without a copy: the only branch of the move
    that deletes anything it has not first read back. It has to clear the old id and nothing else."""
    entry = _entry(hass)
    entity_id = _register(hass, entry, "direct", "helios_direct_irradiance")
    await _write_legacy(hass, entity_id, "W/m\u00b2", datetime(2026, 8, 1, tzinfo=_UTC))

    real_read = archive._read

    async def _reads_empty(hass_, statistic_id, units=None, **window):
        return [] if statistic_id == entity_id else await real_read(hass_, statistic_id, units, **window)

    cleared: list = []
    monkeypatch.setattr(archive, "_read", _reads_empty)
    monkeypatch.setattr(
        type(archive.get_instance(hass)), "async_clear_statistics", lambda _self, ids: cleared.extend(ids)
    )

    await archive.async_migrate(hass, entry)

    assert cleared == [entity_id]


async def test_every_archived_series_has_a_valid_statistic_id(hass) -> None:
    from homeassistant.components.recorder.statistics import valid_statistic_id

    for key, _unit, _name in ARCHIVED_SERIES:
        assert valid_statistic_id(external_statistic_id(_ENTRY_ID, key))


# --- the two states the first version of these tests could not see ---------------------------


async def test_a_legacy_series_in_fahrenheit_arrives_in_celsius(hass) -> None:
    # The recorder stores an entity-bound statistic in the unit the entity displayed, so an
    # installation on the US customary system holds this series in Fahrenheit. Copying the numbers
    # under this integration's own unit would turn 20 degrees into 68 and then delete the only copy.
    entry = _entry(hass)
    entity_id = _register(hass, entry, "temperature", "helios_temperature")
    await _write_legacy(hass, entity_id, "°F", datetime(2026, 8, 1, tzinfo=_UTC), 24)

    await archive.async_migrate(hass, entry)
    await async_wait_recording_done(hass)

    statistic_id = external_statistic_id(entry.entry_id, "temperature")
    known = await hass.async_add_executor_job(get_metadata, hass)
    assert known[statistic_id][1]["unit_of_measurement"] == "°C"
    moved = await _read(hass, statistic_id)
    assert len(moved) == 24
    # _rows writes i as the value, so hour 0 is 0 F, which is -17.8 C.
    assert moved[0]["mean"] == pytest.approx(-17.7778, abs=0.01)
    assert await _read(hass, entity_id) == []


async def test_a_series_in_a_unit_that_cannot_be_converted_is_left_alone(hass) -> None:
    entry = _entry(hass)
    entity_id = _register(hass, entry, "cloud_cover", "helios_cloud_cover")
    await _write_legacy(hass, entity_id, "kWh", datetime(2026, 8, 1, tzinfo=_UTC))

    await archive.async_migrate(hass, entry)
    await async_wait_recording_done(hass)

    # Nothing was relabelled, and nothing was deleted: the history stays where it is, readable.
    assert len(await _read(hass, entity_id)) == _HOURS
    assert await _read(hass, external_statistic_id(entry.entry_id, "cloud_cover")) == []


async def test_the_old_series_survives_when_the_copy_does_not_land(hass, monkeypatch) -> None:
    # The destination is already populated by the coordinator's own backfill, and holds MORE rows
    # than the legacy series does. A guard counting rows would be satisfied by hours this move never
    # wrote, and would then delete the only copy of the oldest ones.
    entry = _entry(hass)
    entity_id = _register(hass, entry, "cloud_cover", "helios_cloud_cover")
    statistic_id = external_statistic_id(entry.entry_id, "cloud_cover")
    await _write_legacy(hass, entity_id, "%", datetime(2025, 6, 1, tzinfo=_UTC), 10)
    await _write_owned(hass, statistic_id, "%", datetime(2026, 8, 1, tzinfo=_UTC), 100)

    monkeypatch.setattr(archive, "async_add_external_statistics", lambda *a, **k: None)
    monkeypatch.setattr(archive, "_COMMIT_TIMEOUT", 0.0)
    await archive.async_migrate(hass, entry)
    await async_wait_recording_done(hass)

    assert len(await _read(hass, entity_id)) == 10, "the only copy of the 2025 hours was deleted"


async def test_a_populated_destination_does_not_stop_a_real_move(hass) -> None:
    # The same shape, with the copy allowed through: the old series goes, and both sets of hours are
    # readable at the destination.
    entry = _entry(hass)
    entity_id = _register(hass, entry, "cloud_cover", "helios_cloud_cover")
    statistic_id = external_statistic_id(entry.entry_id, "cloud_cover")
    await _write_legacy(hass, entity_id, "%", datetime(2025, 6, 1, tzinfo=_UTC), 10)
    await _write_owned(hass, statistic_id, "%", datetime(2026, 8, 1, tzinfo=_UTC), 100)

    await archive.async_migrate(hass, entry)
    await async_wait_recording_done(hass)

    assert await _read(hass, entity_id) == []
    assert len(await _read(hass, statistic_id)) == 110


async def test_the_unit_asked_for_is_the_one_stored_not_the_one_on_display(hass) -> None:
    # The recorder converts a statistic to the unit the LIVE entity displays unless it is asked for
    # one by name. An installation that switched Home Assistant to another unit system after these
    # statistics were written holds them in one unit and shows another: read without asking, the
    # values would arrive converted and then be labelled with the archive's own unit.
    entry = _entry(hass)
    entity_id = _register(hass, entry, "temperature", "helios_temperature")
    await _write_legacy(hass, entity_id, "°C", datetime(2026, 8, 1, tzinfo=_UTC), 24)
    hass.states.async_set(entity_id, "20", {"unit_of_measurement": "°F", "device_class": "temperature"})

    await archive.async_migrate(hass, entry)
    await async_wait_recording_done(hass)

    statistic_id = external_statistic_id(entry.entry_id, "temperature")
    known = await hass.async_add_executor_job(get_metadata, hass)
    assert known[statistic_id][1]["unit_of_measurement"] == "°C"
    moved = await _read(hass, statistic_id)
    # _rows writes i as the value, so hour 0 is 0 and hour 10 is 10, in the stored Celsius.
    assert [row["mean"] for row in moved[:3]] == [0.0, 1.0, 2.0]
