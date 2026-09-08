"""Diagnostics download for a config entry: what the check-up found, what the learning stands on.

Home Assistant offers this file from the integration's page. It names no person and no address: the
configuration as it stands, and otherwise counts, coverages and the problem list, which is exactly
what an issue report needs.
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any, Dict

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import DOMAIN


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry: ConfigEntry) -> Dict[str, Any]:
    coordinator = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    config = {**entry.data, **entry.options}
    out: Dict[str, Any] = {"config": config, "problems": [], "learning": {}, "consumption": {}, "reliability": None}
    if coordinator is None:
        return out
    out["problems"] = [asdict(p) for p in coordinator.problems]
    buckets = coordinator._production_buckets
    out["learning"] = {
        "production_hours": len(buckets),
        "curtailed_hours": sum(1 for b in buckets if b.curtailed),
        "archive_points": len(coordinator.archive_points),
    }
    profile = coordinator._consumption_profile
    out["consumption"] = {
        "samples": profile.samples if profile is not None else 0,
        "overall_w": round(profile.overall_w) if profile is not None else None,
        "coverage": {sid: round(share, 3) for sid, share in coordinator.consumption_coverage.items()},
    }
    if coordinator.data is not None:
        out["reliability"] = asdict(coordinator.data.reliability)
    return out
