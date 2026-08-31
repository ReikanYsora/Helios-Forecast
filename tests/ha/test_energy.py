"""Tests for the Energy dashboard solar-forecast provider hook."""

from __future__ import annotations

from types import SimpleNamespace


from custom_components.helios_forecast.const import DOMAIN
from custom_components.helios_forecast.energy import async_get_solar_forecast


async def test_no_coordinator_for_entry_returns_none(hass) -> None:
    hass.data[DOMAIN] = {}
    assert await async_get_solar_forecast(hass, "missing_entry") is None


async def test_no_domain_data_at_all_returns_none(hass) -> None:
    hass.data.pop(DOMAIN, None)
    assert await async_get_solar_forecast(hass, "missing_entry") is None


async def test_coordinator_with_no_data_yet_returns_none(hass) -> None:
    coordinator = SimpleNamespace(data=None)
    hass.data[DOMAIN] = {"entry_1": coordinator}
    assert await async_get_solar_forecast(hass, "entry_1") is None


async def test_returns_wh_hours_from_the_summary(hass) -> None:
    wh_hours = {"2026-06-12T10:00:00+00:00": 1234.0, "2026-06-12T11:00:00+00:00": 2000.0}
    summary = SimpleNamespace(wh_hours=wh_hours)
    coordinator = SimpleNamespace(data=SimpleNamespace(summary=summary))
    hass.data[DOMAIN] = {"entry_1": coordinator}

    result = await async_get_solar_forecast(hass, "entry_1")

    assert result == {"wh_hours": wh_hours}
    # A defensive copy: mutating the result must not reach back into the summary.
    result["wh_hours"]["new"] = 1.0
    assert "new" not in wh_hours
