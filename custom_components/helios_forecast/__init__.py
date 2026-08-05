"""The Helios Solar Forecast integration.

Computes a PV production forecast server-side from Open-Meteo irradiance and the
installation geometry, and publishes it three ways: a first-class entity set for
automations, the Energy dashboard's solar-forecast provider, and a websocket
detail series for the Helios card. The learned correction lands in a later phase.

Home Assistant imports stay inside the setup / unload functions so importing this
package needs no running Home Assistant: the pure forecast model under it can be
imported and unit-tested on its own.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from .const import DOMAIN

if TYPE_CHECKING:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.core import HomeAssistant


_LEGACY_MULTI_ARRAY = "legacy_multi_array"


def _legacy_issue_id(entry: ConfigEntry) -> str:
    return f"{_LEGACY_MULTI_ARRAY}_{entry.entry_id}"


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Helios Solar Forecast from a config entry."""
    from homeassistant.const import Platform
    from homeassistant.helpers import issue_registry as ir

    from . import websocket
    from .coordinator import HeliosForecastCoordinator

    # Several panel lines in one entry are supported again (they share one production
    # sensor and one inverter cap; the model sums them by kWp share). Clear any repair
    # issue an older version raised against a multi-line entry.
    ir.async_delete_issue(hass, DOMAIN, _legacy_issue_id(entry))

    coordinator = HeliosForecastCoordinator(hass, entry)
    await coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, [Platform.SENSOR])

    # The sensor entities now exist, so the weather archive's first backfill can land. It walks a
    # 60-day window and is not needed for the live forecast, so run it off the setup path as a
    # background task: a fresh install finishes setting up promptly instead of waiting on the
    # trailing statistics build (#31).
    from homeassistant.util import dt as dt_util

    async def _initial_statistics_archive() -> None:
        # full=True: the one-time 60-day backfill (install + self-heal after downtime); refreshes then
        # import only the new hours.
        coordinator.write_weather_statistics(dt_util.utcnow(), full=True)
        coordinator.write_forecast_statistics()
        _purge_orphan_forecast_stats(hass, entry)

    entry.async_create_background_task(hass, _initial_statistics_archive(), "helios_forecast_initial_statistics")

    entry.async_on_unload(entry.add_update_listener(_async_reload_entry))
    websocket.async_register(hass)
    return True


def _purge_orphan_forecast_stats(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Clear long-term statistics left on the live forecast energy sensors.

    Earlier versions gave these sensors a state_class, so HA recorded statistics for them. They
    are point-in-time forecast values, not meters, and now carry no state_class, which makes HA
    flag "entity no longer has a state class" on every statistics cycle. We clear those orphan
    stats so testers do not have to do it by hand. predicted_energy is excluded: it is the archive
    entity whose statistics are kept on purpose (it carries a valid state_class again). Idempotent:
    the live sensors never regain a state_class, so this is a no-op once their stats are gone.
    """
    from homeassistant.components.recorder import get_instance
    from homeassistant.helpers import entity_registry as er

    live_energy_keys = [
        "energy_today_remaining",
        "energy_this_hour",
        "energy_next_hour",
        *(f"energy_day_{n}" for n in range(1, 8)),
    ]
    registry = er.async_get(hass)
    stat_ids = [
        eid
        for key in live_energy_keys
        if (eid := registry.async_get_entity_id("sensor", DOMAIN, f"{entry.entry_id}_{key}"))
    ]
    if stat_ids:
        get_instance(hass).async_clear_statistics(stat_ids)


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    from homeassistant.const import Platform

    unloaded = await hass.config_entries.async_unload_platforms(entry, [Platform.SENSOR])
    if unloaded:
        hass.data[DOMAIN].pop(entry.entry_id)
    return unloaded


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Clear any legacy repair issue when the entry is deleted."""
    from homeassistant.helpers import issue_registry as ir

    ir.async_delete_issue(hass, DOMAIN, _legacy_issue_id(entry))


async def _async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload when options change so the new layout / cap takes effect."""
    await hass.config_entries.async_reload(entry.entry_id)
