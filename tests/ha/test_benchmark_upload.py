"""Tests for the benchmark upload hook: when an entry emits, when it stays silent, and that a
collector having a bad day never reaches the forecast."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.helios_forecast.benchmark import DEFAULT_ENDPOINT, SCHEMA_VERSION
from custom_components.helios_forecast.config import CONF_BENCHMARK_ENABLED
from custom_components.helios_forecast.const import DOMAIN
from custom_components.helios_forecast.coordinator import HeliosForecastCoordinator
from custom_components.helios_forecast.reliability import Reliability

pytestmark = pytest.mark.usefixtures("recorder_mock")

_NOW = datetime(2026, 9, 5, 12, 30, tzinfo=timezone.utc)
_ON = {CONF_BENCHMARK_ENABLED: True}
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


async def test_the_switch_is_the_whole_of_it(hass, enable_custom_integrations) -> None:
    """No key to hold and no address to type: an entry that says yes emits, full stop. What used to
    stand between the two was a credential nobody could be asked to keep safe."""
    coordinator = _coordinator(hass, {CONF_BENCHMARK_ENABLED: True})
    with patch("custom_components.helios_forecast.coordinator.async_upload", AsyncMock()) as upload:
        await _emit(hass, coordinator)
    assert upload.call_count == 1
    assert "key" not in str(upload.call_args.args).lower()


async def test_one_emission_an_hour_however_often_the_forecast_refreshes(hass, enable_custom_integrations) -> None:
    coordinator = _coordinator(hass, _ON)
    with patch("custom_components.helios_forecast.coordinator.async_upload", AsyncMock()) as upload:
        await _emit(hass, coordinator, _NOW)
        await _emit(hass, coordinator, _NOW.replace(minute=59))
        await _emit(hass, coordinator, _NOW.replace(hour=13, minute=1))
    assert upload.call_count == 2
    _session, url, payload = upload.call_args.args
    assert url == DEFAULT_ENDPOINT
    # The version the collector gates on, and the shape it is allowed to read.
    assert payload["model_version"]
    assert payload["schema"] == SCHEMA_VERSION
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


# --- the wiring, not the method ------------------------------------------------------------------

# Every test above calls the upload hook directly, which says whether the hook works and nothing
# about whether anything calls it. It did not, in a shipped release: the method was restored without
# the one line in the refresh that reaches it, and a whole round of the benchmark would have
# collected nothing while every test here stayed green. These two drive a real refresh instead.


async def _refresh(hass, monkeypatch, data):
    """A full coordinator refresh, with only the weather service stubbed."""
    from unittest.mock import AsyncMock as _AsyncMock

    from homeassistant.util import dt as dt_util

    import custom_components.helios_forecast.coordinator as coordinator_mod

    from _weather import make_weather_series

    entry = MockConfigEntry(domain=DOMAIN, data=data, entry_id="bench_wiring")
    entry.add_to_hass(hass)
    coordinator = HeliosForecastCoordinator(hass, entry)
    monkeypatch.setattr(
        coordinator_mod, "fetch_weather", _AsyncMock(return_value=make_weather_series(dt_util.utcnow()))
    )
    with patch("custom_components.helios_forecast.coordinator.async_upload", AsyncMock(return_value=None)) as upload:
        await coordinator.async_refresh()
        await hass.async_block_till_done()
    assert coordinator.last_update_success
    return coordinator, upload


async def test_a_refresh_emits_for_an_entry_that_opted_in(hass, monkeypatch, enable_custom_integrations) -> None:
    _coord, upload = await _refresh(hass, monkeypatch, _ON)
    assert upload.call_count == 1, "a refresh must reach the benchmark upload"
    _session, url, payload = upload.call_args.args
    assert url == DEFAULT_ENDPOINT
    assert payload["schema"] == SCHEMA_VERSION
    assert payload["forecast"], "the emission must carry the curve the refresh just built"


async def test_a_refresh_emits_nothing_for_an_entry_that_did_not(hass, monkeypatch, enable_custom_integrations) -> None:
    _coord, upload = await _refresh(hass, monkeypatch, {})
    assert upload.call_count == 0


async def test_the_collectors_verdict_survives_the_next_refresh(hass, monkeypatch, enable_custom_integrations) -> None:
    """The exclusion is a repair the owner must keep seeing. The refresh republishes the whole problem
    list, so a verdict recorded only by the upload would vanish half an hour later."""
    coordinator, _upload = await _refresh(hass, monkeypatch, _ON)
    coordinator._benchmark_quality = {"excluded": "kwp"}

    with patch("custom_components.helios_forecast.coordinator.async_upload", AsyncMock(return_value=None)):
        await coordinator.async_refresh()
        await hass.async_block_till_done()

    assert any(p.key == "benchmark_excluded" for p in coordinator.problems)
