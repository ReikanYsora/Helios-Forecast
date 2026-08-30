"""Tests for the sensor entities: thin readers over the coordinator's summary."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.helios_forecast import sensor as sensor_mod
from custom_components.helios_forecast.battery import BatterySocPoint
from custom_components.helios_forecast.const import DOMAIN
from custom_components.helios_forecast.coordinator import ForecastData, HeliosForecastCoordinator
from custom_components.helios_forecast.forecast import ForecastPoint
from custom_components.helios_forecast.reliability import Reliability
from custom_components.helios_forecast.summary import summarize
from custom_components.helios_forecast.trend import TodayTrend

_UTC = timezone.utc


def _points(now: datetime) -> list[ForecastPoint]:
    # One flat-ish day per horizon day, distinct amplitude per day so a day-index
    # binding bug (the loop-captured lambdas in _build_descriptions) would show up
    # as day N reading day 1's value instead of its own.
    points = []
    day0 = now.replace(hour=0, minute=0, second=0, microsecond=0)
    for d in range(7):
        for h in range(24):
            t = day0 + timedelta(days=d, hours=h)
            pv = 100.0 * (d + 1) if 6 <= h <= 18 else 0.0
            points.append(
                ForecastPoint(
                    t=t,
                    pv_w=pv,
                    pv_raw_w=pv * 0.9,
                    pv_p10=pv * 0.8 if pv else None,
                    pv_p90=pv * 1.2 if pv else None,
                )
            )
    return points


def _forecast_data(now: datetime, *, battery_soc: list[BatterySocPoint] | None = None) -> ForecastData:
    points = _points(now)
    summary = summarize(points, now=now, tz=_UTC, step_minutes=60)
    reliability = Reliability(
        overall=72.5,
        data_maturity=0.5,
        recent_skill=0.6,
        today_predictability=0.7,
        days_learned=30,
        per_day=[72.5, 65.0, 60.0, 55.0, 50.0, 48.0, 46.0],
    )
    trend = TodayTrend(
        delta_kwh=1.23,
        reference_kwh=5.0,
        reference_time=now,
        current_kwh=6.23,
        direction="up",
    )
    return ForecastData(
        points=points,
        summary=summary,
        observed={"cloud_cover": 42.0, "ghi": 300.0},
        weather_forecast={"cloud_cover": [{"datetime": now.isoformat(), "cloud_cover": 42.0}]},
        reliability=reliability,
        trend=trend,
        battery_soc=battery_soc or [],
    )


@pytest.fixture
def entry(hass) -> MockConfigEntry:
    e = MockConfigEntry(domain=DOMAIN, data={}, entry_id="test_entry")
    e.add_to_hass(hass)
    return e


@pytest.fixture
def coordinator(hass, entry) -> HeliosForecastCoordinator:
    return HeliosForecastCoordinator(hass, entry)


def test_forecast_sensor_reads_from_summary(hass, entry, coordinator) -> None:
    now = datetime(2026, 6, 21, 10, tzinfo=_UTC)
    coordinator.data = _forecast_data(now)
    description = sensor_mod._power("power_now", "Power now", lambda s: s.power_now_w)
    entity = sensor_mod.HeliosForecastSensor(coordinator, entry, description)
    assert entity.native_value == coordinator.data.summary.power_now_w


def test_forecast_sensor_none_when_no_coordinator_data(hass, entry, coordinator) -> None:
    coordinator.data = None
    description = sensor_mod._power("power_now", "Power now", lambda s: s.power_now_w)
    entity = sensor_mod.HeliosForecastSensor(coordinator, entry, description)
    assert entity.native_value is None
    assert entity.extra_state_attributes is None


def test_power_now_forecast_attribute_shape_and_rounding(hass, entry, coordinator) -> None:
    now = datetime(2026, 6, 21, 10, tzinfo=_UTC)
    coordinator.data = _forecast_data(now)
    description = sensor_mod._power(
        "power_now", "Power now", lambda s: s.power_now_w, attrs_fn=sensor_mod._forecast_attrs
    )
    entity = sensor_mod.HeliosForecastSensor(coordinator, entry, description)
    attrs = entity.extra_state_attributes
    assert attrs is not None
    forecast = attrs["forecast"]
    assert len(forecast) == len(coordinator.data.points)
    # A daytime bucket carries a numeric p10/p90 band, rounded to 2 dp, matching the source point.
    src = next(p for p in coordinator.data.points if p.pv_w > 0)
    daytime = next(p for p in forecast if p["watts"] == round(src.pv_w, 2))
    assert daytime["p10"] == round(src.pv_p10, 2)
    assert daytime["p90"] == round(src.pv_p90, 2)
    # A night bucket (pv_w == 0) has no band.
    night = next(p for p in forecast if p["watts"] == 0.0)
    assert night["p10"] is None
    assert night["p90"] is None


def test_energy_sensor_has_no_state_class(hass, entry, coordinator) -> None:
    # ENERGY device class + `measurement` state_class is rejected by HA; a forecast is not a meter.
    description = sensor_mod._energy(
        "energy_today_remaining", "Energy today remaining", lambda s: s.energy_today_remaining_kwh
    )
    assert description.state_class is None
    assert description.device_class.value == "energy"


def test_archive_energy_sensor_has_measurement_but_no_device_class(hass, entry, coordinator) -> None:
    # The archive entity needs a state_class for its long-term statistics, but kWh + MEASUREMENT is only
    # valid without the energy device class (HA rejects energy + measurement).
    description = sensor_mod._archive_energy("predicted_energy", "Predicted energy", lambda s: s.energy_this_hour_kwh)
    assert description.state_class is not None
    assert description.device_class is None


def test_build_descriptions_day_lambdas_bind_their_own_index(hass, entry, coordinator) -> None:
    now = datetime(2026, 6, 21, 10, tzinfo=_UTC)
    coordinator.data = _forecast_data(now)
    descriptions = {d.key: d for d in sensor_mod._build_descriptions()}
    for n in range(1, 8):
        value = descriptions[f"energy_day_{n}"].value_fn(coordinator.data.summary)
        assert value == coordinator.data.summary.days[n - 1].energy_kwh
    # Distinct days must not collapse onto the same (day-1) value.
    values = [descriptions[f"energy_day_{n}"].value_fn(coordinator.data.summary) for n in range(1, 8)]
    assert len(set(values)) == 7


def test_build_descriptions_enabled_defaults(hass, entry, coordinator) -> None:
    descriptions = {d.key: d for d in sensor_mod._build_descriptions()}
    assert descriptions["power_now"].entity_registry_enabled_default is True
    assert descriptions["energy_today_remaining"].entity_registry_enabled_default is True
    assert descriptions["energy_day_1"].entity_registry_enabled_default is True
    assert descriptions["energy_day_2"].entity_registry_enabled_default is True
    assert descriptions["energy_day_3"].entity_registry_enabled_default is False
    assert descriptions["power_next_hour"].entity_registry_enabled_default is False
    for n in range(1, 8):
        assert descriptions[f"peak_power_day_{n}"].entity_registry_enabled_default is False
        assert descriptions[f"peak_time_day_{n}"].entity_registry_enabled_default is False
    # The archive entities stay enabled (their statistics are the card's past-prediction curve).
    assert descriptions["predicted_power"].entity_registry_enabled_default is True
    assert descriptions["predicted_energy"].entity_registry_enabled_default is True


def test_weather_sensor_reads_observed_and_forecast(hass, entry, coordinator) -> None:
    now = datetime(2026, 6, 21, 10, tzinfo=_UTC)
    coordinator.data = _forecast_data(now)
    descriptions = {d.key: d for d in sensor_mod._build_weather_descriptions()}
    entity = sensor_mod.HeliosWeatherSensor(coordinator, entry, descriptions["cloud_cover"])
    assert entity.native_value == 42.0
    assert entity.extra_state_attributes == {"forecast": [{"datetime": now.isoformat(), "cloud_cover": 42.0}]}


def test_weather_sensor_missing_field_and_no_forecast(hass, entry, coordinator) -> None:
    now = datetime(2026, 6, 21, 10, tzinfo=_UTC)
    coordinator.data = _forecast_data(now)
    descriptions = {d.key: d for d in sensor_mod._build_weather_descriptions()}
    # "direct" has no observed value and no forecast series in this fixture.
    entity = sensor_mod.HeliosWeatherSensor(coordinator, entry, descriptions["direct"])
    assert entity.native_value is None
    assert entity.extra_state_attributes is None


def test_weather_descriptions_cover_every_field(hass) -> None:
    from custom_components.helios_forecast.statistics import WEATHER_FIELDS

    descriptions = sensor_mod._build_weather_descriptions()
    assert {d.key for d in descriptions} == {f.key for f in WEATHER_FIELDS}


def test_reliability_sensor(hass, entry, coordinator) -> None:
    now = datetime(2026, 6, 21, 10, tzinfo=_UTC)
    coordinator.data = _forecast_data(now)
    entity = sensor_mod.HeliosReliabilitySensor(coordinator, entry)
    assert entity.native_value == 72.5
    attrs = entity.extra_state_attributes
    assert attrs["data_maturity"] == 0.5
    assert attrs["days_learned"] == 30
    assert attrs["per_day"] == coordinator.data.reliability.per_day


def test_reliability_sensor_none_when_no_data(hass, entry, coordinator) -> None:
    coordinator.data = None
    entity = sensor_mod.HeliosReliabilitySensor(coordinator, entry)
    assert entity.native_value is None
    assert entity.extra_state_attributes is None


def test_battery_soc_sensor_reads_first_point_and_min_max(hass, entry, coordinator) -> None:
    now = datetime(2026, 6, 21, 10, tzinfo=_UTC)
    soc = [
        BatterySocPoint(t=now, soc=50.0),
        BatterySocPoint(t=now, soc=20.0),
        BatterySocPoint(t=now, soc=90.0),
    ]
    coordinator.data = _forecast_data(now, battery_soc=soc)
    entity = sensor_mod.HeliosBatterySocSensor(coordinator, entry)
    assert entity.native_value == 50.0  # the state is the FIRST projected point, not the min/max
    attrs = entity.extra_state_attributes
    assert attrs["min_soc"] == 20.0
    assert attrs["max_soc"] == 90.0
    assert attrs["reliability"] == 72.5
    assert len(attrs["forecast"]) == 3


def test_battery_soc_sensor_none_when_projection_empty(hass, entry, coordinator) -> None:
    now = datetime(2026, 6, 21, 10, tzinfo=_UTC)
    coordinator.data = _forecast_data(now, battery_soc=[])
    entity = sensor_mod.HeliosBatterySocSensor(coordinator, entry)
    assert entity.native_value is None
    assert entity.extra_state_attributes is None


def test_today_trend_sensor(hass, entry, coordinator) -> None:
    now = datetime(2026, 6, 21, 10, tzinfo=_UTC)
    coordinator.data = _forecast_data(now)
    entity = sensor_mod.HeliosTodayTrendSensor(coordinator, entry)
    assert entity.native_value == 1.23
    attrs = entity.extra_state_attributes
    assert attrs["direction"] == "up"
    assert attrs["reference_kwh"] == 5.0
    assert attrs["current_kwh"] == 6.23


def test_today_trend_sensor_unknown_reference(hass, entry, coordinator) -> None:
    now = datetime(2026, 6, 21, 10, tzinfo=_UTC)
    data = _forecast_data(now)
    unknown_trend = TodayTrend(
        delta_kwh=None, reference_kwh=None, reference_time=None, current_kwh=3.0, direction="unknown"
    )
    coordinator.data = ForecastData(
        points=data.points,
        summary=data.summary,
        observed=data.observed,
        weather_forecast=data.weather_forecast,
        reliability=data.reliability,
        trend=unknown_trend,
        battery_soc=data.battery_soc,
    )
    entity = sensor_mod.HeliosTodayTrendSensor(coordinator, entry)
    assert entity.native_value is None
    assert entity.extra_state_attributes["reference_time"] is None


async def test_async_setup_entry_without_battery_config(hass, entry, coordinator) -> None:
    now = datetime(2026, 6, 21, 10, tzinfo=_UTC)
    coordinator.data = _forecast_data(now)
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator

    added: list = []

    def _add_entities(entities, update_before_add=False):
        added.extend(entities)

    await sensor_mod.async_setup_entry(hass, entry, _add_entities)
    unique_ids = {e.unique_id for e in added}
    assert f"{entry.entry_id}_predicted_battery_soc" not in unique_ids
    # descriptions + weather + reliability + trend, no battery sensor.
    expected = len(sensor_mod._build_descriptions()) + len(sensor_mod._build_weather_descriptions()) + 2
    assert len(added) == expected


async def test_async_setup_entry_with_battery_config_adds_soc_sensor(hass) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        data={"battery_capacity_kwh": 10.0, "battery_soc_entity": "sensor.battery_soc"},
        entry_id="battery_entry",
    )
    entry.add_to_hass(hass)
    coordinator = HeliosForecastCoordinator(hass, entry)
    now = datetime(2026, 6, 21, 10, tzinfo=_UTC)
    coordinator.data = _forecast_data(now)
    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator

    added: list = []

    def _add_entities(entities, update_before_add=False):
        added.extend(entities)

    await sensor_mod.async_setup_entry(hass, entry, _add_entities)
    unique_ids = {e.unique_id for e in added}
    assert f"{entry.entry_id}_predicted_battery_soc" in unique_ids
