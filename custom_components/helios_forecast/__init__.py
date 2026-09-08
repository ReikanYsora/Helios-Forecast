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

    from . import archive, services, websocket
    from .config import CONF_BATTERY_SOC_ENTITY
    from .coordinator import HeliosForecastCoordinator

    # Clear the legacy multi-line repair issue if one is still registered against this entry.
    ir.async_delete_issue(hass, DOMAIN, _legacy_issue_id(entry))

    _drop_retired_keys(hass, entry)

    coordinator = HeliosForecastCoordinator(hass, entry)

    # Refresh once the hour has rolled over: the archive only rebuilds inside a refresh (once an
    # hour, see coordinator._last_archive_hour), so without this nudge the just-elapsed hour stays
    # served from the unclamped live series for up to 30 minutes.
    #
    # A few minutes past, not on the hour. Weather models publish on the hour and so does every
    # scheduled task there is, so an installation asking at HH:00:05 asks in the worst second of the
    # hour, every hour, and all of them ask together. That is what the "Open-Meteo returned no
    # weather data" line at HH:00:09 in the logs is: not a quota, twenty of these requests back to
    # back answer without one refusal, but the one moment the provider is least able to answer.
    # Nothing here is urgent to the second: it rebuilds an hour that has already finished.
    from homeassistant.core import callback
    from homeassistant.helpers.event import async_track_time_change

    @callback
    def _hour_rolled_over(_now) -> None:
        hass.async_create_task(coordinator.async_request_refresh())

    entry.async_on_unload(async_track_time_change(hass, _hour_rolled_over, minute=7, second=0))

    # Re-project the battery SoC the moment its source entity comes back from unavailable/unknown
    # rather than waiting for the 30-minute tick: battery integrations are often briefly unavailable
    # at startup. Armed before the first refresh: that refresh routinely runs before the entity
    # exists, and its arrival right afterwards is the transition this listener catches.
    soc_entity = {**entry.data, **entry.options}.get(CONF_BATTERY_SOC_ENTITY)
    if soc_entity:
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

    # The 60-day backfill is already done by the refresh above: its high-water mark starts unset, so
    # that first pass writes the whole window and the forecast archive with it. What is left is the
    # one-off cleanup of statistics the live sensors used to carry, which needs no weather and no
    # model. It stays a background task to keep it off the setup path.
    async def _purge_orphans() -> None:
        _purge_orphan_forecast_stats(hass, entry)

    entry.async_create_background_task(hass, _purge_orphans(), "helios_forecast_purge_orphans")

    # Move any archived history still held under an entity id onto this integration's own series
    # (archive.py). Armed for the started event, never awaited here: the recorder waits for that same
    # event before processing its queue, so a setup that waits on a recorder write waits on a start
    # that is waiting on it, and Home Assistant cancels the entry after five minutes of that. It also
    # keeps a slow first move off the startup path entirely.
    from homeassistant.helpers.start import async_at_started

    @callback
    def _repair_archive(_hass: HomeAssistant) -> None:
        entry.async_create_background_task(
            hass, archive.async_migrate(hass, entry), "helios_forecast_archive_migration"
        )

    entry.async_on_unload(async_at_started(hass, _repair_archive))

    entry.async_on_unload(entry.add_update_listener(_async_reload_entry))

    websocket.async_register(hass)
    services.async_register_services(hass)
    return True


# Settings an earlier build stored and nothing reads any more. One of them was a write credential,
# and the diagnostics download hands the configuration over as it stands, so they are cleared from
# the entry rather than filtered on the way out: what is not stored cannot leak. The opt-in itself is
# NOT among them: an installation that chose to take part keeps that choice across the update.
_RETIRED_SETTINGS = ("benchmark_url", "benchmark_key")


def _drop_retired_keys(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Remove settings no version reads any more from an entry written by an older one."""
    data = {k: v for k, v in entry.data.items() if k not in _RETIRED_SETTINGS}
    options = {k: v for k, v in entry.options.items() if k not in _RETIRED_SETTINGS}
    if len(data) == len(entry.data) and len(options) == len(entry.options):
        return
    hass.config_entries.async_update_entry(entry, data=data, options=options)


def _purge_orphan_forecast_stats(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Clear long-term statistics left on the live forecast energy sensors.

    These sensors are point-in-time forecast values, not meters, and carry no state_class, which
    makes HA flag "entity no longer has a state class" on every statistics cycle if any statistics
    exist for them. We clear those to keep that warning from firing. The predicted-production archive
    is not concerned: it lives in this integration's own series, behind no entity at all (archive.py).
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
    """Take everything the entry created with it: repair issues, archived series, stored state.

    The nine archived series belong to the integration rather than to an entity, which is what stops
    the recorder compiling them from its side, and also means Home Assistant has nothing that could
    ever offer to clean them up: its statistics validation only looks at series that carry an entity
    id. Left behind, they sit in the database for good, under the id of an entry that no longer
    exists.
    """
    from homeassistant.components.recorder import get_instance
    from homeassistant.helpers import issue_registry as ir
    from homeassistant.helpers.storage import Store

    from . import repairs
    from .statistics import ARCHIVED_SERIES, external_statistic_id

    ir.async_delete_issue(hass, DOMAIN, _legacy_issue_id(entry))
    repairs.clear(hass, entry)

    get_instance(hass).async_clear_statistics(
        [external_statistic_id(entry.entry_id, key) for key, _unit, _name in ARCHIVED_SERIES]
    )
    for name in ("trend", "day_ahead"):
        await Store(hass, 1, f"{DOMAIN}.{entry.entry_id}.{name}").async_remove()


async def _async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload when options change so the new layout / cap takes effect."""
    await hass.config_entries.async_reload(entry.entry_id)
