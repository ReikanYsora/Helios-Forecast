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


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Helios Solar Forecast from a config entry."""
    from homeassistant.const import Platform

    from . import websocket
    from .coordinator import HeliosForecastCoordinator

    coordinator = HeliosForecastCoordinator(hass, entry)
    await coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, [Platform.SENSOR])

    # The sensor entities now exist, so the weather archive's first backfill can
    # land immediately rather than waiting for the next 30-minute refresh.
    from homeassistant.util import dt as dt_util

    coordinator.write_weather_statistics(dt_util.utcnow())
    coordinator.write_forecast_statistics()

    _purge_orphan_forecast_stats(hass, entry)

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


async def _async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload when options change so the new layout / cap takes effect."""
    await hass.config_entries.async_reload(entry.entry_id)
