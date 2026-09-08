"""Diagnostics download for a config entry: what the check-up found, what the learning stands on.

Home Assistant offers this file from the integration's page, and the README invites people to attach
it to a public issue, so it must stay safe to hand over: no person, no address, and no credential.
Anything in the configuration whose name reads like a secret is masked here whatever it is, because
a settings key outliving the code that wrote it is how one gets into such a file unnoticed.
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any, Dict

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import DOMAIN


_SECRET_HINTS = ("key", "token", "secret", "password")


def _safe(name: str, value: Any) -> Any:
    """Mask a setting whose name reads like a credential, whatever the setting turns out to be."""
    return "**redacted**" if value and any(hint in name.lower() for hint in _SECRET_HINTS) else value


async def async_get_config_entry_diagnostics(hass: HomeAssistant, entry: ConfigEntry) -> Dict[str, Any]:
    coordinator = hass.data.get(DOMAIN, {}).get(entry.entry_id)
    config = {k: _safe(k, v) for k, v in {**entry.data, **entry.options}.items()}
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
