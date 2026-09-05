"""The Helios Solar Forecast integration.

Computes a PV production forecast server-side from Open-Meteo irradiance and the
installation geometry, and publishes it as a first-class entity set, the Energy
dashboard's solar-forecast provider, response services for automations, and a
websocket detail series for the Helios card. A learned residual, built from the recorder's
own production history, corrects the model against the site's real output.

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

    from . import services, websocket
    from .config import CONF_BATTERY_SOC_ENTITY
    from .coordinator import HeliosForecastCoordinator

    # Clear the legacy multi-line repair issue if one is still registered against this entry.
    ir.async_delete_issue(hass, DOMAIN, _legacy_issue_id(entry))

    coordinator = HeliosForecastCoordinator(hass, entry)

    # Refresh right after each hour boundary: the archive only rebuilds inside a refresh (once an
    # hour, see coordinator._last_archive_hour), so without this nudge the just-elapsed hour stays
    # served from the unclamped live series for up to 30 minutes.
    from homeassistant.core import callback
    from homeassistant.helpers.event import async_track_time_change

    @callback
    def _hour_rolled_over(_now) -> None:
        hass.async_create_task(coordinator.async_request_refresh())

    entry.async_on_unload(async_track_time_change(hass, _hour_rolled_over, minute=0, second=5))

    # Re-project the battery SoC the moment its source entity comes back from unavailable/unknown
    # rather than waiting for the 30-minute tick: battery integrations are often briefly unavailable
    # at startup. Armed before the first refresh: that refresh routinely runs before the entity
    # exists, and its arrival right afterwards is the transition this listener catches.
    soc_entity = {**entry.data, **entry.options}.get(CONF_BATTERY_SOC_ENTITY)
    if soc_entity:
        from homeassistant.core import callback
        from homeassistant.helpers.event import async_track_state_change_event

        _UNAVAILABLE = ("unavailable", "unknown")

        @callback
        def _soc_recovered(event) -> None:
            new_state = event.data.get("new_state")
            if new_state is None or new_state.state in _UNAVAILABLE:
                return
            # Only on the transition TO available (first appearance or recovery), so ordinary SoC %
            # changes don't force off-cycle refreshes; the 30-minute cadence handles those.
            old_state = event.data.get("old_state")
            if old_state is None or old_state.state in _UNAVAILABLE:
                hass.async_create_task(coordinator.async_request_refresh())

        entry.async_on_unload(async_track_state_change_event(hass, [soc_entity], _soc_recovered))

    await coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, [Platform.SENSOR])

    # The sensor entities now exist, so the weather archive's first backfill can land. It walks a
    # 60-day window and is not needed for the live forecast, so run it off the setup path as a
    # background task: a fresh install finishes setting up promptly instead of waiting on the
    # trailing statistics build.
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
    services.async_register_services(hass)
    return True


def _purge_orphan_forecast_stats(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Clear long-term statistics left on the live forecast energy sensors.

    These sensors are point-in-time forecast values, not meters, and carry no state_class, which
    makes HA flag "entity no longer has a state class" on every statistics cycle if any statistics
    exist for them. We clear those to keep that warning from firing. predicted_energy is excluded:
    it is the archive entity whose statistics are kept on purpose (it carries a valid state_class).
    Idempotent: the live sensors never regain a state_class, so this is a no-op once their stats
    are gone.
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
