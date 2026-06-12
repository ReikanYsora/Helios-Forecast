"""Energy dashboard solar-forecast provider.

The energy component calls ``async_get_solar_forecast`` on the integration that
owns the config entry selected as the dashboard's solar forecast source, so our
hourly wh_hours lands in Home Assistant's official Energy view.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from homeassistant.core import HomeAssistant

from .const import DOMAIN


async def async_get_solar_forecast(hass: HomeAssistant, config_entry_id: str) -> Optional[Dict[str, Any]]:
    """Return ``{"wh_hours": {iso_hour: wh}}`` for the Energy dashboard, or None."""
    coordinator = hass.data.get(DOMAIN, {}).get(config_entry_id)
    if coordinator is None or coordinator.data is None:
        return None
    return {"wh_hours": dict(coordinator.data.summary.wh_hours)}
