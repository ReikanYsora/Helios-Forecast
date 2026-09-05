"""Tests for the benchmark upload hook: when an entry emits, when it stays silent, and that a
collector having a bad day never reaches the forecast."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.helios_forecast.config import (
    CONF_BENCHMARK_ENABLED,
    CONF_BENCHMARK_KEY,
    CONF_BENCHMARK_URL,
)
from custom_components.helios_forecast.const import DOMAIN
from custom_components.helios_forecast.coordinator import HeliosForecastCoordinator
from custom_components.helios_forecast.reliability import Reliability

pytestmark = pytest.mark.usefixtures("recorder_mock")

_NOW = datetime(2026, 9, 5, 12, 30, tzinfo=timezone.utc)
_ON = {CONF_BENCHMARK_ENABLED: True, CONF_BENCHMARK_KEY: "write-key", CONF_BENCHMARK_URL: "https://example.test/in"}
_RELIABILITY = Reliability(
    overall=50.0, data_maturity=0.4, recent_skill=None, today_predictability=None, days_learned=12, per_day=[]
)


def _coordinator(hass, data) -> HeliosForecastCoordinator:
    entry = MockConfigEntry(domain=DOMAIN, data=data, entry_id="bench_entry")
    entry.add_to_hass(hass)
    return HeliosForecastCoordinator(hass, entry)


async def _emit(hass, coordinator, when=_NOW) -> None:
    await coordinator._maybe_upload_benchmark({**coordinator.entry.data}, 44.1, 1.4, [], _RELIABILITY, when)
    await hass.async_block_till_done()


async def test_an_entry_that_did_not_opt_in_sends_nothing(hass, enable_custom_integrations) -> None:
    coordinator = _coordinator(hass, {})
    with patch("custom_components.helios_forecast.coordinator.async_upload", AsyncMock()) as upload:
        await _emit(hass, coordinator)
    assert upload.call_count == 0


async def test_opted_in_without_a_key_sends_nothing(hass, enable_custom_integrations) -> None:
    coordinator = _coordinator(hass, {CONF_BENCHMARK_ENABLED: True, CONF_BENCHMARK_KEY: "  "})
    with patch("custom_components.helios_forecast.coordinator.async_upload", AsyncMock()) as upload:
        await _emit(hass, coordinator)
    assert upload.call_count == 0


async def test_one_emission_an_hour_however_often_the_forecast_refreshes(hass, enable_custom_integrations) -> None:
    coordinator = _coordinator(hass, _ON)
    with patch("custom_components.helios_forecast.coordinator.async_upload", AsyncMock()) as upload:
        await _emit(hass, coordinator, _NOW)
        await _emit(hass, coordinator, _NOW.replace(minute=59))
        await _emit(hass, coordinator, _NOW.replace(hour=13, minute=1))
    assert upload.call_count == 2
    session, url, key, payload = upload.call_args.args
    assert url == "https://example.test/in"
    assert key == "write-key"
    assert payload["model_version"]
    assert payload["site"]["latitude"] == 44.1


async def test_a_collector_having_a_bad_day_never_reaches_the_forecast(hass, enable_custom_integrations) -> None:
    coordinator = _coordinator(hass, _ON)

    class _DeadSession:
        def post(self, *args, **kwargs):
            raise OSError("connection refused")

    with patch("custom_components.helios_forecast.coordinator.async_get_clientsession", return_value=_DeadSession()):
        await _emit(hass, coordinator)
    # The emission was attempted, the refresh returned, and the failure died in the upload.
    assert coordinator._last_upload_hour is not None
