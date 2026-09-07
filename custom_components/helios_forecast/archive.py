"""The integration's own long-term statistics: the metadata they carry, and the one-time move of
the series that used to be written under an entity id.

Home Assistant knows two kinds of statistics. A series named after an entity belongs to the
recorder, which compiles it from that entity's state every hour on its own. A series named
``<domain>:<object_id>`` belongs to the integration that declares it, and the recorder never
touches it. Everything this integration archives is of the second kind, because it is written from
here: the past weather hours Open-Meteo only serves for 60 days, and the predicted-production curve
the card draws behind today.

Until 2026.9.5 those series were written under the entity ids of the weather sensors and of two
archive sensors, which put the recorder and this integration on the same unique index. A collision
there does not fail our write alone: it rolls back the recorder's entire hourly compile, so every
other integration on the machine silently loses that hour of history. `async_migrate` moves the
history off those entity ids once and for all, and never deletes anything it has not first checked
it could read back somewhere else.

It runs once Home Assistant has finished starting, never during setup. The recorder waits for that
same event before it processes its queue, so anything here that waits on a recorder write while the
start is still in progress waits on a start that is waiting on it.
"""

from __future__ import annotations

import asyncio
import logging
import time
from functools import partial
from typing import Any, Dict, List, Optional

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.models import StatisticMetaData
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    get_metadata,
    statistics_during_period,
)

try:
    from homeassistant.components.recorder.statistics import UNIT_CLASS_TO_UNIT_CONVERTER
except ImportError:  # pragma: no cover - older HA cores name the map differently
    UNIT_CLASS_TO_UNIT_CONVERTER = {}
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .statistics import ARCHIVED_SERIES, FORECAST_ENERGY_KEY, FORECAST_POWER_KEY, external_statistic_id

_LOGGER = logging.getLogger(__name__)

# The source Home Assistant stamps on a series it compiles itself. Only those are ours to move.
RECORDER_SOURCE = "recorder"

# The two entities dropped in 2026.9.5. They existed only to anchor an entity-bound statistic, which
# is exactly what no longer exists; their live value duplicated `power_now` and `energy_this_hour`.
# Their registry entries go once their history is safe, so no dead entity is left behind.
RETIRED_KEYS: tuple[str, ...] = (FORECAST_POWER_KEY, FORECAST_ENERGY_KEY)

# Every archived series is a mean over its hour (mean = min = max, one sample per hour); none is a
# meter, so no sum is ever read or written.
_TYPES = {"mean", "min", "max"}

# Rows are handed to the recorder in batches rather than as one transaction of tens of thousands.
_BATCH = 2000

# How long to wait for the recorder to have committed the copy, and how often to look. Generous:
# tens of thousands of hours on a small machine take minutes. Reaching the deadline deletes nothing,
# it just leaves the old series in place for the next start to retry, so it can never cost history.
_COMMIT_TIMEOUT = 900.0
_POLL = 1.0

# Compat: `mean_type` and `unit_class` are mandatory in newer StatisticMetaData and absent from older
# cores, so they are imported defensively.
try:
    from homeassistant.components.recorder.models import StatisticMeanType

    _MEAN_TYPE_ARITHMETIC = StatisticMeanType.ARITHMETIC
except ImportError:  # pragma: no cover - older HA cores
    _MEAN_TYPE_ARITHMETIC = None


# `unit_class` names the unit-conversion class HA uses to migrate a statistic's history if its unit
# later changes. We derive the unit -> class map from the core's own converters so the value always
# matches the installed core. Units with no converter (e.g. W/m2 irradiance) map to None, the correct
# "not convertible" answer. The key is always declared in the metadata; cores predating it ignore it.
def _build_unit_classes() -> Dict[str, Optional[str]]:
    mapping: Dict[str, Optional[str]] = {}
    try:
        from homeassistant.util import unit_conversion as _uc
    except ImportError:  # pragma: no cover - older HA cores
        return mapping
    for name in (
        "PowerConverter",
        "EnergyConverter",
        "TemperatureConverter",
        "SpeedConverter",
        "DistanceConverter",
        "UnitlessRatioConverter",
    ):
        converter = getattr(_uc, name, None)
        unit_class = getattr(converter, "UNIT_CLASS", None)
        if converter is None or unit_class is None:
            continue
        for unit in getattr(converter, "VALID_UNITS", ()):  # e.g. "W" -> "power"
            mapping.setdefault(unit, unit_class)
    return mapping


UNIT_CLASSES: Dict[str, Optional[str]] = _build_unit_classes()


def metadata(statistic_id: str, unit: str, name: str) -> StatisticMetaData:
    """Metadata for one integration-owned series.

    mean_type and unit_class are declared statically (not added after the literal) so both the
    runtime and static API scanners see them. has_mean stays for cores that predate mean_type, where
    the extra key is ignored. The name is what the interface shows: these series have no entity to
    borrow a name from.
    """
    return {
        "has_mean": True,
        "mean_type": _MEAN_TYPE_ARITHMETIC,
        "has_sum": False,
        "name": name,
        "source": DOMAIN,
        "statistic_id": statistic_id,
        "unit_of_measurement": unit,
        "unit_class": UNIT_CLASSES.get(unit),
    }


async def async_migrate(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Move any history still held under an entity id onto this integration's own series.

    Armed on every setup rather than once behind a marker, deliberately: an installation that skips a
    version, or that is restored from a backup taken before the move, still gets the fix on its next
    start. Once there is nothing left under an entity id it costs one metadata read, so it can stay
    armed for good. Called from the started event and off the setup path (see the module docstring);
    a caller must not wait on it.
    """
    registry = er.async_get(hass)
    legacy = {
        key: entity_id
        for key, _unit, _name in ARCHIVED_SERIES
        if (entity_id := registry.async_get_entity_id("sensor", DOMAIN, f"{entry.entry_id}_{key}"))
    }
    stuck: set[str] = set()
    hours = 0
    started = time.monotonic()
    if legacy:
        instance = get_instance(hass)
        known = await instance.async_add_executor_job(partial(get_metadata, hass, statistic_ids=set(legacy.values())))
        for key, unit, name in ARCHIVED_SERIES:
            entity_id = legacy.get(key)
            found = known.get(entity_id) if entity_id is not None else None
            # Only a series the recorder owns is ours to move. Anything else under that id was not
            # written by us and is left strictly alone.
            if entity_id is None or found is None or found[1].get("source") != RECORDER_SOURCE:
                continue
            try:
                moved = await _move(hass, entity_id, external_statistic_id(entry.entry_id, key), unit, name, found[1])
            except Exception:  # noqa: BLE001 - a repair must never take the integration down with it
                _LOGGER.exception("Could not move the statistics of %s, they are left untouched", entity_id)
                moved = None
            if moved is None:
                stuck.add(key)
            else:
                hours += moved

    # A retired entity's registry entry goes only once its history is safe elsewhere, so a failed
    # move can be retried on the next start with the entity id it needs still resolvable.
    for key in RETIRED_KEYS:
        entity_id = legacy.get(key)
        if entity_id is not None and key not in stuck:
            registry.async_remove(entity_id)

    # Logged once, at the end: this runs on every start, and how long the one that actually moved
    # something took is the only thing worth knowing about it afterwards.
    if hours:
        _LOGGER.info(
            "Moved %d archived hours onto this integration's own statistics in %.1f s",
            hours,
            time.monotonic() - started,
        )


async def _move(
    hass: HomeAssistant,
    legacy_id: str,
    statistic_id: str,
    unit: str,
    name: str,
    legacy: StatisticMetaData,
) -> Optional[int]:
    """Copy every archived hour of `legacy_id` onto `statistic_id`, then drop the old series.

    Returns how many hours were moved, or None when the old series had to be kept. The old series is
    deleted only once every hour that was read from it has been read back at the destination, so an
    interrupted move leaves the history in place twice rather than not at all. Re-running is safe: the
    recorder updates an hour it already has instead of adding a second one.
    """
    instance = get_instance(hass)
    units = _conversion(legacy, unit)
    if units is None:
        _LOGGER.error(
            "Kept the statistics of %s: they are stored in %s, which cannot be converted to the %s "
            "this integration archives, so moving them would relabel the values rather than convert "
            "them. Nothing was deleted",
            legacy_id,
            legacy.get("unit_of_measurement"),
            unit,
        )
        return None

    rows = await _read(hass, legacy_id, units)
    if not rows:
        instance.async_clear_statistics([legacy_id])
        return 0

    meta = metadata(statistic_id, unit, name)
    for offset in range(0, len(rows), _BATCH):
        async_add_external_statistics(hass, meta, [_row(r) for r in rows[offset : offset + _BATCH]])

    # The hours themselves, not how many there are. The destination is normally already populated by
    # the coordinator's own backfill before this runs, so a count would be satisfied by rows this move
    # never wrote, and the series it then deleted would be the only copy of its oldest hours.
    wanted = {row["start"] for row in rows}
    missing = await _wait_for(hass, statistic_id, wanted)
    if missing:
        _LOGGER.error(
            "Kept the statistics of %s: %d of its %d hours are still not readable under %s after "
            "%.0f s, so nothing was deleted. The move will be retried on the next start",
            legacy_id,
            len(missing),
            len(rows),
            statistic_id,
            _COMMIT_TIMEOUT,
        )
        return None

    instance.async_clear_statistics([legacy_id])
    _LOGGER.info("Moved %d hours of statistics from %s to %s", len(rows), legacy_id, statistic_id)
    return len(rows)


def _conversion(legacy: StatisticMetaData, unit: str) -> Optional[Dict[str, str]]:
    """How to read a legacy series so its values arrive in `unit`, or None when they cannot.

    The recorder stores an entity-bound statistic in the unit the entity displayed, so an
    installation on the US customary system holds its temperature in Fahrenheit, its wind in miles per
    hour and its snow depth in feet, and a user may also have changed a series' unit by hand. Copying
    those values under this integration's own unit would relabel 68 degrees Fahrenheit as 68 degrees
    Celsius and then delete the only copy, so a series whose unit cannot be converted is left where it
    is instead. An empty mapping means no conversion is possible at all, which for these quantities
    also means none can happen.
    """
    stored = legacy.get("unit_of_measurement")
    raw_class = legacy.get("unit_class") or UNIT_CLASSES.get(unit)
    unit_class = str(raw_class) if raw_class else None
    converter = UNIT_CLASS_TO_UNIT_CONVERTER.get(unit_class) if unit_class and UNIT_CLASS_TO_UNIT_CONVERTER else None
    if converter is None or unit not in converter.VALID_UNITS:
        # Nothing can convert this quantity, so the recorder hands the values back exactly as stored
        # and they are only usable if the unit already matches.
        return {} if stored == unit else None
    if stored is not None and stored not in converter.VALID_UNITS:
        return None
    # Always asked for by name, even when the stored unit already matches. Left unasked, the recorder
    # converts to whatever the live entity is displaying at that moment (statistics.py, where
    # display_unit falls back to state_unit), which is not necessarily the unit the series is stored
    # in: an installation that switched Home Assistant to another unit system after these statistics
    # were written would have had its values converted on read and then labelled with ours.
    return {unit_class: unit}


async def _wait_for(hass: HomeAssistant, statistic_id: str, wanted: set) -> set:
    """Wait until `statistic_id` holds every hour in `wanted`, and return those still missing.

    The copy is committed by the recorder on its own thread, and asking that thread when it is done
    has a hole in it: it answers "queue empty" from the moment it takes the import off the queue,
    which is before the rows are written. So the wait is the read-back itself, which is in any case
    the only thing that has to be true before the old series can go.
    """
    deadline = time.monotonic() + _COMMIT_TIMEOUT
    while True:
        missing = wanted - {row["start"] for row in await _read(hass, statistic_id)}
        if not missing or time.monotonic() >= deadline:
            return missing
        await asyncio.sleep(_POLL)


async def _read(hass: HomeAssistant, statistic_id: str, units: Optional[Dict[str, str]] = None) -> List[Dict[str, Any]]:
    """Every hour ever archived under `statistic_id`, oldest first.

    `units` asks the recorder to convert as it reads, which is how a legacy series stored in another
    unit reaches the archive as a value and not as a relabelling. Empty or None reads it as stored.
    """
    result = await get_instance(hass).async_add_executor_job(
        statistics_during_period,
        hass,
        dt_util.utc_from_timestamp(0),
        None,
        {statistic_id},
        "hour",
        units or None,
        _TYPES,
    )
    return result.get(statistic_id, [])


def _row(row: Dict[str, Any]) -> Dict[str, Any]:
    """One row as read back turned into one row to write: the hour, and whichever of the three
    aggregates it carries."""
    out: Dict[str, Any] = {"start": dt_util.utc_from_timestamp(row["start"])}
    for field in ("mean", "min", "max"):
        value = row.get(field)
        if value is not None:
            out[field] = value
    return out
