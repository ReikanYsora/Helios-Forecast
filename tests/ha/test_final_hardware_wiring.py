"""Configured hardware limits reach live, elapsed and reconstructed archive enrichment."""

from unittest.mock import AsyncMock

import pytest
from homeassistant.util import dt as dt_util
from pytest_homeassistant_custom_component.common import MockConfigEntry

import custom_components.helios_forecast.coordinator as coordinator_mod
from custom_components.helios_forecast.const import DOMAIN
from custom_components.helios_forecast.coordinator import HeliosForecastCoordinator

from _weather import make_weather_series

pytestmark = pytest.mark.usefixtures("recorder_mock")


@pytest.mark.parametrize(("entry_kw", "expected_w"), [(None, 5000.0), (4.5, 4500.0)])
async def test_every_enrichment_receives_composed_hardware_bound(hass, monkeypatch, freezer, entry_kw, expected_w):
    freezer.move_to("2026-06-20T12:00:00+00:00")
    data = {
        "arrays": [
            {"tilt": 30.0, "azimuth": 180.0, "kwp": 5.0, "line_inverter_max_kw": 2.0},
            {"tilt": 30.0, "azimuth": 270.0, "kwp": 5.0, "line_inverter_max_kw": 3.0},
        ]
    }
    if entry_kw is not None:
        data["inverter_max_kw"] = entry_kw
    entry = MockConfigEntry(domain=DOMAIN, data=data, entry_id="cap_wiring")
    entry.add_to_hass(hass)
    coordinator = HeliosForecastCoordinator(hass, entry)
    weather = make_weather_series(dt_util.utcnow())
    monkeypatch.setattr(coordinator_mod, "fetch_weather", AsyncMock(return_value=weather))
    live_calls = []
    archive_calls = []
    original_live = coordinator_mod.enrich_points
    original_archive = coordinator_mod.enrich_archive_points

    def live(points, library, weather, lat, lon, now, hardware_max_w):
        live_calls.append(hardware_max_w)
        return original_live(points, library, weather, lat, lon, now, hardware_max_w)

    def archive(points, library, weather, lat, lon, hardware_max_w):
        archive_calls.append(hardware_max_w)
        return original_archive(points, library, weather, lat, lon, hardware_max_w)

    monkeypatch.setattr(coordinator_mod, "enrich_points", live)
    monkeypatch.setattr(coordinator_mod, "enrich_archive_points", archive)
    await coordinator.async_refresh()
    assert coordinator.last_update_success
    assert live_calls == [expected_w]
    # Separate elapsed/card and reconstructed Recorder archive paths both run.
    assert archive_calls == [expected_w, expected_w]
    assert coordinator.data.points and coordinator.archive_points
    assert all(p.pv_w <= expected_w for p in coordinator.data.points + coordinator.archive_points)
