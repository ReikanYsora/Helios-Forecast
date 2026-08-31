"""Tests for the response-only services: get_forecast / get_battery_soc_forecast."""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from homeassistant.exceptions import ServiceValidationError

from custom_components.helios_forecast import services as services_mod
from custom_components.helios_forecast.battery import BatterySocPoint
from custom_components.helios_forecast.const import DOMAIN
from custom_components.helios_forecast.forecast import ForecastPoint

_UTC = timezone.utc


def _coordinator_with(points=None, battery_soc=None):
    """A minimal stand-in exposing only the `.data.points` / `.data.battery_soc` the services read."""
    data = SimpleNamespace(points=points or [], battery_soc=battery_soc or [])
    return SimpleNamespace(data=data)


@pytest.fixture(autouse=True)
def _clean_registration_flag(hass):
    # async_register_services is a once-per-process guard keyed in hass.data; each test gets a fresh hass
    # fixture already, but be explicit so a future change to that fixture's scope can't leak registration.
    hass.data.pop(services_mod._SERVICES_REGISTERED, None)
    yield


async def test_get_forecast_returns_rounded_points(hass) -> None:
    now = datetime(2026, 6, 21, 10, tzinfo=_UTC)
    points = [
        ForecastPoint(t=now, pv_w=100.005, pv_raw_w=90.0, pv_p10=80.001, pv_p90=120.0),
        ForecastPoint(t=now, pv_w=50.0, pv_raw_w=45.0),  # no analog band
    ]
    hass.data[DOMAIN] = {"entry1": _coordinator_with(points=points)}
    services_mod.async_register_services(hass)

    result = await hass.services.async_call(DOMAIN, "get_forecast", {}, blocking=True, return_response=True)
    assert result == {
        "forecast": [
            {"datetime": now.isoformat(), "watts": 100.0, "p10": 80.0, "p90": 120.0},
            {"datetime": now.isoformat(), "watts": 50.0, "p10": None, "p90": None},
        ]
    }


async def test_get_forecast_empty_when_no_coordinator_data(hass) -> None:
    coordinator = SimpleNamespace(data=None)
    hass.data[DOMAIN] = {"entry1": coordinator}
    services_mod.async_register_services(hass)

    result = await hass.services.async_call(DOMAIN, "get_forecast", {}, blocking=True, return_response=True)
    assert result == {"forecast": []}


async def test_get_battery_soc_forecast_returns_points(hass) -> None:
    now = datetime(2026, 6, 21, 10, tzinfo=_UTC)
    soc = [BatterySocPoint(t=now, soc=55.5)]
    hass.data[DOMAIN] = {"entry1": _coordinator_with(battery_soc=soc)}
    services_mod.async_register_services(hass)

    result = await hass.services.async_call(DOMAIN, "get_battery_soc_forecast", {}, blocking=True, return_response=True)
    assert result == {"forecast": [{"datetime": now.isoformat(), "soc": 55.5}]}


async def test_resolve_fails_when_no_installation_set_up(hass) -> None:
    hass.data[DOMAIN] = {}
    services_mod.async_register_services(hass)
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(DOMAIN, "get_forecast", {}, blocking=True, return_response=True)


async def test_resolve_fails_on_unknown_config_entry_id(hass) -> None:
    hass.data[DOMAIN] = {"entry1": _coordinator_with()}
    services_mod.async_register_services(hass)
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(
            DOMAIN, "get_forecast", {"config_entry_id": "does_not_exist"}, blocking=True, return_response=True
        )


async def test_resolve_requires_entry_id_with_several_installations(hass) -> None:
    hass.data[DOMAIN] = {"entry1": _coordinator_with(), "entry2": _coordinator_with()}
    services_mod.async_register_services(hass)
    with pytest.raises(ServiceValidationError):
        await hass.services.async_call(DOMAIN, "get_forecast", {}, blocking=True, return_response=True)


async def test_resolve_picks_named_entry_among_several(hass) -> None:
    now = datetime(2026, 6, 21, 10, tzinfo=_UTC)
    wanted = [ForecastPoint(t=now, pv_w=42.0, pv_raw_w=40.0)]
    hass.data[DOMAIN] = {
        "entry1": _coordinator_with(points=[ForecastPoint(t=now, pv_w=1.0, pv_raw_w=1.0)]),
        "entry2": _coordinator_with(points=wanted),
    }
    services_mod.async_register_services(hass)
    result = await hass.services.async_call(
        DOMAIN, "get_forecast", {"config_entry_id": "entry2"}, blocking=True, return_response=True
    )
    assert result["forecast"][0]["watts"] == 42.0


async def test_async_register_services_is_idempotent(hass) -> None:
    hass.data[DOMAIN] = {"entry1": _coordinator_with()}
    services_mod.async_register_services(hass)
    services_mod.async_register_services(hass)  # must not raise / double-register
    assert hass.services.has_service(DOMAIN, "get_forecast")
    assert hass.services.has_service(DOMAIN, "get_battery_soc_forecast")
